"""Command line entry point: ``python -m trading_agent <command>``."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from importlib import resources
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
            recs = h.history(sym)
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
        return run_screen(members, prices, top=ft.data["top"], bands=book_for(settings))

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
    from .live import GttStopManager
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
        mgr = GttStopManager(make_groww(settings, prices), st, notifier=make_notifier(settings),
                             bars_fn=lambda sym: prices.history(sym, "1y"))
        for a in mgr.sync():
            print("  ", a)
        st.save()
    stops = st.data.get("gtt_stops", {})
    if not stops:
        print("No GTT stop-losses recorded.")
    for sym, r in sorted(stops.items()):
        print(f"{sym:<12} qty {r.get('qty')}  trigger {r.get('trigger')}  limit {r.get('limit')}  "
              f"{r.get('status')}  {r.get('smart_order_id')}" + (f"  last error: {r['last_error']}" if r.get("last_error") else ""))
    return 0


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
                        bands=book_for(settings))
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
    forward = ForwardScheduler(settings.state_dir, lambda: print(run_forward_due(settings, forward_prices, holidays, notifier)),
                               holidays=holidays)
    w = Watcher(settings, every=args.every, news=news, digest=digest, forward=forward,
                integration=make_integration_scheduler(settings, notifier, holidays),
                backup=make_backup_scheduler(settings, holidays),
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
    try:
        w.run_forever()
    except KeyboardInterrupt:
        pass
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
        delivered = send_digest(notifier, email, allowed_hosts=getattr(settings, "allowed_hosts", None))
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
    sp.add_argument("--force", action="store_true",
                    help="ask Groww for a token even while waiting after a refusal (may extend Groww's wait)")
    sp.add_argument("--ip", action="store_true",
                    help="only print this machine's public IP and whether it matches GROWW_ALLOWED_IP (no credentials)")
    sp.set_defaults(func=cmd_groww_check)
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
    sp = sub.add_parser("fundamentals-history", help="download NSE quarterly results for a universe (resumable)")
    sp.add_argument("--universe", default="NIFTYMIDCAP150")
    sp.add_argument("--years", type=int, default=5, help="also cover stocks that were members this far back")
    sp.add_argument("--max", type=int, default=3000, help="most new filings to download this run")
    sp.set_defaults(func=cmd_fundamentals_history)
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
    try:
        return args.func(args)
    except GrowwTokenUnavailable as e:  # any command: a clear line instead of a traceback
        print(_token_error_text(e), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
