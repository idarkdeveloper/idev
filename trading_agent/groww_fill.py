"""A daily bar Yahoo has not posted yet, filled from NSE's own daily file (or, in principle, Groww's candles).

Yahoo sometimes posts an Indian close a day late (it did for the NIFTYBEES fund on 13 and 14 Oct 2026). Every daily
series (``YahooPrices.history`` and the daily ``_ohlc``) passes through ``GrowwFill.fill``: when the series stops
before the latest completed NSE session, the missing session(s) are appended, marked with their source. The
pre-market check and the trading code read the same series, so the check never passes on data the agent would not see.

The source (``make_fill``) is NSE's full bhavcopy: the official open, high, low, close and volume of every NSE stock,
which the breadth job downloads each evening after 19:00 IST and keeps per day in ``cache/nse_bhav/<date>.json``
(``NseDailyFile``, disk only, marked ``"source": "nse"``). Groww's historical candles (``GrowwBroker.daily_candles``)
were the first source, but Groww's plan refuses them (403); the class still accepts such a client.

Limits:

* only Indian NSE codes (``XYZ.NS``); the Nifty index (``^NSEI``) is not in the bhavcopy and is not filled;
* at most ``MAX_GAP`` missing sessions; each filled bar is kept in ``cache/<source>_bars/<code>.json``; with a network
  source, at most one request per ``min_interval``, a code that failed is left alone for ``RETRY_S``, and a refusal
  (401 / 403) stops every fill until the next day.

Yahoo's own bar replaces the filled one as soon as Yahoo posts it (a filled bar is never written to the price archive).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable


from .timezones import IST

log = logging.getLogger(__name__)

INDEX = {"^NSEI": "NIFTY"}   # Yahoo index symbol -> Groww trading symbol
MAX_GAP = 3                  # sessions; a longer gap is not a late bar
MIN_INTERVAL_S = 0.25
RETRY_S = 1800.0
KEEP_DAYS = 30


def groww_symbol(ysym: str) -> str | None:
    """The Groww NSE trading symbol for a Yahoo symbol, or None when this fallback does not cover it."""
    s = str(ysym or "").upper()
    if s in INDEX:
        return INDEX[s]
    if s.endswith(".NS") and len(s) > 3 and "/" not in s:
        return s[:-3]
    return None


class GrowwFill:
    """``groww_fn()`` returns a client with ``daily_candles(symbol, start, end)`` or None (no saved token);
    ``window_fn(now)`` says whether Groww may be called now; ``sessions_fn(now)`` lists the completed NSE sessions
    up to now, newest last (a few weeks are enough)."""

    def __init__(self, groww_fn: Callable[[], Any], window_fn: Callable[[datetime], bool],
                 sessions_fn: Callable[[datetime], list[date]], cache_dir: Path | None = None,
                 clock: Callable[[], datetime] | None = None, monotonic: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep, source: str = "groww",
                 min_interval: float = MIN_INTERVAL_S):
        self.groww_fn, self.window_fn, self.sessions_fn = groww_fn, window_fn, sessions_fn
        self.source, self.min_interval = source, min_interval   # "nse": NSE's daily file, read from disk
        self.cache_dir = Path(cache_dir) / f"{source}_bars" if cache_dir else None
        self.clock = clock or (lambda: datetime.now(IST))
        self.monotonic, self.sleep = monotonic, sleep
        self._lock = threading.Lock()
        self._mem: dict[str, dict[str, dict[str, Any]]] = {}
        self._failed: dict[str, float] = {}
        self._refused_on: date | None = None
        self._last = -1e9
        self.filled = 0
        self.last_error: str | None = None

    # -- cache ----------------------------------------------------------------
    def _load(self, sym: str) -> dict[str, dict[str, Any]]:
        if sym not in self._mem:
            rows: dict[str, dict[str, Any]] = {}
            p = self.cache_dir / f"{sym.replace('&', '_')}.json" if self.cache_dir else None
            if p and p.exists():
                try:
                    rows = {b["date"]: b for b in json.loads(p.read_text(encoding="utf-8")) if isinstance(b, dict)}
                except (OSError, ValueError, KeyError):
                    rows = {}
            self._mem[sym] = rows
        return self._mem[sym]

    def _store(self, sym: str, bars: list[dict[str, Any]]) -> None:
        rows = self._load(sym)
        for b in bars:
            rows[b["date"]] = b
        cutoff = (self.clock().date() - timedelta(days=KEEP_DAYS)).isoformat()
        for k in [k for k in rows if k < cutoff]:
            del rows[k]
        if self.cache_dir:
            from .state import atomic_write
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            atomic_write(self.cache_dir / f"{sym.replace('&', '_')}.json", json.dumps(sorted(rows.values(), key=lambda b: b["date"])))

    def _note(self, ok: bool, rows: int | None = None, error: str | None = None) -> None:
        if self.cache_dir is None or self.cache_dir.parent.name != "cache":
            return
        from .health import note_run
        note_run(self.cache_dir.parent.parent, f"{self.source}_fill", ok=ok, rows=rows, error=error)

    # -- the fill -------------------------------------------------------------
    def missing(self, last: str | None, now: datetime) -> list[date]:
        """Completed sessions after ``last`` (ISO date), oldest first; [] when the gap is longer than MAX_GAP."""
        if not last:
            return []
        gap = [d for d in self.sessions_fn(now) if d.isoformat() > last]
        return gap if 0 < len(gap) <= MAX_GAP else []

    def _fetch(self, sym: str, days: list[date], now: datetime) -> None:
        if self._refused_on == now.date() or self.monotonic() - self._failed.get(sym, -1e9) < RETRY_S:
            return
        if not self.window_fn(now):
            return
        client = self.groww_fn()
        if client is None:
            return
        wait = self.min_interval - (self.monotonic() - self._last)
        if wait > 0:
            self.sleep(wait)
        self._last = self.monotonic()
        try:
            got = client.daily_candles(sym, days[0], days[-1])
        except Exception as e:  # noqa: BLE001 - a fill is a nicety: Yahoo's series is returned as it was
            status = getattr(getattr(e, "response", None), "status_code", None) or getattr(e, "status", None)
            if status in (401, 403):
                self._refused_on = now.date()
            self._failed[sym] = self.monotonic()
            self.last_error = f"{sym}: {type(e).__name__}: {str(e)[:200]}"
            log.info("Groww daily candles for %s unavailable: %s", sym, self.last_error)
            self._note(False, error=self.last_error)
            return
        wanted = {d.isoformat() for d in days}
        bars = [b for b in got if b.get("date") in wanted]
        if bars:
            self._store(sym, bars)
            self.filled += len(bars)
            self._note(True, rows=len(bars))

    def fill(self, ysym: str, bars: list[dict[str, Any]], *, ohlc: bool = False) -> list[dict[str, Any]]:
        """``bars`` (oldest first) with the missing latest session(s) appended from Groww when it has them. Never
        raises; returns ``bars`` unchanged when there is nothing to do."""
        sym = groww_symbol(ysym)
        if sym is None or not bars:
            return bars
        try:
            now = self.clock()
            days = self.missing(str(bars[-1].get("date") or "")[:10], now)
            if not days:
                return bars
            with self._lock:
                have = self._load(sym)
                if any(d.isoformat() not in have for d in days):
                    self._fetch(sym, [d for d in days if d.isoformat() not in have], now)
                    have = self._load(sym)
            extra = [have[d.isoformat()] for d in days if d.isoformat() in have]
        except Exception as e:  # noqa: BLE001
            log.info("Groww fill skipped for %s: %s", ysym, e)
            return bars
        out = list(bars)
        for b in extra:
            if ohlc:
                out.append({"ts": f"{b['date']}T15:30:00+05:30", "date": b["date"], "open": b["open"], "high": b["high"],
                            "low": b["low"], "close": b["close"], "volume": b["volume"], "source": self.source})
            else:   # the newest bar's dividend-adjusted close is its close: Yahoo adjusts only older bars
                out.append({"date": b["date"], "close": b["close"], "adj_close": b["close"], "volume": b["volume"],
                            "source": self.source})
        return out


def parse_candles(payload: Any) -> list[dict[str, Any]]:
    """Groww's ``candles`` as daily bars {date, open, high, low, close, volume} with IST dates. A row is
    [time, open, high, low, close, volume(, open interest)]; the time is epoch seconds or milliseconds (the old
    endpoint) or an IST timestamp string "yyyy-MM-ddTHH:mm:ss" / "yyyy-MM-dd HH:mm:ss" (the current one). Rows with
    a missing price or an unreadable time are dropped."""
    rows = payload.get("candles") if isinstance(payload, dict) else None
    out = []
    for c in rows if isinstance(rows, list) else []:
        if not isinstance(c, (list, tuple)) or len(c) < 5:
            continue
        try:
            o, h, lo, cl = (float(x) for x in c[1:5])
            v = float(c[5]) if len(c) > 5 and c[5] is not None else 0.0
            d = _candle_date(c[0])
        except (TypeError, ValueError, OverflowError, OSError):
            continue
        out.append({"date": d, "open": o, "high": h, "low": lo, "close": cl, "volume": v})
    return out


def _candle_date(t: Any) -> str:
    """The IST date of a candle's time: epoch seconds / milliseconds, or an IST timestamp string."""
    if isinstance(t, str) and not t.strip().lstrip("-").replace(".", "", 1).isdigit():
        return date.fromisoformat(t.strip()[:10]).isoformat()
    ts = float(t)
    return datetime.fromtimestamp(ts / 1000 if ts > 1e11 else ts, tz=IST).date().isoformat()


def sessions_for(holidays: Any) -> Callable[[datetime], list[date]]:
    """The completed NSE sessions of the last three weeks (today counts after its 15:30 close), oldest first."""
    from .groww_hours import latest_session
    from .safety import is_trading_day

    def sessions(now: datetime) -> list[date]:
        last = latest_session(now.astimezone(IST), holidays)
        days = [last - timedelta(days=k) for k in range(21)]
        return sorted(d for d in days if is_trading_day(d, holidays))
    return sessions


class NseDailyFile:
    """Daily bars from NSE's full bhavcopy (``sec_bhavdata_full``), the official close of every NSE stock, which the
    breadth job already downloads each evening after 19:00 IST and keeps per day in ``cache/nse_bhav/<date>.json``.
    Read from disk only: no request here. Indices (``NIFTY``) are not in that file and get nothing."""

    def __init__(self, cache_dir: Path):
        self.dir = Path(cache_dir) / "nse_bhav"

    def daily_candles(self, symbol: str, start: Any, end: Any) -> list[dict[str, Any]]:
        out = []
        d, last = date.fromisoformat(str(start)[:10]), date.fromisoformat(str(end)[:10])
        while d <= last:
            path = self.dir / f"{d.isoformat()}.json"
            if path.exists():
                try:
                    bar = json.loads(path.read_text(encoding="utf-8")).get(str(symbol).upper())
                except (OSError, ValueError):
                    bar = None
                if bar:
                    out.append({"date": d.isoformat(), "open": bar[0], "high": bar[1], "low": bar[2], "close": bar[3],
                                "volume": bar[4]})
            d += timedelta(days=1)
        return out


def make_fill(settings: Any, cache_dir: Path | None = None) -> GrowwFill | None:
    """The fill for Indian settings: a late Yahoo bar is taken from NSE's own daily file (the bhavcopy the breadth job
    keeps), read from disk at any hour. Groww's daily candles are not used: Groww's plan refuses them (403)."""
    if getattr(settings, "market", "in") != "in":
        return None
    from . import groww_hours
    holidays = groww_hours.default_calendar(settings)
    cache = cache_dir if cache_dir is not None else Path(settings.state_dir) / "cache"
    source = NseDailyFile(cache)
    return GrowwFill(lambda: source, lambda now: True, sessions_for(holidays), cache_dir=cache, source="nse",
                     min_interval=0.0)