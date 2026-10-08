"""Free price fallback (Yahoo Finance) so no paid market-data plan is required.

Groww's Free Trial API plan excludes the Live Data endpoints; Yahoo quotes NSE stocks
with a ``.NS`` suffix (BSE: ``.BO``) and US stocks bare. Quotes may be delayed by a few
minutes, which is fine for comparing a disclosed deal against a portfolio.
"""

from __future__ import annotations

from typing import Any

import requests

YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}


class YahooPrices:
    def __init__(self, suffix: str = ".NS", session: requests.Session | None = None,
                 timeout: float = 20.0):
        self.suffix = suffix
        self.session = session or requests.Session()
        self.timeout = timeout

    def yahoo_symbol(self, symbol: str) -> str:
        symbol = symbol.upper()
        for prefix in ("NSE_", "BSE_"):
            if symbol.startswith(prefix):
                symbol = symbol[len(prefix):]
        if "/" in symbol:  # crypto pair like BTC/USD
            base, quote = symbol.split("/", 1)
            return f"{base}-{quote}"
        if "." in symbol or not self.suffix:
            return symbol
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
