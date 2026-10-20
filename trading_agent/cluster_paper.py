"""Cluster buying on paper: stocks where several insiders bought in the open market, held six months.

The plan is ``docs/research/cluster-buying-paper-account-2026-10-21.md`` (committed before the first run). A stock has a
cluster when, among the insider trades DISCLOSED in the last 30 days (``state/insider_book.json``, refreshed by the
watch), at least 3 different insiders (promoters, promoter group, directors, key managers, immediate relatives) bought
equity shares in the open market, on at least 2 different trade dates (a one-day family block does not count).

The account buys new clusters that pass the forward test's screen filters (liquidity, price bands, ASM/GSM, results,
pledges; no trend rule) in 5% slots, up to 20 names, at the first run of each month after the close; it sells a holding
at the first monthly run 182 days or more after buying it, or any day it closes 25% below its buy price. Benchmark:
HDFCSML250 (the Nifty Smallcap 250 fund). From ``CLUSTER_PAPER_START`` (default 22 Oct 2026; ``off`` disables it), in
its own files, practice money only. Nothing is retuned on what it shows.
"""

from __future__ import annotations

import logging
import math
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable

from .timezones import IST

log = logging.getLogger(__name__)

# --- fixed by the plan (cluster-buying-paper-account-2026-10-21.md); do not edit ------------------------------- #
MIN_INSIDERS = 3
MIN_TRADE_DATES = 2
WINDOW_DAYS = 30
SLOTS = 20
SLOT_WEIGHT = 0.05
HOLD_DAYS = 182
FLOOR = 0.25
BENCHMARK = "HDFCSML250"
VARIANT = "Cluster buying: 3+ insiders in the open market on 2+ dates within 30 days (cluster-buying-paper-account-2026-10-21.md)"
# ------------------------------------------------------------------------------------------------------------------ #
_RELATIVE = re.compile(r"relative", re.I)


def account_name(universe: str) -> str:
    return f"cluster_{universe.lower()}"


def _person(name: Any) -> str:
    return " ".join(str(name or "").upper().split())


def counts(r: dict[str, Any]) -> bool:
    """An open-market purchase of equity shares by a promoter, director, key manager or immediate relative."""
    from .insider_watch import is_equity, is_insider, is_market
    return (r.get("side") == "buy" and is_market(r) and is_equity(r)
            and (is_insider(r) or bool(_RELATIVE.search(str(r.get("category") or "")))))


def clusters(rows: Iterable[dict[str, Any]], day: str) -> list[dict[str, Any]]:
    """The stocks with a cluster on ``day`` (ISO), from compact insider-book rows; the largest total value first."""
    since = (date.fromisoformat(day) - timedelta(days=WINDOW_DAYS)).isoformat()
    by: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        public = str(r.get("reported") or "")[:10]
        if not public or not (since < public <= day) or not counts(r) or not r.get("symbol"):
            continue
        by.setdefault(str(r["symbol"]).upper(), []).append(r)
    out = []
    for sym, rs in by.items():
        people = {_person(r.get("person")) for r in rs} - {""}
        dates = {str(r.get("date") or "")[:10] for r in rs} - {""}
        if len(people) >= MIN_INSIDERS and len(dates) >= MIN_TRADE_DATES:
            out.append({"symbol": sym, "company": next((r.get("company") for r in rs if r.get("company")), None),
                        "insiders": len(people), "dates": len(dates), "value": sum(float(r.get("value") or 0) for r in rs),
                        "first": min(dates), "last": max(dates)})
    return sorted(out, key=lambda c: (-c["value"], c["symbol"]))


class ClusterPaper:
    """The cluster-buying paper account: a ForwardTest (start, mark, floor, summary) traded by its own rule."""

    def __init__(self, state_dir: Path, *, universe: str = "NIFTYMIDCAP150", capital: float = 500_000.0,
                 price_fn: Callable[[str], float] | None, cost_model: Any | None = None,
                 now: Callable[[], datetime] | None = None, holidays: Any | None = None):
        from .forward import ForwardTest
        self.universe = universe.upper()
        self.cost_model = cost_model
        self.ft = ForwardTest(state_dir, universe=self.universe, top=SLOTS, capital=capital, benchmark=BENCHMARK,
                              price_fn=price_fn, cost_model=cost_model, now=now, holidays=holidays,
                              rules={"floor": FLOOR}, name=account_name(self.universe))

    def _monthly(self, found: list[dict[str, Any]], screen_fn: Callable[[list[str]], dict[str, Any]]) -> dict[str, Any]:
        ft, d = self.ft, self.ft.data
        today = ft.now().date()
        fees_before = ft.broker.performance()["fees_paid"]
        bought = d.setdefault("bought", {})
        held = {p.symbol: p for p in ft.broker.positions()}
        for sym in [s for s in bought if s not in held]:     # sold by the floor since: forget the date
            bought.pop(sym)
        sold = []
        for sym, p in held.items():
            since = bought.get(sym)
            if since and (today - date.fromisoformat(since)).days >= HOLD_DAYS:
                row: dict[str, Any] = {"symbol": sym, "qty": p.qty, "bought": since}
                try:
                    o = ft.broker.submit_order(sym, "sell", qty=int(p.qty))
                    row["price"] = o["filled_avg_price"]
                    bought.pop(sym, None)
                except Exception as e:  # noqa: BLE001 - reported, tried again next month
                    row["error"] = str(e)
                sold.append(row)
        still = {p.symbol for p in ft.broker.positions()}
        new = [c for c in found if c["symbol"] not in still]
        passed: list[str] = []
        if new:
            result = screen_fn([c["symbol"] for c in new])
            ok = {r["symbol"] for r in result.get("all", []) if r.get("eligible")}
            passed = [c["symbol"] for c in new if c["symbol"] in ok]
        equity = ft.broker.account().equity
        bought_now = []
        for sym in passed[:max(0, SLOTS - len(still))]:
            cash = ft.broker.account().cash
            budget = min(cash, equity * SLOT_WEIGHT)
            try:
                price = float(ft.price_fn(sym))
                charges = float(self.cost_model.charges("buy", budget)) if self.cost_model else 0.0
                qty = int(math.floor((budget - charges) / price))
                if qty < 1:
                    continue
                o = ft.broker.submit_order(sym, "buy", qty=qty)
                bought[sym] = today.isoformat()
                bought_now.append({"symbol": sym, "qty": qty, "price": o["filled_avg_price"]})
            except Exception as e:  # noqa: BLE001 - one bad price never stops the rest
                bought_now.append({"symbol": sym, "error": str(e)})
        entry = {"date": today.isoformat(), "month": ft._month(), "clusters": [c["symbol"] for c in found],
                 "passed": passed, "bought": bought_now, "sold": sold, "picks": sorted(still | {b["symbol"] for b in bought_now if "error" not in b}),
                 "trades": bought_now + sold,
                 "charges": round(ft.broker.performance()["fees_paid"] - fees_before, 2)}
        d["rebalances"] = (d["rebalances"] + [entry])[-60:]
        d["last_rebalance"] = ft._month()
        return entry

    def run(self, insider_rows: list[dict[str, Any]], screen_fn: Callable[[list[str]], dict[str, Any]]) -> dict[str, Any]:
        """One after-close run: start, the 25% floor, the monthly buys and sales, today's point."""
        ft, d = self.ft, self.ft.data
        d.setdefault("variant", VARIANT)
        if not ft.started:
            ft.start()
        if "rules" not in d:
            d["rules"] = {"floor": FLOOR, "since": ft.now().date().isoformat()}
        floor_sales = ft.apply_floor()
        found = clusters(insider_rows, ft.now().date().isoformat())
        monthly = self._monthly(found, screen_fn) if ft.rebalance_due() else None
        ft.mark()
        ft.save()
        acct = ft.broker.account()
        return {**ft.summary(), "rebalanced": monthly, "floor_sales": floor_sales, "clusters_now": found[:20],
                "invested": (acct.equity - acct.cash) / acct.equity if acct.equity else 0.0}


def format_cluster_paper(s: dict[str, Any], control: dict[str, Any] | None = None) -> str:
    def pct(v: Any) -> str:
        return "n/a" if v is None else f"{v * 100:+.2f}%"
    lines = [f"Cluster-buying paper account (3+ insiders, held 6 months), since {str(s.get('started') or '')[:10]}: "
             f"{pct(s.get('strategy_return'))} vs {s['benchmark']} {pct(s.get('benchmark_return'))} "
             f"(gap {pct(s.get('gap'))}), worst fall {pct(s.get('strategy_max_drawdown'))}, {s['days']} day(s), "
             f"charges {s.get('charges_paid', 0):,.0f}; {len(s.get('holdings') or [])} holding(s), "
             f"{(s.get('invested') or 0) * 100:.0f}% invested."]
    r = s.get("rebalanced")
    if r:
        ok = [b["symbol"] for b in r["bought"] if "error" not in b]
        lines.append(f"  This month: {len(r['clusters'])} cluster(s), {len(r['passed'])} passed the filters, bought "
                     f"{', '.join(ok) or 'nothing'}" + (f"; sold after 6 months: {', '.join(x['symbol'] for x in r['sold'])}"
                                                         if r["sold"] else "") + ".")
    if control and control.get("strategy_return") is not None and s.get("strategy_return") is not None:
        lines.append(f"  The adopted rules (forward test) over the same days, {control['from']} to {control['to']}: "
                     f"{pct(control['strategy_return'])}.")
    lines.append("  Judged after 12 months against the Smallcap 250 fund. Practice money only; it changes nothing live.")
    if s["days"] < 60:
        lines.append("  Too early to judge.")
    return "\n".join(lines)


def run_cluster_paper_due(settings: Any, prices: Any, holidays: Any, *, today: date | None = None,
                          rows_fn: Callable[[], list[dict[str, Any]]] | None = None,
                          screen_fn: Callable[[list[str]], dict[str, Any]] | None = None) -> str:
    """One after-close run: nothing before CLUSTER_PAPER_START; floor daily, buys and sales monthly, mark every
    trading day. Never raises (a failure is reported in the text)."""
    from . import killswitch
    from .costs import cost_model_for
    from .resmom_paper import control_over
    start = getattr(settings, "cluster_paper_start", None)
    today = today or datetime.now(IST).date()
    if not start:
        return "Cluster-buying paper account: off (CLUSTER_PAPER_START=off)."
    if today.isoformat() < start:
        return f"Cluster-buying paper account: starts {start}."
    halted = killswitch.reason(settings.state_dir, "run the cluster-buying paper account")
    if halted:
        return halted
    try:
        universe = settings.forward_universe.upper()
        cp = ClusterPaper(settings.state_dir, universe=universe, capital=settings.paper_starting_cash,
                          price_fn=prices.latest_price, cost_model=cost_model_for("in"), holidays=holidays)
        if not cp.ft.due():
            return "Cluster-buying paper account: nothing due."

        def rows() -> list[dict[str, Any]]:
            if rows_fn is not None:
                return rows_fn()
            from .insider_watch import book_for
            return list(book_for(settings).load().get("rows") or [])

        def screen(symbols: list[str]) -> dict[str, Any]:
            if screen_fn is not None:
                return screen_fn(symbols)
            from .bands import book_for as bands_for
            from .indian_filters import filters_for
            from .screen import run_screen
            return run_screen([{"symbol": s} for s in symbols], prices, top=len(symbols), require_above_200dma=False,
                              bands=bands_for(settings), filters=filters_for(settings))

        summary = cp.run(rows(), screen)
        return format_cluster_paper(summary, control_over(settings.state_dir, universe, summary.get("started")))
    except Exception as e:  # noqa: BLE001 - the forward test's job must carry on
        log.exception("cluster-buying paper account failed")
        return f"Cluster-buying paper account failed: {type(e).__name__}: {e}"
