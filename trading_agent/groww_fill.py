"""Groww daily candles as a fallback for a daily bar Yahoo has not posted yet.

Yahoo sometimes posts an Indian close a day late (it did for the NIFTYBEES fund on 13 and 14 Oct 2026). Every daily
series (``YahooPrices.history`` and the daily ``_ohlc``) passes through ``GrowwFill.fill``: when the series stops
before the latest completed NSE session, the missing session(s) are read from Groww's historical candles
(``GrowwBroker.daily_candles``) and appended, marked ``"source": "groww"``. The pre-market check and the trading code
read the same series, so the check never passes on data the agent would not see.

Limits, so this can never cost a Groww login or flood Groww:

* only Indian NSE codes (``XYZ.NS``) and the Nifty index (``^NSEI`` as ``NIFTY``); never BSE-only codes;
* only inside the Groww window (08:30–16:00 IST on trading days), and only with a token that is already saved
  (``integration.cached_groww``): a new token is never requested;
* at most ``MAX_GAP`` missing sessions; each fetched bar is kept in ``cache/groww_bars/<code>.json`` and never asked for
  again; at most one request per ``MIN_INTERVAL_S``; a code that failed is left alone for ``RETRY_S``; a refusal
  (401 / 403: the plan or the token) stops every fill until the next day.

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

import requests

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
                 sleep: Callable[[float], None] = time.sleep):
        self.groww_fn, self.window_fn, self.sessions_fn = groww_fn, window_fn, sessions_fn
        self.cache_dir = Path(cache_dir) / "groww_bars" if cache_dir else None
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
        note_run(self.cache_dir.parent.parent, "groww_fill", ok=ok, rows=rows, error=error)

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
        wait = MIN_INTERVAL_S - (self.monotonic() - self._last)
        if wait > 0:
            self.sleep(wait)
        self._last = self.monotonic()
        try:
            got = client.daily_candles(sym, days[0], days[-1])
        except Exception as e:  # noqa: BLE001 - a fill is a nicety: Yahoo's series is returned as it was
            status = getattr(getattr(e, "response", None), "status_code", None)
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
                            "low": b["low"], "close": b["close"], "volume": b["volume"], "source": "groww"})
            else:   # the newest bar's dividend-adjusted close is its close: Yahoo adjusts only older bars
                out.append({"date": b["date"], "close": b["close"], "adj_close": b["close"], "volume": b["volume"],
                            "source": "groww"})
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


def make_fill(settings: Any, cache_dir: Path | None = None) -> GrowwFill | None:
    """The fill for Indian settings with Groww linked, else None."""
    if getattr(settings, "market", "in") != "in" or not getattr(settings, "use_groww", False):
        return None
    from . import groww_hours
    from .integration import cached_groww
    holidays = groww_hours.default_calendar(settings)

    def client() -> Any:
        try:
            return cached_groww(settings)
        except (requests.RequestException, OSError, ValueError) as e:
            log.info("Groww fill: no client (%s)", e)
            return None
    return GrowwFill(client, lambda now: groww_hours.window_open(settings, now, holidays), sessions_for(holidays),
                     cache_dir=cache_dir if cache_dir is not None else Path(settings.state_dir) / "cache")
