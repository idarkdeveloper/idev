"""Foreign stocks for viewing only: United States, United Kingdom and Japan, beside the Indian (NSE/BSE) ones.

A foreign stock is written with its market's suffix, the same way Yahoo Finance writes it (except the US, which Yahoo
writes bare and this app would otherwise read as an NSE code):

* ``AAPL.US``  - United States (NYSE, Nasdaq), prices in US dollars;
* ``VOD.L``    - United Kingdom (London Stock Exchange), prices in pence (Yahoo's "GBp");
* ``7203.T``   - Japan (Tokyo Stock Exchange), prices in yen.

They can be looked up, charted and read about. Nothing else in the app applies to them: no orders (practice or real),
no Indian filters, price bands, NSE announcements or shareholding, and they are never screened or recommended.
"""

from __future__ import annotations

from typing import Any

MARKETS: dict[str, dict[str, Any]] = {
    "US": {"suffix": ".US", "yahoo_suffix": "", "name": "United States", "exchange": "NYSE / Nasdaq", "currency": "USD"},
    "UK": {"suffix": ".L", "yahoo_suffix": ".L", "name": "United Kingdom", "exchange": "LSE", "currency": "GBp"},
    "JP": {"suffix": ".T", "yahoo_suffix": ".T", "name": "Japan", "exchange": "TSE", "currency": "JPY"},
}
VIEW_ONLY_NOTE = "US, UK and Japan stocks are for viewing only: no orders, practice or real."


def foreign_market(ticker: Any) -> str | None:
    """"US", "UK" or "JP" when ``ticker`` carries that market's suffix, else None (an Indian stock)."""
    t = str(ticker or "").strip().upper()
    for key, m in MARKETS.items():
        if t.endswith(m["suffix"]) and len(t) > len(m["suffix"]):
            return key
    return None


def qualify(text: Any, market: str | None) -> str:
    """What a person typed, with ``market``'s suffix added when it has none ("aapl", "US" -> "AAPL.US"). An already
    suffixed code, or the Indian market (None / "IN"), is returned as typed (upper-cased)."""
    t = str(text or "").strip().upper()
    m = MARKETS.get(str(market or "").upper())
    if not t or m is None or foreign_market(t) or "." in t:
        return t
    return t + m["suffix"]


def yahoo_symbol(ticker: str) -> str:
    """The Yahoo Finance symbol of a foreign ticker ("AAPL.US" -> "AAPL", "VOD.L" stays). Others unchanged."""
    t = ticker.strip().upper()
    key = foreign_market(t)
    if key is None:
        return t
    m = MARKETS[key]
    return t[: -len(m["suffix"])] + m["yahoo_suffix"]


def info(ticker: str) -> dict[str, Any] | None:
    """{"market", "name", "exchange", "currency", "view_only", "note"} for a foreign ticker, else None."""
    key = foreign_market(ticker)
    if key is None:
        return None
    m = MARKETS[key]
    return {"market": key, "market_name": m["name"], "exchange": m["exchange"], "currency": m["currency"],
            "view_only": True, "note": VIEW_ONLY_NOTE}
