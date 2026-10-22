"""Pre-registered batch 4 (docs/research/preregistered-batch-2026-10-22.md), ONCE: two variants of the adopted rules.

* H, quarterly rebalance: the same rules on the first session of each calendar quarter instead of each month; the
  tax-aware wait's age band widens from 335-365 to 273-365 days (quarterly checks are up to 92 days apart).
* V, volatility scaling (Barroso and Santa-Clara 2015): at each monthly rebalance the invested share is
  min(1, the basket's volatility over 756 sessions / its volatility over the last 126), applied when it moves by 0.10
  or more; never leveraged, no floor.

Both run once on the development years beside the control (the adopted rules), all three on the repaired data of
``survivorship-2026-10-22.md``: members with no Yahoo history filled from NSE's daily files, and a 30% haircut when a
held stock stops trading. The control is also simulated on Yahoo's names alone without the haircut, as a survivorship
diagnostic (printed, not a trial). Pass rules, after charges and tax:

* H: beats the fund; CAGR within 1 point of the control's; worst fall within 5 points; turnover at most two-thirds.
* V: beats the fund; worst fall at least 5 points shallower than the control's; CAGR within 1 point; turnover at most 1.5x.

A pass only earns a 12-month paper account. Everything below is fixed by the pre-registration.
"""

from __future__ import annotations

import math
from typing import Any

from .research_sim import Market, Rules, simulate, summarize, with_rules
from .survivorship import DELIST_HAIRCUT

PREREGISTRATION = "docs/research/preregistered-batch-2026-10-22.md"
STUDY = "Batch4"
TAX_AWARE_FROM_QUARTERLY = 273      # 365 - 92: quarterly checks are up to 92 days apart
CAGR_TOLERANCE = 0.01               # a variant may trail the control by at most 1 point a year
FALL_MARGIN = 0.05                  # H: a worst fall at most 5 points deeper than the control's
FALL_GAIN = 0.05                    # V: a worst fall at least 5 points shallower than the control's
H_MAX_TURNOVER_RATIO = 2 / 3
V_MAX_TURNOVER_RATIO = 1.5
NAMES = {"control": "the adopted rules", "H": "quarterly rebalance", "V": "volatility scaling"}


def rule_sets() -> dict[str, Rules]:
    """The control and the two variants, all with the delisting haircut."""
    from .research_studies import FINALIST
    control = with_rules(FINALIST, delist_haircut=DELIST_HAIRCUT)
    return {"control": control,
            "H": with_rules(control, rebalance="quarterly", tax_aware_from=TAX_AWARE_FROM_QUARTERLY),
            "V": with_rules(control, vol_scale=True)}


def checks_for(variant: str, x: dict[str, Any], c: dict[str, Any]) -> dict[str, bool]:
    """The pre-registered pass rule of one variant (``x``) against the control (``c``), both ``summarize`` outputs."""
    def have(*keys: str) -> bool:
        return all(x.get(k) is not None and c.get(k) is not None for k in keys)
    beats = x.get("excess") is not None and x["excess"] > 0
    cagr_ok = have("cagr") and x["cagr"] >= c["cagr"] - CAGR_TOLERANCE
    if variant == "H":
        return {"beats the fund": beats,
                "CAGR within 1 point of the control's": cagr_ok,
                "worst fall within 5 points of the control's": have("max_drawdown")
                and x["max_drawdown"] >= c["max_drawdown"] - FALL_MARGIN,
                "turnover at most two-thirds of the control's": have("turnover")
                and x["turnover"] <= H_MAX_TURNOVER_RATIO * c["turnover"]}
    return {"beats the fund": beats,
            "worst fall at least 5 points shallower than the control's": have("max_drawdown")
            and x["max_drawdown"] >= c["max_drawdown"] + FALL_GAIN,
            "CAGR within 1 point of the control's": cagr_ok,
            "turnover at most 1.5x the control's": have("turnover")
            and x["turnover"] <= V_MAX_TURNOVER_RATIO * c["turnover"]}


def batch4(m: Market, m_plain: Market | None, *, cost_model: Any, capital: float = 500_000.0,
           extra_trials: int = 0) -> dict[str, Any]:
    """The one run: control, H and V on the repaired market ``m``; the adopted rules on ``m_plain`` (Yahoo's names
    alone, no haircut) as the survivorship diagnostic."""
    from .research_studies import FINALIST, _first_ranked_day, _pbo, _skill
    start, end = _first_ranked_day(m), len(m.dates) - 1
    rules = rule_sets()
    runs = {k: simulate(m, r, start, end, capital=capital, cost_model=cost_model) for k, r in rules.items()}
    results = {k: summarize(r, m, start, end) for k, r in runs.items()}
    skill = _skill(runs, m, start, end, extra_trials)
    c = results["control"]
    for k, x in results.items():
        x["name"], x["skill"], x["rules"] = NAMES[k], skill[k], rules[k].label()
        x["delisted"] = runs[k]["delisted"]
        if k != "control":
            x["checks"] = checks_for(k, x, c)
            x["passes"] = all(x["checks"].values())
    log = runs["V"]["scale_log"]
    applied = [e["applied"] for e in log]
    results["V"]["scaling"] = {"rebalances": len(log), "changes": runs["V"]["events"]["scale_trades"],
                               "mean_share": (math.fsum(applied) / len(applied)) if applied else None,
                               "min_share": min(applied) if applied else None,
                               "months_below_1": sum(1 for a in applied if a < 1.0)}
    diagnostic = None
    if m_plain is not None:
        s0 = _first_ranked_day(m_plain)
        plain = simulate(m_plain, FINALIST, s0, len(m_plain.dates) - 1, capital=capital, cost_model=cost_model)
        diagnostic = {"plain": summarize(plain, m_plain, s0, len(m_plain.dates) - 1), "repaired": c,
                      "start_plain": m_plain.dates[s0], "start": m.dates[start],
                      "filled": list(getattr(m, "filled", [])), "missing": list(getattr(m, "missing", []))}
    return {"start": m.dates[start], "end": m.dates[end], "results": results,
            "passing": [k for k, x in results.items() if k != "control" and x.get("passes")],
            "pbo": _pbo(runs, m, start, end), "diagnostic": diagnostic}


def format_batch4(r: dict[str, Any], fill_note: str = "") -> str:
    from .research_studies import pbo_line

    def pct(v: Any, f: str = "+.1f") -> str:
        return "n/a" if v is None or (isinstance(v, float) and math.isnan(v)) else f"{v * 100:{f}}%"
    lines = [f"BATCH 4, run once ({PREREGISTRATION}), {r['start']} to {r['end']}, after charges and tax, against the "
             "adopted rules on the same run and the same repaired data. Development years: weak evidence; a pass only "
             "earns a paper account.",
             f"  {'variant':<28}{'return':>9}{'vs fund':>9}{'a year':>8}{'worst fall':>11}{'turnover':>9}"
             f"{'skill':>7}  verdict"]
    for k, x in r["results"].items():
        p = x["skill"].get("probability")
        verdict = "control" if k == "control" else ("PASS" if x["passes"] else "fail")
        lines.append(f"  {k + ' ' + x['name']:<28}{pct(x['total_return']):>9}{pct(x['excess']):>9}{pct(x['cagr']):>8}"
                     f"{pct(x['max_drawdown']):>11}{pct(x['turnover'], '.0f'):>9}"
                     f"{'n/a' if p is None else f'{p:.2f}':>7}  {verdict}")
    for k, x in r["results"].items():
        failed = [n for n, ok in (x.get("checks") or {}).items() if not ok]
        if failed:
            lines.append(f"  {k} misses: {'; '.join(failed)}.")
    sc = r["results"]["V"].get("scaling") or {}
    if sc.get("rebalances"):
        lines.append(f"  V's share: applied below 1 in {sc['months_below_1']} of {sc['rebalances']} rebalances, changed "
                     f"{sc['changes']} time(s); mean {sc['mean_share']:.2f}, lowest {sc['min_share']:.2f}.")
    lines.append(pbo_line(r["pbo"]))
    d = r.get("diagnostic")
    if d:
        p, q = d["plain"], d["repaired"]
        lines.append(f"  Survivorship diagnostic (a description, not a trial): the adopted rules on Yahoo's names alone, "
                     f"no haircut: {pct(p['total_return'])} ({pct(p['excess'])} vs the fund, worst fall "
                     f"{pct(p['max_drawdown'])}); on the repaired data: {pct(q['total_return'])} ({pct(q['excess'])}, "
                     f"{pct(q['max_drawdown'])}). Filled: {', '.join(d['filled']) or 'none'}; still missing: "
                     f"{', '.join(d['missing']) or 'none'}.")
    for k, x in r["results"].items():
        if x.get("delisted"):
            cut = ", ".join(f"{e['symbol']} {e['date']}" for e in x["delisted"][:12])
            lines.append(f"  {k}: {len(x['delisted'])} holding(s) cut 30% for stopping trading: {cut}"
                         + (" …" if len(x["delisted"]) > 12 else "") + ".")
    if fill_note:
        lines.append(f"  {fill_note}")
    lines.append(f"  Verdict: passing {', '.join(r['passing']) or 'none'}"
                 + ("; each earns a 12-month paper account beside the adopted rules, built only on the owner's "
                    "approval" if r["passing"] else "; the adopted rules stay as they are")
                 + ". Skill odds are information only. This batch is now spent.")
    return "\n".join(lines)
