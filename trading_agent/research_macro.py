"""Macro diagnostic: how the adopted rules did around oil moves, rupee moves and RBI rate changes, and how exposed
their holdings were to oil and the rupee. A description, not a filter: nothing here changes a rule or counts as a
trial, and every label uses only what was known the day before the month began.

Data: Brent crude futures (Yahoo ``BZ=F``) and USD/INR (Yahoo ``INR=X``), aligned to the index fund's trading days
(a day without a quote carries the last one); RBI repo-rate changes from the policy statements (``RBI_REPO_CHANGES``).
"""
from __future__ import annotations

import math
import statistics
from datetime import date
from typing import Any

from .research_sim import Market, Rules, fund_curve, simulate

OIL, RUPEE = "BZ=F", "INR=X"
OIL_MOVE = 0.10        # Brent up or down more than 10% over the month before
RUPEE_MOVE = 0.02      # USD/INR up (a weaker rupee) or down more than 2% over the month before
RBI_WINDOW = 45        # a rate change in the 45 days before the month counts as "after a hike / cut"
BETA_DAYS = 252        # each stock's sensitivity is measured on the year before the rebalance
LOOKBACK = 21          # "the month before" in sessions

# RBI repo-rate changes, (policy announcement date, change in basis points), from the RBI's monetary policy statements.
# Months with no change are holds. Hand-entered; covers the research window and the year before it.
RBI_REPO_CHANGES: tuple[tuple[str, int], ...] = (
    ("2019-02-07", -25), ("2019-04-04", -25), ("2019-06-06", -25), ("2019-08-07", -35), ("2019-10-04", -25),
    ("2020-03-27", -75), ("2020-05-22", -40),
    ("2022-05-04", 40), ("2022-06-08", 50), ("2022-08-05", 50), ("2022-09-30", 50), ("2022-12-07", 35),
    ("2023-02-08", 25),
    ("2025-02-07", -25), ("2025-04-09", -25), ("2025-06-06", -50),
)

OIL_STATES = ("oil up >10%", "oil within 10%", "oil down >10%")
RUPEE_STATES = ("rupee weaker >2%", "rupee within 2%", "rupee stronger >2%")
RBI_STATES = ("after a hike", "no change", "after a cut")


def aligned(m: Market, bars: list[dict[str, Any]]) -> list[float | None]:
    """A factor's closes on the market's calendar (the last quote carried over days it did not trade)."""
    close, _ = m._align(bars)
    return close


def _move(series: list[float | None], t: int, n: int = LOOKBACK) -> float | None:
    if t - n < 0:
        return None
    a, b = series[t - n], series[t]
    return b / a - 1 if a and b else None


def oil_state(series: list[float | None], t: int) -> str | None:
    x = _move(series, t)
    return None if x is None else OIL_STATES[0] if x > OIL_MOVE else OIL_STATES[2] if x < -OIL_MOVE else OIL_STATES[1]


def rupee_state(series: list[float | None], t: int) -> str | None:
    x = _move(series, t)
    return None if x is None else (RUPEE_STATES[0] if x > RUPEE_MOVE else RUPEE_STATES[2] if x < -RUPEE_MOVE
                                   else RUPEE_STATES[1])


def rbi_state(day: str, changes: tuple[tuple[str, int], ...] = RBI_REPO_CHANGES) -> str:
    """The latest repo change announced on or before ``day`` and within RBI_WINDOW days of it."""
    d = date.fromisoformat(day)
    recent = [bp for when, bp in changes if 0 <= (d - date.fromisoformat(when)).days <= RBI_WINDOW]
    if not recent:
        return RBI_STATES[1]
    return RBI_STATES[0] if recent[-1] > 0 else RBI_STATES[2]


def beta(stock: list[float | None], factor: list[float | None], t: int, n: int = BETA_DAYS) -> float | None:
    """Slope of the stock's daily log return on the factor's over the n sessions up to day t (both quoted)."""
    xs, ys = [], []
    for k in range(max(1, t - n + 1), t + 1):
        a0, a1, f0, f1 = stock[k - 1], stock[k], factor[k - 1], factor[k]
        if a0 and a1 and f0 and f1:
            xs.append(math.log(f1 / f0))
            ys.append(math.log(a1 / a0))
    if len(xs) < n // 2:
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    var = sum((x - mx) ** 2 for x in xs)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / var if var else None


def _months(m: Market, run: dict[str, Any], start: int, end: int, capital: float) -> list[dict[str, Any]]:
    curve, fund = run["curve"], fund_curve(m, start, end, capital)
    edges = [t for t in m.rebalance_days(start, end) if t >= start] + [end]
    out = []
    for a, b in zip(edges, edges[1:]):
        i, j = a - start, b - start
        if b <= a or not curve[i] or fund[i] is None or fund[j] is None:
            continue
        r, fr = curve[j] / curve[i] - 1, fund[j] / fund[i] - 1   # type: ignore[operator]
        out.append({"t": a, "start": m.dates[a], "return": r, "fund": fr, "excess": r - fr})
    return out


def _by(months: list[dict[str, Any]], key: str, states: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    out = {}
    for s in states:
        ms = [x for x in months if x.get(key) == s]
        if ms:
            out[s] = {"months": len(ms), "avg_return": statistics.fmean(x["return"] for x in ms),
                      "avg_fund": statistics.fmean(x["fund"] for x in ms),
                      "avg_excess": statistics.fmean(x["excess"] for x in ms),
                      "ahead_share": sum(x["excess"] > 0 for x in ms) / len(ms),
                      "worst_month": min(x["return"] for x in ms)}
    return out


def _exposure(m: Market, held: list[tuple[int, list[str]]], factor: list[float | None]) -> dict[str, Any] | None:
    """At each rebalance: the holdings' average beta to the factor minus that of the eligible names, and how many
    holdings sit in the fifth of eligible names with the LOWEST beta (hurt most when the factor rises)."""
    tilts, shares, counts = [], [], []
    for d, names in held:
        if not names:
            continue
        betas = {s: beta(m.adj[s], factor, d) for s in m.momentum_ranking(d)}
        b = {s: v for s, v in betas.items() if v is not None}
        hb = [b[s] for s in names if s in b]
        if len(b) < 10 or not hb:
            continue
        ordered = sorted(b, key=lambda s: b[s])
        worst = set(ordered[:max(1, len(ordered) // 5)])
        n = sum(1 for s in names if s in worst)
        tilts.append(statistics.fmean(hb) - statistics.fmean(b.values()))
        shares.append(n / len(names))
        counts.append(n)
    if not tilts:
        return None
    return {"rebalances": len(tilts), "avg_tilt": statistics.fmean(tilts), "avg_share_most_hurt": statistics.fmean(shares),
            "max_most_hurt": max(counts), "holdings": statistics.fmean(len(n) for _, n in held if n)}


def macro_diagnostic(m: Market, oil: list[dict[str, Any]], rupee: list[dict[str, Any]], *, cost_model: Any,
                     capital: float = 500_000.0, rules: Rules | None = None) -> dict[str, Any]:
    """The adopted rules run once; each month labelled by the oil move, the rupee move and the RBI's latest rate change
    known the day before it began; the strategy against the index fund per label; and the holdings' tilt to oil and
    the rupee at every rebalance."""
    from .research_studies import FINALIST, _first_ranked_day
    rules = rules or FINALIST
    start, end = _first_ranked_day(m), len(m.dates) - 1
    run = simulate(m, rules, start, end, capital=capital, cost_model=cost_model)
    o, r = aligned(m, oil), aligned(m, rupee)
    months = _months(m, run, start, end, capital)
    for x in months:
        x["oil"], x["rupee"], x["rbi"] = oil_state(o, x["t"] - 1), rupee_state(r, x["t"] - 1), rbi_state(m.dates[x["t"] - 1])
    return {"start": m.dates[start], "end": m.dates[end], "rules": rules.label(), "months": months,
            "oil": _by(months, "oil", OIL_STATES), "rupee": _by(months, "rupee", RUPEE_STATES),
            "rbi": _by(months, "rbi", RBI_STATES),
            "oil_exposure": _exposure(m, run["held"], o), "rupee_exposure": _exposure(m, run["held"], r),
            "quotes": {"oil": sum(v is not None for v in o), "rupee": sum(v is not None for v in r)}}


def format_macro(r: dict[str, Any]) -> str:
    def pct(v: Any, f: str = "+.1f") -> str:
        return "n/a" if v is None else f"{v * 100:{f}}%"
    lines = [f"Macro diagnostic of the adopted rules ({r['rules']}), {r['start']} to {r['end']}: each month labelled by "
             "what was known the day before it began. A description, not a filter."]
    for key, title in (("oil", "Brent crude over the month before"), ("rupee", "USD/INR over the month before"),
                       ("rbi", f"RBI repo rate, latest change in the {RBI_WINDOW} days before")):
        lines.append(f"  {title}:")
        lines.append(f"    {'state':<22}{'months':>7}{'avg month':>10}{'fund':>8}{'vs fund':>9}{'ahead':>7}{'worst':>8}")
        for state, x in r[key].items():
            lines.append(f"    {state:<22}{x['months']:>7}{pct(x['avg_return']):>10}{pct(x['avg_fund']):>8}"
                         f"{pct(x['avg_excess']):>9}{pct(x['ahead_share'], '.0f'):>7}{pct(x['worst_month']):>8}")
    for key, what, hurt in (("oil_exposure", "oil", "a rise in oil"), ("rupee_exposure", "the rupee (USD/INR)",
                                                                       "a weaker rupee")):
        x = r[key]
        if not x:
            lines.append(f"  Exposure to {what}: not enough quotes to measure.")
            continue
        lines.append(f"  Exposure to {what}, over {x['rebalances']} rebalances: holdings' sensitivity minus the eligible "
                     f"names' {x['avg_tilt']:+.3f} on average; {pct(x['avg_share_most_hurt'], '.0f')} of holdings sat in "
                     f"the fifth of names hurt most by {hurt} (20% = no tilt), at most {x['max_most_hurt']} of "
                     f"{x['holdings']:.0f} at one rebalance.")
    lines.append("  Months per label are few (a handful of rate changes, a few oil shocks), so differences of a point "
                 "or two are within noise. RBI dates are hand-entered from the policy statements.")
    return "\n".join(lines)
