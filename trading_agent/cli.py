"""Command line entry point: ``python -m trading_agent <command>``."""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from importlib import resources
from pathlib import Path
from typing import Any

import requests

from .broker import LocalPaperBroker
from .config import load_settings, parse_investors
from .groww import GrowwTokenUnavailable
from .quiver import _norm_congress, fetch_followed, filter_by_investors
from .runner import check, make_broker
from .state import State


def _demo_inputs(settings):
    """Fixture trades + a local broker with fixed prices, so no data keys are needed."""
    from .costs import cost_model_for

    fx = resources.files("trading_agent") / "fixtures"
    if settings.market == "in":
        from .nse import _norm_deal
        snap = json.loads((fx / "nse_deals_sample.json").read_text())
        rows = [_norm_deal(r, "bulk") for r in snap["BULK_DEALS_DATA"]]
        rows += [_norm_deal(r, "block") for r in snap["BLOCK_DEALS_DATA"]]
        prices = json.loads((fx / "prices_in.json").read_text())
    else:
        rows = [_norm_congress(r) for r in json.loads((fx / "congress_sample.json").read_text())]
        prices = json.loads((fx / "prices.json").read_text())
    trades = filter_by_investors(rows, settings.investors)
    broker = LocalPaperBroker(settings.state_dir / "paper_broker.json",
                              starting_cash=settings.paper_starting_cash,
                              price_fn=lambda s: prices[s], currency=settings.currency,
                              whole_shares=settings.market == "in",
                              cost_model=cost_model_for(settings.market))
    return trades, broker


def _print_result(result) -> None:
    if result.skipped and not result.new_trades:
        print(f"No new disclosed trades for {result.investor}. Nothing to do.")
        return
    print(f"New disclosed trades for {result.investor}: {len(result.new_trades)}")
    for t in result.new_trades:
        print(f"  - {t.summary()}")
    if result.skipped:
        print("(baseline: recorded as seen, Claude was not called)" if result.baseline
              else "(dry run: Claude was not called)")
        return
    print(f"\nModel: {result.model}")
    print(f"Recommendations sent: {len(result.recommendations)}")
    for r in result.recommendations:
        print(f"  - {r['action'].upper()} {r['ticker']} [{r['confidence']}] {r['headline']}")
    if result.orders:
        print(f"Paper orders placed: {len(result.orders)}")
        for o in result.orders:
            print(f"  - {o}")
    if result.final_text:
        print(f"\n{result.final_text}")


def _investor_names(args: argparse.Namespace) -> list[str] | None:
    """--investor values (repeatable, each may be a comma list) as one validated list; None when not given."""
    given = getattr(args, "investor", None)
    if not given:
        return None
    try:
        return parse_investors(given)
    except ValueError as e:
        raise SystemExit(f"--investor: {e}") from None


def _settings(args: argparse.Namespace):
    if getattr(args, "market", None):
        import os
        os.environ["MARKET"] = args.market
    return load_settings()


def _holiday_today(settings: Any) -> str | None:
    """The NSE holiday's name when Indian equities are closed today for one, else None."""
    if settings.market != "in":
        return None
    from datetime import datetime
    from .holidays import IST, NSEHolidays
    return NSEHolidays(cache_dir=settings.state_dir / "cache").holiday(datetime.now(IST).date())


def cmd_check(args: argparse.Namespace) -> int:
    settings = _settings(args)
    if getattr(args, "skip_holidays", False):
        name = _holiday_today(settings)
        if name:
            print(f"NSE is closed today ({name}): nothing to check.")
            return 0
    names = _investor_names(args)
    if names:
        settings.investors = names
    if args.auto_trade:
        settings.auto_trade = True
    kwargs: dict[str, Any] = {}
    if args.demo:
        trades, broker = _demo_inputs(settings)
        kwargs.update(trades=trades, broker=broker)
    result = check(settings, force=args.force, dry_run=args.dry_run,
                   baseline=getattr(args, "baseline", False), **kwargs)
    _print_result(result)
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, default=str))
    return 0


def cmd_loop(args: argparse.Namespace) -> int:
    """Poor man's routine: re-run ``check`` every N minutes until interrupted."""
    while True:
        try:
            cmd_check(args)
        except SystemExit as e:
            if e.code not in (0, None):
                raise
        except Exception as e:  # noqa: BLE001 - keep the loop alive
            logging.exception("check failed: %s", e)
        print(f"\n--- sleeping {args.every} min ---\n", flush=True)
        time.sleep(args.every * 60)


def cmd_portfolio(args: argparse.Namespace) -> int:
    settings = _settings(args)
    broker = make_broker(settings)
    acct = broker.account()
    cur = "₹" if acct.currency == "INR" or settings.market == "in" else "$"
    print(f"Broker: {broker.name}   cash {cur}{acct.cash:,.2f}   equity {cur}{acct.equity:,.2f}")
    positions = broker.positions()
    if not positions:
        print("No open positions.")
    for p in positions:
        pl = f"{p.unrealized_pl:+,.2f}" if p.unrealized_pl is not None else "n/a"
        px = f"{p.current_price:,.2f}" if p.current_price is not None else "n/a"
        print(f"  {p.symbol:<8} qty {p.qty:<12g} avg {p.avg_entry_price:,.2f}  now {px}  P&L {pl}")
    if isinstance(broker, LocalPaperBroker):
        perf = broker.performance()
        print(f"\nPaper performance: {perf['pnl']:+,.2f} ({perf['pnl_pct']:+.2f}%) "
              f"over {perf['orders']} orders from {cur}{perf['starting_cash']:,.0f}")
        eq = State(settings.state_dir / "state.json").equity_stats(key="practice_equity")
        if eq:
            print(f"Peak {cur}{eq['peak']:,.0f}; now {eq['drawdown_now']*100:+.1f}% from peak; "
                  f"worst fall {eq['max_drawdown']*100:+.1f}% over {eq['points']} recorded points")
    return 0


def cmd_holdings(args: argparse.Namespace) -> int:
    """Your real Groww holdings: buy price, current price, profit or loss. Read-only."""
    from .groww import GrowwBroker
    from .runner import free_prices, resolve_groww_token
    settings = _settings(args)
    if not settings.has_groww_credentials:
        print("Groww isn't linked: add GROWW_API_KEY + GROWW_API_SECRET / GROWW_TOTP_SECRET to .env.")
        return 1
    from .instruments import CompanyNames, nse_then_bse
    from .prices import YahooPrices
    from .price_archive import archive_for
    bse = YahooPrices(suffix=".BO", cache_dir=settings.state_dir / "cache", archive=archive_for(settings))
    g = GrowwBroker(resolve_groww_token(settings), live_orders=False, exchange=settings.groww_exchange,
                    price_fallback=nse_then_bse(free_prices(settings), bse))
    rows = sorted(g.positions(), key=lambda p: -(p.market_value or p.qty * p.avg_entry_price))
    names = CompanyNames(settings.state_dir / "cache").lookup([p.symbol for p in rows])
    if not rows:
        print("No holdings in your Groww account.")
        return 0
    print(f"{'Stock':<12} {'Company':<44} {'Qty':>6} {'Buy price':>11} {'Current':>11} {'Invested':>12} {'Value':>12} {'P&L':>12} {'P&L %':>8}")
    def company(sym: str) -> str:
        info = names.get(sym.upper()) or {}
        n = (info.get("name") or "").replace(" Limited", " Ltd")
        if info.get("exchange") == "BSE":
            n += " (BSE)"
        return n[:44]

    inv_all = inv_priced = value = 0.0
    for p in rows:
        invested = p.qty * p.avg_entry_price
        inv_all += invested
        if p.current_price is None:
            print(f"{p.symbol:<12} {company(p.symbol):<44} {p.qty:>6g} {p.avg_entry_price:>11,.2f} {'no price':>11} {invested:>12,.0f}")
            continue
        v = p.qty * p.current_price
        inv_priced += invested
        value += v
        pct = (p.current_price / p.avg_entry_price - 1) * 100 if p.avg_entry_price else 0.0
        print(f"{p.symbol:<12} {company(p.symbol):<44} {p.qty:>6g} {p.avg_entry_price:>11,.2f} {p.current_price:>11,.2f} {invested:>12,.0f} "
              f"{v:>12,.0f} {v - invested:>+12,.0f} {pct:>+7.2f}%")
    pl = value - inv_priced
    print(f"\nInvested ₹{inv_all:,.0f} · value now ₹{value:,.0f} · P&L ₹{pl:+,.0f} "
          f"({(value / inv_priced - 1) * 100 if inv_priced else 0:+.2f}%) on holdings with a price")
    return 0


def cmd_groww_token(args: argparse.Namespace) -> int:
    """Print a Groww access token (valid until 06:00 IST); reuses the cached one."""
    from .runner import resolve_groww_token
    settings = _settings(args)
    settings.groww_access_token = None  # use key + secret / TOTP (cached until 06:00 IST)
    if args.force:
        print(_FORCE_WARNING, file=sys.stderr)
    try:
        token = resolve_groww_token(settings, fresh=args.fresh, force=args.force)
    except (GrowwTokenUnavailable, requests.HTTPError) as e:
        print(_token_error_text(e), file=sys.stderr)
        return 2
    print(token)
    print("This is your Groww access token - treat it like a password.", file=sys.stderr)
    return 0


_FORCE_WARNING = ("Warning: --force ignores the wait after Groww refused a login token; asking again may "
                  "extend Groww's wait.")


def _token_error_text(e: BaseException) -> str:
    """The line shown when a token request fails: the clear message, never the token or a traceback."""
    if isinstance(e, GrowwTokenUnavailable):
        return str(e)
    return f"Groww refused the login token request ({type(e).__name__}). Try again later."


def _fmt_live_order(o: dict[str, Any]) -> str:
    px = o.get("average_fill_price") or o.get("limit_price")
    return (f"{(o.get('placed_at') or '')[:19]:<19}  {str(o.get('side', '')).upper():<4} {o.get('symbol', ''):<12} "
            f"qty {o.get('qty')!s:<5} filled {o.get('filled_quantity') or 0:<5g} @ {px if px is not None else 'n/a'}  "
            f"{o.get('status', ''):<6} {o.get('order_status') or ''}  {o.get('groww_order_id') or ''}"
            + (f"  ({o.get('remark') or o.get('error')})" if o.get('remark') or o.get('error') else ""))


def cmd_orders(args: argparse.Namespace) -> int:
    """List live Groww orders recorded in state.json; --refresh re-checks open ones."""
    from .live import refresh_open_orders
    from .runner import make_groww, make_notifier
    settings = _settings(args)
    st = State(settings.state_dir / "state.json")
    orders = st.data.get("live_orders", [])
    if args.refresh:
        if not any(o.get("status") == "open" for o in orders):
            print("No open live orders to refresh.")
        else:
            updated = refresh_open_orders(make_groww(settings), st, make_notifier(settings))
            st.save()
            print(f"Re-checked {len(updated)} open order(s).")
    if not orders:
        print("No live orders recorded.")
    for o in orders[-args.limit:]:
        print(_fmt_live_order(o))
    return 0


def cmd_fundamentals_history(args: argparse.Namespace) -> int:
    """Download and cache NSE quarterly results (XBRL) for every stock in a universe."""
    from datetime import date, timedelta
    from .fundamentals_history import ResultsHistory
    from .index_history import point_in_time
    from .screen import load_universe

    settings = _settings(args)
    members = [m["symbol"] for m in load_universe(args.universe)]
    try:
        membership = point_in_time(args.universe, members, settings.state_dir, progress=print)
        since = (date.today() - timedelta(days=365 * args.years)).isoformat()
        symbols = sorted(set(members) | set(membership.ever_members(since)))
    except Exception as e:  # noqa: BLE001 - fall back to today's members
        print(f"Past members unavailable ({e}); using today's {len(members)} members.")
        symbols = sorted(members)
    h = ResultsHistory(settings.state_dir / "cache", max_new_downloads=args.max)
    print(f"{len(symbols)} stocks; downloading results filings NSE hasn't given us yet "
          f"(about 2 a second, at most {args.max} this run)…")
    done = empty = 0
    for i, sym in enumerate(symbols, 1):
        try:
            recs = h.annual(sym) if args.annual else h.history(sym)
        except Exception as e:  # noqa: BLE001
            print(f"  {sym}: listing failed ({type(e).__name__})")
            continue
        done += 1
        empty += not recs
        if i % 10 == 0 or i == len(symbols):
            print(f"  {i}/{len(symbols)} stocks, {h.downloads} downloaded this run", flush=True)
        if h.refused >= 2:
            print("NSE is refusing downloads for now; run this again in a few minutes to continue.")
            break
    left = h.downloads >= args.max or h.refused >= 2
    print(f"Done: {done} stocks read, {empty} with no XBRL results, {h.downloads} filings downloaded. "
          + ("Run again to continue." if left else "Cache complete for now."))
    return 0


def cmd_fundamentals_inspect(args: argparse.Namespace) -> int:
    """Download one March results filing and show its contexts and balance-sheet items (reading only)."""
    from .fundamentals_history import ResultsHistory, inspect_xbrl
    settings = _settings(args)
    h = ResultsHistory(settings.state_dir / "cache")
    pe = f"{args.fy}-03-31"
    found = [f for f in h.filings(args.symbol) if f["period_end"] == pe]
    if not found:
        print(f"No March {args.fy} results filing listed for {args.symbol.upper()}.")
        return 1
    f = sorted(found, key=lambda f: (not f["consolidated"], f["available"]))[0]
    print(f"{args.symbol.upper()} FY{args.fy} ({'consolidated' if f['consolidated'] else 'standalone'}), "
          f"broadcast {f['available']}: {f['xbrl']}")
    info = inspect_xbrl(h.fetch_xml(f))
    print("Contexts (id: dates, facts):")
    for cid, (dates, n) in info["contexts"].items():
        print(f"  {cid}: {dates}, {n}")
    print(f"Balance-sheet-like items ({len(info['balance'])}):" if info["balance"] else
          "No balance-sheet-like items in this filing.")
    for tag, cid, value in info["balance"][:80]:
        print(f"  {tag} [{cid}] = {value}")
    return 0


def cmd_market_data(args: argparse.Namespace) -> int:
    """FII/DII flows, NIFTY 500 breadth and NSE price bands: show what is stored, or fetch now (public NSE files)."""
    from datetime import datetime
    from .bands import book_for
    from .breadth import BreadthStore, archive_bars_fn, breadth_line, nifty500_members
    from .filing_time import previous_trading_day
    from .flows import FlowStore, flows_line
    from .holidays import NSEHolidays
    from .nse import NSEClient
    from .timezones import IST

    settings = _settings(args)
    now = datetime.now(IST)
    cal = NSEHolidays(cache_dir=settings.state_dir / "cache")
    which = args.fetch
    todo = ["flows", "breadth", "bands"] if which == "all" else [which] if which else []
    status = 0
    if todo:
        client = NSEClient(cache_dir=settings.state_dir / "cache")
        for name in todo:
            try:
                if name == "flows":
                    row = FlowStore(settings.state_dir).fetch(client)
                    print(f"flows: stored the session of {row['date']}")
                elif name == "bands":
                    n = book_for(settings).fetch(client.session, now.date())
                    print(f"bands: stored {n} symbols")
                else:
                    today = now.date()
                    day = today if (now.hour >= 19 and cal.is_trading_day(today)) else previous_trading_day(today, cal)
                    row = BreadthStore(settings.state_dir).fetch_day(
                        client.session, day, nifty500_members(client.session), archive_bars_fn(settings.state_dir))
                    print(f"breadth: stored {row['date']} ({row['n']} stocks, ratio {row['ratio']})")
            except Exception as e:  # noqa: BLE001
                status = 1
                print(f"{name}: failed ({type(e).__name__}: {e})")
    print(flows_line(FlowStore(settings.state_dir).rows(), calendar=cal) or "flows: nothing stored yet")
    print(breadth_line(BreadthStore(settings.state_dir).rows(), calendar=cal) or "breadth: nothing stored yet")
    book = book_for(settings)
    print(f"price bands: list from {book.fetched_on() or 'never'}, filter {'on' if book.enabled else 'off'}"
          + ("" if book.active() or not book.enabled else " (list missing or stale: not filtering)"))
    return status


def cmd_forward(args: argparse.Namespace) -> int:
    """Paper-trade the factor screen forward, month by month, against its index fund."""
    from .costs import cost_model_for
    from .forward import ForwardTest, format_forward
    from .runner import free_prices
    from .screen import load_universe, run_screen

    settings = _settings(args)
    prices = free_prices(settings)
    if args.rebuild_from:
        return _forward_rebuild(args, settings, prices)
    from .holidays import NSEHolidays
    ft = ForwardTest(settings.state_dir, universe=args.universe, top=args.top,
                     capital=args.capital or settings.paper_starting_cash, price_fn=prices.latest_price,
                     cost_model=cost_model_for("in"), holidays=NSEHolidays(cache_dir=settings.state_dir / "cache"))
    if args.status:
        print(format_forward(ft.summary()))
        return 0
    if args.if_due and not ft.due():
        print("Forward test: nothing due (runs on weekdays after 15:40 IST, once a day).")
        return 0

    def screen() -> dict:
        members = load_universe(ft.universe)
        print(f"Ranking {len(members)} {ft.universe} members for this month's rebalance…")
        from .bands import book_for
        from .indian_filters import filters_for
        from .forward_schedule import with_caps
        return with_caps(settings, run_screen(members, prices, top=ft.data["top"], bands=book_for(settings),
                                              filters=filters_for(settings)))

    print(format_forward(ft.run(screen, force_rebalance=args.rebalance)))
    return 0


def _forward_rebuild(args: argparse.Namespace, settings: Any, prices: Any) -> int:
    """forward --rebuild-from DATE: recreate the paper forward account as the first routine run made it on DATE."""
    from .forward import format_rebuild
    from .forward_schedule import rebuild_for_settings

    try:
        summary = rebuild_for_settings(settings, prices, _market_holidays(settings), args.rebuild_from,
                                       universe=args.universe, top=args.top, capital=args.capital, force=args.force,
                                       progress=print)
    except (FileExistsError, ValueError, LookupError) as e:
        print(f"Not rebuilt: {e}", file=sys.stderr)
        return 1
    print(format_rebuild(summary))
    return 0


def _check_public_ip(settings: Any) -> int:
    from .netcheck import public_ip
    from .runner import groww_session
    try:
        ip = public_ip(groww_session(settings))
    except Exception as e:  # noqa: BLE001
        print(f"Could not find this machine's public IP: {e}")
        return 1
    print(f"Public IP seen from this machine: {ip}")
    allowed = settings.groww_allowed_ip
    if not allowed:
        print("GROWW_ALLOWED_IP is not set, so live orders are not restricted to one IP.")
        return 0
    if ip == allowed.strip():
        print(f"Matches GROWW_ALLOWED_IP ({allowed}).")
        return 0
    print(f"Does NOT match GROWW_ALLOWED_IP ({allowed}): Groww would reject live orders from here.")
    return 1


def cmd_groww_check(args: argparse.Namespace) -> int:
    """Check the live-trading assumptions against your Groww account (read-only by default)."""
    from .groww import InstrumentTicks
    from .groww_check import format_rows, live_test, read_only_checks, save
    from .runner import make_groww, token_cache
    settings = _settings(args)
    if args.symbol and not args.live_test:
        print("--symbol only applies with --live-test; ignoring it.", file=sys.stderr)
    if args.ip:  # needs no credentials: what IP does Groww see from this machine?
        return _check_public_ip(settings)
    if not settings.has_groww_credentials:
        print("No Groww credentials in .env.")
        return 1
    if args.live_test and not (settings.groww_live_orders and args.i_understand_real_orders):
        print("Refusing the live test: it places a REAL 1-share limit order (and a GTT) on Groww. "
              "It needs GROWW_LIVE_ORDERS=true and --i-understand-real-orders.")
        return 1
    if args.live_test:
        from .forward_telemetry import gate
        g = gate(settings.state_dir)
        if g and g.get("tripped") and not args.confirm_friction_override:
            print("Refusing the live test: " + g["text"] + " Add --confirm-friction-override to go ahead anyway.")
            return 1
    cache = token_cache(settings)
    if settings.groww_access_token:
        source = "GROWW_ACCESS_TOKEN in .env"
    elif settings.groww_api_key and cache.get(settings.groww_api_key):
        source = "cached token (no new generation used)"
    else:
        source = "newly generated from the API key (counts toward 150 a day)"
    if args.force:
        print(_FORCE_WARNING, file=sys.stderr)
    try:
        broker = make_groww(settings, force=True) if args.force else make_groww(settings)
    except (GrowwTokenUnavailable, requests.HTTPError) as e:
        print(_token_error_text(e), file=sys.stderr)
        return 2
    ticks = InstrumentTicks(settings.state_dir / "cache")
    c = read_only_checks(broker, token_source=source, cache=cache, api_key=settings.groww_api_key,
                         tick_fn=lambda sym: ticks.tick_size(sym, settings.groww_exchange),
                         ddpi_confirmed=settings.groww_ddpi_confirmed)
    if args.live_test:
        sym = (args.symbol or args.live_test).upper()
        print(f"Placing a REAL 1-share limit BUY of {sym} {args.offset_pct:g}% below "
              "the last price, nudging its limit up 0.5%, then cancelling it...")
        live_test(broker, sym, offset_pct=args.offset_pct, c=c)
    print(format_rows(c))
    st = State(settings.state_dir / "state.json")
    save(st, c, live=bool(args.live_test))
    st.save()
    return 0 if all(r["ok"] is not False for r in c.rows) else 2


def cmd_gtt(args: argparse.Namespace) -> int:
    """Show the Groww GTT stop-losses; --sync creates/raises/cancels them (live only)."""
    from .runner import free_prices, make_groww, make_notifier
    settings = _settings(args)
    st = State(settings.state_dir / "state.json")
    if args.sync:
        if not settings.groww_live_orders:
            print("Refusing: GROWW_LIVE_ORDERS is not true, so no GTT orders are placed.")
            return 1
        if not settings.groww_gtt_stops:
            print("Refusing: GROWW_GTT_STOPS is not true.")
            return 1
        prices = free_prices(settings)
        from .live import locked_gtt_sync
        for a in locked_gtt_sync(settings, make_groww(settings, prices), notifier=make_notifier(settings),
                                 bars_fn=lambda sym: prices.history(sym, "1y")) or []:
            print("  ", a)
        st = State(settings.state_dir / "state.json")
    stops = st.data.get("gtt_stops", {})
    if not stops:
        print("No GTT stop-losses recorded.")
    for sym, r in sorted(stops.items()):
        print(f"{sym:<12} qty {r.get('qty')}  trigger {r.get('trigger')}  limit {r.get('limit')}  "
              f"{r.get('status')}  {r.get('smart_order_id')}" + (f"  last error: {r['last_error']}" if r.get("last_error") else ""))
    return 0


def cmd_kill_switch(args: argparse.Namespace) -> int:
    """Stop all trading (state/HALT), or with --release allow it again."""
    from . import killswitch
    from .runner import make_groww, make_notifier
    settings = _settings(args)
    if args.release:
        if killswitch.release(settings, by="cli", notifier=make_notifier(settings)):
            print("HALT released: order paths, the watch and the CLI work again.")
        else:
            print("Not halted: nothing to release.")
        return 0
    if args.status:
        print(killswitch.reason(settings.state_dir) or "Not halted.")
        return 0
    report = killswitch.engage(settings, by="cli", cancel_gtt=args.cancel_gtt,
                               broker_fn=lambda: make_groww(settings, halt_ok=True), notifier=make_notifier(settings))
    print(killswitch.summary(report))
    return 0


# Under HALT the CLI refuses everything (exit 1) except the switch itself, the dashboard (its order routes refuse),
# the watch (it idles, so systemd / Docker do not restart it in a loop) and offline commands that cannot trade or
# reach Groww.
HALT_ALLOWED = {"kill-switch", "app-device", "ui", "watch", "history", "backtest", "momentum", "news", "screen", "scorecard",
                "factor-backtest", "fundamentals-history", "index-history", "signal-lab", "costs", "replay",
                "market-data", "insider-history", "research-gap", "research-stops", "research-portfolio",
                "research-pead", "research-coffee-can", "research-barrier", "research-cpcv", "research-meta",
                "research-model", "research-robustness", "research-regimes",
                "research-capacity", "research-leakage",
                "research-momentum", "research-holdout",
                "research-macro", "research-sectors", "research-backward", "research-replication",
                "research-batch3", "research-circuits"}


def _halt_refusal(args: argparse.Namespace) -> str | None:
    if args.command in HALT_ALLOWED:
        return None
    from . import killswitch
    try:
        state_dir = _settings(args).state_dir
    except (SystemExit, Exception):  # noqa: BLE001 - settings that do not load: the default place
        import os
        state_dir = Path(os.environ.get("STATE_DIR") or "state")
    return killswitch.reason(state_dir, f"run `{args.command}`")


def cmd_app_device(args: argparse.Namespace) -> int:
    """Owner-only: manage the phone app's device secrets. Offline; reads no .env (hence --state-dir)."""
    from . import app_auth
    state_dir = Path(args.state_dir)
    if args.action == "add":
        try:
            secret = app_auth.add_device(state_dir, args.name)
        except (ValueError, RuntimeError) as e:
            print(e, file=sys.stderr)
            return 1
        print(f"Device {args.name!r} added. Its secret (enter it in the app):\n\n  {secret}\n\n"
              "Copy it now; it is not shown again.")
        return 0
    if args.action == "list":
        for d in app_auth.list_devices(state_dir):
            print(f"{d['name']}  (added {d['created']})")
        return 0
    if not args.name and not args.all:
        print("revoke needs a NAME or --all", file=sys.stderr)
        return 1
    try:
        n = app_auth.revoke(state_dir, args.name, all_=args.all)
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 1
    print(f"revoked {n} device(s)")
    return 0 if n else 1


def cmd_history(args: argparse.Namespace) -> int:
    settings = load_settings()
    path = settings.state_dir / "state.json"
    if not path.exists():
        print("No state yet. Run `check` first.")
        return 0
    data = json.loads(path.read_text())
    recs = data.get("recommendations", [])[-args.limit:]
    if not recs:
        print("No recommendations recorded yet.")
    for r in recs:
        print(f"{r['at']}  {r['action'].upper():<5} {r['ticker']:<8} [{r['confidence']}] {r['headline']}")
    runs = data.get("runs", [])
    print(f"\nRuns recorded: {len(runs)}  (trades remembered: {len(data.get('seen', {}))})")
    costs = [r["usage"]["cost_usd"] for r in runs if (r.get("usage") or {}).get("cost_usd") is not None]
    if costs:
        print(f"Claude API spend over {len(costs)} runs: about ${sum(costs):.2f} (avg ${sum(costs)/len(costs):.3f} a run)")
    return 0


def cmd_reset(args: argparse.Namespace) -> int:
    settings = load_settings()
    removed = []
    for name in ("state.json", "paper_broker.json"):
        p = settings.state_dir / name
        if p.exists():
            p.unlink()
            removed.append(str(p))
    print("Removed: " + (", ".join(removed) if removed else "nothing to remove"))
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    """Replay an investor's disclosed deals against subsequent returns vs NIFTY 50."""
    from .backtest import format_summary, run_backtest, run_backtest_followed
    from .runner import free_prices, make_data_source

    settings = _settings(args)
    names = _investor_names(args) or settings.investors
    horizons = tuple(int(h) for h in args.horizons.split(","))
    if args.demo:
        import dataclasses
        import datetime as dt
        deals, broker = _demo_inputs(settings)
        # Sample deals are dated today; pretend they happened a quarter ago so there is
        # price history after them to measure.
        back = (dt.date.today() - dt.timedelta(days=100)).isoformat()
        deals = [dataclasses.replace(d, transaction_date=back, report_date=back)
                 for d in filter_by_investors(deals, names)]
        prices: Any = _DemoHistory(broker.price_fn)
    else:
        data = make_data_source(settings)
        deals = fetch_followed(data, names, settings.watch_source,
                               days=args.days if settings.data_source == "nse" else None)
        prices = free_prices(settings)
    print(f"{len(deals)} disclosed deals by {', '.join(names)} in the last {args.days} days; pricing…")
    if len(names) == 1:
        result = run_backtest(names[0], deals, prices, horizons=horizons, cost_bps=args.cost_bps,
                              benchmark=args.benchmark)
    else:
        result = run_backtest_followed(names, deals, prices, horizons=horizons, cost_bps=args.cost_bps,
                                       benchmark=args.benchmark)
    print(format_summary(result.summary()))
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, default=str))
    return 0


class _DemoHistory:
    """Synthetic 2-year history for demo mode: a gentle drift around the fixture price."""

    def __init__(self, price_fn):
        self.price_fn = price_fn

    def history(self, symbol, range_="2y"):
        import datetime as dt
        import math
        try:
            last = float(self.price_fn(symbol.replace("^", "IDX_")))
        except Exception:  # noqa: BLE001
            last = 100.0 if symbol.startswith("^") else 50.0
        out = []
        today = dt.date.today()
        n = 500
        drift = 0.6 + (sum(map(ord, symbol)) % 7) / 10  # 0.6 .. 1.2, fixed per symbol
        for i in range(n):
            d = today - dt.timedelta(days=int((n - i) * 365 / 252))
            px = last * (drift + (1 - drift) * i / n) * (1 + 0.03 * math.sin(i / 9))
            out.append({"date": d.isoformat(), "close": px, "adj_close": px, "volume": 1e5})
        return out


def cmd_momentum(args: argparse.Namespace) -> int:
    from .momentum import MomentumScreen, momentum_summary
    from .runner import free_prices
    settings = _settings(args)
    screen = MomentumScreen(free_prices(settings))
    for t in args.tickers:
        stats = screen.stats(t)
        print(f"{t.upper():<12} {stats.get('error') or momentum_summary(stats)}")
    return 0


def _short_link(link: str, source: str) -> str:
    """Google News links are ~600 characters of redirect: print the publisher; others host + path, 80 characters."""
    from urllib.parse import urlparse
    u = urlparse(link)
    if u.netloc.endswith("news.google.com"):
        return f"via {source} (Google News)"
    text = u.netloc + u.path
    return text if len(text) <= 80 else text[:79] + "…"


def cmd_news(args: argparse.Namespace) -> int:
    from .news import NewsService, make_tagger
    settings = _settings(args)
    if args.check_tagger:
        tagger = make_tagger(settings)
        print(f"NEWS_TAGGER={settings.news_tagger}: active tagger is {tagger.name}.")
        if settings.news_tagger in ("auto", "ollama"):
            from .news import OllamaTagger
            o = OllamaTagger(settings.ollama_url, settings.ollama_model)
            if o.available():
                print(f"Ollama at {settings.ollama_url} is running and has {settings.ollama_model}.")
            else:
                print(f"Ollama at {settings.ollama_url} is not ready for {settings.ollama_model}. "
                      f"Start it with `ollama serve` and fetch the model with `ollama pull {settings.ollama_model}`.")
        elif settings.news_tagger == "claude":
            print(f"Claude ({settings.news_claude_model}) labels headlines; this uses your API credit.")
        return 0
    if not args.symbol:
        print("Give a SYMBOL, or use --check-tagger.")
        return 2
    symbol, name = args.symbol.upper(), None
    svc = NewsService(settings)
    try:
        from .instruments import CompanyNames
        symbol, name = CompanyNames(settings.state_dir / "cache").resolve(args.symbol)
    except Exception:  # noqa: BLE001 - names are a nicety
        pass
    res = svc.for_symbol(symbol, name)
    print(f"{symbol}{' - ' + name if name else ''}: {len(res['items'])} headline(s), last 7 days (tagger: {res['tagger']})")
    for i in res["items"]:
        tag = f"{i['sentiment']}/{i['event']}/{i['confidence']}" if i["sentiment"] else "untagged"
        print(f"  {i['published'][:16].replace('T', ' ')} IST  {i['source']}  [{tag}]\n    {i['title']}\n"
              f"    {_short_link(i['link'], i['source'])}")
    for e in res["errors"]:
        print(f"  note: {e}")
    return 0


def cmd_screen(args: argparse.Namespace) -> int:
    """Rank an NSE index universe on momentum, trend, low volatility and liquidity."""
    from .runner import free_prices
    from .screen import format_screen, load_universe, run_screen
    from .bands import book_for
    from .indian_filters import filters_for

    settings = _settings(args)
    members = load_universe(args.universe)
    print(f"Scoring {len(members)} stocks in {args.universe.upper()} (price history via Yahoo, cached)…")
    fundamentals = None
    if args.quality or args.value:
        from .fundamentals import YahooFundamentals
        fundamentals = YahooFundamentals(settings.state_dir / "cache")
        print("Adding fundamentals from Yahoo (cached for a day; the first run takes about a minute)…")
    result = run_screen(members, free_prices(settings), top=args.top, workers=args.workers,
                        require_above_200dma=not args.no_trend_filter, fundamentals=fundamentals,
                        quality=1.0 if args.quality else 0.0, value=1.0 if args.value else 0.0,
                        bands=book_for(settings), filters=filters_for(settings))
    print(format_screen(result, top=args.top))
    if args.json:
        result.pop("all", None)
        print(json.dumps(result, indent=2, default=str))
    return 0


def cmd_scorecard(args: argparse.Namespace) -> int:
    """How Claude's past recommendations did against the index."""
    from .costs import cost_model_for
    from .runner import free_prices
    from .scorecard import format_scorecard, score_recommendations
    from .state import State

    settings = _settings(args)
    st = State(settings.state_dir / "state.json")
    cm = cost_model_for(settings.market)
    from .scorecard import build_memberships
    r = score_recommendations(st.data.get("recommendations", []), free_prices(settings),
                              benchmark="^NSEI" if settings.market == "in" else "^GSPC",
                              cost_model=cm if hasattr(cm, "round_trip") else None,
                              memberships=build_memberships(settings.state_dir, progress=print)
                              if settings.market == "in" and st.data.get("recommendations") else None)
    print(format_scorecard(r))
    if args.json:
        print(json.dumps(r, indent=2, default=str))
    return 0


def cmd_replay_blind_test(args: argparse.Namespace) -> int:
    """Does Ask Claude answer differently when it cannot tell which company it is? (costs API money)"""
    from .replay.blind import run_cli

    return run_cli(args, _settings(args))


def cmd_replay_debate_test(args: argparse.Namespace) -> int:
    """Research: a bull-vs-bear debate before the recommendation vs today's single call, on blind cases (costs API money)."""
    from .replay.debate import run_cli

    return run_cli(args, _settings(args))


def cmd_signal_lab(args: argparse.Namespace) -> int:
    """Test whether common algo-trading signals predicted next-period returns."""
    from .costs import cost_model_for
    from .index_history import point_in_time
    from .runner import free_prices
    from .screen import load_universe
    from .signal_lab import SIGNALS, format_signal_lab, run_signal_lab

    settings = _settings(args)
    members = load_universe(args.universe)
    membership = None if args.todays_members else point_in_time(
        args.universe, [m["symbol"] for m in members], settings.state_dir, changes_csv=args.changes, progress=print)
    horizons = [int(h) for h in str(args.horizons).split(",") if h.strip()]
    print(f"Testing {len(SIGNALS)} signals and a walk-forward model on {args.universe.upper()} over {args.years} years…")
    r = run_signal_lab(members, free_prices(settings), horizons=horizons, years=args.years,
                       membership=membership, cost_model=cost_model_for("in"))
    print(format_signal_lab(r))
    if args.json:
        print(json.dumps(r, indent=2, default=str))
    return 0


def cmd_index_history(args: argparse.Namespace) -> int:
    """Rebuild past index membership from NSE Indices press releases and check it."""
    from .index_history import SIZES, build_history
    from .screen import load_universe

    settings = _settings(args)
    for idx in args.index:
        current = [m["symbol"] for m in load_universe(idx)]
        print(f"{idx.upper()}: downloading and parsing NSE Indices press releases since {args.since}…")
        meta = build_history(idx, current, settings.state_dir, since=args.since)
        size = SIZES.get(meta["index"])
        print(f"  {meta['changes']} change dates, {meta['stocks_added']} stocks added; saved to {meta['path']}")
        if meta["problems"]:
            print(f"  {len(meta['problems'])} inconsistencies (index not {size} stocks, or a change that does not fit):")
            for p in meta["problems"][:10]:
                print("   ", p)
        else:
            print(f"  Checked: exactly {size} members on every date, every change consistent.")
    return 0


def cmd_factor_backtest(args: argparse.Namespace) -> int:
    """Backtest the factor screen as a monthly-rebalanced portfolio with real charges."""
    from .costs import cost_model_for
    from .factor_backtest import (INDEX_FUNDS, format_factor_backtest, format_validation, run_factor_backtest,
                                  validate_factor_backtest)
    from .index_history import point_in_time
    from .runner import free_prices
    from .screen import load_universe

    settings = _settings(args)
    members = load_universe(args.universe)
    print(f"Backtesting top {args.top} of {args.universe.upper()} over {args.years} years (monthly rebalance)…")
    membership = None if args.todays_members else point_in_time(
        args.universe, [m["symbol"] for m in members], settings.state_dir, changes_csv=args.changes, progress=print)
    funds = None
    if args.quality or args.value:
        from .fundamentals_history import ResultsHistory
        # cache only: run `fundamentals-history` first, so a half-filled cache is visible, not silent
        funds = ResultsHistory(settings.state_dir / "cache", max_new_downloads=0)
    prices = free_prices(settings)

    def run_top(n: int) -> dict[str, Any]:
        return run_factor_backtest(members, prices, top=n, years=args.years,
                                   cost_model=cost_model_for("in"), capital=settings.paper_starting_cash,
                                   require_above_200dma=not args.no_trend_filter, benchmark=args.benchmark,
                                   membership=membership, index_fund=INDEX_FUNDS.get(args.universe.upper()),
                                   fundamentals=funds, quality=1.0 if args.quality else 0.0,
                                   value=1.0 if args.value else 0.0)

    r = run_top(args.top)
    print(format_factor_backtest(r))
    if args.validate:
        r["validation"] = validate_factor_backtest(run_top, args.top, base=r, universe=args.universe,
                                                  progress=print)
        print("\n" + format_validation(r["validation"]))
    fx = r.get("fundamentals")
    if fx:
        print(f"\nFundamentals ({fx['source']}): quality x{fx['quality']:g}, value x{fx['value']:g}; "
              f"{fx['avg_coverage']*100:.0f}% of eligible names had results on an average rebalance. {fx['note']}")
        if fx["avg_coverage"] < 0.8:
            print(f"Coverage is low: run `python -m trading_agent fundamentals-history --universe {args.universe}` "
                  "(repeat until it reports nothing left) and backtest again.")
    if args.json:
        print(json.dumps(r, indent=2, default=str))
    return 0


def cmd_research_gap(args: argparse.Namespace) -> int:
    """Research A: split the screen's shortfall to its index fund into cash timing, stock selection and charges."""
    from .costs import cost_model_for
    from .factor_backtest import INDEX_FUNDS, run_factor_backtest
    from .gap_diagnosis import format_diagnosis, diagnose, log_trial
    from .index_history import point_in_time
    from .runner import free_prices
    from .screen import load_universe

    from .gap_diagnosis import HOLDOUT_START, LIQUID_RATE
    args.until = args.until or HOLDOUT_START
    args.liquid_rate = LIQUID_RATE if args.liquid_rate is None else args.liquid_rate
    settings = _settings(args)
    universe = args.universe.upper()
    if universe not in INDEX_FUNDS:
        print(f"{universe} has no index fund to compare with; use one of {', '.join(INDEX_FUNDS)}")
        return 2
    members = load_universe(universe)
    print(f"Research A: top {args.top} of {universe}, {args.years} years ending before {args.until} (monthly)…")
    membership = point_in_time(universe, [m["symbol"] for m in members], settings.state_dir, progress=print)
    r = run_factor_backtest(members, free_prices(settings), top=args.top, years=args.years,
                            cost_model=cost_model_for("in"), capital=settings.paper_starting_cash,
                            membership=membership, index_fund=INDEX_FUNDS[universe], until=args.until)
    try:
        d = diagnose(r, liquid_rate=args.liquid_rate)
    except ValueError as e:
        print(f"Cannot split the gap: {e}")
        return 1
    if not r.get("point_in_time"):
        print(f"Note: {r['caveat']}")
    print(format_diagnosis(d))
    path = log_trial(settings.state_dir, "A-gap", {"universe": universe, "top": args.top, "years": args.years,
                                                   "until": args.until, "liquid_rate": args.liquid_rate},
                     {k: d["cash_at_zero"][k] for k in ("strategy", "index_fund", "timing", "selection", "charges")})
    print(f"Logged in {path}")
    if args.json:
        print(json.dumps(d, indent=2, default=str))
    return 0


def _research_market(args: argparse.Namespace, settings: Any) -> Any:
    """The Midcap 150 (or another index with a fund) on the daily simulator, holdout sealed."""
    from .factor_backtest import INDEX_FUNDS
    from .index_history import point_in_time
    from .research_sim import load_market
    from .runner import free_prices
    from .screen import load_universe
    universe = args.universe.upper()
    if universe not in INDEX_FUNDS:
        raise SystemExit(f"{universe} has no index fund; use one of {', '.join(INDEX_FUNDS)}")
    members = load_universe(universe)
    membership = point_in_time(universe, [m["symbol"] for m in members], settings.state_dir, progress=print)
    flows: dict[str, float] = {}
    try:
        raw = json.loads((settings.state_dir / "flows.json").read_text(encoding="utf-8"))
        flows = {d: r["fii_net"] + r["dii_net"] for d, r in (raw.get("days") or {}).items()}
    except (OSError, ValueError, KeyError, TypeError):
        pass
    print(f"Loading {universe} prices for {args.years} years before {args.until} (this takes a few minutes)…")
    m = load_market([x["symbol"] for x in members], free_prices(settings), regime=INDEX_FUNDS[universe],
                    years=args.years, until=args.until, membership=membership, flows=flows)
    if m.missing:
        print(f"No price history for {len(m.missing)} past member(s): {', '.join(m.missing[:12])}"
              + (" …" if len(m.missing) > 12 else ""))
    if not getattr(args, "skip_leakage_guard", False):
        # the one sanctioned holdout run loads the sealed months on purpose; every other look-ahead check still runs
        _leakage_guard(m, holdout_start="9999-12-31" if getattr(args, "opening_holdout", False) else None)
    return m


def _leakage_guard(m: Any, holdout_start: str | None = None) -> None:
    """Stop the run if a quick leakage check finds a decision that used data from after its day."""
    from .costs import cost_model_for
    from .research_leakage import LeakageError, guard
    print("Checking for look-ahead (six decision days, two simulation cuts)…")
    try:
        guard(m, cost_model=cost_model_for("in"), holdout_start=holdout_start)
    except LeakageError as e:
        print(str(e))
        raise SystemExit("Stopped: a decision used data published after it. Fix the leak before trusting this run.") from e


def cmd_research_momentum(args: argparse.Namespace) -> int:
    """Momentum robustness: other lookbacks, rebalance spacings and size halves of the adopted rules."""
    from .costs import cost_model_for
    from .gap_diagnosis import log_trial
    from .research_studies import format_momentum_robustness, momentum_robustness, prior_trials
    settings = _settings(args)
    m = _research_market(args, settings)
    r = momentum_robustness(m, cost_model=cost_model_for("in"), capital=settings.paper_starting_cash,
                            extra_trials=prior_trials(settings.state_dir), progress=print)
    print(format_momentum_robustness(r))
    for name, x in r["results"].items():
        log_trial(settings.state_dir, "momentum_robustness", {"variant": name, "until": args.until},
                  {"return": x["total_return"], "excess": x["excess"], "max_drawdown": x["max_drawdown"],
                   "skill": x["skill"].get("probability")})
    if args.json:
        print(json.dumps({**r, "results": {k: {kk: vv for kk, vv in v.items() if kk != "skill"}
                                           for k, v in r["results"].items()}}, default=str))
    return 0


def _once_used(settings: Any, study: str) -> bool:
    """A one-time study already run (in the trial log) or claimed (its marker exists)? Prints why when it was."""
    from .gap_diagnosis import once_marker, trial_logged
    if trial_logged(settings.state_dir, study):
        print(f"{study} was already run (see the trial log): it is used once only.")
        return True
    marker = once_marker(settings.state_dir, study)
    if marker.exists():
        print(f"{study} was already claimed ({marker}): it is used once only. If that run crashed before a result, "
              "the window is spent; only you can decide to delete the marker.")
        return True
    return False


def _claim_once(settings: Any, study: str) -> bool:
    """Claim a one-time study before it runs (exclusive create); False, with the reason printed, if it was claimed."""
    from .gap_diagnosis import claim_once
    try:
        path = claim_once(settings.state_dir, study)
    except FileExistsError:
        _once_used(settings, study)
        return False
    print(f"Claimed {study} ({path}): this is the only run. If it stops before printing a result, the window is "
          "still spent.")
    return True


def cmd_research_circuits(args: argparse.Namespace) -> int:
    """Circuit-lock fill audit of the adopted rules on the development years (a diagnostic, not a trial)."""
    from .costs import cost_model_for
    from .research_circuits import circuit_audit, format_circuit_audit
    settings = _settings(args)
    from .runner import free_prices
    m = _research_market(args, settings)
    prices = free_prices(settings)   # the simulator's daily history holds closes only: open/high/low come from here
    print("Loading open/high/low for the traded stocks…")
    r = circuit_audit(m, cost_model=cost_model_for("in"), capital=settings.paper_starting_cash,
                      ohlc=lambda sym: prices.history_ohlc(sym, "10y", ttl=7 * 86400))
    print(format_circuit_audit(r))
    if args.json:
        print(json.dumps(r, default=str))
    return 0


def cmd_research_batch3(args: argparse.Namespace) -> int:
    """Pre-registered batch 3 (smooth momentum F, institutional buying G) on the development years, ONCE. Refused
    without --confirm and after the first time (the trial log records it)."""
    from .costs import cost_model_for
    from .gap_diagnosis import log_trial
    from .nse import NSEClient
    from .research_batch3 import (PREREGISTRATION, STUDY, DealHistoryError, batch3, fetch_deal_history,
                                  format_batch3, institutional_flows)
    from .research_studies import HOLDOUT_START, prior_trials
    settings = _settings(args)
    if _once_used(settings, STUDY):
        return 2
    if not args.confirm:
        print(f"This runs pre-registered batch 3 ({PREREGISTRATION}) on Oct 2020 to Sep 2025, ONCE. The result is "
              "final and the batch is spent. Run again with --confirm.")
        return 2
    print("Loading NSE bulk and block deals, Oct 2019 to Sep 2025, in 90-day windows (cached once)…")
    try:
        rows = fetch_deal_history(NSEClient(cache_dir=settings.state_dir / "cache"),
                                  settings.state_dir / "research" / "deals", progress=print)
    except DealHistoryError as e:
        print(f"Could not load the deal history ({e}). Batch 3 did not run; nothing is spent.")
        return 1
    flows, skipped = institutional_flows(rows)
    note = (f"{len(rows):,} deals read, {sum(len(v) for v in flows.values()):,} by FUND / INSURANCE / PENSION clients "
            f"in {len(flows)} stocks, {skipped} skipped for a missing quantity, price, side or date.")
    print(note)
    args.universe, args.years, args.until = "NIFTYMIDCAP150", 5, HOLDOUT_START
    m = _research_market(args, settings)
    if not _claim_once(settings, STUDY):   # after the data loaded: a refused download does not spend it
        return 2
    r = batch3(m, flows, cost_model=cost_model_for("in"), capital=settings.paper_starting_cash,
               extra_trials=prior_trials(settings.state_dir))
    for k, x in r["results"].items():   # logged before printing: once run, the batch is spent
        log_trial(settings.state_dir, STUDY, {"variant": k, "name": x["name"], "from": r["start"], "to": r["end"]},
                  {"return": x["total_return"], "excess": x["excess"], "cagr": x["cagr"],
                   "max_drawdown": x["max_drawdown"], "turnover": x["turnover"], "passes": x.get("passes"),
                   "skill": x["skill"].get("probability")})
    print(format_batch3(r, note))
    if args.json:
        print(json.dumps({**r, "results": {k: {kk: vv for kk, vv in v.items() if kk != "skill"}
                                           for k, v in r["results"].items()}}, default=str))
    return 0


def cmd_research_replication(args: argparse.Namespace) -> int:
    """The adopted rules on US, Japanese or UK midcaps, ONCE per market (pre-registered 18 Oct 2026). Refused without
    --confirm and after the first time (the trial log records it)."""
    from .gap_diagnosis import log_trial
    from .research_replication import (MARKETS, WINDOW_END, fetch_members, format_replication,
                                       load_replication_market, prices_for, replicate)
    from .research_studies import prior_trials
    settings = _settings(args)
    market = args.replication_market.lower()
    spec = MARKETS[market]
    study = spec["study"]
    if _once_used(settings, study):
        return 2
    if not args.confirm:
        print(f"This runs the pre-registered replication ({spec['plan']}) on the {spec['index']} ({spec['name']}), "
              f"Oct 2016 to Sep 2026, ONCE. The result is final and this market is spent. Run again with --confirm.")
        return 2
    print(f"Loading today's {spec['index']} members…")
    current, changes = fetch_members(market)
    if len(current) < spec["min_members"]:
        print(f"Only {len(current)} members were read; not running (nothing is spent).")
        return 1
    print(f"Loading prices for {len(current)} members and {spec['benchmark']} since 2015 (this takes a few minutes)…")
    m, notes = load_replication_market(market, prices_for(market, settings.state_dir), current, changes)
    _leakage_guard(m, holdout_start=WINDOW_END)
    from .research_replication import data_problems
    problems = data_problems(m)
    if problems:   # a data fault must never spend the market: stop before the claim
        print("The data cannot carry this test, so it is not run (nothing is spent):")
        for x in problems:
            print(f"  - {x}")
        return 1
    if m.unit_jumps:   # a pence/pound mix-up would fake a 100x move; stop before anything is spent
        print(f"Prices jump more than 50x in a day for {len(m.unit_jumps)} stocks, more than 10% of them "
              f"({', '.join(m.unit_jumps[:12])}): the data cannot be trusted. "
              "Not running (nothing is spent).")
        return 1
    if not _claim_once(settings, study):   # after the data loaded: a refused download does not spend it
        return 2
    r = replicate(market, m, extra_trials=prior_trials(settings.state_dir))
    log_trial(settings.state_dir, study, {"market": market, "from": r["start"], "to": r["end"], "rules": r["rules"],
                                          "point_in_time": r["point_in_time"]},
              {"return": r["total_return"], "excess": r["excess"], "cagr": r["cagr"], "max_drawdown": r["max_drawdown"],
               "turnover": r["turnover"], "passes": r["passes"], "skill": (r["skill"] or {}).get("probability")})
    print(format_replication(r, notes))
    if args.json:
        print(json.dumps({k: v for k, v in r.items() if k != "skill"}, default=str))
    return 0


def cmd_research_holdout(args: argparse.Namespace) -> int:
    """Open the sealed holdout ONCE: the adopted rules, unchanged, on the months since 1 Oct 2025. Refused after the
    first time (the trial log records it) and without --confirm."""
    from .costs import cost_model_for
    from .gap_diagnosis import log_trial
    from .research_studies import HOLDOUT_START, HOLDOUT_STUDY, format_holdout, holdout
    settings = _settings(args)
    if _once_used(settings, HOLDOUT_STUDY):
        return 2
    if not args.confirm:
        print("This opens the sealed holdout (1 Oct 2025 to today) for the adopted rules. It can be done ONCE and the "
              "result is final. Run again with --confirm to open it.")
        return 2
    args.until, args.years, args.opening_holdout = None, 2, True   # the holdout plus the ranking's lookback year
    m = _research_market(args, settings)
    m.start_day = HOLDOUT_START
    if not _claim_once(settings, HOLDOUT_STUDY):   # after the data loaded: a refused download does not spend it
        return 2
    r = holdout(m, cost_model=cost_model_for("in"), capital=settings.paper_starting_cash)
    for name, x in r["results"].items():   # logged before printing: once opened, it is spent
        log_trial(settings.state_dir, HOLDOUT_STUDY, {"variant": name, "from": r["start"], "to": r["end"]},
                  {"return": x["total_return"], "excess": x["excess"], "max_drawdown": x["max_drawdown"],
                   "fund_max_drawdown": x["fund_max_drawdown"], "passes": r["passes"] if name == "adopted" else None})
    print(format_holdout(r))
    if args.json:
        print(json.dumps(r, default=str))
    return 0


def cmd_research_macro(args: argparse.Namespace) -> int:
    """Macro diagnostic: the adopted rules around oil moves, rupee moves and RBI rate changes (not a filter)."""
    from .costs import cost_model_for
    from .research_macro import OIL, RUPEE, format_macro, macro_diagnostic
    from .runner import free_prices
    settings = _settings(args)
    m = _research_market(args, settings)
    prices = free_prices(settings)
    try:
        oil, rupee = prices.history(OIL, "10y"), prices.history(RUPEE, "10y")
    except Exception as e:  # noqa: BLE001 - say which quote is missing instead of a traceback
        print(f"Could not load Brent ({OIL}) or USD/INR ({RUPEE}) history: {type(e).__name__}: {e}")
        return 1
    until = args.until or "9999-12-31"
    oil, rupee = [b for b in oil if b["date"] < until], [b for b in rupee if b["date"] < until]   # holdout stays sealed
    r = macro_diagnostic(m, oil, rupee, cost_model=cost_model_for("in"), capital=settings.paper_starting_cash)
    print(format_macro(r))
    if args.json:
        print(json.dumps({k: v for k, v in r.items() if k != "months"}, default=str))
    return 0


def cmd_research_sectors(args: argparse.Namespace) -> int:
    """Sector concentration: how crowded the adopted rules' holdings were in one industry (a diagnostic)."""
    from .costs import cost_model_for
    from .research_sectors import format_sectors, industry_map, sector_diagnostic
    from .screen import load_universe
    settings = _settings(args)
    m = _research_market(args, settings)
    industry, failed = industry_map(load_universe)
    if failed:
        print(f"Could not load NSE's {', '.join(failed)} list(s); industries come from the rest.")
    if not industry:
        print("No industry list could be loaded from NSE; try again later.")
        return 1
    r = sector_diagnostic(m, industry, cost_model=cost_model_for("in"), capital=settings.paper_starting_cash)
    print(format_sectors(r))
    if args.json:
        print(json.dumps({k: v for k, v in r.items() if k != "rebalances"}, default=str))
    return 0


def cmd_research_backward(args: argparse.Namespace) -> int:
    """The pre-registered backward check (Apr 2017 to Sep 2020), ONCE: the adopted rules and four variants. Refused
    without --confirm and after the first time (the trial log records it)."""
    from .costs import cost_model_for
    from .gap_diagnosis import log_trial
    from .index_history import build_history
    from .membership import membership_for
    from .research_backward import (PREREGISTRATION, STUDY, TRADING_FROM, WINDOW_END, backward_batch, fetch_tri,
                                    format_backward, load_backward_market, tri_from_csv)
    from .research_sectors import industry_map
    from .research_studies import prior_trials
    from .runner import free_prices
    from .screen import load_universe
    settings = _settings(args)
    if _once_used(settings, STUDY):
        return 2
    if not args.confirm:
        print(f"This runs the pre-registered backward check ({PREREGISTRATION}) on Apr 2017 to Sep 2020, ONCE. The "
              "result is final and the window is spent. Run again with --confirm to run it.")
        return 2
    universe = "NIFTYMIDCAP150"
    current = [x["symbol"] for x in load_universe(universe)]
    print("Rebuilding the Midcap 150's members since 2016 from NSE's index notices (downloaded once)…")
    meta = build_history(universe, current, settings.state_dir / "backward", since="2016-01-01", progress=print)
    membership = membership_for(universe, current, meta["path"])
    if membership is None or membership.known_since > TRADING_FROM:
        print(f"Membership is only known from {membership.known_since if membership else 'never'}; the window starts "
              f"{TRADING_FROM}. Not running.")
        return 1
    notes = [f"rebuilt from {meta['changes']} index changes since {membership.known_since}."]
    if meta["problems"]:
        notes.append(f"{len(meta['problems'])} inconsistencies remain (the latest at "
                     f"{max(p['date'] for p in meta['problems'])}): a stock or two may be counted wrongly there.")
    print("Loading the Nifty Midcap 150 total return index…")
    tri = tri_from_csv(args.tri_csv) if args.tri_csv else fetch_tri(settings.state_dir / "cache")
    print("Loading prices for every member since 2016 (this takes a few minutes)…")
    m = load_backward_market(membership, free_prices(settings), tri)
    from .research_replication import data_problems
    problems = data_problems(m, trading_from=TRADING_FROM, latest_start="2017-07-01")
    if problems:   # a data fault must never spend the window: stop before the claim
        print("The data cannot carry this test, so it is not run (nothing is spent):")
        for x in problems:
            print(f"  - {x}")
        return 1
    if m.excluded:
        notes.append(f"{len(m.excluded)} stock(s) left out for a one-day move of more than 50x (a data error): "
                     f"{', '.join(m.excluded[:12])}.")
    notes.append("rerun with daily prices (docs/research/rerun-addendum-2026-10-18c.md); the first run used "
                 "monthly bars and is void.")
    _leakage_guard(m, holdout_start=WINDOW_END)
    industries, failed = industry_map(load_universe)
    if failed:
        print(f"Could not load NSE's {', '.join(failed)} list(s); industries come from the rest.")
    if not _claim_once(settings, STUDY):   # after the data loaded: a refused download does not spend it
        return 2
    r = backward_batch(m, industries, cost_model=cost_model_for("in"), capital=settings.paper_starting_cash,
                       extra_trials=prior_trials(settings.state_dir))
    for vid, x in r["results"].items():   # logged before printing: once run, the window is spent
        log_trial(settings.state_dir, STUDY, {"variant": vid, "name": x["name"], "from": r["start"], "to": r["end"]},
                  {"return": x["total_return"], "excess": x["excess"], "cagr": x["cagr"],
                   "max_drawdown": x["max_drawdown"], "turnover": x["turnover"], "passes": x["passes"],
                   "skill": x["skill"].get("probability")})
    print(format_backward(r, notes))
    if args.json:
        print(json.dumps({**r, "results": {k: {kk: vv for kk, vv in v.items() if kk != "skill"}
                                           for k, v in r["results"].items()}}, default=str))
    return 0


def cmd_research_leakage(args: argparse.Namespace) -> int:
    """The full leakage test: every rebalance day and four end-to-end cuts of the adopted rules."""
    from .costs import cost_model_for
    from .research_leakage import format_leakage, leakage_check
    settings = _settings(args)
    args.skip_leakage_guard = True
    m = _research_market(args, settings)
    r = leakage_check(m, cost_model=cost_model_for("in"), ranking_days=args.days, simulation_days=args.cuts,
                      progress=print)
    print(format_leakage(r))
    if args.json:
        print(json.dumps(r, default=str))
    return 0 if r["ok"] else 1


def cmd_research_stops(args: argparse.Namespace) -> int:
    """Research B1: six stop rules over one-year windows, accepted by the rules set before the run."""
    from .costs import cost_model_for
    from .gap_diagnosis import log_trial
    from .research_studies import compact, format_stop_race, prior_trials, stop_race
    settings = _settings(args)
    m = _research_market(args, settings)
    r = stop_race(m, cost_model=cost_model_for("in"), capital=settings.paper_starting_cash,
                  extra_trials=prior_trials(settings.state_dir), progress=print)
    print(format_stop_race(r))
    for rule, x in r["rules"].items():
        log_trial(settings.state_dir, "B1-stops", {"rule": rule, "until": args.until},
                  {"median_hold_days": x["median_hold_days"], "excess": x["whole"]["excess"],
                   "verdict": x["verdict"]})
    if args.json:
        print(compact(r))
    return 0


def cmd_research_portfolio(args: argparse.Namespace) -> int:
    """Research B2-B4: rank buffer, tax-aware buffer, drawdown brake variants, inverse-volatility weights."""
    from .costs import cost_model_for
    from .gap_diagnosis import log_trial
    from .research_studies import compact, format_controls, portfolio_controls, prior_trials
    settings = _settings(args)
    m = _research_market(args, settings)
    r = portfolio_controls(m, cost_model=cost_model_for("in"), stop=args.stop, capital=settings.paper_starting_cash,
                           extra_trials=prior_trials(settings.state_dir), progress=print)
    print(format_controls(r))
    for name, x in r["results"].items():
        log_trial(settings.state_dir, "B2-B4", {"variant": name, "stop": args.stop, "until": args.until},
                  {"return": x["total_return"], "excess": x["excess"], "max_drawdown": x["max_drawdown"],
                   "passed": r["passed"].get(name)})
    if args.json:
        print(compact(r))
    return 0


def cmd_research_robustness(args: argparse.Namespace) -> int:
    """The forward test's rules under harder assumptions: next-day fills, 1.5x and 2x costs; survivorship count."""
    from .costs import cost_model_for
    from .gap_diagnosis import log_trial
    from .research_studies import format_robustness, prior_trials, robustness
    settings = _settings(args)
    m = _research_market(args, settings)
    r = robustness(m, cost_model=cost_model_for("in"), capital=settings.paper_starting_cash,
                   extra_trials=prior_trials(settings.state_dir), progress=print)
    print(format_robustness(r))
    for name, x in r["results"].items():
        log_trial(settings.state_dir, "robustness", {"variant": name, "until": args.until},
                  {"return": x["total_return"], "excess": x["excess"], "max_drawdown": x["max_drawdown"],
                   "skill": x["skill"].get("probability")})
    if args.json:
        print(json.dumps({**r, "results": {k: {kk: vv for kk, vv in v.items() if kk != "skill"}
                                           for k, v in r["results"].items()}}, default=str))
    return 0


def cmd_model_paper(args: argparse.Namespace) -> int:
    """The prediction model's paper account: show it, or with --run do today's after-close run now (paper only)."""
    from .model_paper import ModelPaper, format_model_paper, forward_return, run_model_paper_due
    from .runner import free_prices
    settings = _settings(args)
    prices = free_prices(settings)
    if args.run:
        print(run_model_paper_due(settings, prices, _market_holidays(settings)))
        return 0
    mp = ModelPaper(settings.state_dir, universe=settings.forward_universe, capital=settings.paper_starting_cash,
                    price_fn=prices.latest_price)
    if not mp.ft.started:
        print(f"Model paper account: not started (starts {settings.model_paper_start or 'never: MODEL_PAPER_START=off'}"
              ", after the close, from the watch service).")
        return 0
    print(format_model_paper({**mp.ft.summary(), "rebalanced": None, "model": mp.model()}, forward_return(settings)))
    return 0


def cmd_resmom_paper(args: argparse.Namespace) -> int:
    """Residual momentum's (variant E's) paper account: show it, or with --run do today's after-close run now."""
    from .resmom_paper import ResidualPaper, control_over, format_resmom_paper, run_resmom_paper_due
    from .runner import free_prices
    settings = _settings(args)
    prices = free_prices(settings)
    if args.run:
        print(run_resmom_paper_due(settings, prices, _market_holidays(settings)))
        return 0
    rp = ResidualPaper(settings.state_dir, universe=settings.forward_universe, capital=settings.paper_starting_cash,
                       price_fn=prices.latest_price)
    if not rp.ft.started:
        print("Residual momentum paper account: not started (starts "
              f"{settings.resmom_paper_start or 'never: RESMOM_PAPER_START=off'}, after the close, from the watch service).")
        return 0
    s = {**rp.ft.summary(), "rebalanced": None}
    print(format_resmom_paper(s, control_over(settings.state_dir, settings.forward_universe, s.get("started"))))
    return 0


def cmd_top10_paper(args: argparse.Namespace) -> int:
    """Variant B's (top 10) paper account: show it, or with --run do today's after-close run now."""
    from .resmom_paper import control_over
    from .runner import free_prices
    from .top10_paper import Top10Paper, format_top10_paper, run_top10_paper_due
    settings = _settings(args)
    prices = free_prices(settings)
    if args.run:
        print(run_top10_paper_due(settings, prices, _market_holidays(settings)))
        return 0
    tp = Top10Paper(settings.state_dir, universe=settings.forward_universe, capital=settings.paper_starting_cash,
                    price_fn=prices.latest_price)
    if not tp.ft.started:
        print("Top-10 paper account: not started (starts "
              f"{settings.top10_paper_start or 'never: TOP10_PAPER_START=off'}, after the close, from the watch service).")
        return 0
    s = {**tp.ft.summary(), "rebalanced": None}
    print(format_top10_paper(s, control_over(settings.state_dir, settings.forward_universe, s.get("started"))))
    return 0


def cmd_research_regimes(args: argparse.Namespace) -> int:
    """How the adopted rules did in bull, sideways, bear and high-volatility months (a diagnostic, not a filter)."""
    from .costs import cost_model_for
    from .research_studies import format_regimes, regime_diagnostic
    settings = _settings(args)
    m = _research_market(args, settings)
    r = regime_diagnostic(m, cost_model=cost_model_for("in"), capital=settings.paper_starting_cash)
    print(format_regimes(r))
    if args.json:
        print(json.dumps(r, default=str))
    return 0


def cmd_research_capacity(args: argparse.Namespace) -> int:
    """How much capital the adopted rules can run: orders against daily traded value, and a rough impact cost."""
    from .costs import cost_model_for
    from .research_studies import capacity, format_capacity
    settings = _settings(args)
    m = _research_market(args, settings)
    r = capacity(m, cost_model=cost_model_for("in"), capital=settings.paper_starting_cash)
    print(format_capacity(r))
    if args.json:
        print(json.dumps(r, default=str))
    return 0


def cmd_insider_history(args: argparse.Namespace) -> int:
    """Download NSE insider (PIT) disclosures month by month into the cache (resumable)."""
    from datetime import date as _date
    from .event_signals import InsiderHistory
    from .nse import NSEClient
    settings = _settings(args)
    cache = settings.state_dir / "cache"
    nse = NSEClient(cache_dir=cache)
    end = _date.today()
    start = _date(end.year - args.years, end.month, 1)
    r = InsiderHistory(cache, nse._get).download(start, end, progress=print, max_months=args.max_months)
    print(f"Insider history: {r['cached']} month(s) already cached, {r['fetched']} downloaded, {r['failed']} failed."
          + (" Run again to continue." if r["failed"] or r["fetched"] >= args.max_months else ""))
    return 0 if not r["failed"] else 1


def _event_data(m: Any, settings: Any) -> Any:
    """Earnings and insider signals for the market's stocks, from the caches only (no downloads here)."""
    from .event_signals import EventData, InsiderHistory
    from .fundamentals_history import ResultsHistory
    src = ResultsHistory(settings.state_dir / "cache", max_new_downloads=0)
    results: dict[str, Any] = {}
    for sym in m.raw:
        try:
            results[sym] = src.history(sym)
        except Exception:  # noqa: BLE001 - no cached listing: no earnings signals for it
            results[sym] = []
    covered = sum(1 for v in results.values() if v)
    insider = InsiderHistory(settings.state_dir / "cache").trades(m.dates[0], m.dates[-1])
    print(f"Results filings cached for {covered} of {len(results)} stocks; {len(insider)} insider trades cached.")
    if covered < 0.8 * len(results):
        print("Coverage is low: run `fundamentals-history --universe NIFTYMIDCAP150` until nothing is left, then again.")
    if not insider:
        print("No insider history: run `insider-history` first; the promoter signals are empty without it.")
    return EventData(m, results, insider)


def _research_dir(settings: Any) -> Any:
    d = settings.state_dir / "research"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _num(v: Any, fmt: str, width: int) -> str:
    return ("n/a" if v is None else format(v, fmt)).rjust(width)


def cmd_research_barrier(args: argparse.Namespace) -> int:
    """Signal lab, fourth horizon: each signal's top fifth exited by the triple barrier, after charges and cash."""
    from .costs import cost_model_for
    from .gap_diagnosis import log_trial
    from .research_ml import barrier_study
    from .research_studies import _first_ranked_day
    settings = _settings(args)
    m = _research_market(args, settings)
    cost = cost_model_for("in").round_trip_bps(25_000) / 10_000
    start = _first_ranked_day(m)
    r = barrier_study(m, start, len(m.dates) - 1, rule=args.stop, cost=cost, target_r=args.target, max_days=args.days)
    print(f"Triple barrier ({args.stop} stop, target +{args.target:g}R, at most {args.days} days), top fifth at each "
          f"month start, {m.dates[start]} to {m.dates[-1]}; after a {cost * 100:.2f}% round trip and the liquid fund:")
    print(f"  {'signal':<22}{'per trade':>10}{'t':>7}{'t adj':>7}{'hit rate':>9}{'avg days':>9}  "
          f"{'exits t/s/time':<16}verdict")
    for k, x in sorted(r.items(), key=lambda kv: -(kv[1]["mean_net"] if kv[1]["mean_net"] is not None else -9)):
        e = x["exits"]
        print(f"  {k:<22}" + _num(None if x["mean_net"] is None else x["mean_net"] * 100, "+.2f", 9) + "%"
              + _num(x["t"], "+.1f", 7) + _num(x.get("t_overlap"), "+.1f", 7)
              + _num(None if x["hit_rate"] is None else x["hit_rate"] * 100, ".0f", 8) + "%"
              + _num(x["avg_days"], ".0f", 9) + f"  {e['target']}/{e['stop']}/{e['time']}".ljust(18) + x["verdict"])
        log_trial(settings.state_dir, "signal-barrier", {"signal": k, "stop": args.stop, "target": args.target,
                                                         "days": args.days},
                  {"mean_net": x["mean_net"], "t": x["t"], "t_overlap": x.get("t_overlap")})
    lags = next((x.get("overlap_lags") for x in r.values()), None)
    print(f"  t adj = Newey-West over {lags} months of overlap (trades last up to {args.days} days but start monthly); "
          f"the verdict uses it. It does not correct for the {len(r)} signals tried.")
    return 0


def cmd_research_cpcv(args: argparse.Namespace) -> int:
    """The signal lab's ridge model under combinatorial purged cross-validation: 5 out-of-sample account curves."""
    import itertools as _it
    from .costs import cost_model_for
    from .gap_diagnosis import log_trial
    from .research_ml import cpcv_signal_model
    from .research_studies import _first_ranked_day
    settings = _settings(args)
    m = _research_market(args, settings)
    cost = cost_model_for("in").round_trip_bps(25_000) / 10_000
    start = _first_ranked_day(m)
    r = cpcv_signal_model(m, start, len(m.dates) - 1, cost=cost, horizon=args.horizon, progress=print)
    print(f"Walk-forward ridge model, purged cross-validation: {r['samples']} stock-months, {r['splits']} splits, "
          f"{len(r['paths'])} out-of-sample paths, {args.horizon}-day holding, top fifth, after a round trip:")
    for i, (sr, tot) in enumerate(zip(r["sharpe"], r["total"]), 1):
        print(f"  path {i}: monthly Sharpe {sr:+.3f}, sum of monthly results above the fund {tot * 100:+.1f}%")
    out = _research_dir(settings) / "cpcv_fan.png"
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(8, 4.5), dpi=110)
    for c in r["paths"]:
        ax.plot(list(_it.accumulate(c)), color="#3b6fd8", alpha=0.6)
    ax.axhline(0, color="#999", lw=0.8)
    ax.set_title("Ridge model: 5 out-of-sample paths (purged CV)")
    ax.set_xlabel("month")
    ax.set_ylabel("cumulative result above the index fund")
    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)
    print(f"  The fan of account curves: {out}")
    log_trial(settings.state_dir, "signal-cpcv", {"horizon": args.horizon}, {"sharpe": r["sharpe"]})
    return 0


def _meta_run(args: argparse.Namespace, *, with_events: bool, study: str) -> int:
    from .costs import cost_model_for
    from .gap_diagnosis import log_trial
    from .research_ml import (calibration_chart, evaluate_cpcv, fan_chart, fit_final, format_meta, meta_samples,
                              monthly_sized, predict, verdict)
    from .research_studies import HOLDOUT_START, _first_ranked_day, prior_trials
    settings = _settings(args)
    if getattr(args, "holdout", False) and _once_used(settings, f"{study}-holdout"):
        return 2
    m = _research_market(args, settings)
    ev = _event_data(m, settings) if with_events else None
    cost = cost_model_for("in").round_trip_bps(25_000) / 10_000
    start = _first_ranked_day(m)
    samples, feats = meta_samples(m, start, len(m.dates) - 1, rule=args.stop, cost=cost, ev=ev)
    if len({s["month"] for s in samples}) < 12:
        print("Fewer than 12 months of samples: not enough for purged cross-validation.")
        return 1
    r = evaluate_cpcv(samples, cost=cost, progress=print)
    v = verdict(r, prior_trials(settings.state_dir))
    print(f"{study}: sizing the momentum screen's picks ({args.stop} stop, target +3R, 60 days; success = beat the "
          f"index fund after charges), {m.dates[start]} to {m.dates[-1]}")
    print(format_meta(r, v, feats))
    d = _research_dir(settings)
    fan_chart(r, v["best_objective"], d / f"{study.lower()}_fan.png",
              f"{study}: {v['best_objective']} sizing, 5 out-of-sample paths")
    if r.get("calibration"):
        calibration_chart(r["calibration"], d / f"{study.lower()}_calibration.png", f"{study}: calibration")
    print(f"  Charts: {d / (study.lower() + '_fan.png')} and {d / (study.lower() + '_calibration.png')}")
    for obj, x in r["objectives"].items():
        log_trial(settings.state_dir, study, {"objective": obj, "stop": args.stop},
                  {"sharpe": x["sharpe"], "passes": v["passes"] and obj == v["best_objective"]})
    if not getattr(args, "holdout", False):
        if with_events:
            print("  Next: " + ("passes on the development years; run once on the sealed holdout with --holdout."
                                if v["passes"] else "does not pass; the holdout stays sealed."))
        return 0
    if not v["passes"]:
        print("Not opening the holdout: the model did not pass on the development years.")
        return 2
    beta = fit_final(v["best_objective"], samples, cost)
    args.until, args.years = None, 2
    hm = _research_market(args, settings)
    hm.start_day = HOLDOUT_START
    if not _claim_once(settings, f"{study}-holdout"):
        return 2
    hev = _event_data(hm, settings) if with_events else None
    hs, _ = meta_samples(hm, _first_ranked_day(hm), len(hm.dates) - 1, rule=args.stop, cost=cost, ev=hev)
    sized = monthly_sized(hs, predict(beta, [s["x"] for s in hs]))
    simple = monthly_sized(hs, [1.0] * len(hs))
    top10 = monthly_sized(hs, [1.0 if s["rank"] <= 10 else 0.0 for s in hs])
    best_simple = max(sum(simple), sum(top10))
    print(f"THE SEALED HOLDOUT ({hm.dates[_first_ranked_day(hm)]} to {hm.dates[-1]}, opened once): model "
          f"{sum(sized) * 100:+.1f}% above the fund vs the best simple rule {best_simple * 100:+.1f}% "
          f"({len(sized)} months). " + ("The model WINS." if sum(sized) > best_simple else "The model does NOT win."))
    log_trial(settings.state_dir, f"{study}-holdout", {"objective": v["best_objective"], "stop": args.stop},
              {"model": sum(sized), "best_simple": best_simple})
    return 0


def cmd_research_meta(args: argparse.Namespace) -> int:
    """Signal lab: meta-labeling with three fitting objectives on purged splits (price features only)."""
    return _meta_run(args, with_events=False, study="Meta-labeling")


def cmd_research_model(args: argparse.Namespace) -> int:
    """The prediction model: the sizing model with earnings and promoter features; one sealed-holdout run."""
    return _meta_run(args, with_events=True, study="Prediction-model")


def cmd_research_pead(args: argparse.Namespace) -> int:
    """Research: post-results drift and who-traded signals (IC study + portfolio), optionally the sealed holdout once."""
    from .costs import cost_model_for
    from .event_signals import format_signal_study, pead_ranker, signal_study
    from .gap_diagnosis import log_trial
    from .research_sim import Rules, simulate, summarize
    from .research_studies import HOLDOUT_START, SKILL_BAR, _first_ranked_day, _skill, prior_trials
    settings = _settings(args)
    if args.holdout:
        if _once_used(settings, "PEAD-holdout"):
            return 2
        args.until, args.years = None, 2   # a year of holdout plus the ranking's lookback
    m = _research_market(args, settings)
    if args.holdout:
        m.start_day = HOLDOUT_START
        if not _claim_once(settings, "PEAD-holdout"):
            return 2
    ev = _event_data(m, settings)
    start, end = _first_ranked_day(m), len(m.dates) - 1
    print(f"Window {m.dates[start]} to {m.dates[end]}" + (" (THE SEALED HOLDOUT, opened once)" if args.holdout else ""))
    if not args.holdout:
        print(format_signal_study(signal_study(m, ev, start, end)))
    cm = cost_model_for("in")
    base = Rules(tax=True)
    runs = {"momentum (today)": simulate(m, base, start, end, cost_model=cm),
            "momentum + post-results drift": simulate(m, base, start, end, cost_model=cm, ranker=pead_ranker(ev)),
            "  + promoter buying": simulate(m, base, start, end, cost_model=cm,
                                            ranker=pead_ranker(ev, with_promoters=True))}
    skill = _skill(runs, m, start, end, prior_trials(settings.state_dir))
    print(f"  {'portfolio (after charges and tax)':<34}{'return':>9}{'vs fund':>9}{'worst fall':>11}{'skill odds':>11}")
    for name, run in runs.items():
        x = summarize(run, m, start, end)
        p = skill[name]["probability"]
        print(f"  {name:<34}{x['total_return'] * 100:>+8.1f}%{x['excess'] * 100:>+8.1f}%{x['max_drawdown'] * 100:>+10.1f}%"
              f"{('n/a' if p is None else f'{p:.2f}'):>11}")
        log_trial(settings.state_dir, "PEAD-holdout" if args.holdout else "PEAD",
                  {"variant": name.strip(), "until": args.until}, {"return": x["total_return"], "excess": x["excess"]})
    pead = summarize(runs["momentum + post-results drift"], m, start, end)
    if args.holdout:
        print("  Verdict: " + ("post-results drift BEAT MID150BEES after charges on the holdout."
                               if (pead["excess"] or 0) > 0 else
                               "post-results drift did NOT beat MID150BEES after charges on the holdout."))
    else:
        ok = (pead["excess"] or 0) > 0 and (skill["momentum + post-results drift"]["probability"] or 0) >= SKILL_BAR
        print("  Next: " + ("passes on the development years; open the sealed holdout once with --holdout."
                            if ok else "does not pass on the development years; the holdout stays sealed."))
    return 0


def cmd_research_coffee_can(args: argparse.Namespace) -> int:
    """Research C: the Coffee Can screen (revenue growth > 10% and ROCE > 15% every year), held a year at a time."""
    from .coffee_can import UnionMembership, coffee_can_ranker
    from .costs import cost_model_for
    from .fundamentals_history import ResultsHistory
    from .gap_diagnosis import log_trial
    from .index_history import point_in_time
    from .research_sim import Rules, load_market, simulate, stats, summarize
    from .research_studies import HOLDOUT_START, SKILL_BAR, _skill, prior_trials
    from .runner import free_prices
    from .screen import load_universe
    settings = _settings(args)
    until, years = args.until, args.years
    if args.holdout:
        if _once_used(settings, "CoffeeCan-holdout"):
            return 2
        until, years = None, 2
    members, industries, parts = [], {}, []
    for uni in ("NIFTYMIDCAP150", "NIFTYSMALLCAP250"):
        rows = load_universe(uni)
        members += [r["symbol"] for r in rows]
        industries.update({r["symbol"]: r.get("industry", "") for r in rows})
        parts.append(point_in_time(uni, [r["symbol"] for r in rows], settings.state_dir, progress=print))
    membership = UnionMembership(parts)
    prices = free_prices(settings)
    print(f"Loading {len(set(members))} Midcap 150 and Smallcap 250 stocks (this takes a few minutes)…")
    m = load_market(sorted(set(members)), prices, regime="MID150BEES", years=years, until=until,
                    membership=membership if membership.parts else None)
    if args.holdout:
        m.start_day = HOLDOUT_START
        if not _claim_once(settings, "CoffeeCan-holdout"):
            return 2
    src = ResultsHistory(settings.state_dir / "cache", max_new_downloads=0)
    annual: dict[str, Any] = {}
    for sym in m.raw:
        try:
            annual[sym] = src.annual(sym)
        except Exception:  # noqa: BLE001 - no cached listing
            annual[sym] = []
    covered = sum(1 for v in annual.values() if v)
    print(f"Full-year filings cached for {covered} of {len(annual)} stocks.")
    if covered < 0.8 * len(annual):
        print("Coverage is low: run `fundamentals-history --annual --universe NIFTYMIDCAP150` and "
              "`--universe NIFTYSMALLCAP250` until nothing is left, then again.")
    picked: dict[str, Any] = {}
    reasons: dict[str, Any] = {}
    window = getattr(args, "window", None)
    debt_rate = 0.09 if getattr(args, "estimate_debt", False) else None
    ranker = coffee_can_ranker(annual, industries, min_years=args.min_years, window=window, log=picked,
                               reasons=reasons, debt_rate=debt_rate)
    from .coffee_can import rebuilt_equity_check
    both, agree = rebuilt_equity_check(annual)
    print(f"Equity rebuilt from paid-up capital + reserves (years without a balance sheet): it matches the balance "
          f"sheet within 2% in {agree} of {both} filings that have both"
          + ("" if not both else f" ({agree / both * 100:.0f}%)") + ". "
          + ("Borrowings before FY2023 estimated as finance costs / 9%." if debt_rate else
             "Without --estimate-debt, years before FY2023 have no ROCE (no borrowings figure)."))
    rules = Rules(top=40, weights="equal_picks", rebalance="yearly", rebalance_month=7, tax=True)
    first = None
    for t in m.rebalance_days(0, len(m.dates) - 1, "yearly", 7):
        if m.dates[t] >= m.start_day and int(m.dates[t][5:7]) == 7 and len(ranker(m, t)) >= 5:
            first = t
            break
    if first is None:
        print(f"No July with 5+ qualifying stocks (need {args.min_years} years of growth each"
              + (f", the latest {window} tested" if window else ", every year seen tested") + "): nothing to test yet.")
        for day in sorted(reasons):
            if int(day[5:7]) != 7:
                continue
            why = reasons[day]
            print(f"  {day}: {len(picked.get(day) or [])} qualify, {sum(why.values())} turned down: "
                  + "; ".join(f"{k} {n}" for k, n in why.most_common(6)))
        from .coffee_can import filing_coverage
        print("  March filings cached, by financial year "
              "(filings / revenue / profit before tax / balance sheet / equity rebuilt):")
        for fy, n, rev, pbt, bs, rb in filing_coverage(annual):
            print(f"    FY{fy}: {n} / {rev} / {pbt} / {bs} / {rb}")
        return 1
    end = len(m.dates) - 1
    run = simulate(m, rules, first, end, cost_model=cost_model_for("in"), ranker=ranker)
    x = summarize(run, m, first, end)
    nb = prices.history("NIFTYBEES", "5y")
    nb_by = {b["date"]: b["adj_close"] for b in nb}
    nb_curve = [nb_by.get(d) for d in run["dates"]]
    nbs = stats(nb_curve, run["dates"])
    skill = _skill({"coffee can": run}, m, first, end, prior_trials(settings.state_dir))["coffee can"]
    print(f"Coffee Can, {m.dates[first]} to {m.dates[end]}" + (" (THE SEALED HOLDOUT, opened once)" if args.holdout else "")
          + f": after charges and tax {x['total_return'] * 100:+.1f}%, worst fall {x['max_drawdown'] * 100:.1f}%; "
          f"MID150BEES {x['fund_total_return'] * 100:+.1f}%, NIFTYBEES "
          + ("n/a" if nbs["total_return"] is None else f"{nbs['total_return'] * 100:+.1f}%")
          + f"; tax paid {x['tax_paid']:,.0f}; skill odds "
          + ("n/a" if skill["probability"] is None else f"{skill['probability']:.2f}"))
    for day, names in picked.items():
        if names and int(day[5:7]) == 7:
            print(f"  {day}: {len(names)} qualify: {', '.join(names[:15])}" + (" …" if len(names) > 15 else ""))
    beats = (x["excess"] or 0) > 0 and nbs["total_return"] is not None and x["total_return"] > nbs["total_return"]
    print("  Verdict: " + ("beats both index funds after charges and tax" if beats else
                           "does not beat both index funds after charges and tax")
          + ("" if args.holdout else (" on the development years; " + (
              "open the sealed holdout once with --holdout." if beats and (skill["probability"] or 0) >= SKILL_BAR
              else "the holdout stays sealed."))))
    print("  ROCE uses the closing balance sheet (the book averages opening and closing); "
          f"{args.min_years} years of growth required (the book asks for 10; filings start in late 2018).")
    if debt_rate:
        print("  INDICATIVE ONLY: before FY2023 equity is rebuilt from paid-up capital + reserves and borrowings are "
              "estimated from finance costs at 9%, so a ROCE near 15% may be misjudged either way.")
    log_trial(settings.state_dir, "CoffeeCan-holdout" if args.holdout else "CoffeeCan",
              {"min_years": args.min_years, "window": window, "until": until, "debt_rate": debt_rate},
              {"return": x["total_return"], "excess": x["excess"]})
    return 0


def cmd_costs(args: argparse.Namespace) -> int:
    from .costs import FlatCosts, cost_model_for
    settings = _settings(args)
    m = cost_model_for(settings.market)
    if isinstance(m, FlatCosts):
        print(f"flat model: {m.round_trip_bps(0):.0f} bps round trip")
        return 0
    for n in args.amounts:
        rt = m.round_trip(n)
        print(f"₹{n:,.0f} delivery round trip: charges ₹{rt['charges']:.2f} ({rt['charges_bps']:.1f} bps), "
              f"with {m.slippage_bps:.0f} bps/side slippage {rt['total_bps']:.1f} bps")
        if args.verbose:
            for side in ("buy", "sell"):
                print(f"   {side}: " + ", ".join(f"{k} {v:.2f}" for k, v in rt[side].items() if k not in ("charges", "total")))
    return 0


def cmd_size(args: argparse.Namespace) -> int:
    from .risk import atr, position_size
    from .runner import free_prices, make_broker
    settings = _settings(args)
    broker = make_broker(settings)
    prices = free_prices(settings)
    equity = args.equity or broker.account().equity
    for t in args.tickers:
        try:
            px = broker.latest_price(t)
            a = atr(prices.history(t, "1y"))
        except Exception as e:  # noqa: BLE001
            print(f"{t.upper():<12} error: {e}")
            continue
        r = position_size(equity, px, a, whole_shares=settings.market == "in")
        print(f"{t.upper():<12} price {px:,.2f}  ATR {a or 0:,.2f} ({(r['atr_pct'] or 0)*100:.1f}%)  "
              f"-> {r['qty']} sh = {r['notional']:,.0f}  stop {r['stop']:,.2f}  [{r['basis']}]")
    return 0


def _market_holidays(settings: Any) -> Any | None:
    if settings.market != "in":
        return None
    from .holidays import NSEHolidays
    return NSEHolidays(cache_dir=settings.state_dir / "cache")


def _app_feed_read(settings: Any, prices: Any) -> Any:
    """The feed's Groww read. The runner's error text for an unexpected exception is ``Type: message`` and the message
    can carry a URL or key, so only the type is kept; the deliberate refusal texts (cool-down, not linked) stay."""
    def read() -> dict[str, Any]:
        from datetime import datetime
        from .runner import read_groww_portfolio
        from .timezones import IST
        out = read_groww_portfolio(settings, prices, datetime.now(IST).isoformat(timespec="seconds"))
        err = out.get("error")
        if isinstance(err, str):
            m = re.match(r"([A-Za-z_][\w.]*): ", err)   # runner's "Type: message"; deliberate texts never look so
            out = {**out, "error": m.group(1) if m else err[:200]}
        return out
    return read


def _app_feed_cash(settings: Any) -> Any:
    """The cash balance through a read-only Groww client (live orders off); None on any failure."""
    def cash() -> float | None:
        try:
            from .groww import GrowwBroker
            from .runner import resolve_groww_token
            return float(GrowwBroker(resolve_groww_token(settings), live_orders=False,
                                     exchange=settings.groww_exchange, timeout=10.0).available_cash())
        except (Exception, SystemExit) as e:  # noqa: BLE001 - includes GrowwTokenUnavailable; the feed shows no cash
            logging.getLogger(__name__).warning("app feed cash read failed (%s)", type(e).__name__)
            return None
    return cash


def _app_feed_prev_close(prices: Any) -> Any:
    from datetime import datetime
    from .day_brake import prev_close_fn
    from .timezones import IST
    return lambda symbol: prev_close_fn(prices, datetime.now(IST).date())(symbol)   # the date moves on in a long run


def _app_feed_trend(prices: Any) -> Any:
    """Up to 30 daily closes, oldest first."""
    def trend(symbol: str) -> list[float]:
        return [float(b["close"]) for b in prices.history(symbol, "1mo") if b.get("close") is not None][-30:]
    return trend


def _app_feed(settings: Any, holidays: Any) -> Any:
    from .app_feed import AppFeed
    from .runner import free_prices
    prices = free_prices(settings)
    return AppFeed(settings, _app_feed_read(settings, prices), _app_feed_cash(settings), _app_feed_prev_close(prices),
                   _app_feed_trend(prices), holidays)


def cmd_watch(args: argparse.Namespace) -> int:
    """Always-on local mode: poll deals and announcements during market hours."""
    from .runner import make_broker, make_data_source, make_notifier
    from .watch import Watcher

    settings = _settings(args)
    if args.auto_trade:
        settings.auto_trade = True
    data = make_data_source(settings, breaker_file=settings.state_dir / "nse_breaker.json")
    try:
        broker = make_broker(settings)
    except GrowwTokenUnavailable as e:
        # Live mode while Groww refuses a token: do not exit (a service manager would restart-loop and the deal
        # alerts would stop). Watch alerts-only and build the broker again once the cool-down ends.
        print(_token_error_text(e), file=sys.stderr)
        broker = None
    notifier = make_notifier(settings)
    from .runner import free_prices
    news = None
    if settings.market == "in":
        from .news import NewsService
        from .instruments import CompanyNames
        news = NewsService(settings, names=CompanyNames(settings.state_dir / "cache"))
    from .broker import LocalPaperBroker
    from .digest_schedule import make_scheduler
    holidays = _market_holidays(settings)
    digest = make_scheduler(settings, notifier, data=data, prices=free_prices(settings), news=news, holidays=holidays,
                            practice=broker if isinstance(broker, LocalPaperBroker) else None)
    from .forward_schedule import ForwardScheduler, run_forward_due
    from .heartbeat import Heartbeat
    from .backup import make_scheduler as make_backup_scheduler
    from .clockcheck import startup_check
    from .integration import make_scheduler as make_integration_scheduler
    from .notify import install_log_redaction
    install_log_redaction()   # bot tokens and the heartbeat path never reach a log, even at -v
    forward_prices = free_prices(settings)
    close_snapshot = None
    if settings.use_groww and settings.has_groww_credentials:
        from datetime import datetime
        from .timezones import IST
        from .groww_hours import CloseSnapshot
        from .runner import read_groww_portfolio
        close_prices = free_prices(settings)
        close_snapshot = CloseSnapshot(
            settings, lambda day: read_groww_portfolio(settings, close_prices, datetime.now(IST).isoformat(timespec="seconds"),
                                                       force=True, close_session=day), holidays)
    app_feed = None
    if settings.app_feed and settings.use_groww and settings.has_groww_credentials:
        app_feed = _app_feed(settings, holidays)
    def after_close() -> None:
        print(run_forward_due(settings, forward_prices, holidays, notifier))
        from .model_paper import run_model_paper_due   # the prediction model's own paper account (never raises)
        print(run_model_paper_due(settings, forward_prices, holidays))
        from .resmom_paper import run_resmom_paper_due   # variant E's own paper account (never raises)
        print(run_resmom_paper_due(settings, forward_prices, holidays))
        from .top10_paper import run_top10_paper_due   # variant B's own paper account (never raises)
        print(run_top10_paper_due(settings, forward_prices, holidays))
    forward = ForwardScheduler(settings.state_dir, after_close, holidays=holidays)
    w = Watcher(settings, every=args.every, news=news, digest=digest, forward=forward,
                integration=make_integration_scheduler(settings, notifier, holidays),
                backup=make_backup_scheduler(settings, holidays), close_snapshot=close_snapshot, app_feed=app_feed,
                heartbeat=Heartbeat(settings.heartbeat_url, fail_enabled=True if settings.heartbeat_fail else None),
                window=(args.window_start, args.window_end),
                data=data, broker=broker, notifier=notifier, prices=free_prices(settings),
                auto_exit=settings.auto_trade,
                holidays=holidays, broker_factory=lambda: make_broker(settings),
                check_fn=lambda: check(settings, broker=w._broker, data=data, notifier=notifier,
                                       dry_run=not settings.anthropic_api_key))
    try:
        startup_check(settings, notifier)   # TOTP logins need a correct clock: log it, alert once a day if it is wrong
    except Exception:  # noqa: BLE001 - the watch must always start
        logging.getLogger(__name__).exception("the start-up clock check failed; carrying on")
    print(f"Watching {', '.join(settings.investors)} every {w.every}s, {args.window_start}-{args.window_end} IST, "
          f"NSE trading days. Ctrl+C to stop.")
    from . import telegram_bot   # TELEGRAM_COMMANDS=true: its own thread, so read commands work even under HALT
    bot_stop = telegram_bot.start(settings)
    try:
        w.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if bot_stop is not None:
            bot_stop.set()
    return 0


def cmd_integration_check(args: argparse.Namespace) -> int:
    """Read-only check of the real services (NSE, BSE, Yahoo, NSE archives, Groww with a cached token only, optionally
    Claude). Stores state/integration_check.json and alerts once a day on failure. Never places an order."""
    from .integration import format_result, run_and_record
    from .runner import make_notifier
    settings = _settings(args)
    if args.claude:
        settings.integration_claude = True
    result = run_and_record(settings, notifier=None if args.no_alert else make_notifier(settings),
                            holidays=_market_holidays(settings))
    print(format_result(result))
    return 0 if result["ok"] else 2


def cmd_digest(args: argparse.Namespace) -> int:
    """Print today's morning or evening email (rules, plus the written summary unless --writer none); --send
    emails it through the configured channels. Never places an order."""
    from .digest_schedule import build_digest, make_context, send_digest
    from .runner import make_data_source, make_notifier
    settings = _settings(args)
    try:
        data = make_data_source(settings)
    except SystemExit:
        data = None
    holidays = _market_holidays(settings)
    ctx = make_context(settings, data=data, holidays=holidays)
    email = build_digest(args.kind, ctx, writer=args.writer)
    print(f"Subject: {email['subject']}" + chr(10))
    print(email["text"])
    if args.send:
        notifier = make_notifier(settings)
        if set(notifier.channels) == {"console"}:
            print("Not sent: no email (RESEND_API_KEY + NOTIFY_EMAIL_TO) or webhook is configured.", file=sys.stderr)
            return 1
        from .telegram_bot import commands_configured
        delivered = send_digest(notifier, email, allowed_hosts=getattr(settings, "allowed_hosts", None),
                                commands=commands_configured(settings))
        print(f"Sent via: {', '.join(d for d in delivered if d != 'console') or 'nothing (delivery failed)'}")
        return 0 if set(delivered) - {"console", "telegram"} else 1
    return 0


def cmd_ui(args: argparse.Namespace) -> int:
    """Serve the local dashboard."""
    from .ui import serve
    settings = _settings(args)
    serve(settings, host=args.host, port=args.port, open_browser=not args.no_open, demo=args.demo)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="trading_agent",
                                description="Claude agent that follows an investor's disclosed trades.")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--market", choices=["in", "us"], help="override MARKET (in = NSE/Groww)")
    sub = p.add_subparsers(dest="command", required=True)

    def add_check_args(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--investor", action="append", metavar="NAME[,NAME...]",
                        help="follow these investors this run instead of INVESTORS (comma list; repeatable)")
        sp.add_argument("--force", action="store_true", help="run Claude even with no new trades")
        sp.add_argument("--dry-run", action="store_true", help="fetch & diff only; don't call Claude")
        sp.add_argument("--demo", action="store_true", help="use bundled sample trades/prices")
        sp.add_argument("--auto-trade", action="store_true", help="allow PAPER orders this run")
        sp.add_argument("--json", action="store_true", help="also print the result as JSON")
        sp.add_argument("--skip-holidays", action="store_true",
                        help="do nothing on NSE trading holidays (used by the scheduled routine)")
        sp.add_argument("--baseline", action="store_true",
                        help="record current trades as seen without calling Claude (after lost state)")

    sp = sub.add_parser("check", help="run one check now"); add_check_args(sp)
    sp.set_defaults(func=cmd_check)
    sp = sub.add_parser("loop", help="keep checking every N minutes"); add_check_args(sp)
    sp.add_argument("--every", type=int, default=30, help="minutes between checks")
    sp.set_defaults(func=cmd_loop)
    sub.add_parser("portfolio", help="show the paper account").set_defaults(func=cmd_portfolio)
    sp = sub.add_parser("history", help="show past recommendations")
    sp.add_argument("--limit", type=int, default=20); sp.set_defaults(func=cmd_history)
    sub.add_parser("reset", help="forget seen trades and reset the local paper account") \
        .set_defaults(func=cmd_reset)
    sp = sub.add_parser("groww-token", help="print a Groww access token (cached until 06:00 IST)")
    sp.add_argument("--fresh", action="store_true", help="generate a new one even if the cached token is valid")
    sp.add_argument("--force", action="store_true",
                    help="ask Groww even while waiting after a refusal (may extend Groww's wait)")
    sp.set_defaults(func=cmd_groww_token)
    sp = sub.add_parser("holdings", help="your real Groww holdings: buy price, current price, P&L (read-only)")
    sp.set_defaults(func=cmd_holdings)
    sp = sub.add_parser("orders", help="list live Groww orders; --refresh re-checks open ones")
    sp.add_argument("--refresh", action="store_true")
    sp.add_argument("--limit", type=int, default=30)
    sp.set_defaults(func=cmd_orders)
    sp = sub.add_parser("market-data", help="FII/DII flows, NIFTY 500 breadth and price bands: show, or fetch now")
    sp.add_argument("--fetch", nargs="?", const="all", choices=["flows", "breadth", "bands", "all"],
                    help="fetch from the public NSE files now (default: only show what is stored)")
    sp.set_defaults(func=cmd_market_data)
    sp = sub.add_parser("forward", help="paper-trade the factor screen forward against its index fund")
    sp.add_argument("--universe", default="NIFTYMIDCAP150")
    sp.add_argument("--top", type=int, default=20, help="names to hold (set on the first run)")
    sp.add_argument("--capital", type=float, help="starting capital (first run only; default PAPER_STARTING_CASH)")
    sp.add_argument("--rebalance", action="store_true", help="rebalance now even if this month is done")
    sp.add_argument("--status", action="store_true", help="show the current standing without trading")
    sp.add_argument("--if-due", action="store_true", help="for schedules: skip unless a weekday after the close")
    sp.add_argument("--rebuild-from", metavar="YYYY-MM-DD",
                    help="recreate the forward account as the first run made it on that date (refuses if one exists)")
    sp.add_argument("--force", action="store_true", help="with --rebuild-from: delete an existing forward account first")
    sp.set_defaults(func=cmd_forward)
    sp = sub.add_parser("groww-check", help="verify live-trading assumptions on your Groww account")
    sp.add_argument("--live-test", metavar="SYMBOL", nargs="?", const="ITC",
                    help="also place a REAL 1-share limit buy below market (modify, then cancel) and a test GTT; "
                         "SYMBOL defaults to ITC")
    sp.add_argument("--symbol", help="the stock for --live-test (overrides SYMBOL; default ITC)")
    sp.add_argument("--offset-pct", type=float, default=3.0, help="how far below the last price to rest the buy")
    sp.add_argument("--i-understand-real-orders", action="store_true")
    sp.add_argument("--confirm-friction-override", action="store_true",
                    help="run --live-test even though the forward test's friction gate is tripped")
    sp.add_argument("--force", action="store_true",
                    help="ask Groww for a token even while waiting after a refusal (may extend Groww's wait)")
    sp.add_argument("--ip", action="store_true",
                    help="only print this machine's public IP and whether it matches GROWW_ALLOWED_IP (no credentials)")
    sp.set_defaults(func=cmd_groww_check)
    sp = sub.add_parser("kill-switch", help="stop ALL trading now (state/HALT); --release allows it again")
    sp.add_argument("--cancel-gtt", action="store_true",
                    help="also cancel the agent's GTT stop-losses at Groww (by default they stay and keep protecting)")
    sp.add_argument("--release", action="store_true", help="remove state/HALT so trading can resume")
    sp.add_argument("--status", action="store_true", help="only say whether trading is halted")
    sp.set_defaults(func=cmd_kill_switch)
    sp = sub.add_parser("gtt", help="show Groww GTT stop-losses; --sync updates them (live only)")
    sp.add_argument("--sync", action="store_true")
    sp.set_defaults(func=cmd_gtt)
    sp = sub.add_parser("backtest", help="replay an investor's disclosed deals vs NIFTY 50")
    sp.add_argument("--investor", action="append", metavar="NAME[,NAME...]",
                    help="investors to replay (comma list; repeatable); default: all followed (INVESTORS)")
    sp.add_argument("--days", type=int, default=365, help="how far back to fetch deals")
    sp.add_argument("--horizons", default="5,20,60", help="holding periods in trading days")
    sp.add_argument("--cost-bps", type=float, default=50.0, help="round-trip cost in basis points")
    sp.add_argument("--benchmark", default="^NSEI")
    sp.add_argument("--demo", action="store_true", help="use bundled sample deals and synthetic prices")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_backtest)
    sp = sub.add_parser("momentum", help="momentum stats for one or more tickers")
    sp.add_argument("tickers", nargs="+")
    sp.set_defaults(func=cmd_momentum)
    sp = sub.add_parser("news", help="recent headlines for a stock, tagged positive/neutral/negative")
    sp.add_argument("symbol", nargs="?")
    sp.add_argument("--check-tagger", action="store_true", help="show which tagger is active and whether Ollama is ready")
    sp.set_defaults(func=cmd_news)
    sp = sub.add_parser("screen", help="rank an NSE index on momentum, trend, low vol, liquidity")
    sp.add_argument("--universe", default="NIFTY200", help="NIFTY50 | NIFTY100 | NIFTY200 | NIFTY500 | NIFTYMIDCAP150 | NIFTYSMALLCAP250")
    sp.add_argument("--top", type=int, default=20)
    sp.add_argument("--workers", type=int, default=8)
    sp.add_argument("--no-trend-filter", action="store_true", help="don't require price above 200-day MA")
    sp.add_argument("--quality", action="store_true", help="add quality: ROE, low debt, earnings growth (not backtested)")
    sp.add_argument("--value", action="store_true", help="add value: earnings yield, book-to-price (not backtested)")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_screen)
    sp = sub.add_parser("scorecard", help="how Claude's past recommendations did vs the index")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_scorecard)
    sp = sub.add_parser("factor-backtest", help="backtest the factor screen as a monthly portfolio")
    sp.add_argument("--universe", default="NIFTY200")
    sp.add_argument("--top", type=int, default=20)
    sp.add_argument("--years", type=int, default=4, help="test window; one extra year is fetched for lookback")
    sp.add_argument("--no-trend-filter", action="store_true")
    sp.add_argument("--benchmark", default="NIFTYBEES",
                    help="NIFTYBEES includes dividends; ^NSEI is the price-only index")
    sp.add_argument("--changes", metavar="CSV",
                    help="index change log (date,added,removed) for point-in-time membership; NIFTY 50 is built in")
    sp.add_argument("--todays-members", action="store_true",
                    help="ignore membership history and use today's constituents (survivorship-biased)")
    sp.add_argument("--quality", action="store_true", help="also rank on point-in-time quality (NSE results)")
    sp.add_argument("--value", action="store_true", help="also rank on point-in-time value (NSE results)")
    sp.add_argument("--validate", action="store_true",
                    help="also test whether the result is skill or luck (walk-forward, deflated Sharpe, "
                         "Monte Carlo); re-runs the backtest for 2 more portfolio sizes")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_factor_backtest)
    sp = sub.add_parser("research-backward", help="the pre-registered backward check on Apr 2017 to Sep 2020, ONCE "
                                                  "(needs --confirm)")
    sp.add_argument("--confirm", action="store_true", help="really run it: the result is final and the window is spent")
    sp.add_argument("--tri-csv", default=None, help="a Nifty Midcap 150 total return CSV downloaded from niftyindices.com "
                                                    "(instead of fetching it)")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_research_backward)
    sp = sub.add_parser("research-holdout", help="open the sealed holdout ONCE for the adopted rules (needs --confirm)")
    sp.add_argument("--universe", default="NIFTYMIDCAP150")
    sp.add_argument("--confirm", action="store_true", help="really open it: the result is final and the holdout is spent")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_research_holdout)
    sp = sub.add_parser("research-gap", help="research A: why the screen trailed its index fund (timing, "
                                             "selection, charges)")
    sp.add_argument("--universe", default="NIFTYMIDCAP150")
    sp.add_argument("--top", type=int, default=20)
    sp.add_argument("--years", type=int, default=5, help="test years before --until (one more is fetched)")
    sp.add_argument("--until", default=None, help="first date NOT used (default: the sealed holdout, 2025-10-01)")
    sp.add_argument("--liquid-rate", type=float, default=None, help="yearly interest on cash (default 0.065)")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_research_gap)
    for name, fn, hlp in (("research-stops", cmd_research_stops, "research B1: race six stop rules"),
                          ("research-portfolio", cmd_research_portfolio,
                           "research B2-B4: rank buffer, drawdown brake, inverse-volatility weights"),
                          ("research-robustness", cmd_research_robustness,
                           "the adopted rules with next-day fills and 1.5x / 2x costs, and the survivorship count"),
                          ("research-regimes", cmd_research_regimes,
                           "the adopted rules month by month in bull, sideways, bear and high-volatility markets"),
                          ("research-capacity", cmd_research_capacity,
                           "how much capital the adopted rules can run before their own orders move prices"),
                          ("research-momentum", cmd_research_momentum,
                           "the adopted rules with other lookbacks, rebalance spacings and size halves"),
                          ("research-macro", cmd_research_macro,
                           "the adopted rules around oil moves, rupee moves and RBI rate changes (a diagnostic)"),
                          ("research-sectors", cmd_research_sectors,
                           "how crowded the adopted rules' holdings were in one industry (a diagnostic)"),
                          ("research-circuits", cmd_research_circuits,
                           "how many sells fell on days locked at the lower band, and what delaying them costs"),
                          ("research-leakage", cmd_research_leakage,
                           "check that no decision used data published after its day (cut the data and re-decide)")):
        sp = sub.add_parser(name, help=hlp)
        sp.add_argument("--universe", default="NIFTYMIDCAP150")
        sp.add_argument("--years", type=int, default=5, help="years before --until (plus a lookback year)")
        sp.add_argument("--until", default="2025-10-01", help="first date NOT used (the sealed holdout)")
        if name == "research-portfolio":
            from .research_sim import STOP_RULES
            sp.add_argument("--stop", default="none", choices=STOP_RULES,
                            help="stop rule for every variant (the B1 winner, once chosen)")
        if name == "research-leakage":
            sp.add_argument("--days", type=int, default=None, help="rebalance days to check (default: all)")
            sp.add_argument("--cuts", type=int, default=4, help="end-to-end simulation cuts (default 4)")
        sp.add_argument("--json", action="store_true")
        sp.set_defaults(func=fn)
    sp = sub.add_parser("research-batch3", help="research: pre-registered batch 3 (smooth momentum, institutional "
                                                "buying), ONCE")
    sp.add_argument("--confirm", action="store_true", help="really run it (once)")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_research_batch3)
    sp = sub.add_parser("research-replication", help="research: the adopted rules on US, Japanese or UK midcaps, ONCE "
                                                     "per market (pre-registered)")
    # its own dest: an argument named "market" would be read by _settings as the app's MARKET (in / us)
    sp.add_argument("--market", dest="replication_market", choices=("us", "jp", "uk"), required=True)
    sp.add_argument("--confirm", action="store_true", help="really run it (once per market)")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_research_replication)
    sp = sub.add_parser("model-paper", help="the prediction model's own paper account (practice money only)")
    sp.add_argument("--run", action="store_true", help="do today's after-close run now (start, monthly sizing, mark)")
    sp.set_defaults(func=cmd_model_paper)
    sp = sub.add_parser("resmom-paper", help="residual momentum's (variant E's) paper account (practice money only)")
    sp.add_argument("--run", action="store_true", help="do today's after-close run now (start, monthly rebalance, mark)")
    sp.set_defaults(func=cmd_resmom_paper)
    sp = sub.add_parser("app-device", help="phone app: add / list / revoke device secrets (read-only access)")
    sp.add_argument("--state-dir", default="state", help="the state folder (default: state)")
    asub = sp.add_subparsers(dest="action", required=True)
    asub.add_parser("add", help="make a new device secret (shown once)").add_argument("name")
    asub.add_parser("list", help="device names and dates (never secrets)")
    rp = asub.add_parser("revoke", help="stop a device (or --all) from logging in")
    rp.add_argument("name", nargs="?")
    rp.add_argument("--all", action="store_true")
    sp.set_defaults(func=cmd_app_device)
    sp = sub.add_parser("top10-paper", help="variant B's (top 10) paper account (practice money only)")
    sp.add_argument("--run", action="store_true", help="do today's after-close run now (start, monthly rebalance, mark)")
    sp.set_defaults(func=cmd_top10_paper)
    sp = sub.add_parser("research-pead", help="research: post-results drift and promoter-buying signals")
    sp.add_argument("--universe", default="NIFTYMIDCAP150")
    sp.add_argument("--years", type=int, default=5)
    sp.add_argument("--until", default="2025-10-01", help="first date NOT used (the sealed holdout)")
    sp.add_argument("--holdout", action="store_true",
                    help="run once on the sealed holdout (1 Oct 2025 to today); refused after the first time")
    sp.set_defaults(func=cmd_research_pead)
    from .research_sim import STOP_RULES as _RULES
    for name, fn, hlp in (("research-barrier", cmd_research_barrier,
                           "signal lab: triple-barrier exits for every signal's top fifth"),
                          ("research-cpcv", cmd_research_cpcv,
                           "signal lab: the ridge model under purged cross-validation (5 paths)"),
                          ("research-meta", cmd_research_meta,
                           "signal lab: meta-labeling, three fitting objectives"),
                          ("research-model", cmd_research_model,
                           "the prediction model (sizes the momentum picks); --holdout once")):
        sp = sub.add_parser(name, help=hlp)
        sp.add_argument("--universe", default="NIFTYMIDCAP150")
        sp.add_argument("--years", type=int, default=5)
        sp.add_argument("--until", default="2025-10-01", help="first date NOT used (the sealed holdout)")
        if name != "research-cpcv":
            sp.add_argument("--stop", default="two_stage", choices=_RULES,
                            help="stop rule for the barrier (use the B1 winner)")
        if name == "research-barrier":
            sp.add_argument("--target", type=float, default=3.0, help="profit target in R (R = 2.5 x ATR)")
            sp.add_argument("--days", type=int, default=60, help="time limit in trading days")
        if name == "research-cpcv":
            sp.add_argument("--horizon", type=int, default=20)
        if name == "research-model":
            sp.add_argument("--holdout", action="store_true",
                            help="after a pass on the development years, run once on the sealed holdout")
        sp.set_defaults(func=fn)
    sp = sub.add_parser("research-coffee-can", help="research C: the Coffee Can screen, held a year at a time")
    sp.add_argument("--years", type=int, default=5)
    sp.add_argument("--until", default="2025-10-01", help="first date NOT used (the sealed holdout)")
    sp.add_argument("--min-years", type=int, default=4, help="years of growth every stock needs (the book: 10)")
    sp.add_argument("--estimate-debt", action="store_true",
                    help="before FY2023 (no balance sheet): borrowings = finance costs / 9%%, marked estimated")
    sp.add_argument("--window", type=int, default=None,
                    help="test only the latest N years of growth (the book's rolling window); default: every year seen")
    sp.add_argument("--holdout", action="store_true", help="run once on the sealed holdout; refused after that")
    sp.set_defaults(func=cmd_research_coffee_can)
    sp = sub.add_parser("insider-history", help="download NSE insider disclosures month by month (resumable)")
    sp.add_argument("--years", type=int, default=7)
    sp.add_argument("--max-months", type=int, default=120)
    sp.set_defaults(func=cmd_insider_history)
    sp = sub.add_parser("fundamentals-history", help="download NSE quarterly results for a universe (resumable)")
    sp.add_argument("--universe", default="NIFTYMIDCAP150")
    sp.add_argument("--years", type=int, default=5, help="also cover stocks that were members this far back")
    sp.add_argument("--max", type=int, default=3000, help="most new filings to download this run")
    sp.add_argument("--annual", action="store_true",
                    help="the March (full-year) filings with profit before tax and finance costs (Coffee Can research)")
    sp.set_defaults(func=cmd_fundamentals_history)
    sp = sub.add_parser("fundamentals-inspect",
                        help="show what one March results filing holds (contexts, balance-sheet items); reads only")
    sp.add_argument("symbol")
    sp.add_argument("--fy", type=int, default=2021, help="financial year ending March of this year")
    sp.set_defaults(func=cmd_fundamentals_inspect)
    sp = sub.add_parser("index-history", help="rebuild past index members from NSE press releases")
    sp.add_argument("index", nargs="+", help="e.g. NIFTYMIDCAP150 NIFTYSMALLCAP250")
    sp.add_argument("--since", default="2021-01-01")
    sp.set_defaults(func=cmd_index_history)
    sp = sub.add_parser("signal-lab", help="test whether algo-trading signals predicted returns")
    sp.add_argument("--universe", default="NIFTY50")
    sp.add_argument("--years", type=int, default=5)
    sp.add_argument("--horizons", default="5,20,60", help="holding periods in trading days")
    sp.add_argument("--changes", metavar="CSV", help="index change log for point-in-time membership")
    sp.add_argument("--todays-members", action="store_true",
                    help="use today's constituents (survivorship-biased)")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_signal_lab)
    sp = sub.add_parser("costs", help="show the verified Indian delivery cost model")
    sp.add_argument("amounts", nargs="*", type=float, default=[10_000, 25_000, 100_000])
    sp.set_defaults(func=cmd_costs)
    sp = sub.add_parser("size", help="volatility-based position size for tickers")
    sp.add_argument("tickers", nargs="+")
    sp.add_argument("--equity", type=float, help="override account equity")
    sp.set_defaults(func=cmd_size)
    sp = sub.add_parser("watch", help="always-on local mode: poll deals + announcements in market hours")
    sp.add_argument("--every", type=int, default=60, help="seconds between polls")
    sp.add_argument("--window-start", default="08:45", help="IST, HH:MM")
    sp.add_argument("--window-end", default="18:30", help="IST, HH:MM")
    sp.add_argument("--auto-trade", action="store_true", help="allow paper orders")
    sp.set_defaults(func=cmd_watch)
    sp = sub.add_parser("integration-check",
                        help="read-only check that NSE, BSE, Yahoo, Groww (cached token) and the rest still answer as expected")
    sp.add_argument("--claude", action="store_true", help="also make one tiny Claude call (same as INTEGRATION_CLAUDE=true)")
    sp.add_argument("--no-alert", action="store_true", help="do not send the once-a-day failure alert")
    sp.set_defaults(func=cmd_integration_check)
    sp = sub.add_parser("digest", help="print (and with --send, email) the morning or evening digest")
    sp.add_argument("kind", choices=["morning", "evening"])
    sp.add_argument("--send", action="store_true", help="email it through the configured channels")
    sp.add_argument("--writer", choices=["rules", "auto", "ollama", "claude", "none"],
                    help="who writes the summary on top (default DIGEST_WRITER); none = rules only")
    sp.set_defaults(func=cmd_digest)
    sp = sub.add_parser("replay", help="Replay tools")
    rsub = sp.add_subparsers(dest="replay_cmd", required=True)
    sp = rsub.add_parser("blind-test", help="paired masked/unmasked Ask Claude test for hindsight (costs API money)")
    sp.add_argument("slug", help="the replay's name (its folder under state/replay)")
    sp.add_argument("--cases", type=int, default=5, help="how many cases to pick from the replay's history")
    sp.add_argument("--case", action="append", metavar="DATE:TICKER", help="a specific case (repeatable)")
    sp.add_argument("--model", help="Claude model (default: CLAUDE_MODEL)")
    sp.add_argument("--yes", action="store_true", help="really send the calls (without it, only the estimate)")
    sp.add_argument("--fresh", action="store_true", help="start a new run instead of resuming today's unfinished one")
    sp.set_defaults(func=cmd_replay_blind_test)
    sp = rsub.add_parser("debate-test", help="research: bull-vs-bear debate vs the single call (costs API money)")
    sp.add_argument("slug", help="the replay's name (its folder under state/replay)")
    sp.add_argument("--cases", type=int, default=10, help="how many blind cases")
    sp.add_argument("--since", default="2025-10-01", help="prefer cases from this date on (months not tuned on)")
    sp.add_argument("--model", help="Claude model (default: CLAUDE_MODEL)")
    sp.add_argument("--yes", action="store_true", help="really send the calls (without it, only the estimate)")
    sp.set_defaults(func=cmd_replay_debate_test)
    sp = sub.add_parser("ui", help="open the local web dashboard")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8787)
    sp.add_argument("--no-open", action="store_true", help="don't open a browser tab")
    sp.add_argument("--demo", action="store_true", help="use bundled sample deals and prices")
    sp.set_defaults(func=cmd_ui)
    return p


def _utf8_output() -> None:
    """Rupee signs and Indian names must print even when output goes to a file or pipe on Windows,
    where Python otherwise falls back to the cp1252 code page."""
    for stream in (sys.stdout, sys.stderr):
        try:
            if (getattr(stream, "encoding", "") or "").lower().replace("-", "") != "utf8":
                stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):  # not a real text stream (e.g. under a test capture)
            pass


def main(argv: list[str] | None = None) -> int:
    _utf8_output()
    from .notify import install_log_redaction
    install_log_redaction()   # bot tokens and the heartbeat path never reach a log, even at -v (guarded: once)
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    halted = _halt_refusal(args)
    if halted:
        print(halted, file=sys.stderr)
        return 1
    try:
        return args.func(args)
    except GrowwTokenUnavailable as e:  # any command: a clear line instead of a traceback
        print(_token_error_text(e), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
