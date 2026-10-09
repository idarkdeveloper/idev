"""Top-level orchestration: fetch -> diff -> (maybe) run Claude -> persist."""

from __future__ import annotations

import logging
from typing import Any

from .agent import AgentContext, RunResult, run_agent
from .broker import AlpacaPaperBroker, Broker, LocalPaperBroker
from .config import Settings
from .costs import cost_model_for
from .momentum import MomentumScreen
from .notify import Notifier
from .prices import YahooPrices
from .regime import GlobalContext
from .quiver import DisclosedTrade
from .state import State

log = logging.getLogger(__name__)


def token_cache(settings: Settings) -> Any:
    from .groww import TokenCache
    return TokenCache(settings.state_dir / "groww_token.json")


def resolve_groww_token(settings: Settings, *, fresh: bool = False, session: Any | None = None) -> str:
    """GROWW_ACCESS_TOKEN wins; otherwise reuse the cached generated token until it
    expires (06:00 IST), and only then generate a new one (Groww allows 150 a day)."""
    from .groww import cached_access_token, totp_now

    if settings.groww_access_token and not fresh:
        return settings.groww_access_token
    key = settings.groww_api_key
    if key and settings.groww_api_secret:
        return cached_access_token(key, token_cache(settings), secret=settings.groww_api_secret,
                                   fresh=fresh, session=session)
    if key and settings.groww_totp_secret:
        totp_secret = settings.groww_totp_secret
        return cached_access_token(key, token_cache(settings), totp_fn=lambda: totp_now(totp_secret),
                                   fresh=fresh, session=session)
    raise SystemExit("Groww selected but no GROWW_ACCESS_TOKEN or GROWW_API_KEY + "
                     "GROWW_API_SECRET / GROWW_TOTP_SECRET is set.")


def make_groww(settings: Settings, price_fn: Any | None = None) -> Any:
    """The real Groww client. Order calls inside it refuse unless GROWW_LIVE_ORDERS=true."""
    from .groww import GrowwBroker, InstrumentTicks

    ticks = InstrumentTicks(settings.state_dir / "cache") if settings.groww_live_orders else None
    return GrowwBroker(resolve_groww_token(settings), live_orders=settings.groww_live_orders,
                       exchange=settings.groww_exchange, price_fallback=price_fn,
                       max_slippage_pct=settings.max_slippage_pct,
                       tick_size_fn=(lambda sym: ticks.tick_size(sym, settings.groww_exchange)) if ticks else None)


def free_prices(settings: Settings) -> YahooPrices:
    """Keyless quotes and history: NSE via Yahoo (.NS / .BO suffix), US bare symbols."""
    suffix = (".BO" if settings.groww_exchange == "BSE" else ".NS") if settings.market == "in" else ""
    return YahooPrices(suffix=suffix, cache_dir=settings.state_dir / "cache")


def make_broker(settings: Settings, price_fn: Any | None = None) -> Broker:
    sim_path = settings.state_dir / "paper_broker.json"
    price_fn = price_fn or free_prices(settings)
    if settings.use_groww:
        groww = make_groww(settings, price_fn)
        if settings.groww_live_orders:
            log.warning("GROWW_LIVE_ORDERS=true: orders will use REAL money on Groww.")
            return groww
        # Paper mode: real holdings + live prices from Groww, simulated fills.
        sim = LocalPaperBroker(sim_path, starting_cash=settings.paper_starting_cash,
                               price_fn=groww.latest_price, currency="INR", whole_shares=True,
                               cost_model=cost_model_for("in"))
        if sim.is_fresh:
            try:
                acct = groww.account()
                sim.seed(groww.positions(), cash=acct.cash, label="groww")
                log.info("Seeded paper account from Groww: %d holdings, cash %.2f",
                         len(sim.positions()), acct.cash)
            except Exception as e:  # noqa: BLE001
                log.warning("Could not mirror Groww holdings (%s); starting from cash only.", e)
        return sim
    if settings.use_alpaca:
        return AlpacaPaperBroker(settings.alpaca_key_id, settings.alpaca_secret,
                                 settings.alpaca_base_url)
    return LocalPaperBroker(sim_path, starting_cash=settings.paper_starting_cash, price_fn=price_fn,
                            currency=settings.currency, whole_shares=settings.market == "in",
                            cost_model=cost_model_for(settings.market))


def make_data_source(settings: Settings) -> Any:
    if settings.data_source == "nse":
        from .nse import NSEClient

        return NSEClient(cache_dir=settings.state_dir / "cache")
    from .quiver import QuiverClient

    if not settings.quiver_api_key:
        raise SystemExit("QUIVER_API_KEY is not set (or use MARKET=in / --demo).")
    return QuiverClient(settings.quiver_api_key)


def make_notifier(settings: Settings) -> Notifier:
    return Notifier(resend_api_key=settings.resend_api_key, email_to=settings.notify_email_to,
                    email_from=settings.notify_email_from, webhook_url=settings.notify_webhook_url)


def record_equity(state: State, broker: Any) -> None:
    """Best-effort equity point for the paper-account curve."""
    try:
        acct = broker.account()
        n = len(broker.positions())
        state.record_equity(acct.equity, acct.cash, n)
    except Exception as e:  # noqa: BLE001
        log.debug("equity point skipped: %s", e)


def _after_run_gtt(settings: Settings, broker: Any, state: State, notifier: Any) -> None:
    """Keep the Groww GTT stop-losses in line with holdings (live + GROWW_GTT_STOPS only)."""
    from .live import gtt_enabled, sync_gtt_stops

    if not gtt_enabled(settings, broker):
        return
    prices = free_prices(settings)
    actions = sync_gtt_stops(settings, broker, state, notifier=notifier,
                             bars_fn=lambda sym: prices.history(sym, "1y"))
    if actions:
        log.info("GTT stops: %s", actions)


def check(settings: Settings, *, force: bool = False, dry_run: bool = False,
          trades: list[DisclosedTrade] | None = None, broker: Broker | None = None,
          data: Any | None = None, notifier: Notifier | None = None,
          runner_factory: Any | None = None, momentum: Any | None = None,
          context: Any | None = None, baseline: bool = False) -> RunResult:
    """One pass of the routine.

    * ``trades`` overrides the data-source fetch (demo / tests).
    * ``force`` runs Claude even when nothing new was disclosed.
    * ``dry_run`` stops before calling Claude.
    * ``baseline`` records every current trade as seen without calling Claude: used when
      the saved state was lost, so a month of old deals isn't re-sent as new.
    """
    state = State(settings.state_dir / "state.json")
    broker = broker or make_broker(settings)
    notifier = notifier or make_notifier(settings)

    live_run = trades is None  # trades fetched here, not supplied by a demo or test
    if live_run:
        data = data or make_data_source(settings)
        trades = data.trades_for_investor(settings.watch_investor, settings.watch_source)

    new = state.new_trades(trades)
    result = RunResult(investor=settings.watch_investor, new_trades=new)
    log.info("%d disclosed trades for %s, %d new", len(trades), settings.watch_investor, len(new))

    if baseline:
        state.mark_seen(new)
        result.skipped = result.baseline = True
        state.record_run({"new_trades": len(new), "baseline": True})
        record_equity(state, broker)
        state.save()
        log.warning("Baseline: %d current trades recorded as seen without analysis", len(new))
        return result

    if not new and not force:
        result.skipped = True
        state.record_run({"new_trades": 0, "skipped": True})
        record_equity(state, broker)
        state.save()
        return result

    if dry_run:
        result.skipped = True
        state.record_run({"new_trades": len(new), "dry_run": True})
        state.save()
        return result

    if live_run:  # free price history for momentum and the global regime
        prices = free_prices(settings)
        momentum = momentum or MomentumScreen(prices)
        context = context or GlobalContext(YahooPrices(suffix="", cache_dir=settings.state_dir / "cache",
                                                       cache_ttl=900))
    ctx = AgentContext(settings=settings, broker=broker, data=data, notifier=notifier,
                       state=state, result=result, momentum=momentum, context=context)
    run_agent(ctx, runner_factory=runner_factory)
    # Only remember trades once they were actually analysed, so a failed API call
    # (bad key, outage) is retried on the next run instead of silently dropped.
    state.mark_seen(new)
    _after_run_gtt(settings, broker, state, notifier)
    record_equity(state, broker)
    state.record_run({"new_trades": len(new), "recommendations": len(result.recommendations),
                      "orders": len(result.orders), "model": result.model,
                      "refusal": result.refusal, "fallback_used": result.fallback_used,
                      "stop": result.stop, "usage": result.usage.to_dict()})
    state.save()
    return result
