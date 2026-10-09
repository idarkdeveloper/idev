"""Free price fallback (Yahoo Finance) so no paid market-data plan is required.

Groww's Free Trial API plan excludes the Live Data endpoints; Yahoo quotes NSE stocks
with a ``.NS`` suffix (BSE: ``.BO``) and US stocks bare. Quotes may be delayed by a few
minutes, which is fine for comparing a disclosed deal against a portfolio.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}


def _check_status(resp: Any, ysym: str) -> None:
    """4xx (not 429) means Yahoo does not serve this symbol: an answer, not an outage."""
    status = getattr(resp, "status_code", 200)
    if isinstance(status, int) and 400 <= status < 500 and status != 429:
        raise LookupError(f"Yahoo has no data for {ysym} (HTTP {status})")
    resp.raise_for_status()


class YahooPrices:
    def __init__(self, suffix: str = ".NS", session: requests.Session | None = None,
                 timeout: float = 20.0, cache_dir: Path | None = None,
                 cache_ttl: float = 6 * 3600):
        self.suffix = suffix
        self.session = session or requests.Session()
        self.timeout = timeout
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.cache_ttl = cache_ttl

    def yahoo_symbol(self, symbol: str) -> str:
        symbol = symbol.upper()
        for prefix in ("NSE_", "BSE_"):
            if symbol.startswith(prefix):
                symbol = symbol[len(prefix):]
        if "/" in symbol:  # crypto pair like BTC/USD
            base, quote = symbol.split("/", 1)
            return f"{base}-{quote}"
        if "." in symbol or symbol.startswith("^") or not self.suffix:
            return symbol  # already qualified, or an index like ^NSEI
        return f"{symbol}{self.suffix}"

    def __call__(self, symbol: str) -> float:
        return self.latest_price(symbol)

    def latest_price(self, symbol: str) -> float:
        ysym = self.yahoo_symbol(symbol)
        resp = self.session.get(YAHOO_URL.format(symbol=ysym), headers=HEADERS,
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
        """Daily bars, oldest first: {date, close, adj_close, volume}. Cached on disk."""
        ysym = self.yahoo_symbol(symbol)
        cache = self.cache_dir / f"yahoo_{ysym.replace('^', 'IDX_')}_{range_}.json" if self.cache_dir else None
        if cache and cache.exists() and time.time() - cache.stat().st_mtime < self.cache_ttl:
            return json.loads(cache.read_text())
        resp = self.session.get(YAHOO_URL.format(symbol=ysym), headers=HEADERS,
                                params={"range": range_, "interval": "1d"}, timeout=self.timeout)
        _check_status(resp, ysym)
        data: Any = resp.json()
        try:
            res = data["chart"]["result"][0]
            ts = res["timestamp"]
            quote = res["indicators"]["quote"][0]
            adj = (res["indicators"].get("adjclose") or [{}])[0].get("adjclose") or quote["close"]
        except (KeyError, IndexError, TypeError) as e:
            raise LookupError(f"Yahoo returned no history for {ysym}") from e
        bars = []
        for i, t in enumerate(ts):
            close = quote["close"][i]
            if close is None:
                continue
            bars.append({
                "date": datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d"),
                "close": float(close),
                "adj_close": float(adj[i] if adj[i] is not None else close),
                "volume": float(quote["volume"][i] or 0),
            })
        if cache:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(bars))
        return bars

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
                       "amount": float(v["amount"])} for v in events.values()), key=lambda d: d["date"])
        if cache:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(out))
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
