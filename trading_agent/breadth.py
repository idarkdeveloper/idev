"""Market breadth for the NIFTY 500: how many stocks rose, and how many sit above their 50-day average.

Daily series from NSE's full bhavcopy (``sec_bhavdata_full_DDMMYYYY.csv`` on nsearchives.nseindia.com, public, no login):
for the NIFTY 500 members, advances = close above the previous close, declines = close below it (NSE's own previous
close, already adjusted for corporate actions), advance ratio = advances / (advances + declines). The share above the
50-day average comes from the price archive and skips names with fewer than 50 bars.

``history(start, end, ...)`` rebuilds the same series for past days from the price archive and point-in-time NIFTY 500
members (no NSE calls), which is what a research variant needs.

Information only. "Broad selling" (three sessions in a row with advance ratio below 0.35) is a note in the morning email;
braking on it is a registered research variant (B3), not a live rule.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import threading
import time
from bisect import bisect_right
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable

from .filing_time import _is_trading, next_trading_day, previous_trading_day
from .state import atomic_write
from .timezones import IST

log = logging.getLogger(__name__)

URL = "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{ddmmyyyy}.csv"
WEAK = 0.35          # advance ratio below this is a weak day
WEAK_DAYS = 3        # this many weak days in a row is "broad selling"
MA_DAYS = 50
FETCH_AFTER_HOUR = 19
MAX_TRIES = 8
BACKFILL_DAYS = 10
BACKFILL_PER_TICK = 3
RETRY_GAP = timedelta(minutes=20)
_LOCK = threading.Lock()
_MONTHS = {m: i for i, m in enumerate(("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), 1)}


# -- bhavcopy --------------------------------------------------------------------
def parse_bhavcopy(text: str) -> tuple[str, dict[str, tuple[float, float]]]:
    """(ISO date, {symbol: (previous close, close)}) for the EQ series of a full bhavcopy. Raises ValueError if the file is
    not a bhavcopy."""
    rdr = csv.reader(io.StringIO(text.lstrip("﻿")))
    head = [h.strip().upper() for h in next(rdr, [])]
    try:
        i_sym, i_ser, i_dt = head.index("SYMBOL"), head.index("SERIES"), head.index("DATE1")
        i_prev, i_close = head.index("PREV_CLOSE"), head.index("CLOSE_PRICE")
    except ValueError:
        raise ValueError("not an NSE bhavcopy (header columns missing)") from None
    day, out = None, {}
    for r in rdr:
        if len(r) <= max(i_sym, i_ser, i_dt, i_prev, i_close) or r[i_ser].strip() != "EQ":
            continue
        try:
            prev, close = float(r[i_prev]), float(r[i_close])
            d, m, y = r[i_dt].strip().split("-")
            day = day or date(int(y), _MONTHS[m.upper()[:3]], int(d)).isoformat()
        except (ValueError, KeyError):
            continue
        if prev > 0 and close > 0:
            out[r[i_sym].strip().upper()] = (prev, close)
    if day is None or not out:
        raise ValueError("bhavcopy has no EQ rows")
    return day, out


def above_ma(bars: list[dict[str, Any]], asof: str, n: int = MA_DAYS) -> bool | None:
    """Is the last close on or before ``asof`` above the mean of the last ``n`` closes (dividend-adjusted)? None with
    fewer than ``n`` bars."""
    upto = [b for b in bars if b["date"] <= asof]
    if len(upto) < n:
        return None
    w = [float(b.get("adj_close") or b["close"]) for b in upto[-n:]]
    return w[-1] > sum(w) / n


def compute_day(day: str, closes: dict[str, tuple[float, float]], members: Iterable[str],
                bars_fn: Callable[[str], list[dict[str, Any]]] | None = None) -> dict[str, Any]:
    """One day's breadth row for ``members`` (symbols; those missing from ``closes`` are ignored)."""
    adv = dec = unch = n50 = up50 = 0
    for sym in members:
        pc = closes.get(sym)
        if pc is None:
            continue
        prev, close = pc
        if close > prev:
            adv += 1
        elif close < prev:
            dec += 1
        else:
            unch += 1
        if bars_fn is not None:
            try:
                a = above_ma(bars_fn(sym), day)
            except Exception:  # noqa: BLE001
                a = None
            if a is not None:
                n50 += 1
                up50 += bool(a)
    moved = adv + dec
    return {"date": day, "advances": adv, "declines": dec, "unchanged": unch, "n": adv + dec + unch,
            "ratio": round(adv / moved, 4) if moved else None,
            "above_50dma_pct": round(100 * up50 / n50, 1) if n50 else None, "n50": n50}


# -- storage + the daily fetch ---------------------------------------------------
class BreadthStore:
    def __init__(self, state_dir: Path):
        self.path = Path(state_dir) / "breadth.json"

    def _load(self) -> dict[str, Any]:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(d, dict) and isinstance(d.get("days"), dict):
                d.setdefault("attempts", {})
                return d
        except (OSError, ValueError):
            pass
        return {"days": {}, "attempts": {}}

    def _save(self, d: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        d["days"] = dict(sorted(d["days"].items())[-1500:])
        d["attempts"] = dict(sorted(d["attempts"].items())[-30:])
        atomic_write(self.path, json.dumps(d, indent=1))

    def rows(self) -> list[dict[str, Any]]:
        return [v for _, v in sorted(self._load()["days"].items())]

    def add_many(self, rows: Iterable[dict[str, Any]], overwrite: bool = False) -> int:
        n = 0
        with _LOCK:
            d = self._load()
            for r in rows:
                if overwrite or r["date"] not in d["days"]:
                    d["days"][r["date"]] = r
                    n += 1
            self._save(d)
        return n

    def due(self, now: datetime, calendar: Any | None = None) -> bool:
        now = now.astimezone(IST) if now.tzinfo else now
        if now.hour < FETCH_AFTER_HOUR:
            return False
        if calendar is not None and hasattr(calendar, "is_trading_day"):
            if not calendar.is_trading_day(now.date()):
                return False
        elif now.date().weekday() >= 5:
            return False
        a = self._load()["attempts"].get(now.date().isoformat())
        if not a:
            return True
        if a.get("done") or a.get("tries", 0) >= MAX_TRIES:
            return False
        try:
            last = datetime.fromisoformat(a["last"])
            last = last if last.tzinfo else last.replace(tzinfo=IST)
        except (KeyError, ValueError):
            return True
        return now - last >= RETRY_GAP

    def may_try(self, key: str, now: datetime, max_tries: int = MAX_TRIES) -> bool:
        a = self._load()["attempts"].get(key)
        if not a:
            return True
        if a.get("done") or a.get("tries", 0) >= max_tries:
            return False
        try:
            last = datetime.fromisoformat(a["last"])
            last = last if last.tzinfo else last.replace(tzinfo=IST)
        except (KeyError, ValueError):
            return True
        return now - last >= RETRY_GAP

    def missing_days(self, now: datetime, calendar: Any | None = None, n: int = BACKFILL_DAYS) -> list[str]:
        """Up to ``n`` trading days before today that have no stored row (newest first)."""
        have = set(self._load()["days"])
        out, d = [], now.date()
        for _ in range(n):
            d = previous_trading_day(d, calendar)
            if d.isoformat() not in have:
                out.append(d.isoformat())
        return sorted(out, reverse=True)   # newest first: the recent sessions matter most

    def note_try(self, now: datetime, done: bool, outcome: str, key: str | None = None) -> None:
        with _LOCK:
            d = self._load()
            key = key or now.date().isoformat()
            a = d["attempts"].get(key, {})
            d["attempts"][key] = {"tries": a.get("tries", 0) + 1, "done": done, "last": now.isoformat(timespec="seconds"),
                                  "outcome": outcome}
            self._save(d)

    def fetch_day(self, session: Any, day: date, members: Iterable[str],
                  bars_fn: Callable[[str], list[dict[str, Any]]] | None = None, timeout: float = 60.0) -> dict[str, Any]:
        """Download the day's bhavcopy, compute and store the row. Raises on any failure (a 404 before NSE posts it)."""
        from .nse import HEADERS
        resp = session.get(URL.format(ddmmyyyy=day.strftime("%d%m%Y")), headers={**HEADERS, "Accept": "*/*"}, timeout=timeout)
        resp.raise_for_status()
        got_day, closes = parse_bhavcopy(resp.content.decode("utf-8-sig", errors="replace"))
        if got_day != day.isoformat():
            raise ValueError(f"bhavcopy is for {got_day}, not {day}")
        row = compute_day(got_day, closes, members, bars_fn)
        self.add_many([row], overwrite=True)
        return row

    def tick(self, client: Any, now: datetime, members_fn: Callable[[], Iterable[str]],
             bars_fn: Callable[[str], list[dict[str, Any]]] | None = None, calendar: Any | None = None,
             sleep: Callable[[float], Any] = time.sleep) -> dict[str, Any] | None:
        """Watch loop: today's row once per trading day after 19:00 IST (retrying a not-yet-posted file up to MAX_TRIES),
        and a backfill of sessions missed in the last BACKFILL_DAYS trading days (a few per tick, each tried at most twice,
        using today's NIFTY 500 list for the members). Never raises. Returns today's row when stored now."""
        if client is None:
            return None
        members: list[str] | None = None
        todays = None
        try:
            if self.due(now, calendar):
                members = list(members_fn())
                try:
                    todays = self.fetch_day(client.session, now.date(), members, bars_fn)
                    self.note_try(now, True, "ok")
                except Exception as e:  # noqa: BLE001
                    log.info("breadth not stored yet: %s", e)
                    self._note_failure(now, e)
            if _is_trading(now.date(), calendar):
                done = 0
                for day in self.missing_days(now, calendar):
                    key = "bf-" + day
                    if done >= BACKFILL_PER_TICK or not self.may_try(key, now, 2):
                        continue
                    members = members if members is not None else list(members_fn())
                    if done:
                        sleep(2.0)   # public archive host: be polite
                    done += 1
                    try:
                        self.fetch_day(client.session, date.fromisoformat(day), members, bars_fn)
                        self.note_try(now, True, "ok", key)
                    except Exception as e:  # noqa: BLE001
                        log.info("breadth backfill %s failed: %s", day, e)
                        self.note_try(now, False, type(e).__name__, key)
        except Exception:  # noqa: BLE001 - never stops the watch
            log.exception("breadth tick failed")
        return todays

    def _note_failure(self, now: datetime, e: Exception) -> None:
        self.note_try(now, False, type(e).__name__)


def nifty500_members(session: Any = None) -> list[str]:
    """Today's NIFTY 500 symbols from NSE's constituent CSV."""
    from .screen import load_universe
    return [m["symbol"].upper() for m in load_universe("NIFTY500", session)]


def archive_bars_fn(state_dir: Path, suffix: str = ".NS") -> Callable[[str], list[dict[str, Any]]]:
    """symbol -> archived bars from ``<state_dir>/prices/archive.sqlite`` ([] when there are none)."""
    from .price_archive import PriceArchive
    path = Path(state_dir) / "prices" / "archive.sqlite"
    arch = PriceArchive(path) if path.exists() else None
    return lambda sym: arch.bars(sym + suffix) if arch is not None else []


# -- history from the price archive ----------------------------------------------------
def history(start: str, end: str, archive: Any, membership: Any, symbol_fn: Callable[[str], str] = lambda s: s + ".NS",
            members_now: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """Breadth rows for each trading day in [start, end] (ISO dates) from the price archive and point-in-time members.

    ``archive`` is a PriceArchive (``bars(symbol)``), ``membership`` a membership.Membership for NIFTY500 (``members_on``)
    or None to use ``members_now`` for every day. A day counts only if at least 20 members have a bar and a previous bar
    in the archive (so a thinly archived day does not produce a number). Advance/decline use the archive's split-adjusted
    ``close``; the 50-day test uses ``adj_close``."""
    if membership is not None:
        ever = membership.ever_members(start) | membership.members_on(end)
    else:
        ever = set(members_now or [])
    series: dict[str, tuple[list[str], list[float], list[float]]] = {}
    for sym in sorted(ever):
        try:
            bars = archive.bars(symbol_fn(sym))
        except Exception:  # noqa: BLE001
            continue
        if bars:
            series[sym] = ([b["date"] for b in bars], [float(b["close"]) for b in bars],
                           [float(b.get("adj_close") or b["close"]) for b in bars])
    days = sorted({d for dates, _, _ in series.values() for d in dates if start <= d <= end})
    out = []
    for day in days:
        members = membership.members_on(day) if membership is not None else ever
        adv = dec = unch = n50 = up50 = 0
        for sym in members:
            s = series.get(sym)
            if s is None:
                continue
            dates, closes, adjs = s
            i = bisect_right(dates, day) - 1
            if i < 1 or dates[i] != day:
                continue
            if closes[i] > closes[i - 1]:
                adv += 1
            elif closes[i] < closes[i - 1]:
                dec += 1
            else:
                unch += 1
            if i + 1 >= MA_DAYS:
                w = adjs[i + 1 - MA_DAYS:i + 1]
                n50 += 1
                up50 += w[-1] > sum(w) / MA_DAYS
        n = adv + dec + unch
        if n < 20:
            continue
        moved = adv + dec
        out.append({"date": day, "advances": adv, "declines": dec, "unchanged": unch, "n": n,
                    "ratio": round(adv / moved, 4) if moved else None,
                    "above_50dma_pct": round(100 * up50 / n50, 1) if n50 else None, "n50": n50})
    return out


# -- derived + the email line ------------------------------------------------------
def _gap_free(rows: list[dict[str, Any]], calendar: Any | None) -> list[dict[str, Any]]:
    """The longest run at the end of the series with no missing trading day between its rows."""
    run = rows[-1:]
    for r in reversed(rows[:-1]):
        if next_trading_day(date.fromisoformat(r["date"]), calendar) == date.fromisoformat(run[0]["date"]):
            run.insert(0, r)
        else:
            break
    return run


def weak_streak(rows: list[dict[str, Any]], weak: float = WEAK, calendar: Any | None = None) -> int:
    """Consecutive most-recent TRADING DAYS with advance ratio below ``weak`` (a missing session ends the run)."""
    n = 0
    for r in reversed(_gap_free(sorted(rows, key=lambda x: x["date"]), calendar) if rows else []):
        if r.get("ratio") is not None and r["ratio"] < weak:
            n += 1
        else:
            break
    return n


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def breadth_line(rows: list[dict[str, Any]], today: date | None = None, max_age_days: int = 5,
                 calendar: Any | None = None) -> str | None:
    """'Breadth (NIFTY 500): 38% of stocks rose on Fri 9 Oct (below 35% is weak); 3rd day below 35%, broad selling;
    62% are above their 50-day average.' None when nothing is stored or it is too old."""
    if not rows:
        return None
    last = rows[-1]
    if last.get("ratio") is None:
        return None
    d = date.fromisoformat(last["date"])
    if today is not None and (today - d).days > max_age_days:
        return None
    pct = round(last["ratio"] * 100)
    text = f"Breadth (NIFTY 500): {pct}% of stocks rose on {d.strftime('%a')} {d.day} {d.strftime('%b')} (below {round(WEAK * 100)}% is weak)"
    streak = weak_streak(rows, calendar=calendar)
    if streak >= 1:
        text += f"; {_ordinal(streak)} day below {round(WEAK * 100)}%"
        if streak >= WEAK_DAYS:
            text += ", broad selling"
    if last.get("above_50dma_pct") is not None:
        text += f"; {last['above_50dma_pct']:.0f}% are above their 50-day average"
    return text + "."
