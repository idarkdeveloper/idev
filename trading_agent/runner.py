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


def token_block(settings: Settings) -> Any | None:
    """The GrowwTokenUnavailable that asking for a token would raise right now (no network), else None.
    None when an env token is set, or a valid cached token exists, or no key is configured."""
    key = settings.groww_api_key
    if settings.groww_access_token or not key:
        return None
    cache = token_cache(settings)
    if cache.get(key):
        return None
    return cache.block_error(key)


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
                blocked = token_block(settings)  # a file read: no client or Session is built while blocked
                if blocked is not None:
                    warn_token_block_once(blocked, log)
                    return price_fn(symbol)
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


SNAPSHOT_FILE = "groww_holdings.json"
_SNAPSHOT_KEYS = ("symbol", "name", "exchange", "kind", "maturity", "qty", "sellable_qty", "avg_price")


def _assemble(entries: list[dict[str, Any]], stamp: str) -> dict[str, Any]:
    """Holdings rows (each with a ``price`` or None) and the totals, the way the dashboard shows them."""
    rows = []
    for e in entries:
        price, qty, avg = e.get("price"), e["qty"], e["avg_price"]
        invested = qty * avg
        value = qty * price if price is not None else None
        rows.append({"symbol": e["symbol"], "name": e.get("name"), "exchange": e.get("exchange"),
                     "kind": e.get("kind") or "equity", "maturity": e.get("maturity"),
                     "qty": qty, "sellable_qty": e.get("sellable_qty"),
                     "avg_price": avg, "price": price,
                     "invested": round(invested, 2), "value": round(value, 2) if value is not None else None,
                     "pl": round(value - invested, 2) if value is not None else None,
                     "pl_pct": (price / avg - 1) if price is not None and avg else None})
    rows.sort(key=lambda r: -(r["value"] if r["value"] is not None else r["invested"]))
    priced = [r for r in rows if r["value"] is not None]
    inv_priced = sum(r["invested"] for r in priced)
    value = sum(r["value"] for r in priced)
    return {"linked": True, "at": stamp, "holdings": rows,
            "invested": round(sum(r["invested"] for r in rows), 2),
            "value": round(value, 2), "pl": round(value - inv_priced, 2),
            "pl_pct": (value / inv_priced - 1) if inv_priced else None,
            "unpriced": [r["symbol"] for r in rows if r["value"] is None]}


def save_groww_snapshot(settings: Settings, rows: list[dict[str, Any]]) -> None:
    """Remember the holdings (no prices, no tokens or keys) so the page and the daily emails still work while
    Groww refuses a login. Written beside the file and swapped in, owner-only from the moment it is created; a
    failure to save is logged, never raised. A ``source_note`` already in the file (a statement the user loaded)
    is dropped: this snapshot is Groww's own."""
    import json
    import os
    import threading
    from datetime import datetime
    from .timezones import IST
    path = settings.state_dir / SNAPSHOT_FILE
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        body = {"saved_at": datetime.now(IST).isoformat(timespec="seconds"),
                "holdings": [{k: r.get(k) for k in _SNAPSHOT_KEYS} for r in rows]}
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(body, indent=2))
        os.replace(tmp, path)
    except Exception as e:  # noqa: BLE001
        log.warning("could not save the Groww holdings snapshot: %s", e)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def load_groww_snapshot(settings: Settings) -> dict[str, Any] | None:
    import json
    try:
        data = json.loads((settings.state_dir / SNAPSHOT_FILE).read_text(encoding="utf-8"))
        rows = [r for r in data["holdings"] if isinstance(r, dict) and r.get("symbol")
                and isinstance(r.get("qty"), (int, float)) and isinstance(r.get("avg_price"), (int, float))]
        out = {"saved_at": str(data["saved_at"]), "holdings": rows}
        if isinstance(data.get("source_note"), str) and data["source_note"].strip():
            out["source_note"] = " ".join(data["source_note"].split())[:200]
        return out
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def trading_days_old(saved_at: str, now: Any = None) -> int | None:
    """Weekdays after the day the snapshot was saved, up to today (IST); None when the time cannot be read.
    Exchange holidays are not known here, so a holiday counts as a day."""
    from datetime import datetime, timedelta
    from .timezones import IST
    try:
        saved = datetime.fromisoformat(saved_at)
        saved = (saved if saved.tzinfo else saved.replace(tzinfo=IST)).astimezone(IST).date()
    except ValueError:
        return None
    today = (now or datetime.now(IST)).astimezone(IST).date()
    d, n = saved, 0
    while d < today:
        d += timedelta(days=1)
        n += d.weekday() < 5
    return n


def _from_snapshot(settings: Settings, prices: Any, bse: Any, reason: str, error_extra: dict[str, Any]) -> dict[str, Any] | None:
    from .instruments import nse_then_bse
    snap = load_groww_snapshot(settings)
    if snap is None:
        return None
    price_fn = nse_then_bse(prices, bse)
    entries = []
    for r in snap["holdings"]:
        try:
            price = float(price_fn(r["symbol"]))
        except Exception:  # noqa: BLE001 - stays "no price"
            price = None
        entries.append({**{k: r.get(k) for k in _SNAPSHOT_KEYS}, "price": price})
    out = _assemble(entries, snap["saved_at"])
    out.update({"source": "saved", "saved_at": snap["saved_at"], "reason": reason,
                "prices": "yahoo (delayed)", "age_trading_days": trading_days_old(snap["saved_at"]),
                **error_extra})
    if snap.get("source_note"):
        out["source_note"] = snap["source_note"]
    return out


def read_groww_portfolio(settings: Settings, prices: Any, stamp: str = "") -> dict[str, Any]:
    """Your real Groww holdings with buy price, current price and profit or loss. Read-only: the client is built
    with live orders off and never places an order. After every successful read the holdings are saved to
    ``groww_holdings.json``. When Groww cannot be read (cool-down after a refusal, any error, not linked) and a
    snapshot exists, that is returned priced from the free source with ``source: "saved"``, ``saved_at``,
    ``reason`` and ``prices``; with no snapshot: {"linked": False} without credentials, or
    {"linked": True, "error": text[, "blocked_until": iso]}. A successful read has {"linked": True, "at",
    "holdings", "invested", "value", "pl", "pl_pct", "unpriced"}. The dashboard and the daily emails use it."""
    from .groww import GrowwBroker
    from .instruments import CompanyNames, nse_then_bse
    bse = YahooPrices(suffix=".BO", cache_dir=settings.state_dir / "cache")
    if not settings.has_groww_credentials:
        return {"linked": False}   # no credentials: a leftover snapshot is not shown
    failure: dict[str, Any]
    try:
        g = GrowwBroker(resolve_groww_token(settings), live_orders=False, exchange=settings.groww_exchange,
                        price_fallback=nse_then_bse(prices, bse))
        positions = g.positions()
        failure = {}
    except GrowwTokenUnavailable as e:
        failure = {"linked": True, "error": str(e)}
        if e.until is not None:
            failure["blocked_until"] = e.until.isoformat()
    except SystemExit as e:
        failure = {"linked": True, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        failure = {"linked": True, "error": f"{type(e).__name__}: {e}"}
    if failure:
        extra = {k: failure[k] for k in ("blocked_until",) if k in failure}
        saved = _from_snapshot(settings, prices, bse, failure["error"], {"linked": True, **extra})
        return saved or failure
    try:
        names = CompanyNames(settings.state_dir / "cache").lookup([p.symbol for p in positions])
    except Exception:  # noqa: BLE001 - names are optional
        names = {}
    entries = []
    for p in positions:
        info = names.get(p.symbol.upper()) or {}
        entries.append({"symbol": p.symbol, "name": info.get("name"), "exchange": info.get("exchange"),
                        "kind": info.get("kind") or "equity", "maturity": info.get("maturity"),
                        "qty": p.qty, "sellable_qty": p.free_qty, "avg_price": p.avg_entry_price,
                        "price": p.current_price})
    out = _assemble(entries, stamp)
    save_groww_snapshot(settings, out["holdings"])
    return out


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
