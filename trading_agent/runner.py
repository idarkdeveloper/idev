"""Top-level orchestration: fetch -> diff -> (maybe) run Claude -> persist."""

from __future__ import annotations

import logging
from typing import Any

from .agent import AgentContext, RunResult, run_agent
from .broker import AlpacaPaperBroker, Broker, LocalPaperBroker
from .config import Settings
from .notify import Notifier
from .quiver import DisclosedTrade, QuiverClient
from .state import State

log = logging.getLogger(__name__)


def make_broker(settings: Settings, price_fn: Any | None = None) -> Broker:
    if settings.use_alpaca:
        return AlpacaPaperBroker(settings.alpaca_key_id, settings.alpaca_secret,
                                 settings.alpaca_base_url)
    return LocalPaperBroker(settings.state_dir / "paper_broker.json",
                            starting_cash=settings.paper_starting_cash, price_fn=price_fn)


def make_notifier(settings: Settings) -> Notifier:
    return Notifier(resend_api_key=settings.resend_api_key, email_to=settings.notify_email_to,
                    email_from=settings.notify_email_from, webhook_url=settings.notify_webhook_url)


def check(settings: Settings, *, force: bool = False, dry_run: bool = False,
          trades: list[DisclosedTrade] | None = None, broker: Broker | None = None,
          quiver: QuiverClient | None = None, notifier: Notifier | None = None,
          runner_factory: Any | None = None) -> RunResult:
    """One pass of the routine.

    * ``trades`` overrides the QuiverQuant fetch (demo / tests).
    * ``force`` runs Claude even when nothing new was disclosed.
    * ``dry_run`` stops before calling Claude.
    """
    state = State(settings.state_dir / "state.json")
    broker = broker or make_broker(settings)
    notifier = notifier or make_notifier(settings)

    if trades is None:
        if quiver is None:
            if not settings.quiver_api_key:
                raise SystemExit("QUIVER_API_KEY is not set (or pass --demo).")
            quiver = QuiverClient(settings.quiver_api_key)
        trades = quiver.trades_for_investor(settings.watch_investor, settings.watch_source)

    new = state.new_trades(trades)
    result = RunResult(investor=settings.watch_investor, new_trades=new)
    log.info("%d disclosed trades for %s, %d new", len(trades), settings.watch_investor, len(new))

    if not new and not force:
        result.skipped = True
        state.record_run({"new_trades": 0, "skipped": True})
        state.save()
        return result

    if dry_run:
        result.skipped = True
        state.record_run({"new_trades": len(new), "dry_run": True})
        state.save()
        return result

    ctx = AgentContext(settings=settings, broker=broker, quiver=quiver, notifier=notifier,
                       state=state, result=result)
    run_agent(ctx, runner_factory=runner_factory)
    # Only remember trades once they were actually analysed, so a failed API call
    # (bad key, outage) is retried on the next run instead of silently dropped.
    state.mark_seen(new)
    state.record_run({"new_trades": len(new), "recommendations": len(result.recommendations),
                      "orders": len(result.orders), "model": result.model,
                      "refusal": result.refusal})
    state.save()
    return result
