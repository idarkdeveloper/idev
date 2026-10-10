"""Append-only archive of closed daily bars, so Yahoo dropping or rewriting a ticker's history cannot erase what we saw.

Yahoo drops history for demerged or renamed tickers and occasionally rewrites old bars. ``PriceArchive`` keeps every
CLOSED daily bar (date before today in IST, or today after 16:00 IST) in one SQLite file (stdlib ``sqlite3``) and
``YahooPrices.history`` merges it with each fresh fetch:

* an archived bar's ``close`` / ``volume`` is NEVER overwritten by a later fetch, whatever Yahoo now says;
* the one exception is a SPLIT: when every archived close over the overlap divides by the fresh close to roughly the
  same factor, and that factor is not 1, the archive records the split and rescales its own older bars to the new share
  units. The as-first-seen numbers stay in ``close_raw`` / ``volume_raw``; ``bars(symbol, raw=True)`` returns them;
* ``adj_close`` (the dividend-adjusted series, which Yahoo legitimately rewrites at every dividend) is a derived
  column: it follows the fresh fetch, and bars older than the fetch are rescaled by the same factor so returns across
  a dividend stay consistent;
* if Yahoo returns nothing for a symbol (or an error), the archive is served;
* today's bar is never archived before the close (it is still served from the fresh fetch).

One process-wide lock plus ``BEGIN IMMEDIATE`` serialise writers (the watch service and the dashboard may share the file).
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

CLOSED_AFTER = time(16, 0)
_RANGE_DAYS = {"1d": 1, "5d": 5, "1mo": 31, "3mo": 92, "6mo": 183, "1y": 366, "2y": 731, "5y": 1827, "10y": 3654}
_LOCK = threading.RLock()
SPLIT_MIN_OVERLAP = 3
SPLIT_MIN_DEVIATION = 0.03   # a factor within 3% of 1 is not a split
SPLIT_MAX_SPREAD = 0.015     # every ratio within 1.5% of the median ratio


def detect_split(archived: dict[str, float], fresh: dict[str, float]) -> float | None:
    """The split factor f (old share units per new share unit: 5 for a 5-for-1 split) if every archived close divided
    by the fresh close over the overlap is about the same number and that number is not 1; else None."""
    ratios = [archived[d] / fresh[d] for d in archived.keys() & fresh.keys() if archived[d] > 0 and fresh[d] > 0]
    if len(ratios) < SPLIT_MIN_OVERLAP:
        return None
    med = statistics.median(ratios)
    if abs(med - 1) < SPLIT_MIN_DEVIATION:
        return None
    if all(abs(r / med - 1) <= SPLIT_MAX_SPREAD for r in ratios):
        return round(med, 4)
    return None


class PriceArchive:
    def __init__(self, path: Path, now_fn: Callable[[], datetime] | None = None):
        self.path = Path(path)
        self.now_fn = now_fn or (lambda: datetime.now(IST))
        self.conflicts = 0   # Yahoo values that disagreed with the archive and were not a split (kept archive)
        self._init()

    # -- plumbing -------------------------------------------------------------
    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        return c

    def _init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK:
            c = self._conn()
            try:
                c.executescript("""
                    CREATE TABLE IF NOT EXISTS bars (
                        symbol TEXT NOT NULL, date TEXT NOT NULL,
                        close REAL NOT NULL, adj_close REAL NOT NULL, volume REAL NOT NULL,
                        close_raw REAL NOT NULL, volume_raw REAL NOT NULL, captured_at TEXT NOT NULL,
                        PRIMARY KEY (symbol, date));
                    CREATE TABLE IF NOT EXISTS splits (
                        symbol TEXT NOT NULL, detected_on TEXT NOT NULL, factor REAL NOT NULL, overlap INTEGER NOT NULL);
                """)
            finally:
                c.close()

    def is_closed(self, day: str) -> bool:
        """True once the session of ISO date ``day`` is over: before today (IST), or today at or after 16:00."""
        now = self.now_fn()
        now = now.astimezone(IST) if now.tzinfo else now
        today = now.date().isoformat()
        return day < today or (day == today and now.time() >= CLOSED_AFTER)

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
        return [{"detected_on": r["detected_on"], "factor": r["factor"], "overlap": r["overlap"]} for r in rows]

    # -- the merge --------------------------------------------------------------
    def merge(self, symbol: str, fresh: list[dict[str, Any]] | None, range_: str = "2y") -> list[dict[str, Any]]:
        """Archive the closed bars of ``fresh`` (may be empty) and return archive + fresh as one series, oldest first."""
        fresh = [b for b in (fresh or []) if b.get("close") is not None]
        try:
            with _LOCK:
                self._update(symbol, fresh)
        except Exception:  # noqa: BLE001 - the archive must never break a price read
            log.exception("price archive update failed for %s", symbol)
            if fresh:
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

    def _update(self, symbol: str, fresh: list[dict[str, Any]]) -> None:
        if not fresh:
            return
        now = self.now_fn().isoformat(timespec="seconds")
        c = self._conn()
        try:
            c.execute("BEGIN IMMEDIATE")
            try:
                have = {r["date"]: r for r in c.execute("SELECT * FROM bars WHERE symbol=?", (symbol,))}
                fresh_close = {b["date"]: float(b["close"]) for b in fresh}
                # 1. a split: rescale the archive to the new share units (raw columns stay as first seen)
                f = detect_split({d: r["close"] for d, r in have.items()}, fresh_close) if have else None
                if f:
                    c.execute("UPDATE bars SET close=close/?, adj_close=adj_close/?, volume=volume*? WHERE symbol=?",
                              (f, f, f, symbol))
                    c.execute("INSERT INTO splits VALUES (?,?,?,?)",
                              (symbol, now[:10], f, len(have.keys() & fresh_close.keys())))
                    have = {r["date"]: r for r in c.execute("SELECT * FROM bars WHERE symbol=?", (symbol,))}
                    log.info("price archive: %s split detected, factor %s", symbol, f)
                else:
                    for d, r in have.items():   # a disagreement that is not a split: the archive stays as it was
                        if d in fresh_close and abs(r["close"] / fresh_close[d] - 1) > 0.005:
                            self.conflicts += 1
                            break
                # 2. dividend-adjusted series follows the fresh fetch; older archived bars are rescaled to match
                overlap = sorted(d for d in have if d in {b["date"]: 1 for b in fresh})
                fresh_by = {b["date"]: b for b in fresh}
                if overlap:
                    d0 = overlap[0]
                    a0, f0 = have[d0]["adj_close"], float(fresh_by[d0].get("adj_close") or 0)
                    if a0 > 0 and f0 > 0 and 0.2 < f0 / a0 < 5 and abs(f0 / a0 - 1) > 1e-9:
                        c.execute("UPDATE bars SET adj_close=adj_close*? WHERE symbol=? AND date<?", (f0 / a0, symbol, d0))
                    for d in overlap:
                        v = float(fresh_by[d].get("adj_close") or 0)
                        if v > 0:
                            c.execute("UPDATE bars SET adj_close=? WHERE symbol=? AND date=?", (v, symbol, d))
                # 3. new closed bars are appended; today's open bar is not
                for b in fresh:
                    if b["date"] in have or not self.is_closed(b["date"]):
                        continue
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
