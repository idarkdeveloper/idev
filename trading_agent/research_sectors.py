"""Sector concentration diagnostic: how crowded the adopted rules' holdings were in one industry, and how they did
against the index fund in their most crowded months. Oil, metal, monsoon or war shocks hurt a momentum portfolio
mostly through crowding, so this one check covers them all. A description, not a filter: no rule changes and
nothing is logged as a trial.

Industries are NSE's own labels from its published index lists (NIFTY 500, Midcap 150, Smallcap 250), taken as of
today: a company's industry rarely changes, but a stock in none of today's lists is shown as "unknown".
"""
from __future__ import annotations

import statistics
from collections import Counter
from typing import Any

from .research_macro import _months
from .research_sim import Market, Rules, simulate

INDUSTRY_LISTS = ("NIFTY500", "NIFTYMIDCAP150", "NIFTYSMALLCAP250")
CROWD_BANDS = (("at most 3 in one industry", 0, 3), ("4 or 5 in one industry", 4, 5), ("6 or more in one industry", 6, 99))
UNKNOWN = "unknown"


def industry_map(load: Any, lists: tuple[str, ...] = INDUSTRY_LISTS) -> tuple[dict[str, str], list[str]]:
    """{symbol: industry} merged from NSE's lists (the first list naming a stock wins); and the lists that failed."""
    out: dict[str, str] = {}
    failed = []
    for name in lists:
        try:
            for row in load(name):
                if row.get("industry") and row["symbol"] not in out:
                    out[row["symbol"]] = row["industry"]
        except Exception:  # noqa: BLE001 - one list missing still leaves the others
            failed.append(name)
    return out, failed


def crowding(names: list[str], industry: dict[str, str]) -> dict[str, Any]:
    """The largest number of holdings sharing one known industry, which one, and the spread across industries."""
    known = [industry.get(s, UNKNOWN) for s in names]
    counts = Counter(i for i in known if i != UNKNOWN)
    top, n = counts.most_common(1)[0] if counts else (None, 0)
    shares = [c / len(names) for c in Counter(known).values()] if names else []
    return {"top_industry": top, "top_count": n, "industries": len(counts), "unknown": known.count(UNKNOWN),
            "hhi": sum(x * x for x in shares)}


def band_of(n: int) -> str:
    return next(name for name, lo, hi in CROWD_BANDS if lo <= n <= hi)


def sector_diagnostic(m: Market, industry: dict[str, str], *, cost_model: Any, capital: float = 500_000.0,
                      rules: Rules | None = None) -> dict[str, Any]:
    """The adopted rules run once. At every rebalance: the holdings' crowding in one industry, compared with how
    crowded the eligible names themselves were. Each month (rebalance to rebalance) is grouped by the crowding of
    the holdings it started with, then the strategy against the index fund per group; and the worst months named."""
    from .research_studies import FINALIST, _first_ranked_day
    rules = rules or FINALIST
    start, end = _first_ranked_day(m), len(m.dates) - 1
    run = simulate(m, rules, start, end, capital=capital, cost_model=cost_model)
    held = {d: names for d, names in run["held"]}
    rebalances = []
    for d, names in run["held"]:
        if not names:
            continue
        c = crowding(names, industry)
        eligible = m.momentum_ranking(d)
        c["eligible_top_share"] = (crowding(eligible, industry)["top_count"] / len(eligible)) if eligible else None
        c["holdings"] = len(names)
        c["day"] = m.dates[d]
        rebalances.append(c)
    months = _months(m, run, start, end, capital)
    for x in months:
        names = held.get(x["t"] - rules.lag) or held.get(x["t"]) or []
        x["crowding"] = crowding(names, industry) if names else None
        x["band"] = band_of(x["crowding"]["top_count"]) if x["crowding"] else None
    by: dict[str, dict[str, Any]] = {}
    for name, _, _ in CROWD_BANDS:
        ms = [x for x in months if x["band"] == name]
        if ms:
            by[name] = {"months": len(ms), "avg_return": statistics.fmean(x["return"] for x in ms),
                        "avg_fund": statistics.fmean(x["fund"] for x in ms),
                        "avg_excess": statistics.fmean(x["excess"] for x in ms),
                        "ahead_share": sum(x["excess"] > 0 for x in ms) / len(ms),
                        "worst_month": min(x["return"] for x in ms)}
    tops = Counter(r["top_industry"] for r in rebalances if r["top_industry"] and r["top_count"] >= 4)
    worst = sorted((x for x in months if x["crowding"]), key=lambda x: x["excess"])[:5]
    covered = {s for _, names in run["held"] for s in names}
    return {"start": m.dates[start], "end": m.dates[end], "rules": rules.label(), "rebalances": rebalances,
            "by_band": by, "crowded_industries": tops.most_common(6),
            "worst_months": [{"start": x["start"], "excess": x["excess"], "return": x["return"],
                              "top_industry": x["crowding"]["top_industry"], "top_count": x["crowding"]["top_count"]}
                             for x in worst],
            "avg_top_count": statistics.fmean(r["top_count"] for r in rebalances) if rebalances else None,
            "max_top": max(rebalances, key=lambda r: r["top_count"]) if rebalances else None,
            "avg_industries": statistics.fmean(r["industries"] for r in rebalances) if rebalances else None,
            "avg_eligible_top_share": statistics.fmean(r["eligible_top_share"] for r in rebalances
                                                       if r["eligible_top_share"] is not None) if rebalances else None,
            "avg_top_share": statistics.fmean(r["top_count"] / r["holdings"] for r in rebalances) if rebalances else None,
            "unknown_stocks": sorted(s for s in covered if s not in industry)}


def format_sectors(r: dict[str, Any]) -> str:
    def pct(v: Any, f: str = "+.1f") -> str:
        return "n/a" if v is None else f"{v * 100:{f}}%"
    lines = [f"Sector concentration of the adopted rules ({r['rules']}), {r['start']} to {r['end']}: the most holdings "
             "in one NSE industry at each rebalance. A description, not a filter."]
    if r["avg_top_count"] is not None:
        mx = r["max_top"]
        lines.append(f"  On average the most crowded industry held {r['avg_top_count']:.1f} of the holdings "
                     f"({pct(r['avg_top_share'], '.0f')}; the eligible names' own most crowded industry was "
                     f"{pct(r['avg_eligible_top_share'], '.0f')} of them), across {r['avg_industries']:.1f} industries. "
                     f"Most crowded: {mx['top_count']} of {mx['holdings']} in {mx['top_industry']} on {mx['day']}.")
    lines.append(f"  {'months starting with':<28}{'months':>7}{'avg month':>10}{'fund':>8}{'vs fund':>9}{'ahead':>7}"
                 f"{'worst':>8}")
    for band, x in r["by_band"].items():
        lines.append(f"  {band:<28}{x['months']:>7}{pct(x['avg_return']):>10}{pct(x['avg_fund']):>8}"
                     f"{pct(x['avg_excess']):>9}{pct(x['ahead_share'], '.0f'):>7}{pct(x['worst_month']):>8}")
    if r["crowded_industries"]:
        lines.append("  Industries holding 4 or more at a rebalance (times): "
                     + ", ".join(f"{name} ({n})" for name, n in r["crowded_industries"]) + ".")
    lines.append("  The 5 worst months against the fund and their most crowded industry: "
                 + "; ".join(f"{x['start']} {pct(x['excess'])} ({x['top_count']} in {x['top_industry'] or 'none'})"
                             for x in r["worst_months"]) + ".")
    if r["unknown_stocks"]:
        lines.append(f"  {len(r['unknown_stocks'])} stock(s) held had no industry in today's NSE lists (left out of the "
                     f"counts): {', '.join(r['unknown_stocks'][:10])}{' …' if len(r['unknown_stocks']) > 10 else ''}.")
    lines.append("  Industries are NSE's labels as of today. Groups with few months are within noise; a cap per "
                 "industry would be a new trial with its rule written first.")
    return "\n".join(lines)
