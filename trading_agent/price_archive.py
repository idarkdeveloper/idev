"""Append-only archive of closed daily bars, so Yahoo dropping or rewriting a ticker's history cannot erase what we saw.

Yahoo drops history for demerged or renamed tickers and occasionally rewrites old bars. ``PriceArchive`` keeps every
CLOSED daily bar in one SQLite file (stdlib ``sqlite3``) and ``YahooPrices.history`` merges it with each fresh fetch:

* an archived bar's ``close`` / ``volume`` is NEVER overwritten by a later fetch, whatever Yahoo now says;
* the one exception is a SPLIT: when the archived closes divided by the fresh closes over the overlap are about the same
  factor f (not 1) for the dates before the split, and about 1 from the split date on (or for every date, when the archive
  holds no bar after the split), the archive records the split and rescales ONLY its bars before the split date to the new
  share units. The as-first-seen numbers stay in ``close_raw`` / ``volume_raw``; ``bars(symbol, raw=True)`` returns them;
* ``adj_close`` (the dividend-adjusted series, which Yahoo legitimately rewrites at every dividend) is a derived
  column: it follows the fresh fetch where it differs, and bars older than the fetch are rescaled by the same factor so
  returns across a dividend stay consistent;
* if Yahoo returns nothing for a symbol (or an error), the archive is served;
* a bar is archived only once final: for Indian symbols a date before today (IST), or today at or after 18:00; for any
  other market (world indices, crypto) a date at least two days old, because their sessions end after the IST day does.
  Today's bar is still SERVED from the fresh fetch.

Writers are serialised by SQLite itself (``BEGIN IMMEDIATE`` with a 30 s busy timeout, so also across processes) plus an
in-process lock held only around that transaction. A series identical to the last merged one is not written again.
"""

from __future__ import annotations

import logging
import sqlite3
import statistics
import threading
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable

from .timezones import IST

log = logging.getLogger(__name__)

CLOSED_AFTER = time(18, 0)
_RANGE_DAYS = {"1d": 1, "5d": 5, "1mo": 31, "3mo": 92, "6mo": 183, "1y": 366, "2y": 731, "5y": 1827, "10y": 3654}
_LOCK = threading.Lock()
SPLIT_MIN_OVERLAP = 3
SPLIT_MIN_DEVIATION = 0.03   # a factor within 3% of 1 is not a split
SPLIT_MAX_SPREAD = 0.015     # every ratio within 1.5% of the group's median ratio
_INDIAN_INDEX = ("^NSE", "^CNX", "^INDIA", "^BSE")


def is_indian(ysym: str) -> bool:
    s = ysym.upper()
    return s.endswith((".NS", ".BO")) or s.startswith(_INDIAN_INDEX)


def find_split(archived: dict[str, float], fresh: dict[str, float]) -> tuple[float, str | None] | None:
    """(factor, first unaffected date) if the archive disagrees with the fresh closes by a split.

    f = archived / fresh is about one number (not 1) for the early overlap dates and about 1 from some date D on: D is
    returned and only bars before D need rescaling. D is None when every overlap date carries the factor (the archive
    holds nothing after the split). None for anything else (noise, a rewrite, fewer than 3 early dates)."""
    dates = sorted(d for d in archived.keys() & fresh.keys() if archived[d] > 0 and fresh[d] > 0)
    if len(dates) < SPLIT_MIN_OVERLAP:
        return None
    ratios = [archived[d] / fresh[d] for d in dates]
    if abs(ratios[0] - 1) < SPLIT_MIN_DEVIATION:   # the earliest dates agree: no split (the usual case, kept cheap)
        return None
    k = next((i for i, r in enumerate(ratios) if abs(r - 1) <= SPLIT_MAX_SPREAD), len(ratios))   # first unaffected date
    if k < SPLIT_MIN_OVERLAP:
        return None
    early, late = ratios[:k], ratios[k:]
    med = statistics.median(early)
    if all(abs(r / med - 1) <= SPLIT_MAX_SPREAD for r in early) and all(abs(r - 1) <= SPLIT_MAX_SPREAD for r in late):
        return round(med, 4), (dates[k] if k < len(dates) else None)
    return None


def detect_split(archived: dict[str, float], fresh: dict[str, float]) -> float | None:
    """The factor of a split that covers the whole overlap (no unaffected dates), else None."""
    r = find_split(archived, fresh)
    return r[0] if r is not None and r[1] is None else None


def archive_for(settings: Any) -> "PriceArchive":
    """The archive file every price source of one state dir shares: ``<state_dir>/prices/archive.sqlite``."""
    return PriceArchive(Path(settings.state_dir) / "prices" / "archive.sqlite")


class PriceArchive:
    def __init__(self, path: Path, now_fn: Callable[[], datetime] | None = None):
        self.path = Path(path)
        self.now_fn = now_fn or (lambda: datetime.now(IST))
        self.conflicts = 0   # Yahoo values that disagreed with the archive and were not a split (kept archive)
        self.writes = 0      # transactions actually run (tests; a repeated identical fetch must not add to it)
        self._last: dict[str, tuple] = {}
        self._init()

    # -- plumbing -------------------------------------------------------------
    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        return c

    def _init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        c = self._conn()
        try:
            c.execute("PRAGMA journal_mode=WAL")   # readers do not block the writer, nor it them (set once, persists in the file)
            c.executescript("""
                CREATE TABLE IF NOT EXISTS bars (
                    symbol TEXT NOT NULL, date TEXT NOT NULL,
                    close REAL NOT NULL, adj_close REAL NOT NULL, volume REAL NOT NULL,
                    close_raw REAL NOT NULL, volume_raw REAL NOT NULL, captured_at TEXT NOT NULL,
                    PRIMARY KEY (symbol, date));
                CREATE TABLE IF NOT EXISTS splits (
                    symbol TEXT NOT NULL, detected_on TEXT NOT NULL, factor REAL NOT NULL, overlap INTEGER NOT NULL,
                    effective TEXT);
            """)
            cols = {r["name"] for r in c.execute("PRAGMA table_info(splits)")}
            if "effective" not in cols:
                try:
                    c.execute("ALTER TABLE splits ADD COLUMN effective TEXT")
                except sqlite3.OperationalError as e:   # another process upgraded it first
                    if "duplicate column" not in str(e).lower():
                        raise
        finally:
            c.close()

    def is_closed(self, day: str, symbol: str = ".NS") -> bool:
        """True once the bar of ISO date ``day`` is final (see the module docstring)."""
        now = self.now_fn()
        now = now.astimezone(IST) if now.tzinfo else now
        today = now.date()
        if not is_indian(symbol):
            return day <= (today - timedelta(days=2)).isoformat()
        return day < today.isoformat() or (day == today.isoformat() and now.time() >= CLOSED_AFTER)

    # -- reads ----------------------------------------------------------------
    def bars(self, symbol: str, raw: bool = False) -> list[dict[str, Any]]:
        """Archived bars, oldest first. ``raw=True``: close and volume exactly as first seen; default: in today's share
        units (split-adjusted)."""
        c = self._conn()
        try:
            rows = c.execute("SELECT * FROM bars WHERE symbol=? ORDER BY date", (symbol,)).fetchall()
        finally:
            c.close()
        return [{"date": r["date"], "close": r["close_raw"] if raw else r["close"],
                 "adj_close": r["adj_close"], "volume": r["volume_raw"] if raw else r["volume"]} for r in rows]

    def splits(self, symbol: str) -> list[dict[str, Any]]:
        c = self._conn()
        try:
            rows = c.execute("SELECT * FROM splits WHERE symbol=? ORDER BY detected_on", (symbol,)).fetchall()
        finally:
            c.close()
        return [{"detected_on": r["detected_on"], "factor": r["factor"], "overlap": r["overlap"],
                 "effective": r["effective"]} for r in rows]

    # -- the merge --------------------------------------------------------------
    def merge(self, symbol: str, fresh: list[dict[str, Any]] | None, range_: str = "2y") -> list[dict[str, Any]]:
        """Archive the closed bars of ``fresh`` (may be empty) and return archive + fresh as one series, oldest first."""
        fresh = [b for b in (fresh or []) if b.get("close") is not None]
        sig = (hash(tuple((b["date"], b["close"], b.get("adj_close"), b.get("volume")) for b in fresh)),
               tuple(self.is_closed(b["date"], symbol) for b in fresh[-3:]))
        if fresh and self._last.get(symbol) != sig:
            try:
                self._update(symbol, fresh)
                self._last[symbol] = sig
            except Exception:  # noqa: BLE001 - the archive must never break a price read
                log.exception("price archive update failed for %s", symbol)
                return fresh
        try:
            arch = self.bars(symbol)
        except Exception:  # noqa: BLE001
            return fresh
        by_date = {b["date"]: b for b in arch}
        for b in fresh:   # the archive wins for a stored date; a date only Yahoo has (today's open bar, ...) comes along
            by_date.setdefault(b["date"], b)
        out = [by_date[d] for d in sorted(by_date)]
        days = _RANGE_DAYS.get(range_)
        if days and out:
            cutoff = (self.now_fn().date() - timedelta(days=days)).isoformat()
            out = [b for b in out if b["date"] >= cutoff]
        return out

    def _plan(self, c: sqlite3.Connection, symbol: str, fresh_by: dict[str, dict[str, Any]]):
        """What a fresh series would change, judged against the rows as they are RIGHT NOW in ``c``."""
        fresh_close = {d: float(b["close"]) for d, b in fresh_by.items()}
        have = {r["date"]: r for r in c.execute("SELECT * FROM bars WHERE symbol=?", (symbol,))}
        new_bars = [b for d, b in fresh_by.items() if d not in have and self.is_closed(d, symbol)]
        hit = find_split({d: r["close"] for d, r in have.items()}, fresh_close) if have else None
        overlap = sorted(d for d in have if d in fresh_by)
        adj_changes = self._adj_changes(have, fresh_by)
        return have, new_bars, hit, overlap, adj_changes, fresh_close

    @staticmethod
    def _adj_changes(have: dict[str, Any], fresh_by: dict[str, dict[str, Any]]) -> list[tuple[float, str]]:
        out = []
        for d in sorted(d for d in have if d in fresh_by):
            v = float(fresh_by[d].get("adj_close") or 0)
            if v > 0 and abs(v - have[d]["adj_close"]) > 1e-9 * max(1.0, have[d]["adj_close"]):
                out.append((v, d))
        return out

    def _update(self, symbol: str, fresh: list[dict[str, Any]]) -> None:
        now = self.now_fn().isoformat(timespec="seconds")
        fresh_by = {b["date"]: b for b in fresh}
        c = self._conn()
        try:
            # cheap early exit without the write lock: most calls change nothing
            have, new_bars, hit, overlap, adj_changes, fresh_close = self._plan(c, symbol, fresh_by)
            if not hit:
                for d in overlap:   # a disagreement that is not a split: the archive stays as it was
                    if abs(have[d]["close"] / fresh_close[d] - 1) > 0.005:
                        self.conflicts += 1
                        break
            if not (new_bars or hit or adj_changes):
                return
            with _LOCK:
                c.execute("BEGIN IMMEDIATE")
                try:
                    # another process may have applied the same split (or added the same bars) since the check above:
                    # decide again on the rows as they are now that the write lock is ours
                    have, new_bars, hit, overlap, adj_changes, fresh_close = self._plan(c, symbol, fresh_by)
                    if not (new_bars or hit or adj_changes):
                        c.execute("COMMIT")
                        return
                    self.writes += 1
                    if hit:
                        f, boundary = hit
                        where, args = ("AND date<?", (boundary,)) if boundary else ("", ())
                        c.execute(f"UPDATE bars SET close=close/?, adj_close=adj_close/?, volume=volume*? "
                                  f"WHERE symbol=? {where}", (f, f, f, symbol, *args))
                        c.execute("INSERT INTO splits VALUES (?,?,?,?,?)", (symbol, now[:10], f, len(overlap), boundary))
                        log.info("price archive: %s split detected, factor %s from %s", symbol, f, boundary or "all dates")
                        have = {r["date"]: r for r in c.execute("SELECT * FROM bars WHERE symbol=?", (symbol,))}
                        adj_changes = self._adj_changes(have, fresh_by)
                    ov = sorted(d for d in have if d in fresh_by)
                    if ov:
                        d0 = ov[0]
                        a0, f0 = have[d0]["adj_close"], float(fresh_by[d0].get("adj_close") or 0)
                        if a0 > 0 and f0 > 0 and 0.2 < f0 / a0 < 5 and abs(f0 / a0 - 1) > 1e-9:
                            c.execute("UPDATE bars SET adj_close=adj_close*? WHERE symbol=? AND date<?", (f0 / a0, symbol, d0))
                    c.executemany("UPDATE bars SET adj_close=? WHERE symbol=? AND date=?", [(v, symbol, d) for v, d in adj_changes])
                    for b in new_bars:   # new closed bars are appended; a bar that is not final yet is not
                        close = float(b["close"])
                        vol = float(b.get("volume") or 0)
                        c.execute("INSERT OR IGNORE INTO bars VALUES (?,?,?,?,?,?,?,?)",
                                  (symbol, b["date"], close, float(b.get("adj_close") or close), vol, close, vol, now))
                    c.execute("COMMIT")
                except BaseException:
                    c.execute("ROLLBACK")
                    raise
        finally:
            c.close()
