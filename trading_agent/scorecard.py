"""Keep score: what each of Claude's recommendations did afterwards.

For every stored recommendation: entry at the first close after the day it was made,
then the stock's return over 5 / 20 / 60 trading days and its excess over the
benchmark. A buy counts as right when the excess beats the round-trip cost; a sell
when the stock then lagged the index. Watch and hold calls are reported, not scored.
"""

from __future__ import annotations

import statistics
from typing import Any, Iterable

from .backtest import _forward

HORIZONS = (5, 20, 60)


def score_recommendations(recs: Iterable[dict[str, Any]], prices: Any, *, horizons: Iterable[int] = HORIZONS,
                          benchmark: str = "^NSEI", cost_model: Any | None = None,
                          default_notional: float = 25_000.0) -> dict[str, Any]:
    horizons = tuple(horizons)
    bench = prices.history(benchmark, "2y")
    bench_dates = [b["date"] for b in bench]
    cache: dict[str, Any] = {}
    rows: list[dict[str, Any]] = []
    for i, r in enumerate(recs):
        ticker = str(r.get("ticker") or "").upper()
        action = str(r.get("action") or "").lower()
        if not ticker or ticker == "PORTFOLIO" or action not in {"buy", "sell", "watch", "hold"}:
            continue
        day = str(r.get("at") or "")[:10]
        row: dict[str, Any] = {"index": i, "at": r.get("at"), "ticker": ticker, "action": action,
                               "confidence": r.get("confidence"), "headline": r.get("headline"),
                               "dismissed": bool(r.get("dismissed")), "returns": {}, "excess": {},
                               "correct": {}, "error": None}
        try:
            if ticker not in cache:
                cache[ticker] = prices.history(ticker, "2y")
            bars = cache[ticker]
            entry_date, entry_price, rets = _forward(bars, [b["date"] for b in bars], day, horizons)
            _, _, b_rets = _forward(bench, bench_dates, day, horizons)
            row["entry_date"], row["entry_price"] = entry_date, entry_price
            notional = float(r.get("suggested_notional_usd") or 0) or default_notional
            cost = (cost_model.round_trip_bps(notional) / 10_000) if cost_model is not None else 0.0
            row["cost"] = cost
            for h in horizons:
                ret, b = rets.get(h), b_rets.get(h)
                row["returns"][str(h)] = ret
                ex = (ret - b) if ret is not None and b is not None else None
                row["excess"][str(h)] = ex
                if ex is None or action in {"watch", "hold"}:
                    row["correct"][str(h)] = None
                elif action == "buy":
                    row["correct"][str(h)] = ex - cost > 0
                else:
                    row["correct"][str(h)] = ex < 0
            if entry_date is None:
                row["error"] = "no trading day after the recommendation yet"
        except Exception as e:  # noqa: BLE001
            row["error"] = f"{type(e).__name__}: {e}"
        rows.append(row)

    summary: dict[str, Any] = {"recommendations": len(rows), "by_action": {}, "horizons": list(horizons),
                               "benchmark": benchmark}
    for action in ("buy", "sell", "watch", "hold"):
        per_h = {}
        for h in horizons:
            ex = [r["excess"][str(h)] for r in rows if r["action"] == action and r["excess"].get(str(h)) is not None]
            if not ex:
                continue
            item: dict[str, Any] = {"n": len(ex), "mean_excess": statistics.fmean(ex),
                                    "median_excess": statistics.median(ex)}
            calls = [r["correct"][str(h)] for r in rows if r["action"] == action
                     and r["correct"].get(str(h)) is not None]
            if calls:
                item["hit_rate"] = sum(calls) / len(calls)
            per_h[str(h)] = item
        if per_h:
            summary["by_action"][action] = per_h
    pending = sum(1 for r in rows if all(v is None for v in r["excess"].values()))
    summary["pending"] = pending
    return {"summary": summary, "rows": rows}


def format_scorecard(result: dict[str, Any]) -> str:
    s = result["summary"]
    lines = [f"Scorecard: {s['recommendations']} recommendations vs {s['benchmark']}, "
             f"{s['pending']} too recent to score"]
    if not s["by_action"]:
        lines.append("  Nothing to score yet. Recommendations need at least 5 trading days of history.")
    for action, per_h in s["by_action"].items():
        lines.append(f"  {action}:")
        for h, r in per_h.items():
            hit = f"  right {r['hit_rate']*100:4.0f}%" if "hit_rate" in r else ""
            lines.append(f"    {h:>3}d  n={r['n']:<4} mean excess {r['mean_excess']*100:+6.2f}%  "
                         f"median {r['median_excess']*100:+6.2f}%{hit}")
    return "\n".join(lines)
