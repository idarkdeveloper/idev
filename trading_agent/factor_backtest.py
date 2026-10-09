"""Backtest the factor screen as a real portfolio.

On the first trading day of each month: score the universe with exactly the ranking the
`screen` command uses (12-1 momentum, 6-month return, low volatility, above the 200-day
MA, liquidity floor) on data available up to that day, hold the top N equal-weight until
the next month, and pay Indian delivery charges on every buy and sell. If fewer than N
stocks qualify, the rest stays in cash, which is how the trend filter shows up.

Compared with NIFTY 50 and an equal-weight hold of the universe (no costs). The NIFTY 50
line is the NIFTYBEES ETF by default: it reinvests dividends, net of its small fee, so it
is the return an index fund actually delivered, unlike the price-only index
(^NSEI), which trails by roughly 1.2% a year. The price index is still reported.

Given a `Membership`, each month ranks only the stocks that were in the index that day,
including ones later dropped, so the test is free of survivorship bias for as far back
as the change log reaches. Without one, the universe is today's constituents and the
caveat says so.
"""

from __future__ import annotations

import math
import logging
import statistics
from bisect import bisect_right
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Iterable

from .membership import DELISTED, Membership
from .momentum import momentum_stats
from .screen import _vol, score_universe

log = logging.getLogger(__name__)

MIN_BARS = 274  # 12-1 momentum needs 252 + 21 bars of history

# Index funds that track a universe, for an investable like-for-like comparison.
INDEX_FUNDS = {"NIFTYMIDCAP150": "MID150BEES", "NIFTYSMALLCAP250": "HDFCSML250", "NIFTYNEXT50": "JUNIORBEES"}


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
                        benchmark: str = "NIFTYBEES", price_index: str | None = "^NSEI",
                        cost_model: Any | None = None, capital: float = 500_000.0,
                        workers: int = 8, min_turnover: float = 1e7, require_above_200dma: bool = True,
                        membership: Membership | None = None, index_fund: str | None = None,
                        fundamentals: Any | None = None, quality: float = 0.0, value: float = 0.0
                        ) -> dict[str, Any]:
    """Monthly-rebalanced factor portfolio. With ``fundamentals`` (a ResultsHistory) and a
    quality and/or value weight, each rebalance also ranks on NSE results filings that had
    been broadcast by that day (point-in-time)."""
    universe = list(universe)
    # ``value`` is also the name of the portfolio's rupee value below: keep the weight apart
    quality_w, value_w = float(quality), float(value)
    industries = {m["symbol"]: m.get("industry", "") for m in universe}
    current = [m["symbol"] for m in universe]
    rng = _yahoo_range(years)
    bench = prices.history(benchmark, rng)
    bench_dates = [b["date"] for b in bench]
    # all stocks that were members at any time in the window (including the lookback year)
    members = sorted(membership.ever_members(bench_dates[0])) if membership and bench_dates else current

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
    in_index = (lambda day: membership.members_on(day)) if membership else (lambda day: set(current))
    use_funds = fundamentals is not None and bool(quality_w or value_w)
    filings: dict[str, list[dict[str, Any]]] = {}
    shares_now: dict[str, float] = {}
    fund_cover: list[float] = []
    if use_funds:
        from .fundamentals_history import point_in_time as _pit
        for sym in sorted(set().union(*(in_index(d) for d in rebal[:-1])) & set(hist)):
            try:
                filings[sym] = fundamentals.history(sym)
            except Exception as e:  # noqa: BLE001
                log.warning("results history for %s unavailable: %s", sym, e)
                filings[sym] = []
            latest = _pit(filings[sym], rebal[-1]) if filings[sym] else None
            if latest and latest.get("shares"):
                shares_now[sym] = latest["shares"]
    window_members = set().union(*(in_index(d) for d in rebal[:-1]))
    missing = sorted(window_members - set(hist))

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
            for sym in in_index(prev) & set(hist):
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
        eligible_now = in_index(day)
        for sym, bars in hist.items():
            if sym not in eligible_now:
                continue
            i = bisect_right(dates_of[sym], day)
            if i < MIN_BARS:
                continue
            window = bars[:i]
            st = momentum_stats(window)
            st["vol_60d"] = _vol(window)
            stats[sym] = st
        ranked = score_universe(stats, min_turnover=min_turnover, require_above_200dma=require_above_200dma)
        if use_funds:
            from .fundamentals import apply_fundamentals
            from .fundamentals_history import point_in_time as _pit, with_price
            funds = {}
            for r in ranked:
                if not r["eligible"]:
                    continue
                m = _pit(filings.get(r["symbol"], []), day)
                if m and r["symbol"] in shares_now:
                    m = {**m, "shares": shares_now[r["symbol"]]}  # Yahoo prices are split-adjusted
                funds[r["symbol"]] = with_price(m, _price_at(hist[r["symbol"]], dates_of[r["symbol"]], day))
            have = [f for f in funds.values() if "error" not in f]
            fund_cover.append(len(have) / len(funds) if funds else 0.0)
            apply_fundamentals(ranked, funds, quality=quality_w, value=value_w, industries=industries)
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
    stats = {"strategy": s_stats, "benchmark": b_stats, "equal_weight": e_stats}
    if price_index:
        try:
            px = prices.history(price_index, rng)
            px_dates = [b["date"] for b in px]
            p0 = _price_at(px, px_dates, rebal[0])
            stats["price_index"] = _stats([capital * (_price_at(px, px_dates, d) or p0) / p0 for d in rebal], rebal)
        except Exception:  # noqa: BLE001 - informational row only
            pass
    fund_curve: list[float | None] | None = None
    if index_fund:
        try:
            fx = prices.history(index_fund, rng)
            fx_dates = [b["date"] for b in fx]
            if fx_dates and fx_dates[0] <= rebal[0]:  # only when the fund existed for the whole test
                f0 = _price_at(fx, fx_dates, rebal[0])
                fund_curve = [round(capital * (_price_at(fx, fx_dates, d) or f0) / f0, 2) for d in rebal]
                stats["index_fund"] = _stats(fund_curve, rebal)
        except Exception:  # noqa: BLE001 - optional comparison
            pass
    point_in_time = membership is not None and membership.known_since <= rebal[0]
    dropped = sorted(window_members - set(current))
    return {
        "dates": rebal, "strategy": curve, "benchmark": bench_curve, "equal_weight": ew_curve,
        "index_fund": fund_curve, "index_fund_symbol": index_fund if fund_curve else None,
        "stats": stats,
        "costs_paid": round(costs_paid, 2), "trades": trades, "months": len(rebal) - 1,
        "avg_names_held": round(avg_names, 1), "months_all_cash": cash_months,
        "universe_size": len(window_members), "with_history": len(set(hist) & window_members),
        "top": top, "capital": capital, "benchmark_symbol": benchmark, "price_index_symbol": price_index,
        "picks": picks_log[-3:],
        "point_in_time": point_in_time,
        "former_members": dropped,
        "missing_history": [{"symbol": m, "why": DELISTED.get(m, "no price history found")} for m in missing],
        "index_changes": membership.changes_between(rebal[0], rebal[-1]) if membership else [],
        "caveat": _caveat(benchmark, membership, point_in_time, dropped, missing),
        "fundamentals": ({"quality": quality_w, "value": value_w, "source": "NSE results filings, point-in-time",
                          "avg_coverage": round(statistics.fmean(fund_cover), 3) if fund_cover else 0.0,
                          "note": "Valued on today's share count with split-adjusted prices, so later share "
                                  "issues leak in slightly. ROE and debt start around 2022-23, when balance "
                                  "sheets appear in the filings."} if use_funds else None),
    }


def _caveat(benchmark: str, membership: Membership | None, point_in_time: bool,
            dropped: list[str], missing: list[str]) -> str:
    parts = []
    if membership is None:
        parts.append("Universe is today's constituents: stocks that left the index are missing, which "
                     "flatters both the strategy and the equal-weight curve. Point-in-time membership is "
                     "built in for NIFTY 50 and rebuilt from NSE press releases for the broad indices "
                     "(index-history); for others pass a change log.")
    elif not point_in_time:
        parts.append(f"Index membership is known from {membership.known_since}; months before that use "
                     "the earliest known list, so they still carry some survivorship bias.")
    else:
        parts.append(f"Each month ranks only that month's index members ({len(dropped)} later dropped "
                     "are included), so there is no survivorship bias.")
    if membership is not None and membership.warnings:
        parts.extend(membership.warnings)
    if missing:
        parts.append(f"No price history for {', '.join(missing)}, so "
                     f"{'it is' if len(missing) == 1 else 'they are'} left out of the months "
                     f"{'it was' if len(missing) == 1 else 'they were'} in the index.")
    if benchmark.upper().startswith("NIFTYBEES"):
        parts.append("NIFTY 50 is the NIFTYBEES ETF, which includes dividends, like the stock returns.")
    elif benchmark.startswith("^"):
        parts.append("The benchmark is a price index without dividends, so it trails a real index fund "
                     "by about 1.2% a year.")
    return " ".join(parts)


def format_factor_backtest(r: dict[str, Any]) -> str:
    pct = lambda v: "  n/a " if v is None else f"{v*100:+6.1f}%"  # noqa: E731
    lines = [f"Factor portfolio: top {r['top']} of {r['with_history']}/{r['universe_size']} stocks, "
             f"{r['months']} monthly rebalances {r['dates'][0]} to {r['dates'][-1]}"
             + (" (point-in-time members)" if r.get("point_in_time") else ""),
             f"{'':<20}{'total':>9}{'CAGR':>9}{'max DD':>9}{'vol':>8}"]
    rows = [("strategy", "Strategy"), ("benchmark", r["benchmark_symbol"]), ("equal_weight", "Equal weight*")]
    if "index_fund" in r["stats"]:
        rows.append(("index_fund", f"{r.get('index_fund_symbol')} (fund)"))
    if "price_index" in r["stats"]:
        rows.append(("price_index", f"{r.get('price_index_symbol')} (no div)"))
    for key, label in rows:
        s = r["stats"][key]
        lines.append(f"{label:<20}{pct(s['total_return'])}{pct(s['cagr'])}{pct(s['max_drawdown'])}"
                     f"{pct(s['volatility'])}")
    lines.append(f"Charges paid ₹{r['costs_paid']:,.0f} over {r['trades']} trades; "
                 f"avg {r['avg_names_held']} names held; {r['months_all_cash']} months fully in cash.")
    for c in r.get("index_changes", []):
        lines.append(f"  {c['date']}: +{' +'.join(c['added'])}  -{' -'.join(c['removed'])}")
    lines.append("* equal weight of that month's index members, rebalanced monthly, no costs.")
    lines.append("Caveat: " + r["caveat"])
    return "\n".join(lines)


def _comparison(base: dict[str, Any], universe: str | None) -> tuple[list[float | None] | None, dict[str, Any]]:
    """The curve the strategy has to beat, and an honest label for it."""
    expected = INDEX_FUNDS.get((universe or "").upper())
    if base.get("index_fund"):
        return base["index_fund"], {"symbol": base.get("index_fund_symbol") or expected or "index fund",
                                    "kind": "index fund", "note": ""}
    bench = base.get("benchmark")
    sym = base.get("benchmark_symbol") or "the benchmark"
    if not bench or all(v is None for v in bench):
        return None, {"symbol": None, "kind": None, "note": "no comparison curve was available"}
    if sym.startswith("^"):
        return bench, {"symbol": sym, "kind": "price index", "note": "a price index without dividends"}
    if expected is None and (universe or "").upper() == "NIFTY50":
        return bench, {"symbol": sym, "kind": "index fund", "note": ""}
    why = (f"the {expected} fund didn't exist for the whole window" if expected
           else "this universe has no index fund of its own")
    return bench, {"symbol": sym, "kind": "other benchmark", "note": f"a different index; {why}"}


def validate_factor_backtest(run_fn: Callable[[int], dict[str, Any]], top: int, candidates: Iterable[int] = (10, 20, 30),
                             *, base: dict[str, Any] | None = None, universe: str | None = None,
                             progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Is this result skill or luck? Re-run the neighbouring portfolio sizes, then walk forward
    (pick the best size from earlier years, score it on the next), deflate the Sharpe of the
    EXCESS return over the comparison for the number of sizes tried, and resample the returns
    for a range of possible falls."""
    from .validation import deflated_sharpe, excess_returns, monte_carlo, period_returns, sharpe, walk_forward

    tops = sorted({top, *candidates})
    runs: dict[int, dict[str, Any]] = {}
    for n, t in enumerate(tops, 1):
        if t == top and base is not None:
            runs[t] = base
            continue
        if progress:
            progress(f"validating: top {t} ({n} of {len(tops)})")
        runs[t] = run_fn(t)
    b = runs[top]
    comp, label = _comparison(b, universe)
    wf = walk_forward({t: r["strategy"] for t, r in runs.items()}, b["dates"], comp)
    excess = excess_returns(b["strategy"], comp)
    srs = [s for s in (sharpe(excess_returns(r["strategy"], comp)) for r in runs.values()) if s is not None]
    dsr = deflated_sharpe(excess, len(runs), srs)
    mc = monte_carlo(period_returns(b["strategy"]))
    verdict, summary = _verdict(wf, dsr, label)
    return {"walk_forward": wf, "deflated_sharpe": dsr, "monte_carlo": mc, "comparison": label,
            "verdict": verdict, "summary": summary}


def _verdict(wf: dict[str, Any], dsr: dict[str, Any], label: dict[str, Any]) -> tuple[str, str]:
    p, oos, fund = dsr["probability"], wf["oos_return"], wf["fund_return"]
    if label["kind"] is None or p is None:
        return "could be luck", ("Could be luck: there is no comparison to measure against, or too few monthly "
                                 "returns, to tell skill from luck.")
    name = f"{label['symbol']}" + (f" ({label['note']})" if label["note"] else "")
    pct = f"{p * 100:.0f}%"
    head = f"odds it truly beats {name} are {pct} after {dsr['trials']} portfolio sizes were tried"
    years = wf["full_years"]
    if oos is None:
        walk = "There were too few years to test picking the size in advance."
    elif fund is None:
        walk = f"Choosing the size from earlier years returned {oos * 100:+.1f}% on the years that followed."
    else:
        walk = (f"Choosing the size from earlier years returned {oos * 100:+.1f}% on the {len(wf['years'])} years "
                f"that followed, against {fund * 100:+.1f}% for {label['symbol']}.")
    behind = oos is not None and fund is not None and oos < fund - 0.05
    if p < 0.5 or behind:
        return "no edge", f"No edge shown: {head}. {walk}"
    if p >= 0.95 and label["kind"] == "index fund" and fund is not None and oos > fund and years >= 3:
        return "likely skill", f"Likely skill: {head}. {walk}"
    extra = ""
    if label["kind"] != "index fund":
        extra = " It is not compared with its own index fund, so it cannot be called skill."
    elif years < 3:
        extra = " Fewer than 3 full years were tested out of sample, which is too little to call skill."
    elif fund is None or oos is None or oos <= fund:
        extra = " The out-of-sample years did not beat the comparison."
    return "could be luck", f"Could be luck: {head}. {walk}{extra}"


def format_validation(v: dict[str, Any]) -> str:
    wf, d, mc = v["walk_forward"], v["deflated_sharpe"], v["monte_carlo"]
    pct = lambda x: "   n/a " if x is None else f"{x*100:+7.1f}%"  # noqa: E731
    lines = [f"Skill or luck? {v['verdict'].upper()}. {v['summary']}",
             f"Walk-forward (best of {', '.join(map(str, wf['candidates']))} stocks, chosen from earlier years only):",
             f"  {'year':<6}{'stocks':>7}{'return':>10}{'fund':>10}"]
    for y in wf["years"]:
        flag = " (data missing, left out)" if y.get("missing") else " (partial year)" if y.get("partial") else ""
        lines.append(f"  {y['year']:<6}{str(y['chosen_top'] or '-'):>7}{pct(y['return'])}{pct(y['fund_return'])}{flag}")
    if wf["years"]:
        lines.append(f"  {'all':<6}{'':>7}{pct(wf['oos_return'])}{pct(wf['fund_return'])}   "
                     f"(beat the fund in {wf['beat_years']} of {len(wf['years'])} years)")
    c = v.get("comparison") or {}
    if d["probability"] is not None:
        lines.append(f"Odds it truly beats {c.get('symbol')} after {d['trials']} tries: {d['probability']*100:.0f}% "
                     f"({d['periods']} monthly returns). Only the portfolio sizes are counted, not the factors "
                     "and filters chosen with hindsight, so treat this as an upper bound.")
    if mc:
        dd, fr = mc["max_drawdown"], mc["final_return"]
        lines.append(f"Monte Carlo ({mc['paths']} resamples): in 90% the worst fall was between "
                     f"{dd['p5']*100:.0f}% and {dd['p95']*100:.0f}%; final return between "
                     f"{fr['p5']*100:+.0f}% and {fr['p95']*100:+.0f}%; {mc['loss_probability']*100:.0f}% ended in a loss.")
    return "\n".join(lines)
