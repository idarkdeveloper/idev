"""Claude's view of one replay day, from clocked data only. One forced tool, no orders."""

from __future__ import annotations

import json
from typing import Any

from ..momentum import momentum_stats
from ..risk import atr, trailing_stop
from ..untrusted import UNTRUSTED_RULE, wrap_json

TOOL = {
    "name": "record_view",
    "description": "Record your reading of the market and portfolio on the replay date, with recommendations.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "2-4 sentences on the market and the portfolio that day."},
            "recommendations": {"type": "array", "items": {"type": "object", "properties": {
                "action": {"type": "string", "enum": ["buy", "sell", "hold", "watch"]},
                "ticker": {"type": "string"},
                "headline": {"type": "string"},
                "rationale": {"type": "string", "description": "2-5 sentences citing the data provided."},
                "confidence": {"type": "string", "enum": ["low", "medium", "high"]}},
                "required": ["action", "ticker", "headline", "rationale", "confidence"]}}},
        "required": ["summary", "recommendations"]},
}

SYSTEM = """You are reviewing a practice portfolio in a replay of the Indian stock market. Today is {date}.
Use ONLY the data in the user's message. Do not use anything you know about events, prices or
results after {date}: the point of the replay is to decide as one could have on that day. If the
data is not enough to judge a stock, say so and choose "watch". Recommendations are suggestions;
the person decides. Indian delivery charges are about 0.25% per round trip plus ₹20 per sale."""
SYSTEM += "\n" + UNTRUSTED_RULE


def _stock(trial: Any, news: Any, sym: str) -> dict[str, Any]:
    out: dict[str, Any] = {"symbol": sym}
    try:
        bars = trial.prices.history(sym, "2y")
        s = momentum_stats(bars)
        out.update({k: s.get(k) for k in ("last_close", "ret_1m", "ret_6m", "ret_12_1", "above_200dma",
                                         "pct_from_52w_high", "verdict")})
        out["atr_stop"] = round(trailing_stop(max(b["close"] for b in bars[-60:]), atr(bars[-252:])), 2)
    except Exception as e:  # noqa: BLE001
        out["error"] = str(e)
    n = news.for_symbol(sym, days=60)
    out["announcements"] = wrap_json([{"date": a["at"][:10], "category": a.get("category", ""),
                                       "text": (a.get("text") or "")[:300]} for a in n["items"][:5]], "announcements")
    if n["error"]:
        out["announcements_error"] = n["error"]
    return out


def build_context(trial: Any, news: Any, lookup: str | None = None, deals: str | None = None) -> dict[str, Any]:
    nifty = momentum_stats(trial.prices.history("^NSEI", "2y"))
    ctx: dict[str, Any] = {
        "date": trial.clock.today,
        "market": {"nifty_close": nifty.get("last_close"), "nifty_ret_1m": nifty.get("ret_1m"),
                   "nifty_ret_6m": nifty.get("ret_6m"), "nifty_above_200dma": nifty.get("above_200dma")},
        "you": {"cash": trial.you.account().cash,
                "positions": [{**_stock(trial, news, p.symbol), "qty": p.qty, "avg_cost": p.avg_entry_price}
                              for p in trial.you.positions()]},
        "agent": {"holdings": [p.symbol for p in trial.agent.positions()],
                  "latest_picks": [_stock(trial, news, r["symbol"])
                                   for r in ((trial.data.get("picks") or {}).get("rows") or [])[:10]]},
    }
    if lookup:
        ctx["lookup"] = _stock(trial, news, lookup.upper())
    if deals:   # already wrapped as untrusted text and already limited to what was public on the clock's day
        ctx["disclosed_deals_note"] = ("Bulk, block and insider deals that were already public on this date, newest first "
                                       "(followed investors, plus any in the stock being looked up). A deal is a fact about "
                                       "what someone did, not a prediction.")
        ctx["disclosed_deals"] = deals
    return ctx


def request_view(client: Any, model: str, date_label: str, ctx: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    """The one record_view call (forced tool, no order tool): (response, the recorded view)."""
    resp = client.messages.create(model=model, max_tokens=4000, system=SYSTEM.format(date=date_label),
                                  tools=[TOOL], tool_choice={"type": "tool", "name": TOOL["name"]},
                                  messages=[{"role": "user", "content": json.dumps(ctx, default=str)}])
    block = next((b for b in resp.content if getattr(b, "type", None) == "tool_use"), None)
    if block is None or not isinstance(block.input, dict):
        raise ValueError(f"Claude returned no recorded view (stop reason: {getattr(resp, 'stop_reason', None)})")
    return resp, block.input


def ask(trial: Any, client: Any, model: str, news: Any, lookup: str | None = None,
        deals: str | None = None) -> dict[str, Any]:
    ctx = build_context(trial, news, lookup, deals)
    resp, view = request_view(client, model, trial.clock.today, ctx)
    entry = {"date": trial.clock.today, "summary": view.get("summary", ""),
             "recommendations": view.get("recommendations", []),
             "model": getattr(resp, "model", model), "hindsight": True}
    trial.data["claude"].append(entry)
    trial.data["claude_presses"] = trial.data.get("claude_presses", 0) + 1
    trial.save()
    return entry
