"""Top-level orchestration: fetch -> diff -> (maybe) run Claude -> persist."""

from __future__ import annotations

import logging
from typing import Any

import requests

from .agent import AgentContext, RunResult, run_agent
from .broker import AlpacaPaperBroker, Broker, LocalPaperBroker
from .config import Settings
from .groww import GrowwTokenUnavailable, warn_token_block_once
from .costs import cost_model_for
from .momentum import MomentumScreen
from .notify import Notifier
from .prices import YahooPrices
from .regime import GlobalContext
from .quiver import DisclosedTrade, fetch_followed
from .state import State

log = logging.getLogger(__name__)


def token_cache(settings: Settings) -> Any:
    from .groww import TokenCache
    return TokenCache(settings.state_dir / "groww_token.json")


def resolve_groww_token(settings: Settings, *, fresh: bool = False, session: Any | None = None,
                        force: bool = False) -> str:
    """GROWW_ACCESS_TOKEN wins; otherwise reuse the cached generated token until it
    expires (06:00 IST), and only then generate a new one (Groww allows 150 a day).

    Raises GrowwTokenUnavailable, with no network call, while a refused request is cooling down
    (``force`` ignores the cool-down)."""
    from .groww import cached_access_token, totp_now

    if settings.groww_access_token and not fresh:
        return settings.groww_access_token
    key = settings.groww_api_key
    if key and settings.groww_api_secret:
        return cached_access_token(key, token_cache(settings), secret=settings.groww_api_secret,
                                   fresh=fresh, session=session, force=force)
    if key and settings.groww_totp_secret:
        totp_secret = settings.groww_totp_secret
        return cached_access_token(key, token_cache(settings), totp_fn=lambda: totp_now(totp_secret),
                                   fresh=fresh, session=session, force=force)
    raise SystemExit("Groww selected but no GROWW_ACCESS_TOKEN or GROWW_API_KEY + "
                     "GROWW_API_SECRET / GROWW_TOTP_SECRET is set.")


def make_groww(settings: Settings, price_fn: Any | None = None, *, force: bool = False) -> Any:
    """The real Groww client. Order calls inside it refuse unless GROWW_LIVE_ORDERS=true.
    Raises GrowwTokenUnavailable while Groww's token endpoint is cooling down after a refusal."""
    from .groww import GrowwBroker, InstrumentTicks

    ticks = InstrumentTicks(settings.state_dir / "cache") if settings.groww_live_orders else None
    session = groww_session(settings)
    return GrowwBroker(resolve_groww_token(settings, session=session, force=force), live_orders=settings.groww_live_orders,
                       exchange=settings.groww_exchange, price_fallback=price_fn,
                       max_slippage_pct=settings.max_slippage_pct, session=session,
                       allowed_ip=settings.groww_allowed_ip,
                       tick_size_fn=(lambda sym: ticks.tick_size(sym, settings.groww_exchange)) if ticks else None)


def groww_session(settings: Settings) -> requests.Session:
    """A session for every Groww call; through GROWW_PROXY_URL when set, so a runner without a
    fixed IP (GitHub Actions) can still reach Groww from the registered address."""
    s = requests.Session()
    if getattr(settings, "groww_proxy_url", None):
        s.proxies = {"https": settings.groww_proxy_url, "http": settings.groww_proxy_url}
    return s


def free_prices(settings: Settings) -> YahooPrices:
    """Keyless quotes and history: NSE via Yahoo (.NS / .BO suffix), US bare symbols."""
    suffix = (".BO" if settings.groww_exchange == "BSE" else ".NS") if settings.market == "in" else ""
    return YahooPrices(suffix=suffix, cache_dir=settings.state_dir / "cache")


def make_practice_broker(settings: Settings, price_fn: Any | None = None, groww: Any | None = None) -> LocalPaperBroker:
    """The practice (paper) account on ``state/paper_broker.json``. Simulated fills only; with Groww linked its
    prices come from Groww (read-only: the client is built with live orders off), otherwise from ``price_fn``."""
    import dataclasses

    sim_path = settings.state_dir / "paper_broker.json"
    price_fn = price_fn or free_prices(settings)
    if settings.use_groww:
        read_only = dataclasses.replace(settings, groww_live_orders=False)
        try:
            groww = groww or make_groww(read_only, price_fn)
        except GrowwTokenUnavailable as e:
            # Groww will not give us a token right now: price from the free source and keep trying Groww
            # (a file read, no network, while the cool-down lasts) so it is used again once it is lifted.
            warn_token_block_once(e, log)
            groww = None
        holder = {"g": groww}

        def groww_or_free(symbol: str) -> float:
            if holder["g"] is None:
                try:
                    holder["g"] = make_groww(read_only, price_fn)
                except GrowwTokenUnavailable as e:
                    warn_token_block_once(e, log)
                    return price_fn(symbol)
            return holder["g"].latest_price(symbol)

        def make() -> LocalPaperBroker:
            return LocalPaperBroker(sim_path, starting_cash=settings.paper_starting_cash,
                                    price_fn=groww_or_free, currency="INR", whole_shares=True,
                                    cost_model=cost_model_for("in"), shared=True)
        sim = make()
        if sim.is_untouched_mirror:
            # Older versions copied the Groww holdings in on the first run. A copy nobody has
            # traded in is just a stale duplicate, so start the practice account afresh.
            log.info("Replacing the untouched copy of the Groww holdings with a fresh practice "
                     "account of %.0f.", settings.paper_starting_cash)
            sim.path.unlink()
            sim = make()
        return sim
    return LocalPaperBroker(sim_path, starting_cash=settings.paper_starting_cash, price_fn=price_fn,
                            currency=settings.currency, whole_shares=settings.market == "in",
                            cost_model=cost_model_for(settings.market), shared=True)


def make_broker(settings: Settings, price_fn: Any | None = None) -> Broker:
    price_fn = price_fn or free_prices(settings)
    if settings.use_groww:
        if settings.groww_live_orders:
            groww = make_groww(settings, price_fn)  # GrowwTokenUnavailable: live orders are refused, not faked
            log.warning("GROWW_LIVE_ORDERS=true: orders will use REAL money on Groww.")
            return groww
        try:
            groww = make_groww(settings, price_fn)
        except GrowwTokenUnavailable as e:
            warn_token_block_once(e, log)
            groww = None  # practice account on free prices until Groww hands out a token again
        # Paper mode: a separate practice account (PAPER_STARTING_CASH, no stocks) with live
        # prices from Groww and simulated fills. Real holdings are shown on their own and
        # are never copied in, so the two never look like duplicates.
        return make_practice_broker(settings, price_fn, groww)
    if settings.use_alpaca:
        return AlpacaPaperBroker(settings.alpaca_key_id, settings.alpaca_secret,
                                 settings.alpaca_base_url)
    return make_practice_broker(settings, price_fn)


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


def equity_key(broker: Any) -> str:
    """Which curve in state.json a broker's points belong to: the practice account has its own."""
    return "practice_equity" if isinstance(broker, LocalPaperBroker) else "equity_history"


def record_equity(state: State, broker: Any) -> None:
    """Best-effort equity point for the broker's own curve."""
    try:
        acct = broker.account()
        n = len(broker.positions())
        state.record_equity(acct.equity, acct.cash, n, since=getattr(broker, "created_at", None),
                            key=equity_key(broker))
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
        trades = fetch_followed(data, settings.investors, settings.watch_source)

    new = state.new_trades(trades)  # a deal is seen once for everyone who matches it
    names = settings.investors
    result = RunResult(investor=", ".join(names), new_trades=new, investors=list(names))
    log.info("%d disclosed trades for %s, %d new", len(trades), ", ".join(names), len(new))

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
    news = None
    if settings.market == "in":
        from .news import NewsService
        from .instruments import CompanyNames
        news = NewsService(settings, names=CompanyNames(settings.state_dir / "cache"))
    ctx = AgentContext(settings=settings, broker=broker, data=data, notifier=notifier,
                       state=state, result=result, momentum=momentum, context=context, news=news)
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
