"""Deterministic fake market for replay tests: weekday calendar, smooth price paths."""
from __future__ import annotations

from datetime import date, timedelta


def weekdays(start: str = "2019-01-01", end: str = "2026-10-09") -> list[str]:
    d, stop, out = date.fromisoformat(start), date.fromisoformat(end), []
    while d <= stop:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


DAYS = weekdays()


def path(base: float, daily: float, start: str | None = None, end: str | None = None,
         volume: float = 5e6) -> list[dict]:
    """Bars with close = base * (1 + daily) ** i over the weekday calendar."""
    out = []
    for i, d in enumerate(DAYS):
        if (start and d < start) or (end and d > end):
            continue
        c = round(base * (1 + daily) ** i, 4)
        out.append({"date": d, "close": c, "adj_close": c, "volume": volume})
    return out


class FakeSource:
    """Stands in for YahooPrices: history() returns every bar it has; records calls."""

    def __init__(self, bars: dict[str, list[dict]], divs: dict[str, list[dict]] | None = None):
        self.bars = {k.upper(): v for k, v in bars.items()}
        self.divs = {k.upper(): v for k, v in (divs or {}).items()}
        self.calls: list[tuple[str, str]] = []

    def history(self, symbol: str, range_: str = "2y") -> list[dict]:
        self.calls.append(("history", symbol.upper()))
        if symbol.upper() not in self.bars:
            raise LookupError(f"Yahoo returned no history for {symbol}")
        return [dict(b) for b in self.bars[symbol.upper()]]

    def dividends(self, symbol: str, range_: str = "10y") -> list[dict]:
        return [dict(d) for d in self.divs.get(symbol.upper(), [])]


def market() -> FakeSource:
    """A..E grow at different rates; NEWCO lists in June 2022; GONE stops trading in March 2022."""
    return FakeSource({
        "^NSEI": path(10_000, 0.0004),
        "MID150BEES": path(100, 0.0005),
        "NIFTYBEES": path(150, 0.0004),
        "A": path(100, 0.0012), "B": path(200, 0.0009), "C": path(300, 0.0006),
        "D": path(400, 0.0003), "E": path(500, -0.0002),
        "NEWCO": path(50, 0.001, start="2022-06-01"),
        "GONE": path(80, 0.0005, end="2022-03-31"),
    })


class FakeUniverse:
    def __init__(self, symbols=("A", "B", "C", "D", "E")):
        self.current = [{"symbol": s, "name": f"{s} Limited", "industry": ""} for s in symbols]
        self.membership = None

    def members_on(self, day: str) -> list[dict]:
        return [dict(m) for m in self.current]


def top_by_6m(members, prices, top):
    """A tiny screen: rank by 6-month return, using only what the clocked prices show."""
    rows = []
    for m in members:
        bars = prices.history(m["symbol"], "1y")
        if len(bars) > 126:
            rows.append({"symbol": m["symbol"], "name": m.get("name", ""),
                         "ret_6m": bars[-1]["adj_close"] / bars[-127]["adj_close"] - 1})
    rows.sort(key=lambda r: -r["ret_6m"])
    return rows[:top]
