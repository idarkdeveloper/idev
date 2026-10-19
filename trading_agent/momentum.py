"""Momentum screen: the best-evidenced quant factor for Indian equities.

Computes trailing returns and a simple verdict from daily bars so Claude can refuse to
chase a disclosed buy in a stock that is in a downtrend.
"""

from __future__ import annotations

from typing import Any

TRADING_DAYS = {"1m": 21, "3m": 63, "6m": 126, "12m": 252}


def _ret(bars: list[dict[str, Any]], lookback: int, skip: int = 0) -> float | None:
    """Return over the window ending ``skip`` bars ago and starting ``lookback`` bars before that."""
    if len(bars) <= lookback + skip:
        return None
    end = bars[-1 - skip]["adj_close"]
    start = bars[-1 - skip - lookback]["adj_close"]
    return (end / start - 1.0) if start else None


def momentum_stats(bars: list[dict[str, Any]]) -> dict[str, Any]:
    """Trailing returns, 12-1 momentum, 200-day MA position and 60-day turnover."""
    if not bars:
        return {"error": "no price history"}
    stats: dict[str, Any] = {"last_close": bars[-1]["close"], "as_of": bars[-1]["date"],
                             "bars": len(bars)}
    for name, n in TRADING_DAYS.items():
        stats[f"ret_{name}"] = _ret(bars, n)
    # Classic 12-1: the past year excluding the most recent month (short-term reversal).
    stats["ret_12_1"] = _ret(bars, TRADING_DAYS["12m"] - TRADING_DAYS["1m"], skip=TRADING_DAYS["1m"])
    if len(bars) >= 200:
        ma200 = sum(b["adj_close"] for b in bars[-200:]) / 200
        stats["ma200"] = ma200
        stats["above_200dma"] = bars[-1]["adj_close"] > ma200
    else:
        stats["ma200"] = None
        stats["above_200dma"] = None
    recent = bars[-60:]
    stats["avg_turnover_60d"] = sum(b["close"] * b["volume"] for b in recent) / len(recent)
    high_52w = max(b["adj_close"] for b in bars[-252:])
    stats["pct_from_52w_high"] = bars[-1]["adj_close"] / high_52w - 1.0 if high_52w else None
    stats["verdict"] = momentum_verdict(stats)
    return stats


class SeriesArrays:
    """One symbol's bars as plain columns, built once: a backtest asks for the stats at every month, and each answer
    only needs slices of these (C-speed) instead of a copy of the bars and a Python loop over them."""
    __slots__ = ("dates", "close", "adj", "cv")

    def __init__(self, bars: list[dict[str, Any]]):
        self.dates = [b["date"] for b in bars]
        self.close = [b["close"] for b in bars]
        self.adj = [b["adj_close"] for b in bars]
        self.cv = [b["close"] * b["volume"] for b in bars]


def momentum_stats_at(a: SeriesArrays, n: int) -> dict[str, Any]:
    """``momentum_stats(bars[:n])`` from the columns of ``bars``, bit for bit (same sums in the same order)."""
    if n <= 0:
        return {"error": "no price history"}
    adj = a.adj
    stats: dict[str, Any] = {"last_close": a.close[n - 1], "as_of": a.dates[n - 1], "bars": n}

    def ret(lookback: int, skip: int = 0) -> float | None:
        if n <= lookback + skip:
            return None
        end, start = adj[n - 1 - skip], adj[n - 1 - skip - lookback]
        return (end / start - 1.0) if start else None
    for name, k in TRADING_DAYS.items():
        stats[f"ret_{name}"] = ret(k)
    stats["ret_12_1"] = ret(TRADING_DAYS["12m"] - TRADING_DAYS["1m"], TRADING_DAYS["1m"])
    if n >= 200:
        ma200 = sum(adj[n - 200:n]) / 200
        stats["ma200"] = ma200
        stats["above_200dma"] = adj[n - 1] > ma200
    else:
        stats["ma200"] = None
        stats["above_200dma"] = None
    lo = max(0, n - 60)
    stats["avg_turnover_60d"] = sum(a.cv[lo:n]) / (n - lo)
    high_52w = max(adj[max(0, n - 252):n])
    stats["pct_from_52w_high"] = adj[n - 1] / high_52w - 1.0 if high_52w else None
    stats["verdict"] = momentum_verdict(stats)
    return stats


def momentum_verdict(stats: dict[str, Any]) -> str:
    """'strong' | 'neutral' | 'weak' | 'insufficient'."""
    r6, r121, above = stats.get("ret_6m"), stats.get("ret_12_1"), stats.get("above_200dma")
    if r6 is None:
        return "insufficient"
    positives = sum(1 for v in (r6 is not None and r6 > 0, r121 is not None and r121 > 0, above is True) if v)
    negatives = sum(1 for v in (r6 is not None and r6 < 0, r121 is not None and r121 < 0, above is False) if v)
    if positives >= 2 and negatives == 0:
        return "strong"
    if negatives >= 2:
        return "weak"
    return "neutral"


def momentum_summary(stats: dict[str, Any]) -> str:
    if "error" in stats:
        return stats["error"]
    pct = lambda v: "n/a" if v is None else f"{v * 100:+.1f}%"  # noqa: E731
    return (f"{stats['verdict']} momentum: 1m {pct(stats.get('ret_1m'))}, 3m {pct(stats.get('ret_3m'))}, "
            f"6m {pct(stats.get('ret_6m'))}, 12-1 {pct(stats.get('ret_12_1'))}, "
            f"{'above' if stats.get('above_200dma') else 'below' if stats.get('above_200dma') is False else 'no'} 200-day MA, "
            f"{pct(stats.get('pct_from_52w_high'))} from 52w high")


class MomentumScreen:
    """Binds the stats to a price-history source (``YahooPrices`` or anything with ``history``)."""

    def __init__(self, source: Any):
        self.source = source

    def stats(self, symbol: str) -> dict[str, Any]:
        try:
            return momentum_stats(self.source.history(symbol, "2y"))
        except Exception as e:  # noqa: BLE001
            return {"error": f"{type(e).__name__}: {e}"}
