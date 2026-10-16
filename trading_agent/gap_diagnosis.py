"""Research A: why the momentum screen trailed its index fund.

The factor backtest's portfolio grows each month by

    (1 - c) * (w * r_picks + (1 - w) * r_cash)

where ``c`` is that rebalance's charges as a share of the portfolio, ``w`` the share invested after trading
(less than 1 when the 200-day trend filter leaves slots in cash) and ``r_picks`` the stocks' gross return. Three
counterfactual paths built from the same months split the gap to the index fund exactly (they telescope):

* **timing** (cash and risk-off): hold the index fund with the same invested share ``w`` each month, minus the fund:
  what moving in and out of cash did by itself;
* **selection**: the picks, gross of charges, minus that same-``w`` fund path: what choosing these stocks did;
* **charges**: the real result minus the gross one.

timing + selection + charges = strategy - index fund, in percentage points of the starting capital. Each is computed
with cash earning nothing (what the backtest assumes) and with cash in a liquid fund (``LIQUID_RATE`` a year).

Also reported: annual turnover, how long a stock stays held (completed holdings and the ones still open at the end),
and the timing and selection contributions split by months the index fund rose or fell.

Rules agreed before the run (written here so the verdict cannot be tuned afterwards):

* nothing on or after ``HOLDOUT_START`` is used (the sealed holdout shared with research step B1);
* if selection is zero or negative, post-results drift is researched before the drawdown brake and the weights;
* every run is appended to the trial log (``research/trials.jsonl``), so later deflated-Sharpe checks count it.
"""

from __future__ import annotations

import json
import statistics
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

HOLDOUT_START = "2025-10-01"
LIQUID_RATE = 0.065   # a year: roughly what Indian liquid funds paid 2021-2025, before tax


def _days(a: str, b: str) -> int:
    return (date.fromisoformat(b[:10]) - date.fromisoformat(a[:10])).days


def _paths(months: list[dict[str, Any]], fund_rets: list[float], cash_rate: float) -> dict[str, list[float]]:
    """Growth of 1 along the four paths, one value per month end (starting at 1.0)."""
    out = {k: [1.0] for k in ("fund", "timing", "gross", "net")}
    for m, rf in zip(months, fund_rets):
        rc = (1 + cash_rate) ** (_days(m["start"], m["end"]) / 365.0) - 1
        w = m["invested"]
        rp = m["picks_ret"] if m["picks_ret"] is not None else 0.0
        out["fund"].append(out["fund"][-1] * (1 + rf))
        out["timing"].append(out["timing"][-1] * (1 + w * rf + (1 - w) * rc))
        gross = 1 + w * rp + (1 - w) * rc
        out["gross"].append(out["gross"][-1] * gross)
        out["net"].append(out["net"][-1] * (1 - m["cost_frac"]) * gross)
    return out


def _split(paths: dict[str, list[float]]) -> dict[str, float]:
    f, t, g, n = (paths[k][-1] for k in ("fund", "timing", "gross", "net"))
    return {"strategy": n - 1, "index_fund": f - 1, "gap": n - f,
            "timing": t - f, "selection": g - t, "charges": n - g}


def _by_direction(months: list[dict[str, Any]], fund_rets: list[float]) -> dict[str, dict[str, Any]]:
    """Simple (not compounded) sums of the monthly timing and selection effects, split by the fund's direction."""
    out: dict[str, dict[str, Any]] = {}
    for label, keep in (("fund_up", lambda r: r >= 0), ("fund_down", lambda r: r < 0)):
        rows = [(m, rf) for m, rf in zip(months, fund_rets) if keep(rf)]
        out[label] = {"months": len(rows),
                      "avg_invested": statistics.fmean(m["invested"] for m, _ in rows) if rows else None,
                      "timing": sum((m["invested"] - 1) * rf for m, rf in rows),
                      "selection": sum(m["invested"] * ((m["picks_ret"] or 0.0) - rf) for m, rf in rows)}
    return out


def diagnose(result: dict[str, Any], *, liquid_rate: float = LIQUID_RATE) -> dict[str, Any]:
    """The gap split for one factor-backtest result (run with ``until``). Raises ValueError without an index fund."""
    fund = result.get("index_fund")
    diag = result.get("diagnostics") or {}
    months = diag.get("months") or []
    if not fund or len(fund) != len(months) + 1:
        raise ValueError("no index-fund series for the whole test window: run fewer years")
    fund_rets = [fund[i + 1] / fund[i] - 1 for i in range(len(months))]
    years = _days(months[0]["start"], months[-1]["end"]) / 365.25 if months else 0.0
    zero = _paths(months, fund_rets, 0.0)
    liquid = _paths(months, fund_rets, liquid_rate)
    split0, split_l = _split(zero), _split(liquid)
    avg_value = statistics.fmean(result["strategy"]) if result.get("strategy") else 0.0
    done, still = diag.get("holding_months") or [], diag.get("open_holding_months") or []
    turnover = diag.get("traded_value", 0.0) / avg_value / years if avg_value and years else None
    check = abs(split0["strategy"] - (result["strategy"][-1] / result["strategy"][0] - 1))
    sel = split0["selection"]
    if sel > 0.005:
        verdict = ("Stock selection added value; the shortfall comes from cash timing and charges. Next: the stop "
                   "rules (B1), then the drawdown brake and weights (B2-B4).")
    else:
        verdict = ("Stock selection added nothing or lost money, so the screen's picks are the problem. As agreed "
                   "before the run: post-results drift is researched before the drawdown brake and the weights.")
    return {
        "window": {"start": months[0]["start"] if months else None, "end": months[-1]["end"] if months else None,
                   "months": len(months), "years": round(years, 2), "holdout_from": result.get("until")},
        "index_fund": result.get("index_fund_symbol"),
        "cash_at_zero": split0, "cash_in_liquid_fund": {**split_l, "rate": liquid_rate},
        "by_direction": _by_direction(months, fund_rets),
        "avg_invested": statistics.fmean(m["invested"] for m in months) if months else None,
        "months_fully_invested": sum(1 for m in months if m["invested"] >= 0.999),
        "charges_paid": result.get("costs_paid"), "annual_turnover": turnover,
        "holding_months": {"completed": len(done), "median": statistics.median(done) if done else None,
                           "mean": statistics.fmean(done) if done else None,
                           "still_open": len(still), "median_open": statistics.median(still) if still else None},
        "reconciles_to_backtest": check < 1e-6,
        "verdict": verdict,
    }


def pct_points(x: float) -> str:
    return f"{x * 100:+.1f} pts"


def format_diagnosis(d: dict[str, Any]) -> str:
    w, z, lq = d["window"], d["cash_at_zero"], d["cash_in_liquid_fund"]
    lines = [f"Research A: why the screen trailed {d['index_fund']}, {w['start']} to {w['end']} "
             f"({w['months']} months; nothing from {w['holdout_from']} on is used)",
             f"  Strategy {z['strategy'] * 100:+.1f}%   {d['index_fund']} {z['index_fund'] * 100:+.1f}%   "
             f"gap {pct_points(z['gap'])}",
             "  Where the gap comes from (percentage points of the starting capital; the three add up to the gap):",
             f"    {'':28}{'cash earns 0%':>16}{'cash in a liquid fund':>24}",
             f"    {'cash timing (trend filter)':28}{pct_points(z['timing']):>16}{pct_points(lq['timing']):>24}",
             f"    {'stock selection':28}{pct_points(z['selection']):>16}{pct_points(lq['selection']):>24}",
             f"    {'charges':28}{pct_points(z['charges']):>16}{pct_points(lq['charges']):>24}",
             f"    {'total gap':28}{pct_points(z['gap']):>16}{pct_points(lq['gap']):>24}",
             f"  Liquid fund at {lq['rate'] * 100:.1f}% a year on the cash: strategy {lq['strategy'] * 100:+.1f}%."]
    for key, label in (("fund_up", "months the fund rose"), ("fund_down", "months the fund fell")):
        b = d["by_direction"][key]
        if b["months"]:
            lines.append(f"  In the {b['months']} {label}: {b['avg_invested'] * 100:.0f}% invested on average, "
                         f"timing {pct_points(b['timing'])}, selection {pct_points(b['selection'])} (simple sums)")
    h = d["holding_months"]
    lines.append(f"  Invested {d['avg_invested'] * 100:.0f}% on average; fully invested in {d['months_fully_invested']} "
                 f"of {w['months']} months. Charges paid {d['charges_paid']:,.0f}.")
    if d["annual_turnover"] is not None:
        lines.append(f"  Turnover {d['annual_turnover'] * 100:.0f}% of the portfolio a year. Holdings: "
                     f"{h['completed']} completed, median {h['median'] if h['median'] is not None else 'n/a'} months; "
                     f"{h['still_open']} still held at the end.")
    if not d["reconciles_to_backtest"]:
        lines.append("  WARNING: the split does not reconcile with the backtest's own result; do not rely on it.")
    lines.append(f"  Verdict: {d['verdict']}")
    return "\n".join(lines)


def log_trial(state_dir: Path, study: str, params: dict[str, Any], summary: dict[str, Any]) -> Path:
    """Append one line to the research trial log, so every variant tried counts in later deflated-Sharpe checks."""
    path = Path(state_dir) / "research" / "trials.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "study": study, "params": params,
           "summary": summary}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, default=str) + "\n")
    return path
