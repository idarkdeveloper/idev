"""End of a replay: the scorecard, and what each portfolio would have done since."""

from __future__ import annotations

import bisect
from datetime import date
from typing import Any

from .trial import PORTFOLIOS


def _max_drawdown(values: list[float]) -> float:
    peak, worst = values[0] if values else 0.0, 0.0
    for v in values:
        peak = max(peak, v)
        worst = min(worst, v / peak - 1 if peak else 0.0)
    return worst


def _per_symbol(broker: Any) -> dict[str, float]:
    """Profit or loss per stock: sales and current value minus purchases, charges included."""
    pnl: dict[str, float] = {}
    for o in broker.orders():
        flow = o["notional"] - o["fees"] if o["side"] == "sell" else -(o["notional"] + o["fees"])
        pnl[o["symbol"]] = pnl.get(o["symbol"], 0.0) + flow
    for p in broker.positions():
        pnl[p.symbol] = pnl.get(p.symbol, 0.0) + (p.market_value or 0.0)
    return pnl


def dividends(b: Any) -> float:
    """Cash credits paid out (dividends); copies of real holdings are credit entries but add no cash."""
    return round(sum(c["amount"] for c in b.credits() if c.get("kind") != "copy"), 2)


def _card(trial: Any, who: str) -> dict[str, Any]:
    b, cash0 = trial.broker(who), trial.data["cash"]
    values = [p[who] for p in trial.data["equity"]]
    final = values[-1]
    years = max((date.fromisoformat(trial.clock.today) - date.fromisoformat(trial.data["start"])).days / 365.25, 1 / 365.25)
    pnl = _per_symbol(b)
    ranked = sorted(pnl.items(), key=lambda kv: kv[1])
    return {"final": final, "return": final / cash0 - 1, "cagr": (final / cash0) ** (1 / years) - 1,
            "max_drawdown": _max_drawdown(values), "charges": b.performance()["fees_paid"],
            "trades": len(b.orders()), "dividends": dividends(b),
            "best": {"symbol": ranked[-1][0], "pnl": round(ranked[-1][1], 2)} if ranked else None,
            "worst": {"symbol": ranked[0][0], "pnl": round(ranked[0][1], 2)} if ranked else None,
            "hit_rate": sum(1 for v in pnl.values() if v > 0) / len(pnl) if pnl else None}


def _grade_claude(trial: Any) -> list[dict[str, Any]]:
    """Each Claude call against what the stock did by the end date (hindsight possible)."""
    out = []
    for entry in trial.data["claude"]:
        for r in entry.get("recommendations", []):
            g = {"date": entry["date"], "action": r["action"], "ticker": r["ticker"], "return": None}
            try:
                start = trial.prices.price_on(r["ticker"], entry["date"])
                g["return"] = trial.prices.latest_price(r["ticker"]) / start - 1
            except LookupError:
                pass
            out.append(g)
    return out


def scorecard(trial: Any) -> dict[str, Any]:
    return {**{w: _card(trial, w) for w in PORTFOLIOS}, "claude": _grade_claude(trial)}


def end_trial(trial: Any) -> dict[str, Any]:
    if trial.data["ended"]:
        return trial.data["scorecard"]
    s = scorecard(trial)
    trial.data["ended"], trial.data["scorecard"] = trial.clock.today, s
    trial.save()
    return s


def what_happened_next(trial: Any, source: Any, today: str) -> dict[str, list[Any]]:
    """Hold every portfolio unchanged from the end date to today. This is the only place
    Replay reads prices after its clock, and only once the replay has ended."""
    if not trial.data["ended"]:
        raise ValueError("End the replay first: this shows prices after the replay date")
    end, fld = trial.data["ended"], trial.prices.field
    cal = [b["date"] for b in source.history("^NSEI", "10y") if end <= b["date"] <= today]
    series: dict[str, Any] = {}

    def closes(sym: str) -> tuple[list[str], list[float]]:
        if sym not in series:
            bars = source.history(sym, "10y")
            series[sym] = ([b["date"] for b in bars], [b[fld] for b in bars])
        return series[sym]

    out: dict[str, list[Any]] = {"dates": cal}
    for who in PORTFOLIOS:
        b = trial.broker(who)
        cash, held = b.account().cash, [(p.symbol, p.qty, p.current_price) for p in b.positions()]
        vals = []
        for d in cal:
            v = cash
            for sym, qty, last in held:
                try:
                    ds, cs = closes(sym)
                    i = bisect.bisect_right(ds, d)
                    v += qty * (cs[i - 1] if i else (last or 0.0))
                except LookupError:
                    v += qty * (last or 0.0)
            vals.append(round(v, 2))
        out[who] = vals
    return out
