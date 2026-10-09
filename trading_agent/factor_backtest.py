"""Backtest the factor screen as a real portfolio.

On the first trading day of each month: score the universe with exactly the ranking the
`screen` command uses (12-1 momentum, 6-month return, low volatility, above the 200-day
MA, liquidity floor) on data available up to that day, hold the top N equal-weight until
the next month, and pay Indian delivery charges on every buy and sell. If fewer than N
stocks qualify, the rest stays in cash, which is how the trend filter shows up.

Compared with NIFTY 50 and an equal-weight hold of the whole universe (no costs).

Caveat built into the output: the universe is today's constituents, so stocks that
were dropped or delisted are missing. That survivorship bias flatters every curve.
"""

from __future__ import annotations

import math
import statistics
from bisect import bisect_right
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Iterable

from .momentum import momentum_stats
from .screen import _vol, score_universe

MIN_BARS = 274  # 12-1 momentum needs 252 + 21 bars of history


def _yahoo_range(years: int) -> str:
    need = years + 1  # one extra year of lookback for the first ranking
    return "2y" if need <= 2 else "5y" if need <= 5 else "10y" if need <= 10 else "max"


def _price_at(bars: list[dict[str, Any]], dates: list[str], day: str) -> float | None:
    i = bisect_right(dates, day) - 1
    return bars[i]["adj_close"] if i >= 0 else None


def _month_starts(dates: list[str]) -> list[str]:
    out, seen = [], set()
    for d in dates:
        ym = d[:7]
        if ym not in seen:
            seen.add(ym)
            out.append(d)
    return out


def _stats(values: list[float], dates: list[str]) -> dict[str, Any]:
    if len(values) < 2 or values[0] <= 0:
        return {"total_return": None, "cagr": None, "max_drawdown": None, "volatility": None}
    rets = [values[i] / values[i - 1] - 1 for i in range(1, len(values)) if values[i - 1] > 0]
    years = max(len(values) - 1, 1) / 12
    peak, mdd = values[0], 0.0
    for v in values:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    vol = statistics.pstdev(rets) * math.sqrt(12) if len(rets) > 1 else None
    cagr = (values[-1] / values[0]) ** (1 / years) - 1 if values[-1] > 0 else -1.0
    return {"total_return": values[-1] / values[0] - 1, "cagr": cagr, "max_drawdown": mdd, "volatility": vol,
            "start": dates[0], "end": dates[-1]}


def run_factor_backtest(universe: Iterable[dict[str, str]], prices: Any, *, top: int = 20, years: int = 4,
                        benchmark: str = "^NSEI", cost_model: Any | None = None, capital: float = 500_000.0,
                        workers: int = 8, min_turnover: float = 1e7, require_above_200dma: bool = True
                        ) -> dict[str, Any]:
    members = [m["symbol"] for m in universe]
    rng = _yahoo_range(years)
    bench = prices.history(benchmark, rng)
    bench_dates = [b["date"] for b in bench]

    def load(sym: str) -> tuple[str, list[dict[str, Any]] | None]:
        try:
            return sym, prices.history(sym, rng)
        except Exception:  # noqa: BLE001
            return sym, None

    with ThreadPoolExecutor(max_workers=workers) as ex:
        hist = {s: b for s, b in ex.map(load, members) if b}
    dates_of = {s: [b["date"] for b in bars] for s, bars in hist.items()}

    # rebalance on month starts that have a full lookback and fall within the test window
    first_ok = bench_dates[min(MIN_BARS, len(bench_dates) - 1)] if bench_dates else ""
    rebal = [d for d in _month_starts(bench_dates) if d >= first_ok]
    if len(rebal) > years * 12 + 1:
        rebal = rebal[-(years * 12 + 1):]
    if len(rebal) < 2:
        raise ValueError("not enough price history for a backtest; try fewer years")

    value, holdings = capital, {}  # holdings: sym -> rupee value at last rebalance
    holdings_prev: dict[str, float] = {}
    ew_value = capital
    curve, ew_curve, bench_curve, picks_log = [], [], [], []
    costs_paid, trades, cash_months = 0.0, 0, 0
    b0 = _price_at(bench, bench_dates, rebal[0])

    for k, day in enumerate(rebal):
        # 1. mark existing holdings to market from the previous rebalance date
        if k > 0:
            prev = rebal[k - 1]
            for sym in list(holdings):
                p0 = _price_at(hist[sym], dates_of[sym], prev)
                p1 = _price_at(hist[sym], dates_of[sym], day)
                if p0 and p1:
                    holdings[sym] *= p1 / p0
            cash = value - sum(holdings_prev.values())
            value = cash + sum(holdings.values())
            # equal-weight universe, rebalanced monthly, no costs
            rets = []
            for sym in hist:
                p0 = _price_at(hist[sym], dates_of[sym], prev)
                p1 = _price_at(hist[sym], dates_of[sym], day)
                if p0 and p1:  # listed before the previous rebalance
                    rets.append(p1 / p0 - 1)
            if rets:
                ew_value *= 1 + statistics.fmean(rets)

        curve.append(round(value, 2))
        ew_curve.append(round(ew_value, 2))
        bp = _price_at(bench, bench_dates, day)
        bench_curve.append(round(capital * bp / b0, 2) if bp and b0 else None)
        if k == len(rebal) - 1:
            break

        # 2. rank on data up to this day only
        stats: dict[str, dict[str, Any]] = {}
        for sym, bars in hist.items():
            i = bisect_right(dates_of[sym], day)
            if i < MIN_BARS:
                continue
            window = bars[:i]
            st = momentum_stats(window)
            st["vol_60d"] = _vol(window)
            stats[sym] = st
        ranked = score_universe(stats, min_turnover=min_turnover, require_above_200dma=require_above_200dma)
        picks = [r["symbol"] for r in ranked if r["eligible"]][:top]
        if not picks:
            cash_months += 1
        picks_log.append({"date": day, "picks": picks, "eligible": sum(1 for r in ranked if r["eligible"])})

        # 3. trade to equal weight (1/top each; unfilled slots stay in cash)
        target = value / top
        new_holdings: dict[str, float] = {}
        cost = 0.0
        for sym in set(holdings) | set(picks):
            cur = holdings.get(sym, 0.0)
            tgt = target if sym in picks else 0.0
            diff = tgt - cur
            if abs(diff) < 1.0:
                if tgt:
                    new_holdings[sym] = cur
                continue
            trades += 1
            if cost_model is not None:
                cost += cost_model.breakdown("buy" if diff > 0 else "sell", abs(diff))["total"]
            if tgt:
                new_holdings[sym] = tgt
        costs_paid += cost
        value -= cost
        holdings = new_holdings
        holdings_prev = dict(holdings)

    s_stats, b_stats, e_stats = (_stats(curve, rebal), _stats([v for v in bench_curve if v], rebal),
                                 _stats(ew_curve, rebal))
    avg_names = statistics.fmean(len(p["picks"]) for p in picks_log) if picks_log else 0
    return {
        "dates": rebal, "strategy": curve, "benchmark": bench_curve, "equal_weight": ew_curve,
        "stats": {"strategy": s_stats, "benchmark": b_stats, "equal_weight": e_stats},
        "costs_paid": round(costs_paid, 2), "trades": trades, "months": len(rebal) - 1,
        "avg_names_held": round(avg_names, 1), "months_all_cash": cash_months,
        "universe_size": len(members), "with_history": len(hist), "top": top, "capital": capital,
        "benchmark_symbol": benchmark, "picks": picks_log[-3:],
        "caveat": "Universe is today's constituents: stocks that left the index or delisted are missing, "
                  "which flatters both the strategy and the equal-weight curve. The NIFTY 50 line is the "
                  "price index without dividends, while stock returns include them, so it trails by roughly "
                  "1 to 1.5% a year.",
    }


def format_factor_backtest(r: dict[str, Any]) -> str:
    pct = lambda v: "  n/a " if v is None else f"{v*100:+6.1f}%"  # noqa: E731
    lines = [f"Factor portfolio: top {r['top']} of {r['with_history']}/{r['universe_size']} stocks, "
             f"{r['months']} monthly rebalances {r['dates'][0]} to {r['dates'][-1]}",
             f"{'':<16}{'total':>9}{'CAGR':>9}{'max DD':>9}{'vol':>8}"]
    for key, label in (("strategy", "Strategy"), ("benchmark", r["benchmark_symbol"]), ("equal_weight", "Equal weight*")):
        s = r["stats"][key]
        lines.append(f"{label:<16}{pct(s['total_return'])}{pct(s['cagr'])}{pct(s['max_drawdown'])}"
                     f"{pct(s['volatility'])}")
    lines.append(f"Charges paid ₹{r['costs_paid']:,.0f} over {r['trades']} trades; "
                 f"avg {r['avg_names_held']} names held; {r['months_all_cash']} months fully in cash.")
    lines.append("* equal weight of the whole universe, rebalanced monthly, no costs.")
    lines.append("Caveat: " + r["caveat"])
    return "\n".join(lines)
