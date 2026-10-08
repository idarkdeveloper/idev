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
    rows = json.loads((fx / "congress_sample.json").read_text())
    prices = json.loads((fx / "prices.json").read_text())
    trades = filter_by_investor([_norm_congress(r) for r in rows], settings.watch_investor)
    broker = LocalPaperBroker(settings.state_dir / "paper_broker.json",
                              starting_cash=settings.paper_starting_cash,
                              price_fn=lambda s: prices[s])
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


def cmd_check(args: argparse.Namespace) -> int:
    settings = load_settings()
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
    settings = load_settings()
    broker = make_broker(settings)
    acct = broker.account()
    print(f"Broker: {broker.name}   cash ${acct.cash:,.2f}   equity ${acct.equity:,.2f}")
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
              f"over {perf['orders']} orders from ${perf['starting_cash']:,.0f}")
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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="trading_agent",
                                description="Claude agent that follows an investor's disclosed trades.")
    p.add_argument("-v", "--verbose", action="store_true")
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
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
