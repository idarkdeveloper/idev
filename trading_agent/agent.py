"""The Claude agent: strategy prompt, tools, and the per-check run loop."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import anthropic
from anthropic import beta_tool

from .broker import Broker
from .config import Settings
from .notify import Notifier
from .quiver import DisclosedTrade
from .state import State

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a trading research agent that follows one investor's publicly disclosed trades.

Your job each run:
1. Read the NEW disclosed trades you are given (trades you have not analysed before).
2. Look at the user's paper-trading portfolio with get_portfolio and current prices with get_latest_price.
3. For each new trade, decide whether the user should act. Consider: how recent the trade is
   (disclosures lag the actual trade, sometimes by weeks), the size bucket, whether the user
   already holds the ticker, position concentration, and available cash.
4. Call send_recommendation exactly once per ticker that deserves a recommendation
   (buy / sell / hold / watch). If nothing deserves action, call send_recommendation once
   with action "hold" summarising why.
5. If and only if place_paper_order is available AND your confidence is "high", you may
   execute the recommendation with paper money. Never exceed 10% of equity on a single
   order, never buy a ticker already above 20% of equity, and never sell more than is held.

Rules:
- Be decisive but explain the risk in plain language.
- Do not invent prices or trades; use the tools. If a price lookup fails, say so and skip
  order placement for that ticker.
- Keep the final message to a short plain-text summary of what you recommended and why.
"""

MARKET_NOTES = {
    "in": """\
Market: India (NSE). Prices and amounts are in rupees (INR). Orders are in whole shares.
The disclosed trades come from NSE bulk deals, block deals and SEBI insider (PIT) filings.
Bulk/block deals are published the same evening, so they are fresh. The client name in a
bulk deal can be an investor, a fund, or a broker/prop desk acting for a client; weigh the
name accordingly. A bulk deal has a counterparty: a SELL by the watched investor is as
informative as a BUY. Trading hours are 09:15-15:30 IST, Monday-Friday.
""",
    "us": """\
Market: United States. Prices in USD; fractional shares allowed. Disclosed trades come from
congressional (STOCK Act) and SEC insider filings, which lag the real trade by days to weeks.
""",
}


@dataclass
class RunResult:
    investor: str
    new_trades: list[DisclosedTrade]
    recommendations: list[dict[str, Any]] = field(default_factory=list)
    orders: list[dict[str, Any]] = field(default_factory=list)
    final_text: str = ""
    model: str | None = None
    skipped: bool = False
    refusal: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "investor": self.investor,
            "new_trades": [t.to_dict() for t in self.new_trades],
            "recommendations": self.recommendations,
            "orders": self.orders,
            "final_text": self.final_text,
            "model": self.model,
            "skipped": self.skipped,
            "refusal": self.refusal,
        }


@dataclass
class AgentContext:
    settings: Settings
    broker: Broker
    data: Any | None  # NSEClient | QuiverClient | None
    notifier: Notifier
    state: State
    result: RunResult

    @property
    def live_money(self) -> bool:
        return not getattr(self.broker, "name", "").startswith("local-paper") and \
            getattr(self.broker, "live_orders", False)


def build_tools(ctx: AgentContext) -> list[Any]:
    """Create the tool functions bound to this run's context."""

    @beta_tool
    def get_portfolio() -> str:
        """Get the user's paper-trading account (cash, equity) and all open positions
        with current prices and unrealised P&L."""
        acct = ctx.broker.account()
        positions = [p.to_dict() for p in ctx.broker.positions()]
        return json.dumps({"broker": ctx.broker.name, "account": acct.to_dict(),
                           "positions": positions}, default=str)

    @beta_tool
    def get_latest_price(symbol: str) -> str:
        """Get the latest traded price for a stock ticker (e.g. NVDA) or crypto pair (e.g. BTC/USD).

        Args:
            symbol: Ticker symbol to look up.
        """
        try:
            return json.dumps({"symbol": symbol.upper(), "price": ctx.broker.latest_price(symbol)})
        except Exception as e:  # noqa: BLE001 - surface the failure to the model
            return json.dumps({"symbol": symbol.upper(), "error": str(e)})

    @beta_tool
    def get_investor_trade_history(ticker: str) -> str:
        """Get the watched investor's previously disclosed trades in one ticker, newest first,
        to judge whether a new trade continues a pattern or reverses one.

        Args:
            ticker: Stock ticker symbol.
        """
        if ctx.data is None:
            return json.dumps({"error": "market data client not configured"})
        try:
            rows = ctx.data.history_for_ticker(ctx.settings.watch_investor, ticker)
        except Exception as e:  # noqa: BLE001
            return json.dumps({"error": str(e)})
        mine = [t.to_dict() for t in rows][:25]
        for t in mine:
            t.pop("raw", None)
        return json.dumps({"ticker": ticker.upper(), "trades": mine}, default=str)

    @beta_tool
    def send_recommendation(action: str, ticker: str, headline: str, rationale: str,
                            confidence: str, suggested_notional_usd: float = 0.0) -> str:
        """Send the user a recommendation. Call once per ticker (or once with action "hold").

        Args:
            action: One of "buy", "sell", "hold", "watch".
            ticker: Ticker the recommendation concerns, or "PORTFOLIO" for a general note.
            headline: One-line summary, e.g. "Pelosi bought NVDA; you hold none".
            rationale: 2-5 sentences: what changed, how it compares to the portfolio, the risk.
            confidence: One of "low", "medium", "high".
            suggested_notional_usd: Amount in the account currency (INR for India, USD for
                the US) to buy/sell if the user acts (0 if n/a).
        """
        action = action.lower().strip()
        if action not in {"buy", "sell", "hold", "watch"}:
            return json.dumps({"error": "action must be buy, sell, hold or watch"})
        rec = {"action": action, "ticker": ticker.upper(), "headline": headline,
               "rationale": rationale, "confidence": confidence.lower(),
               "suggested_notional_usd": float(suggested_notional_usd),
               "currency": ctx.settings.currency,
               "investor": ctx.settings.watch_investor}
        ctx.result.recommendations.append(rec)
        ctx.state.record_recommendation(rec)
        subject = f"[{action.upper()} {rec['ticker']}] {headline}"
        body = (f"Investor watched: {ctx.settings.watch_investor}\n"
                f"Action: {action.upper()} {rec['ticker']} (confidence: {rec['confidence']})\n"
                + (f"Suggested size: {_money(rec['suggested_notional_usd'], ctx.settings.currency)}\n"
                   if rec['suggested_notional_usd'] else "")
                + f"\n{rationale}\n")
        delivered = ctx.notifier.send(subject, body)
        return json.dumps({"ok": True, "delivered_via": delivered})

    tools: list[Any] = [get_portfolio, get_latest_price, get_investor_trade_history,
                        send_recommendation]

    if ctx.settings.auto_trade:
        @beta_tool
        def place_paper_order(symbol: str, side: str, notional_usd: float) -> str:
            """Place a market order. Only use after send_recommendation, only with high
            confidence, and never more than 10% of equity per order. The order goes to the
            configured brokerage (paper simulator unless live orders were explicitly enabled).

            Args:
                symbol: Ticker symbol.
                side: "buy" or "sell".
                notional_usd: Amount to trade in the account currency (INR or USD).
            """
            try:
                equity = ctx.broker.account().equity
                if notional_usd > 0.10 * equity + 1e-6:
                    return json.dumps({"error": f"order exceeds 10% of equity ({equity:.2f})"})
                order = ctx.broker.submit_order(symbol, side.lower(), notional=float(notional_usd))
            except Exception as e:  # noqa: BLE001
                return json.dumps({"error": str(e)})
            ctx.result.orders.append(order)
            return json.dumps({"ok": True, "order": order}, default=str)

        tools.append(place_paper_order)

    return tools


def _money(amount: float, currency: str) -> str:
    sym = "₹" if currency == "INR" else "$"
    return f"{sym}{amount:,.0f}"


def build_user_message(ctx: AgentContext) -> str:
    trades = [t.to_dict() for t in ctx.result.new_trades]
    for t in trades:
        t.pop("raw", None)
    if not ctx.settings.auto_trade:
        mode = "recommendation-only (no order tool)"
    elif ctx.live_money:
        mode = "LIVE - orders use real money, be conservative"
    else:
        mode = "paper - you may place simulated orders"
    return (
        MARKET_NOTES.get(ctx.settings.market, "") + "\n"
        f"Watched investor: {ctx.settings.watch_investor} (source: {ctx.settings.watch_source}).\n"
        f"Order mode: {mode}.\n\n"
        f"NEW disclosed trades since the last check ({len(trades)}):\n"
        f"{json.dumps(trades, indent=2)}\n\n"
        "Analyse them against the portfolio and send recommendations."
    )


def run_agent(ctx: AgentContext, client: anthropic.Anthropic | None = None,
              runner_factory: Any | None = None) -> RunResult:
    """Drive one Claude run over ``ctx.result.new_trades``.

    ``runner_factory`` lets tests inject a fake runner; it receives the same keyword
    arguments ``client.beta.messages.tool_runner`` would.
    """
    settings = ctx.settings
    client = client or anthropic.Anthropic()
    tools = build_tools(ctx)

    kwargs: dict[str, Any] = dict(
        model=settings.claude_model,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        tools=tools,
        messages=[{"role": "user", "content": build_user_message(ctx)}],
        output_config={"effort": "high"},
        max_iterations=20,
    )
    # Server-side refusal fallback: if a safety classifier declines, the API re-runs
    # the request on a fallback model inside the same call.
    kwargs["betas"] = ["server-side-fallback-2026-07-01"]
    kwargs["fallbacks"] = "default"

    make = runner_factory or client.beta.messages.tool_runner
    runner = make(**kwargs)

    last = None
    for message in runner:
        last = message
    if last is None:
        return ctx.result

    ctx.result.model = getattr(last, "model", None)
    if getattr(last, "stop_reason", None) == "refusal":
        ctx.result.refusal = True
        ctx.result.final_text = "Claude declined this request (stop_reason=refusal)."
        return ctx.result

    ctx.result.final_text = "\n".join(
        b.text for b in last.content if getattr(b, "type", None) == "text"
    ).strip()
    return ctx.result
