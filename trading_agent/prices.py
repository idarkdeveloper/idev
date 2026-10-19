"""Free price fallback (Yahoo Finance) so no paid market-data plan is required.

Groww's Free Trial API plan excludes the Live Data endpoints; Yahoo quotes NSE stocks
with a ``.NS`` suffix (BSE: ``.BO``) and US stocks bare. Quotes may be delayed by a few
minutes, which is fine for comparing a disclosed deal against a portfolio.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from .circuit import CircuitBreaker, GuardedSession

YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
SHORT_DAILY_RANGES = ("1d", "5d")   # a daily answer of one bar is normal for these
HOURLY_RANGE = "2y"                 # Yahoo serves 60-minute bars for at most 730 days


def daily_from_hourly(hourly: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Daily OHLC bars built from intraday bars (each with an IST ``date``), oldest first: the first open, the highest
    high, the lowest low, the last close and the summed volume of each date. Marked ``from_hourly``."""
    days: dict[str, dict[str, Any]] = {}
    for b in sorted(hourly, key=lambda x: str(x.get("ts") or x["date"])):
        d = days.get(b["date"])
        if d is None:
            days[b["date"]] = {"ts": b.get("ts"), "date": b["date"], "open": b["open"], "high": b["high"],
                               "low": b["low"], "close": b["close"], "volume": float(b.get("volume") or 0),
                               "from_hourly": True}
            continue
        d["high"], d["low"] = max(d["high"], b["high"]), min(d["low"], b["low"])
        d["close"], d["volume"] = b["close"], d["volume"] + float(b.get("volume") or 0)
    return list(days.values())


def _check_status(resp: Any, ysym: str) -> None:
    """4xx (not 429) means Yahoo does not serve this symbol: an answer, not an outage."""
    status = getattr(resp, "status_code", 200)
    if isinstance(status, int) and 400 <= status < 500 and status != 429:
        raise LookupError(f"Yahoo has no data for {ysym} (HTTP {status})")
    resp.raise_for_status()


FRESH_TTL = 60.0   # seconds a dashboard price read may be cached while the NSE is open


def price_ttl(now: datetime, default: float, holidays: Any | None = None) -> float:
    """Cache lifetime for the dashboard's price reads: 60 s (never more than ``default``) during NSE hours on a trading
    day, ``default`` otherwise. Only the dashboard App installs this (YahooPrices.ttl_fn); the agent never does."""
    from .safety import market_open
    return min(default, FRESH_TTL) if market_open(now, holidays) else default


_REPLACE_LOCK = threading.Lock()


def atomic_write_text(path: Path, text: str) -> None:
    """Write then swap in, so a research job on another thread never reads a half-written cache file."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(text)
        with _REPLACE_LOCK:   # Windows refuses two simultaneous swaps onto one file
            os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


class YahooPrices:
    def __init__(self, suffix: str = ".NS", session: requests.Session | None = None,
                 timeout: float = 20.0, cache_dir: Path | None = None,
                 cache_ttl: float = 6 * 3600, archive: Any | None = None):
        self.archive = archive   # price_archive.PriceArchive: closed bars kept even if Yahoo drops or rewrites them
        self.suffix = suffix
        # Every fetch goes through a circuit breaker (3 refusals in a row: pause 30 s, 60 s, 300 s) that lives as long as
        # this object, which the watch loop keeps for the whole run.
        self.breaker = CircuitBreaker("Yahoo")
        self.session = GuardedSession(session or requests.Session(), self.breaker)
        self.timeout = timeout
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.cache_ttl = cache_ttl
        self.ttl_fn: Any | None = None   # (default_ttl) -> ttl; set by the dashboard only, so the agent keeps the plain TTLs
        self.fill: Any | None = None   # groww_fill.GrowwFill: a daily bar Yahoo has not posted yet, from NSE's daily file
        self.clock = time.time         # for the provisional close (tests replace it)

    def _ttl(self, default: float) -> float:
        return default if self.ttl_fn is None else self.ttl_fn(default)

    def yahoo_symbol(self, symbol: str) -> str:
        symbol = symbol.upper()
        for prefix in ("NSE_", "BSE_"):
            if symbol.startswith(prefix):
                symbol = symbol[len(prefix):]
        if "/" in symbol:  # crypto pair like BTC/USD
            base, quote = symbol.split("/", 1)
            return f"{base}-{quote}"
        if "." in symbol or "=" in symbol or symbol.startswith("^") or not self.suffix:
            return symbol  # already qualified, an index like ^NSEI, or a future / currency like BZ=F, INR=X
        return f"{symbol}{self.suffix}"

    def __call__(self, symbol: str) -> float:
        return self.latest_price(symbol)

    def latest_price(self, symbol: str) -> float:
        # Not through the circuit breaker: paper fills and stops must never wait on, or run on, a stale price.
        ysym = self.yahoo_symbol(symbol)
        raw = self.session.inner if isinstance(self.session, GuardedSession) else self.session
        resp = raw.get(YAHOO_URL.format(symbol=ysym), headers=HEADERS,
                                params={"range": "1d", "interval": "1d"}, timeout=self.timeout)
        resp.raise_for_status()
        data: Any = resp.json()
        try:
            meta = data["chart"]["result"][0]["meta"]
            price = meta.get("regularMarketPrice") or meta.get("previousClose")
        except (KeyError, IndexError, TypeError) as e:
            raise LookupError(f"Yahoo returned no price for {ysym}") from e
        if price is None:
            raise LookupError(f"Yahoo returned no price for {ysym}")
        return float(price)


    def history(self, symbol: str, range_: str = "2y") -> list[dict[str, Any]]:
        """Daily bars, oldest first: {date, close, adj_close, volume}. Cached on disk. ``close`` is split-adjusted by
        Yahoo (a later split rescales old bars) but NOT dividend-adjusted: use it for every price LEVEL (stops, ATR, fills,
        sizes). ``adj_close`` is split + dividend adjusted: use it only for returns and momentum (ratios inside a window).
        With an ``archive``, closed bars Yahoo no longer serves (or rewrites) are kept and merged in. A bar marked
        ``provisional`` (see ``_provisional``) is served but never archived."""
        ysym = self.yahoo_symbol(symbol)
        if self.archive is None:
            return self._filled(ysym, self._history(ysym, range_))
        try:
            bars = self._history(ysym, range_)
        except Exception:
            merged = self.archive.merge(ysym, [], range_)   # Yahoo failed or has nothing for it: serve what we kept
            if merged:
                return self._filled(ysym, merged)
            raise
        final = [b for b in bars if not b.get("provisional")]
        held = [b for b in bars if b.get("provisional")]
        merged = self.archive.merge(ysym, final, range_) or final
        newest = merged[-1]["date"] if merged else ""
        return self._filled(ysym, merged + [b for b in held if b["date"] > newest])

    def _provisional(self, res: dict[str, Any], t: int) -> float | None:
        """The close of the day bar starting at ``t`` when Yahoo left it empty, else None.

        For hours after an Indian close Yahoo's chart API posts the day's last row with open/high/low but ``close: null``
        (every NSE stock and index; seen on 15 Oct 2026 at 04:39 IST the next morning) while ``meta.regularMarketPrice``
        already holds the day's last price. That price is used only when it is from the same exchange-local day as the
        row and that day's regular session has ended, so a live price mid-session is never taken for a close."""
        meta = res.get("meta") or {}
        price, at = meta.get("regularMarketPrice"), meta.get("regularMarketTime")
        if not isinstance(price, (int, float)) or not isinstance(at, (int, float)) or price <= 0:
            return None
        gmt = meta.get("gmtoffset")
        off = float(gmt) if isinstance(gmt, (int, float)) else 0.0
        day = datetime.fromtimestamp(t + off, tz=timezone.utc).date()
        if datetime.fromtimestamp(at + off, tz=timezone.utc).date() != day:
            return None
        end = ((meta.get("currentTradingPeriod") or {}).get("regular") or {}).get("end")
        if isinstance(end, (int, float)):   # the session's closing time of day, applied to the row's day
            tod = datetime.fromtimestamp(end + off, tz=timezone.utc)
            closes = datetime(day.year, day.month, day.day, tod.hour, tod.minute, tzinfo=timezone.utc).timestamp() - off
        else:                               # no trading period: only once that day is over
            closes = datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp() + 86400 - off
        return float(price) if self.clock() >= closes else None

    def _filled(self, ysym: str, bars: list[dict[str, Any]], ohlc: bool = False) -> list[dict[str, Any]]:
        """``bars`` with a late latest session filled from NSE's daily file (groww_fill), when a fill is attached."""
        return self.fill.fill(ysym, bars, ohlc=ohlc) if self.fill is not None else bars

    def _history(self, ysym: str, range_: str) -> list[dict[str, Any]]:
        # Yahoo quietly serves MONTHLY bars for range=max even with interval=1d (seen 18 Oct 2026: IJH 318 bars since
        # 2000), so the whole history is asked for by dates instead, which keeps daily bars. The cache name changes with
        # it, so a monthly file cached before this fix is never read.
        key = "maxd" if range_ == "max" else range_
        cache = self.cache_dir / f"yahoo_{ysym.replace('^', 'IDX_')}_{key}.json" if self.cache_dir else None
        if cache and cache.exists() and time.time() - cache.stat().st_mtime < self._ttl(self.cache_ttl):
            return json.loads(cache.read_text())
        params: dict[str, Any] = ({"period1": 0, "period2": int(time.time()) + 86400, "interval": "1d"}
                                  if range_ == "max" else {"range": range_, "interval": "1d"})
        resp = self.session.get(YAHOO_URL.format(symbol=ysym), headers=HEADERS, params=params, timeout=self.timeout)
        _check_status(resp, ysym)
        data: Any = resp.json()
        try:
            res = data["chart"]["result"][0]
            ts = res["timestamp"]
            quote = res["indicators"]["quote"][0]
            adj = (res["indicators"].get("adjclose") or [{}])[0].get("adjclose") or quote["close"]
        except (KeyError, IndexError, TypeError) as e:
            raise LookupError(f"Yahoo returned no history for {ysym}") from e
        bars: list[dict[str, Any]] = []
        for i, t in enumerate(ts):
            close = quote["close"][i]
            day = datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d")
            if close is None:
                p = self._provisional(res, t) if i == len(ts) - 1 and (not bars or bars[-1]["date"] < day) else None
                if p is not None:
                    bars.append({"date": day, "close": p, "adj_close": p, "volume": float(quote["volume"][i] or 0),
                                 "provisional": True})
                continue
            bars.append({
                "date": day,
                "close": float(close),
                "adj_close": float(adj[i] if adj[i] is not None else close),
                "volume": float(quote["volume"][i] or 0),
            })
        if range_ not in SHORT_DAILY_RANGES and len(bars) <= 1:   # Yahoo's daily series is broken here (see _hourly_days)
            days = self._hourly_days(ysym)
            if len(days) > len(bars):
                from .timezones import IST
                today = datetime.fromtimestamp(self.clock(), tz=timezone.utc).astimezone(IST).date().isoformat()
                bars = [{"date": d["date"], "close": d["close"], "adj_close": d["close"], "volume": d["volume"],
                         "from_hourly": True} for d in days]
                if bars[-1]["date"] >= today:   # today's hours so far are not a close: served, never archived
                    bars[-1]["provisional"] = True
        if cache:
            cache.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(cache, json.dumps(bars))
        return bars

    def _ohlc(self, symbol: str, range_: str, interval: str, ttl: float) -> list[dict[str, Any]]:
        """Open/high/low/close/volume bars from the Yahoo chart API, oldest first: {ts, date, open, high, low, close,
        volume}. ``ts`` is ISO 8601 in IST; ``date`` the IST date. Bars with a missing price are dropped. Cached on
        disk for ``ttl`` seconds."""
        from .timezones import IST
        ysym = self.yahoo_symbol(symbol)
        key = "maxd" if interval == "1d" and range_ == "max" else range_   # a monthly file cached before is never read
        cache = (self.cache_dir / f"yahoo_{ysym.replace('^', 'IDX_').replace('=', '_')}_{key}_{interval}_ohlc.json"
                 if self.cache_dir else None)
        if cache and cache.exists() and time.time() - cache.stat().st_mtime < ttl:
            cached: list[dict[str, Any]] = json.loads(cache.read_text())
            return self._filled(ysym, cached, ohlc=True) if interval == "1d" else cached
        daily_max = interval == "1d" and range_ == "max"   # by dates: range=max serves MONTHLY bars (see _history)
        params: dict[str, Any] = ({"period1": 0, "period2": int(time.time()) + 86400, "interval": "1d"} if daily_max
                                  else {"range": range_, "interval": interval})
        resp = self.session.get(YAHOO_URL.format(symbol=ysym), headers=HEADERS, params=params, timeout=self.timeout)
        _check_status(resp, ysym)
        data: Any = resp.json()
        try:
            res = data["chart"]["result"][0]
            ts = res["timestamp"]
            q = res["indicators"]["quote"][0]
            o, h, lo, c = q["open"], q["high"], q["low"], q["close"]
            v = q.get("volume") or [0] * len(ts)
        except (KeyError, IndexError, TypeError) as e:
            raise LookupError(f"Yahoo returned no {interval} history for {ysym}") from e
        bars: list[dict[str, Any]] = []
        for i, t in enumerate(ts):
            dt = datetime.fromtimestamp(t, tz=timezone.utc).astimezone(IST)
            if None in (o[i], h[i], lo[i], c[i]):
                p = (self._provisional(res, t) if interval == "1d" and i == len(ts) - 1 and c[i] is None
                     and (not bars or bars[-1]["date"] < dt.date().isoformat()) else None)
                if p is not None:   # Yahoo's empty last close (see _provisional): the day's other prices as posted
                    meta = res.get("meta") or {}
                    op = float(o[i]) if o[i] is not None else p
                    hi = h[i] if h[i] is not None else meta.get("regularMarketDayHigh")
                    lw = lo[i] if lo[i] is not None else meta.get("regularMarketDayLow")
                    hi = max(float(hi) if isinstance(hi, (int, float)) else 0.0, op, p)
                    lw = min(float(lw) if isinstance(lw, (int, float)) and lw > 0 else p, op, p)
                    bars.append({"ts": dt.isoformat(timespec="seconds"), "date": dt.date().isoformat(), "open": op,
                                 "high": hi, "low": lw, "close": p, "volume": float(v[i] or 0), "provisional": True})
                continue
            bars.append({"ts": dt.isoformat(timespec="seconds"), "date": dt.date().isoformat(), "open": float(o[i]),
                         "high": float(h[i]), "low": float(lo[i]), "close": float(c[i]), "volume": float(v[i] or 0)})
        if interval == "1d":   # Yahoo can repeat the last day (a live bar and the settled one): keep the last of each date
            bars = list({b["date"]: b for b in bars}.values())
            if range_ not in SHORT_DAILY_RANGES and len(bars) <= 1:   # Yahoo's daily series is broken here
                days = self._hourly_days(ysym)
                if len(days) > len(bars):
                    bars = days
        if cache:
            cache.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(cache, json.dumps(bars))
        return self._filled(ysym, bars, ohlc=True) if interval == "1d" else bars

    def _hourly_days(self, ysym: str) -> list[dict[str, Any]]:
        """Daily bars built from Yahoo's 60-minute bars, used only when Yahoo's daily series is broken: on 18 Oct 2026
        every BSE (.BO) symbol's daily chart held just the latest day, whatever the range (by range or by dates),
        while its 60-minute bars went back to 17 Jul 2026. Empty when the hourly bars cannot be read either."""
        try:
            return daily_from_hourly(self._ohlc(ysym, HOURLY_RANGE, "60m", 3600.0))
        except Exception:  # noqa: BLE001 - the caller keeps the daily answer it had
            return []

    def history_ohlc(self, symbol: str, range_: str = "1y", ttl: float | None = None) -> list[dict[str, Any]]:
        """Daily OHLC bars (see ``_ohlc``). Cached for at most 15 minutes, so an evening read sees the day's final bar."""
        return self._ohlc(symbol, range_, "1d", self._ttl(min(self.cache_ttl, 900.0)) if ttl is None else ttl)

    def history_intraday(self, symbol: str, interval: str = "15m", range_: str = "5d",
                         ttl: float = 300.0) -> list[dict[str, Any]]:
        """Intraday OHLC bars (15m, 1h ...), oldest first. Same cache conventions as ``history`` but a short TTL
        (5 minutes), since the last session is what matters."""
        return self._ohlc(symbol, range_, interval, self._ttl(ttl))

    def dividends(self, symbol: str, range_: str = "10y") -> list[dict[str, Any]]:
        """Dividends per share by ex-date, oldest first: [{date, amount}]. Cached on disk."""
        ysym = self.yahoo_symbol(symbol)
        cache = self.cache_dir / f"yahoo_div_{ysym.replace('^', 'IDX_')}_{range_}.json" if self.cache_dir else None
        if cache and cache.exists() and time.time() - cache.stat().st_mtime < self.cache_ttl:
            return json.loads(cache.read_text())
        resp = self.session.get(YAHOO_URL.format(symbol=ysym), headers=HEADERS,
                                params={"range": range_, "interval": "1d", "events": "div"}, timeout=self.timeout)
        _check_status(resp, ysym)
        try:
            events = (resp.json()["chart"]["result"][0].get("events") or {}).get("dividends") or {}
        except (KeyError, IndexError, TypeError) as e:
            raise LookupError(f"Yahoo returned no dividend data for {ysym}") from e
        out = sorted(({"date": datetime.fromtimestamp(int(v["date"]), tz=timezone.utc).strftime("%Y-%m-%d"),
                       "amount": float(v["amount"])} for v in events.values()), key=lambda d: str(d["date"]))
        if cache:
            cache.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(cache, json.dumps(out))
        return out


def chain(*fns: Any) -> Any:
    """Try each price function in order; raise the last error if all fail."""

    def _price(symbol: str) -> float:
        last: Exception | None = None
        for fn in fns:
            try:
                return float(fn(symbol))
            except Exception as e:  # noqa: BLE001
                last = e
        raise LookupError(f"no price source could quote {symbol}: {last}")

    return _price
