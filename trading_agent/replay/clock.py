"""The replay clock, and price data that cannot see past it.

Every number the Replay page shows is computed from these wrappers. They fetch a long
history once per symbol and slice it at the clock, so the screen, the signal lab, the
momentum stats and the stops (which all call ``prices.history``) work unchanged.
"""

from __future__ import annotations

import bisect
import calendar as _cal
import threading
from datetime import date
from typing import Any

EARLIEST_START = "2021-01-04"  # point-in-time index membership starts in 2021
_KEEP = {"1y": 252, "2y": 504, "5y": 1260}  # YahooPrices returns about this many bars


class FutureDataError(LookupError):
    """Asked for data dated after the replay clock."""


def _iso(day: Any) -> str:
    return date.fromisoformat(str(day)[:10]).isoformat()


def add_months(day: str, n: int) -> str:
    d = date.fromisoformat(day)
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    return date(y, m, min(d.day, _cal.monthrange(y, m)[1])).isoformat()


class ReplayClock:
    def __init__(self, today: str):
        self._today = _iso(today)

    @property
    def today(self) -> str:
        return self._today

    def advance_to(self, day: str) -> None:
        day = _iso(day)
        if day < self._today:
            raise ValueError(f"the replay clock only moves forward ({day} is before {self._today})")
        self._today = day

    def check(self, day: str) -> None:
        if _iso(day) > self._today:
            raise FutureDataError(f"{_iso(day)} is after the replay date {self._today}")


class ClockedPrices:
    """``field``: "adj_close" values with dividends reinvested, "close" with dividends paid as cash."""

    RANGE = "10y"

    def __init__(self, source: Any, clock: ReplayClock, *, field: str = "adj_close"):
        if field not in ("adj_close", "close"):
            raise ValueError("field must be adj_close or close")
        self.source, self.clock, self.field = source, clock, field
        self._bars: dict[str, list[dict[str, Any]]] = {}
        self._dates: dict[str, list[str]] = {}
        self._divs: dict[str, list[dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def _all(self, symbol: str) -> tuple[list[dict[str, Any]], list[str]]:
        s = symbol.upper()
        with self._lock:
            if s in self._bars:
                return self._bars[s], self._dates[s]
        bars = self.source.history(s, self.RANGE)
        with self._lock:
            self._bars[s], self._dates[s] = bars, [b["date"] for b in bars]
            return self._bars[s], self._dates[s]

    def history(self, symbol: str, range_: str = "2y") -> list[dict[str, Any]]:
        bars, dates = self._all(symbol)
        out = bars[:bisect.bisect_right(dates, self.clock.today)]
        keep = _KEEP.get(range_)
        return out[-keep:] if keep else out

    def latest_price(self, symbol: str) -> float:
        bars = self.history(symbol, self.RANGE)
        if not bars:
            first = self.first_trade_date(symbol)
            when = f"not listed until {first}" if first else "no price history"
            raise LookupError(f"{symbol.upper()} has no price on {self.clock.today} ({when})")
        return float(bars[-1][self.field])

    __call__ = latest_price

    def price_on(self, symbol: str, day: str) -> float:
        self.clock.check(day)
        bars, dates = self._all(symbol)
        i = bisect.bisect_right(dates, _iso(day))
        if not i:
            raise LookupError(f"{symbol.upper()} has no price on or before {day}")
        return float(bars[i - 1][self.field])

    def last_trade_date(self, symbol: str) -> str | None:
        bars = self.history(symbol, self.RANGE)
        return bars[-1]["date"] if bars else None

    def first_trade_date(self, symbol: str) -> str | None:
        bars, _ = self._all(symbol)
        return bars[0]["date"] if bars else None

    def dividends(self, symbol: str) -> list[dict[str, Any]]:
        s = symbol.upper()
        if s not in self._divs:
            # A source error propagates and nothing is cached, so a transient failure is retried
            # on the next call instead of silently dropping the dividends.
            self._divs[s] = sorted(self.source.dividends(s, self.RANGE), key=lambda d: d["date"])
        return [d for d in self._divs[s] if d["date"] <= self.clock.today]

    def calendar(self, symbol: str, after: str, until: str) -> list[str]:
        """Trading days in (after, until] by ``symbol``'s bars. Only the step engine uses this:
        it walks into the future one day at a time, moving the clock as it goes."""
        _, dates = self._all(symbol)
        return [d for d in dates if after < d <= until]
