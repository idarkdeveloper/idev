"""Replay an investor's disclosed deals against what the stock did afterwards.

For each deal: enter at the close of the first trading day *after* the deal date (NSE
publishes bulk/block deals that evening, so that is the earliest honest fill), hold for
each horizon, and compare with NIFTY 50 over the same window. Buys are judged on excess
return after a round-trip cost; sells on whether the stock then lagged the index.
"""

from __future__ import annotations

import statistics
from bisect import bisect_right
from dataclasses import dataclass, field
from typing import Any, Iterable

from .investors import classify_client
from .quiver import DisclosedTrade, followed_names

from .costs import IndianDeliveryCosts

DEFAULT_HORIZONS = (5, 20, 60)
DEFAULT_TRADE_SIZE = 25_000.0  # rupees; the cost model is size-dependent (flat DP charge, brokerage cap)
DEFAULT_COST_BPS = round(IndianDeliveryCosts().round_trip_bps(DEFAULT_TRADE_SIZE), 1)  # ~55 bps incl. slippage
BENCHMARK = "^NSEI"


@dataclass
class DealOutcome:
    deal: DisclosedTrade
    client_type: str
    entry_date: str | None
    entry_price: float | None
    returns: dict[int, float | None] = field(default_factory=dict)  # stock return per horizon
    excess: dict[int, float | None] = field(default_factory=dict)  # vs benchmark
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = self.deal.to_dict()
        d.pop("raw", None)
        d.update(client_type=self.client_type, entry_date=self.entry_date,
                 entry_price=self.entry_price,
                 returns={str(k): v for k, v in self.returns.items()},
                 excess={str(k): v for k, v in self.excess.items()}, error=self.error)
        return d


@dataclass
class BacktestResult:
    investor: str
    horizons: tuple[int, ...]
    cost_bps: float
    outcomes: list[DealOutcome]
    benchmark: str = BENCHMARK
    # "All followed" runs: the same priced deals split by followed investor (a deal matching two is in both rows).
    per_investor: dict[str, "BacktestResult"] = field(default_factory=dict)

    def _group(self, side: str, horizon: int, client_type: str | None = None) -> list[float]:
        vals = []
        for o in self.outcomes:
            if o.error or o.deal.transaction != side:
                continue
            if client_type and o.client_type != client_type:
                continue
            v = o.excess.get(horizon)
            if v is not None:
                vals.append(v)
        return vals

    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {"investor": self.investor, "benchmark": self.benchmark,
                               "cost_bps": self.cost_bps, "deals": len(self.outcomes),
                               "priced": sum(1 for o in self.outcomes if not o.error),
                               "by_side": {}, "by_client_type": {}}
        cost = self.cost_bps / 10_000
        for side in ("Purchase", "Sale"):
            per_h = {}
            for h in self.horizons:
                vals = self._group(side, h)
                if not vals:
                    continue
                if side == "Purchase":
                    net = [v - cost for v in vals]
                    hits = sum(1 for v in net if v > 0)
                else:  # a sale was "right" if the stock then lagged the index
                    net = vals
                    hits = sum(1 for v in vals if v < 0)
                per_h[str(h)] = {"n": len(vals), "mean_excess": statistics.fmean(net),
                                 "median_excess": statistics.median(net),
                                 "hit_rate": hits / len(vals)}
            out["by_side"][side] = per_h
        types = sorted({o.client_type for o in self.outcomes})
        for t in types:
            vals = self._group("Purchase", self.horizons[-1], t)
            if vals:
                out["by_client_type"][t] = {"n": len(vals),
                                            "mean_excess": statistics.fmean(v - cost for v in vals)}
        if self.per_investor:
            out["investors"] = {name: sub.summary() for name, sub in self.per_investor.items()}
        return out

    def to_dict(self) -> dict[str, Any]:
        return {"summary": self.summary(), "outcomes": [o.to_dict() for o in self.outcomes]}


def _forward(bars: list[dict[str, Any]], dates: list[str], after: str,
             horizons: Iterable[int]) -> tuple[str | None, float | None, dict[int, float | None]]:
    """Entry at the first close strictly after ``after``; returns per horizon in trading days."""
    i = bisect_right(dates, after)
    if i >= len(bars):
        return None, None, {h: None for h in horizons}
    entry = bars[i]
    rets: dict[int, float | None] = {}
    for h in horizons:
        j = i + h
        rets[h] = (bars[j]["adj_close"] / entry["adj_close"] - 1.0) if j < len(bars) else None
    return entry["date"], entry["close"], rets


def run_backtest(investor: str, deals: list[DisclosedTrade], prices: Any,
                 horizons: Iterable[int] = DEFAULT_HORIZONS, cost_bps: float = DEFAULT_COST_BPS,
                 benchmark: str = BENCHMARK, history_range: str = "2y") -> BacktestResult:
    """``prices`` needs ``history(symbol, range_) -> bars`` (``YahooPrices`` does)."""
    horizons = tuple(horizons)
    bench = prices.history(benchmark, history_range)
    bench_dates = [b["date"] for b in bench]
    cache: dict[str, list[dict[str, Any]]] = {}
    outcomes: list[DealOutcome] = []
    for d in deals:
        o = DealOutcome(deal=d, client_type=classify_client(d.investor, d.source),
                        entry_date=None, entry_price=None)
        try:
            bars = cache.get(d.ticker)
            if bars is None:
                bars = cache[d.ticker] = prices.history(d.ticker, history_range)
            dates = [b["date"] for b in bars]
            o.entry_date, o.entry_price, o.returns = _forward(bars, dates, d.transaction_date, horizons)
            if o.entry_date is None:
                o.error = "no price after deal date"
            else:
                _, _, b_ret = _forward(bench, bench_dates, d.transaction_date, horizons)
                for h in horizons:
                    r, b = o.returns.get(h), b_ret.get(h)
                    o.excess[h] = (r - b) if r is not None and b is not None else None
        except Exception as e:  # noqa: BLE001
            o.error = f"{type(e).__name__}: {e}"
        outcomes.append(o)
    return BacktestResult(investor=investor, horizons=horizons, cost_bps=cost_bps,
                          outcomes=outcomes, benchmark=benchmark)


def run_backtest_followed(investors: Iterable[str], deals: list[DisclosedTrade], prices: Any,
                          label: str = "All followed", **kw: Any) -> BacktestResult:
    """Pooled backtest of several followed investors' deals, plus one result per investor.

    Each deal is priced once. A deal matching two followed names is counted once in the pooled result and
    once in each of their rows."""
    names = list(investors)
    pooled = run_backtest(label, deals, prices, **kw)
    for n in names:
        mine = [o for o in pooled.outcomes if followed_names(o.deal.investor, [n])]
        pooled.per_investor[n] = BacktestResult(investor=n, horizons=pooled.horizons, cost_bps=pooled.cost_bps,
                                                outcomes=mine, benchmark=pooled.benchmark)
    return pooled


def format_summary(summary: dict[str, Any]) -> str:
    lines = [f"Backtest: {summary['investor']} vs {summary['benchmark']} "
             f"({summary['priced']}/{summary['deals']} deals priced, cost {summary['cost_bps']:.0f} bps round trip)"]
    for side, per_h in summary["by_side"].items():
        if not per_h:
            continue
        verb = "buys: excess return after cost" if side == "Purchase" else "sells: stock vs index afterwards"
        lines.append(f"  {verb}")
        for h, r in per_h.items():
            lines.append(f"    {h:>3}d  n={r['n']:<4} mean {r['mean_excess']*100:+6.2f}%  "
                         f"median {r['median_excess']*100:+6.2f}%  hit {r['hit_rate']*100:4.0f}%")
    if summary["by_client_type"]:
        lines.append("  buys by who traded (longest horizon, after cost):")
        for t, r in summary["by_client_type"].items():
            lines.append(f"    {t:<17} n={r['n']:<4} mean {r['mean_excess']*100:+6.2f}%")
    for name, sub in (summary.get("investors") or {}).items():
        lines.append(f"  {name}: {sub['priced']}/{sub['deals']} deals priced")
        for side, per_h in sub["by_side"].items():
            for h, r in per_h.items():
                tag = "buys" if side == "Purchase" else "sells"
                lines.append(f"    {tag} {h:>3}d  n={r['n']:<4} mean {r['mean_excess']*100:+6.2f}%  "
                             f"hit {r['hit_rate']*100:4.0f}%")
    return "\n".join(lines)
