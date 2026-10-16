"""Research steps B1 to B4: the stop-rule race and the portfolio controls, on the daily simulator (research_sim).

Both studies use the Midcap 150 screen (top 20, monthly), point-in-time membership, Indian delivery charges and the
sealed holdout: nothing from ``HOLDOUT_START`` on is read. Acceptance rules are the ones agreed before any run
(roadmap and coverage pages, 11 to 16 Oct 2026) and are written here, not chosen after seeing results.

B1, stop rules (``stop_race``)
    Six rules: none (the control), 3 x ATR trail (today's agent rule), 5 x ATR trail, two-stage (stop at entry - 2.5
    ATR, raised to break-even after +1.5 R, then the highest close - 3.5 ATR), a 20-day grace before the 3 x ATR trail,
    and a close below the stock's 50-day EMA. Each runs on about ten one-year windows (starting January, May and
    September of 2021 to 2024, all ending by 30 Sep 2025) and once over the whole period. A rule is ACCEPTED when its
    median holding period is at least 60 days, its median worst fall over the windows is no worse than today's
    3 x ATR rule, and the deflated Sharpe of its monthly excess return over the index fund (whole period, counting
    every trial in the log) is at least 0.95. The report recommends; the owner decides.

B2 to B4, portfolio controls (``portfolio_controls``)
    Run over the whole period after tax (Indian STCG / LTCG), each change alone against the baseline (the screen as it
    is, plus the stop rule given, none by default):
    * rank buffer 35: kept if one-way turnover falls by 35% or more without a lower after-tax return;
    * tax-aware buffer: kept if its after-tax return beats the plain buffer's;
    * drawdown brake (50/200-day EMA of the index fund; variants: a 100-day average, + 3 days of breadth below 35%,
      + 5 days of FII/DII selling): kept if the worst fall is smaller with an equal or better return;
    * inverse-volatility weights (2.5% to 8% a stock, equal below 13 names): kept if volatility falls with an equal or
      better return.
    The changes that pass are then combined and run once more; the combination is judged on the deflated Sharpe of
    its excess over the index fund, counting every variant tried.
"""

from __future__ import annotations

import json
import statistics
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .research_sim import Market, Rules, monthly, simulate, summarize, with_rules
from .validation import deflated_sharpe, excess_returns, sharpe

HOLDOUT_START = "2025-10-01"
WINDOW_STARTS = [f"{y}-{m:02d}-01" for y in (2021, 2022, 2023, 2024) for m in (1, 5, 9)]
STOP_SET = ("none", "atr3", "atr5", "two_stage", "grace20", "ema50")
SKILL_BAR = 0.95


def prior_trials(state_dir: Path | None) -> int:
    path = Path(state_dir) / "research" / "trials.jsonl" if state_dir else None
    if not path or not path.exists():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


def _index_on_or_after(m: Market, day: str) -> int | None:
    return next((i for i, d in enumerate(m.dates) if d >= day), None)


def _window(m: Market, start_day: str) -> tuple[int, int] | None:
    s = _index_on_or_after(m, start_day)
    if s is None:
        return None
    end_day = (date.fromisoformat(m.dates[s]) + timedelta(days=365)).isoformat()
    e = max(i for i, d in enumerate(m.dates) if d < end_day)
    return (s, e) if e > s + 150 else None


def _first_ranked_day(m: Market) -> int:
    """The first month start of the test window on which the screen can rank 20 names (enough history)."""
    for t in m.rebalance_days(0, len(m.dates) - 1):
        if m.dates[t] < m.start_day:
            continue
        if len(m.momentum_ranking(t)) >= 20:
            return t
    raise ValueError("not enough history to rank 20 names")


def _excess_monthly(run: dict[str, Any], m: Market, start: int, end: int) -> list[float]:
    from .research_sim import fund_curve
    dates = run["dates"]
    return excess_returns(monthly(run["curve"], dates), monthly(fund_curve(m, start, end, run["capital"]), dates))


def _pbo(runs: dict[str, dict[str, Any]], m: Market, start: int, end: int) -> dict[str, Any] | None:
    """Probability of backtest overfitting across these variants, on their monthly returns above the fund."""
    from .validation import pbo
    return pbo({k: [x for x in _excess_monthly(r, m, start, end) if x is not None] for k, r in runs.items()})


def pbo_line(p: dict[str, Any] | None) -> str:
    if not p:
        return "  Probability of backtest overfitting: not enough months or variants to measure."
    return (f"  Probability of backtest overfitting: {p['pbo']:.2f} ({p['variants']} variants, {p['splits']} splits of "
            f"{p['periods']} months): the chance the variant that looks best is at or below the median on months it "
            "was not picked on; 0.5 or more = picking the best here is no better than chance.")


def _skill(runs: dict[str, dict[str, Any]], m: Market, start: int, end: int, extra_trials: int) -> dict[str, Any]:
    ex = {k: _excess_monthly(r, m, start, end) for k, r in runs.items()}
    srs = [s for s in (sharpe(v) for v in ex.values()) if s is not None]
    return {k: deflated_sharpe(v, len(runs) + extra_trials, srs) for k, v in ex.items()}


# --------------------------------------------------------------------------- #
# B1: the stop race
# --------------------------------------------------------------------------- #
def stop_race(m: Market, *, cost_model: Any, capital: float = 500_000.0, extra_trials: int = 0,
              progress: Any = None) -> dict[str, Any]:
    first = _first_ranked_day(m)
    windows = [w for w in (_window(m, d) for d in WINDOW_STARTS) if w and w[0] >= first]
    out: dict[str, Any] = {"windows": [(m.dates[s], m.dates[e]) for s, e in windows], "rules": {}}
    whole = (first, len(m.dates) - 1)
    full_runs = {}
    for rule in STOP_SET:
        if progress:
            progress(f"stop race: {rule}")
        rules = Rules(stop=rule)
        per = [summarize(simulate(m, rules, s, e, capital=capital, cost_model=cost_model), m, s, e)
               for s, e in windows]
        full = simulate(m, rules, *whole, capital=capital, cost_model=cost_model)
        full_runs[rule] = full
        holds = [x["median_hold_days"] for x in per if x["median_hold_days"] is not None]
        out["rules"][rule] = {
            "windows": per, "whole": summarize(full, m, *whole),
            "median_hold_days": statistics.median(holds) if holds else None,
            "median_window_drawdown": statistics.median(x["max_drawdown"] for x in per) if per else None,
            "worst_window_drawdown": min(x["max_drawdown"] for x in per) if per else None,
            "median_window_excess": statistics.median(x["excess"] for x in per if x["excess"] is not None)
            if per else None,
            "median_stops_per_window": statistics.median(x["stops"] for x in per) if per else None}
    skill = _skill(full_runs, m, *whole, extra_trials)
    out["pbo"] = _pbo(full_runs, m, *whole)
    current = out["rules"]["atr3"]
    accepted = []
    for rule, r in out["rules"].items():
        r["skill"] = skill[rule]
        if rule in ("none", "atr3"):
            r["verdict"] = "control" if rule == "none" else "today's rule"
            continue
        checks = {
            "holds 60+ days": r["median_hold_days"] is not None and r["median_hold_days"] >= 60,
            "worst fall no worse than 3xATR": r["median_window_drawdown"] is not None
            and current["median_window_drawdown"] is not None
            and r["median_window_drawdown"] >= current["median_window_drawdown"],
            "clears the skill bar": (skill[rule]["probability"] or 0) >= SKILL_BAR}
        r["checks"] = checks
        r["verdict"] = "ACCEPTED" if all(checks.values()) else "not accepted"
        if all(checks.values()):
            accepted.append(rule)
    best = max(accepted, key=lambda k: out["rules"][k]["median_window_excess"] or -9) if accepted else None
    out["recommendation"] = (f"Switch the agent's stop to '{best}' (the owner decides)." if best else
                             "No rule passed all three checks: keep today's 3 x ATR rule for now.")
    out["best"] = best
    out["whole_period"] = (m.dates[whole[0]], m.dates[whole[1]])
    return out


# --------------------------------------------------------------------------- #
# B2-B4: portfolio controls
# --------------------------------------------------------------------------- #
def portfolio_controls(m: Market, *, cost_model: Any, stop: str = "none", capital: float = 500_000.0,
                       extra_trials: int = 0, progress: Any = None) -> dict[str, Any]:
    start, end = _first_ranked_day(m), len(m.dates) - 1
    base = Rules(stop=stop, tax=True)
    flows_ok = bool(m.flows) and min(m.flows) <= m.dates[start]
    variants: dict[str, Rules] = {
        "baseline": base,
        "buffer35": with_rules(base, buffer=35),
        "buffer35_tax_aware": with_rules(base, buffer=35, tax_aware=True),
        "brake_ema": with_rules(base, brake="ema"),
        "brake_sma100": with_rules(base, brake="sma100"),
        "brake_ema_breadth": with_rules(base, brake="ema_breadth"),
        "inverse_vol": with_rules(base, weights="inverse_vol"),
    }
    if flows_ok:
        variants["brake_ema_flows"] = with_rules(base, brake="ema_flows")
    runs, res = {}, {}
    for name, rules in variants.items():
        if progress:
            progress(f"portfolio controls: {name}")
        runs[name] = simulate(m, rules, start, end, capital=capital, cost_model=cost_model)
        res[name] = summarize(runs[name], m, start, end)
    b = res["baseline"]

    def ret(x: dict[str, Any]) -> float:
        return x["total_return"] if x["total_return"] is not None else -9.0

    passed: dict[str, bool] = {}
    notes: dict[str, str] = {}
    bt = b["turnover"] or 0
    passed["buffer35"] = bool(res["buffer35"]["turnover"] is not None and bt
                              and res["buffer35"]["turnover"] <= 0.65 * bt and ret(res["buffer35"]) >= ret(b))
    passed["buffer35_tax_aware"] = ret(res["buffer35_tax_aware"]) > ret(res["buffer35"])
    for k in [k for k in res if k.startswith("brake_")]:
        passed[k] = (res[k]["max_drawdown"] or -9) > (b["max_drawdown"] or -9) and ret(res[k]) >= ret(b)
    passed["inverse_vol"] = (res["inverse_vol"]["volatility"] or 9) < (b["volatility"] or 9) and \
        ret(res["inverse_vol"]) >= ret(b)
    if not flows_ok:
        notes["brake_ema_flows"] = ("not testable yet: the FII/DII history only starts when the server began "
                                    "saving it (NSE publishes the latest day only)")
    combo: dict[str, Any] = {}
    if passed["buffer35"]:
        combo.update(buffer=35, tax_aware=passed["buffer35_tax_aware"])
    brakes = [k for k in res if k.startswith("brake_") and passed.get(k)]
    if brakes:
        bk = max(brakes, key=lambda k: res[k]["max_drawdown"] or -9)
        combo["brake"] = variants[bk].brake
    if passed["inverse_vol"]:
        combo["weights"] = "inverse_vol"
    combined = None
    if len([k for k in ("buffer", "brake", "weights") if k in combo]) >= 2:
        if progress:
            progress("portfolio controls: the passing changes combined")
        rules = with_rules(base, **combo)
        runs["combined"] = simulate(m, rules, start, end, capital=capital, cost_model=cost_model)
        res["combined"] = summarize(runs["combined"], m, start, end)
        combined = rules.label()
    skill = _skill(runs, m, start, end, extra_trials)
    for k in res:
        res[k]["skill"] = skill[k]
    return {"window": (m.dates[start], m.dates[end]), "stop": stop, "results": res, "passed": passed,
            "pbo": _pbo(runs, m, start, end),
            "notes": notes, "combined": combined,
            "combined_clears_skill_bar": bool(combined and (skill["combined"]["probability"] or 0) >= SKILL_BAR)}


# --------------------------------------------------------------------------- #
# reports
# --------------------------------------------------------------------------- #
def _p(x: float | None, d: int = 1) -> str:
    return "n/a" if x is None else f"{x * 100:+.{d}f}%"


SIMPLIFICATIONS = ("Trades at the close; close-to-close ATR (no daily high/low); cash earns a liquid-fund rate of "
                   "6.5% a year; one cost basis per holding.")


def format_stop_race(r: dict[str, Any]) -> str:
    w = r["windows"]
    lines = [f"Research B1: the stop race, Midcap 150 top 20, {len(w)} one-year windows from {w[0][0]} to {w[-1][1]}"
             if w else "Research B1: the stop race (no complete window)",
             f"  Whole period {r['whole_period'][0]} to {r['whole_period'][1]}; nothing from {HOLDOUT_START} on.",
             f"  {'rule':<11}{'median hold':>12}{'stops/yr':>9}{'median fall':>12}{'worst fall':>11}"
             f"{'excess/yr':>10}{'skill odds':>11}  verdict"]
    for rule, x in r["rules"].items():
        prob = x["skill"]["probability"]
        lines.append(f"  {rule:<11}{(str(round(x['median_hold_days'])) + 'd') if x['median_hold_days'] is not None else 'n/a':>12}"
                     f"{x['median_stops_per_window'] if x['median_stops_per_window'] is not None else 'n/a':>9}"
                     f"{_p(x['median_window_drawdown']):>12}{_p(x['worst_window_drawdown']):>11}"
                     f"{_p(x['median_window_excess']):>10}{('n/a' if prob is None else f'{prob:.2f}'):>11}  {x['verdict']}")
    lines.append("  Accepted = median hold 60+ days, median worst fall no worse than 3xATR, skill odds 0.95+ "
                 "(deflated Sharpe of the monthly excess over MID150BEES, counting every trial).")
    lines.append(f"  Recommendation: {r['recommendation']}")
    lines.append(pbo_line(r.get("pbo")))
    lines.append(f"  {SIMPLIFICATIONS}")
    return "\n".join(lines)


def format_controls(r: dict[str, Any]) -> str:
    lines = [f"Research B2-B4: portfolio controls, Midcap 150 top 20, {r['window'][0]} to {r['window'][1]} "
             f"(after Indian capital-gains tax; stop rule: {r['stop']})",
             f"  {'variant':<20}{'return':>9}{'vs fund':>9}{'worst fall':>11}{'volatility':>11}{'turnover':>9}"
             f"{'invested':>9}{'tax paid':>10}  result"]
    for k, x in r["results"].items():
        verdict = "" if k in ("baseline", "combined") else ("PASSED" if r["passed"].get(k) else "not passed")
        lines.append(f"  {k:<20}{_p(x['total_return']):>9}{_p(x['excess']):>9}{_p(x['max_drawdown']):>11}"
                     f"{_p(x['volatility'], 0):>11}{_p(x['turnover'], 0):>9}{_p(x['avg_invested'], 0):>9}"
                     f"{x['tax_paid']:>10,.0f}  {verdict}")
    for k, n in r["notes"].items():
        lines.append(f"  {k}: {n}")
    lines.append("  Rules: buffer passes if turnover falls 35%+ without a lower return; tax-aware if it beats the plain "
                 "buffer after tax; a brake if the worst fall shrinks at an equal or better return; inverse-vol if "
                 "volatility falls at an equal or better return.")
    if r["combined"]:
        p = r["results"]["combined"]["skill"]["probability"]
        lines.append(f"  Combined ({r['combined']}): skill odds {('n/a' if p is None else f'{p:.2f}')} -> "
                     + ("clears the bar." if r["combined_clears_skill_bar"] else "does not clear the 0.95 bar."))
    else:
        lines.append("  Fewer than two changes passed, so there is no combination to test.")
    lines.append(pbo_line(r.get("pbo")))
    lines.append(f"  {SIMPLIFICATIONS}")
    return "\n".join(lines)


def compact(r: dict[str, Any]) -> str:
    """A JSON-safe copy (Rules objects as labels)."""
    return json.dumps(r, default=lambda o: getattr(o, "label", lambda: str(o))(), indent=2)


# --------------------------------------------------------------------------- #
# robustness: the adopted strategy under harder assumptions
# --------------------------------------------------------------------------- #
FINALIST = Rules(stop="none", buffer=35, tax_aware=True, tax_aware_floor=50, tax=True, floor=0.25)
ROBUSTNESS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("as researched (same-close fills, no floor)", {"floor": None}),
    ("adopted (+ 25% floor)", {}),
    ("next-day fills", {"lag": 1}),
    ("costs x1.5", {"cost_mult": 1.5}),
    ("costs x2", {"cost_mult": 2.0}),
    ("next-day fills + costs x2", {"lag": 1, "cost_mult": 2.0}),
)


def robustness(m: Market, *, cost_model: Any, capital: float = 500_000.0, extra_trials: int = 0,
               progress: Any = None) -> dict[str, Any]:
    """The forward test's rules (no stop, tax-aware buffer, 25% floor, after tax) re-run with the trades one session
    later and with charges raised, each against the index fund on the same days. It holds when every variant still
    beats the fund; the skill odds count every variant here plus every earlier trial in the log."""
    start = _first_ranked_day(m)
    end = len(m.dates) - 1
    runs: dict[str, dict[str, Any]] = {}
    results: dict[str, dict[str, Any]] = {}
    for name, kw in ROBUSTNESS:
        if progress:
            progress(f"robustness: {name}")
        run = simulate(m, with_rules(FINALIST, **kw), start, end, capital=capital, cost_model=cost_model)
        runs[name] = run
        results[name] = summarize(run, m, start, end)
    skill = _skill(runs, m, start, end, extra_trials)
    for name, x in results.items():
        x["skill"] = skill[name]
    missing = sorted(getattr(m, "missing", []) or [])
    return {"start": m.dates[start], "end": m.dates[end], "results": results,
            "holds": all(x["excess"] is not None and x["excess"] > 0 for x in results.values()),
            "missing": missing, "members": len(m.raw) + len(missing)}


def format_robustness(r: dict[str, Any]) -> str:
    def pct(v: Any, f: str = "+.1f") -> str:
        return "n/a" if v is None else f"{v * 100:{f}}%"
    lines = [f"Robustness of the adopted rules (no stop, tax-aware buffer 35/50, 25% floor, after charges and tax), "
             f"{r['start']} to {r['end']}:",
             f"  {'variant':<44}{'return':>9}{'vs fund':>9}{'a year':>8}{'worst fall':>11}{'turnover':>9}"
             f"{'skill odds':>11}"]
    for name, x in r["results"].items():
        p = x["skill"].get("probability")
        lines.append(f"  {name:<44}{pct(x['total_return']):>9}{pct(x['excess']):>9}{pct(x['cagr']):>8}"
                     f"{pct(x['max_drawdown']):>11}{pct(x['turnover'], '.0f'):>9}"
                     f"{'n/a' if p is None else f'{p:.2f}':>11}")
    first = next(iter(r["results"].values()))
    lines.append(f"  MID150BEES on the same days: {pct(first['fund_total_return'])}, worst fall "
                 f"{pct(first['fund_max_drawdown'])}.")
    n, k = r["members"], len(r["missing"])
    lines.append(f"  Survivorship: {k} of {n} index members in the window have no price history and are left out"
                 + (f" ({', '.join(r['missing'][:10])}{' …' if k > 10 else ''})" if k else "")
                 + ". Delisted stocks are the usual reason, so the returns above may be flattered by that much.")
    worst = r["results"].get("next-day fills + costs x2", first)
    lines.append("  Verdict: " + ("holds: every variant still beats the fund" if r["holds"] else
                                  "fragile: at least one variant does not beat the fund")
                 + f"; the hardest case (next-day fills + costs x2) is {pct(worst['excess'])} against it. "
                 f"Skill odds need 0.95 to count as proven; the holdout stays sealed.")
    return "\n".join(lines)



# --------------------------------------------------------------------------- #
# regimes: how the adopted rules behave in different markets (a diagnostic, not a filter)
# --------------------------------------------------------------------------- #
REGIMES = ("bull", "sideways", "bear", "high volatility")
TREND_BAND = 0.03       # the fund's 3-month move beyond +-3% (with the 200-day average) is a trend
VOL_PCTL = 0.80         # 20-day volatility above the 80th percentile of the past year's readings is "high"


def _vol20(adj: list[float | None], t: int) -> float | None:
    rets = []
    for i in range(max(1, t - 19), t + 1):
        a, b = adj[i], adj[i - 1]
        if a and b:
            rets.append(a / b - 1)
    return statistics.pstdev(rets) * 252 ** 0.5 if len(rets) >= 15 else None


def regime_of(m: Market, t: int) -> str | None:
    """The market's state as known at day t's close, from the index fund alone: high volatility first (its 20-day
    volatility above the 80th percentile of its readings over the past year), else bull (above the 200-day average and
    up more than 3% over 3 months), bear (below it and down more than 3%) or sideways. None without enough history."""
    adj = m.fund_adj
    now, then, c, e = adj[t] if t >= 0 else None, adj[t - 63] if t >= 63 else None, m.fund_close[t], m.fund_ema200[t]
    if t < 252 + 63 or not now or not then or c is None or e is None:
        return None
    v = _vol20(adj, t)
    past = [x for x in (_vol20(adj, k) for k in range(t - 252, t, 5)) if x is not None]
    if v is not None and len(past) >= 20 and v > sorted(past)[int(VOL_PCTL * (len(past) - 1))]:
        return "high volatility"
    move = now / then - 1
    if c >= e and move > TREND_BAND:
        return "bull"
    if c < e and move < -TREND_BAND:
        return "bear"
    return "sideways"


def regime_diagnostic(m: Market, *, cost_model: Any, capital: float = 500_000.0,
                      rules: Rules | None = None) -> dict[str, Any]:
    """The adopted rules run once; each month (rebalance to rebalance) labelled by the regime known the day before it
    began, then the strategy against the index fund per regime: average month, share of months ahead, worst month,
    worst fall inside a month, turnover a month."""
    from .research_sim import fund_curve
    rules = rules or FINALIST
    start = _first_ranked_day(m)
    end = len(m.dates) - 1
    run = simulate(m, rules, start, end, capital=capital, cost_model=cost_model)
    curve, fund, cum = run["curve"], fund_curve(m, start, end, capital), run["traded_cum"]
    edges = [t for t in m.rebalance_days(start, end) if t >= start] + [end]
    months: list[dict[str, Any]] = []
    for a, b in zip(edges, edges[1:]):
        if b <= a:
            continue
        i, j = a - start, b - start
        f0, f1 = fund[i], fund[j]
        if not curve[i] or f0 is None or f1 is None:
            continue
        seg = curve[i:j + 1]
        peak, worst = seg[0], 0.0
        for v in seg:
            peak = max(peak, v)
            worst = min(worst, v / peak - 1)
        r, fr = curve[j] / curve[i] - 1, f1 / f0 - 1
        avg = statistics.fmean(seg)
        months.append({"start": m.dates[a], "regime": regime_of(m, a - 1), "return": r, "fund": fr, "excess": r - fr,
                       "worst_fall": worst, "turnover": (cum[j] - cum[i]) / avg / 2 if avg else 0.0})
    by: dict[str, dict[str, Any]] = {}
    for name in REGIMES + ("unlabelled",):
        ms = [x for x in months if (x["regime"] or "unlabelled") == name]
        if not ms:
            continue
        by[name] = {"months": len(ms), "avg_return": statistics.fmean(x["return"] for x in ms),
                    "avg_fund": statistics.fmean(x["fund"] for x in ms),
                    "avg_excess": statistics.fmean(x["excess"] for x in ms),
                    "ahead_share": sum(x["excess"] > 0 for x in ms) / len(ms),
                    "worst_month": min(x["return"] for x in ms), "worst_fall": min(x["worst_fall"] for x in ms),
                    "turnover": statistics.fmean(x["turnover"] for x in ms)}
    return {"start": m.dates[start], "end": m.dates[end], "rules": rules.label(), "months": months, "by_regime": by}


def format_regimes(r: dict[str, Any]) -> str:
    def pct(v: Any, f: str = "+.1f") -> str:
        return "n/a" if v is None else f"{v * 100:{f}}%"
    lines = [f"Regimes ({r['rules']}, after charges and tax), {r['start']} to {r['end']}: each month labelled by the "
             "index fund's state the day before it began.",
             f"  {'regime':<17}{'months':>7}{'avg month':>10}{'fund':>8}{'vs fund':>9}{'ahead':>7}{'worst month':>12}"
             f"{'worst fall':>11}{'turnover':>9}"]
    for name, x in r["by_regime"].items():
        lines.append(f"  {name:<17}{x['months']:>7}{pct(x['avg_return']):>10}{pct(x['avg_fund']):>8}"
                     f"{pct(x['avg_excess']):>9}{pct(x['ahead_share'], '.0f'):>7}{pct(x['worst_month']):>12}"
                     f"{pct(x['worst_fall']):>11}{pct(x['turnover'], '.0f'):>9}")
    small = [k for k, x in r["by_regime"].items() if x["months"] < 6]
    lines.append("  Regimes: high volatility = the fund's 20-day volatility above the 80th percentile of the past year; "
                 "else bull / bear = above / below its 200-day average with a 3-month move beyond +-3%; else sideways.")
    if small:
        lines.append(f"  Fewer than 6 months in: {', '.join(small)}; read those rows as anecdotes.")
    lines.append("  A diagnostic: nothing here changes a rule. A filter built on this table would be a new trial, "
                 "judged on months it never saw.")
    return "\n".join(lines)
