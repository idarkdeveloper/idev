"""Command line entry point: ``python -m trading_agent <command>``."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from importlib import resources
from pathlib import Path
from typing import Any

from .broker import LocalPaperBroker
from .config import load_settings
from .quiver import _norm_congress, filter_by_investor
from .runner import check, make_broker


def _demo_inputs(settings):
    """Fixture trades + a local broker with fixed prices, so no data keys are needed."""
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
    trades = filter_by_investor(rows, settings.watch_investor)
    broker = LocalPaperBroker(settings.state_dir / "paper_broker.json",
                              starting_cash=settings.paper_starting_cash,
                              price_fn=lambda s: prices[s], currency=settings.currency,
                              whole_shares=settings.market == "in")
    return trades, broker


def _print_result(result) -> None:
    if result.skipped and not result.new_trades:
        print(f"No new disclosed trades for {result.investor}. Nothing to do.")
        return
    print(f"New disclosed trades for {result.investor}: {len(result.new_trades)}")
    for t in result.new_trades:
        print(f"  - {t.summary()}")
    if result.skipped:
        print("(dry run: Claude was not called)")
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


def _settings(args: argparse.Namespace):
    if getattr(args, "market", None):
        import os
        os.environ["MARKET"] = args.market
    return load_settings()


def cmd_check(args: argparse.Namespace) -> int:
    settings = _settings(args)
    if args.investor:
        settings.watch_investor = args.investor
    if args.auto_trade:
        settings.auto_trade = True
    kwargs: dict[str, Any] = {}
    if args.demo:
        trades, broker = _demo_inputs(settings)
        kwargs.update(trades=trades, broker=broker)
    result = check(settings, force=args.force, dry_run=args.dry_run, **kwargs)
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
    return 0


def cmd_groww_token(args: argparse.Namespace) -> int:
    """Print a fresh Groww access token (valid until 06:00 IST next day)."""
    from .runner import resolve_groww_token
    settings = _settings(args)
    settings.groww_access_token = None  # force generation from key + secret / TOTP
    print(resolve_groww_token(settings))
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
    from .backtest import format_summary, run_backtest
    from .runner import free_prices, make_data_source

    settings = _settings(args)
    investor = args.investor or settings.watch_investor
    horizons = tuple(int(h) for h in args.horizons.split(","))
    if args.demo:
        import dataclasses
        import datetime as dt
        deals, broker = _demo_inputs(settings)
        # Sample deals are dated today; pretend they happened a quarter ago so there is
        # price history after them to measure.
        back = (dt.date.today() - dt.timedelta(days=100)).isoformat()
        deals = [dataclasses.replace(d, transaction_date=back, report_date=back) for d in deals]
        prices = _DemoHistory(broker.price_fn)
    else:
        data = make_data_source(settings)
        deals = data.trades_for_investor(investor, settings.watch_source, days=args.days) \
            if settings.data_source == "nse" else data.trades_for_investor(investor, settings.watch_source)
        prices = free_prices(settings)
    print(f"{len(deals)} disclosed deals by {investor} in the last {args.days} days; pricing…")
    result = run_backtest(investor, deals, prices, horizons=horizons, cost_bps=args.cost_bps,
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
        except Exception:
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


def cmd_watch(args: argparse.Namespace) -> int:
    """Always-on local mode: poll deals and announcements during market hours."""
    from .runner import make_broker, make_data_source, make_notifier
    from .watch import Watcher

    settings = _settings(args)
    if args.auto_trade:
        settings.auto_trade = True
    data = make_data_source(settings)
    broker = make_broker(settings)
    notifier = make_notifier(settings)
    w = Watcher(settings, every=args.every, window=(args.window_start, args.window_end),
                data=data, broker=broker, notifier=notifier,
                check_fn=lambda: check(settings, broker=broker, data=data, notifier=notifier))
    print(f"Watching {settings.watch_investor} every {w.every}s, {args.window_start}-{args.window_end} IST, "
          f"weekdays. Ctrl+C to stop.")
    try:
        w.run_forever()
    except KeyboardInterrupt:
        pass
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
        sp.add_argument("--investor", help="override WATCH_INVESTOR")
        sp.add_argument("--force", action="store_true", help="run Claude even with no new trades")
        sp.add_argument("--dry-run", action="store_true", help="fetch & diff only; don't call Claude")
        sp.add_argument("--demo", action="store_true", help="use bundled sample trades/prices")
        sp.add_argument("--auto-trade", action="store_true", help="allow PAPER orders this run")
        sp.add_argument("--json", action="store_true", help="also print the result as JSON")

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
    sub.add_parser("groww-token", help="generate a Groww access token from API key + secret/TOTP") \
        .set_defaults(func=cmd_groww_token)
    sp = sub.add_parser("backtest", help="replay an investor's disclosed deals vs NIFTY 50")
    sp.add_argument("--investor", help="override WATCH_INVESTOR")
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
    sp = sub.add_parser("watch", help="always-on local mode: poll deals + announcements in market hours")
    sp.add_argument("--every", type=int, default=60, help="seconds between polls")
    sp.add_argument("--window-start", default="08:45", help="IST, HH:MM")
    sp.add_argument("--window-end", default="18:30", help="IST, HH:MM")
    sp.add_argument("--auto-trade", action="store_true", help="allow paper orders")
    sp.set_defaults(func=cmd_watch)
    sp = sub.add_parser("ui", help="open the local web dashboard")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8787)
    sp.add_argument("--no-open", action="store_true", help="don't open a browser tab")
    sp.add_argument("--demo", action="store_true", help="use bundled sample deals and prices")
    sp.set_defaults(func=cmd_ui)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
