"""Quote panel figures for the Look up card: trailing P/E, market cap, dividend yield and the 52-week range.

The numbers come from the Yahoo quoteSummary snapshot that ``fundamentals`` already parses; this module only shapes
them for display (Indian crore / lakh-crore formatting; the page shows "n/a" for a missing value) and caches them per
ticker so a second look-up inside the TTL makes no request. Read-only: nothing here touches a broker.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Callable

QUOTE_TTL_SECONDS = 30 * 60
SOURCE = "Yahoo Finance"
FUND_TYPES = {"ETF", "MUTUALFUND", "INDEX"}
# Indian index ETFs often come back as plain equities, so the ticker pattern backs up Yahoo's quoteType.
FUND_TICKER = re.compile(r"(BEES|IETF|ETF)$", re.I)


def group_indian(n: int) -> str:
    """12345678 -> '1,23,45,678' (lakh/crore digit grouping)."""
    s = str(abs(int(n)))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts: list[str] = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts + [tail])
    return ("-" if n < 0 else "") + s


def format_market_cap(rupees: float | None) -> str | None:
    """Rupees -> '₹12,345 Cr' under one lakh crore, '₹18.42 lakh Cr' from there up; None when missing."""
    if rupees is None or rupees <= 0:
        return None
    crore = rupees / 1e7
    if crore >= 1e5:
        return f"₹{crore / 1e5:.2f} lakh Cr"
    if crore >= 1:
        return f"₹{group_indian(round(crore))} Cr"
    return f"₹{crore:.2f} Cr"


def is_fund(ticker: str, parsed: dict[str, Any]) -> bool:
    return (parsed.get("quote_type") or "").upper() in FUND_TYPES or bool(FUND_TICKER.search(ticker))


def build_quote(ticker: str, parsed: dict[str, Any], *, fetched_at: float) -> dict[str, Any]:
    """Shape a parse_summary() result (or {"error": ...}) into the panel's payload."""
    out: dict[str, Any] = {"ticker": ticker, "source": SOURCE, "fetched_at": fetched_at, "error": None, "fund": False}
    if "error" in parsed:
        out["error"] = parsed["error"]
        return out
    fund = is_fund(ticker, parsed)
    lo, hi = parsed.get("week52_low"), parsed.get("week52_high")
    if lo is None or hi is None or not 0 < lo <= hi:
        lo = hi = None
    dy, pe = parsed.get("dividend_yield"), parsed.get("pe")
    if dy is not None and dy < 0:
        dy = None
    elif dy is not None and dy > 0.25:   # quoteSummary gives a fraction; some Yahoo feeds give a percent (2.3 = 2.3%)
        dy = dy / 100 if dy <= 25 else None
    out.update(
        fund=fund,
        pe=None if fund or pe is None or pe <= 0 else round(pe, 2),
        market_cap=parsed.get("market_cap"), market_cap_text=format_market_cap(parsed.get("market_cap")),
        dividend_yield=dy, dividend_yield_text=None if dy is None else f"{dy * 100:.2f}%",
        week52_low=lo, week52_high=hi)
    return out


class QuoteService:
    """``get(ticker)`` -> build_quote() payload, cached per ticker for ``ttl`` seconds. Errors are not cached."""

    def __init__(self, provider: Any, *, ttl: float = QUOTE_TTL_SECONDS, clock: Callable[[], float] = time.time):
        self.provider, self.ttl, self.clock = provider, ttl, clock
        self._cache: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def get(self, ticker: str) -> dict[str, Any]:
        ticker = ticker.strip().upper()
        now = self.clock()
        with self._lock:
            hit = self._cache.get(ticker)
        if hit is not None and now - hit["fetched_at"] < self.ttl:
            return hit
        out = build_quote(ticker, self.provider.get(ticker), fetched_at=now)
        if not out["error"]:
            with self._lock:
                self._cache[ticker] = out
        return out
