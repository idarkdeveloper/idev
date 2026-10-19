"""The drawdown brake (100-day average) on paper, beside the adopted rules.

The pre-registered India test (``docs/research/preregistered-india-2007-2016.md`` with addendum 1) ran the brake once
on Oct 2007 to Mar 2016 and it passed: +295.0% against +119.6% for the adopted rules, worst fall -31.5% against
-73.9%, after double charges and tax. In the research years (Oct 2020 to Sep 2025) the same brake lost to the plain
rules. Addendum 1 fixed in advance that a pass earns a paper account and changes nothing live; the note
``docs/research/brake-paper-account-2026-10-19.md`` sets how it is read.

Everything is the forward test's, plus the brake, exactly as ``research_sim`` runs ``brake="sma100"``:

* the same screen, ranking, price bands, Indian filters and research rules (top 20, buffer 35, tax-aware wait with
  floor 50, 25% floor, no stop);
* after each close, if the benchmark fund (MID150BEES for the Midcap 150) closes below its own 100-day average the
  account sells every holding and holds cash; while the brake is on the monthly rebalance buys nothing;
* when the fund closes back at or above its average the brake lifts, and the account buys again at the next monthly
  rebalance (the simulator re-enters at the next rebalance too).

From ``BRAKE_PAPER_START`` (default 20 Oct 2026; ``off`` disables it), in its own files
(``state/forward/brake_<universe>.json``), practice money only. Nothing is retuned on what it shows.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

from .timezones import IST

log = logging.getLogger(__name__)

SMA_DAYS = 100
VARIANT = ("BRK: drawdown brake, 100-day average (preregistered-india-2007-2016-addendum.md; passed the India "
           "Oct 2007 - Mar 2016 test)")


def account_name(universe: str) -> str:
    return f"brake_{universe.lower()}"


def brake_on(closes: list[float]) -> bool | None:
    """True when the last close is below the average of the last SMA_DAYS closes; None with too little history (the
    simulator then leaves the brake off)."""
    if len(closes) < SMA_DAYS:
        return None
    window = closes[-SMA_DAYS:]
    return closes[-1] < sum(window) / SMA_DAYS


class BrakePaper:
    """The brake's paper account: a ForwardTest under its own name, the adopted rules plus the 100-day brake."""

    def __init__(self, state_dir: Path, *, universe: str = "NIFTYMIDCAP150", capital: float = 500_000.0,
                 price_fn: Callable[[str], float] | None, cost_model: Any | None = None,
                 now: Callable[[], datetime] | None = None, holidays: Any | None = None):
        from .forward import RULES, ForwardTest
        self.universe = universe.upper()
        self.ft = ForwardTest(state_dir, universe=self.universe, top=20, capital=capital, price_fn=price_fn,
                              cost_model=cost_model, now=now, holidays=holidays, rules=dict(RULES),
                              name=account_name(self.universe))

    def _sell_all(self) -> list[dict[str, Any]]:
        sold = []
        for p in self.ft.broker.positions():
            row: dict[str, Any] = {"symbol": p.symbol, "qty": p.qty}
            try:
                o = self.ft.broker.submit_order(p.symbol, "sell", qty=int(p.qty))
                row["price"] = o["filled_avg_price"]
            except Exception as e:  # noqa: BLE001 - reported, tried again at the next run
                row["error"] = str(e)
            sold.append(row)
        return sold

    def run(self, screen_fn: Callable[[], dict[str, Any]] | None,
            closes_fn: Callable[[str], list[float]]) -> dict[str, Any]:
        """One after-close run: read the brake on the benchmark fund's closes, act on it, then the forward test's own
        run (rebalance only when the brake is off)."""
        d = self.ft.data
        d.setdefault("variant", VARIANT)
        if not self.ft.started:
            self.ft.start()
        today = self.ft.now().date().isoformat()
        on = brake_on(closes_fn(d["benchmark"]))
        was = bool(d.get("braked"))
        event = None
        if on and not was:
            event = {"date": today, "brake": "on", "sold": self._sell_all()}
        elif on is False and was:
            event = {"date": today, "brake": "off"}
        if on is not None:
            d["braked"] = bool(on)
        if event:
            d["brake_events"] = (d.get("brake_events", []) + [event])[-100:]
        if d.get("braked") and self.ft.rebalance_due():
            # the simulator buys nothing at a rebalance while the brake is on, and re-enters at the next one
            d["last_rebalance"] = self.ft._month()
            d["rebalances"] = (d["rebalances"] + [{"date": today, "month": self.ft._month(), "picks": [],
                                                   "trades": [], "charges": 0.0, "skipped": "brake on"}])[-60:]
        summary = self.ft.run(None if d.get("braked") else screen_fn)
        return {**summary, "braked": bool(d.get("braked")), "brake_event": event,
                "brake_events": d.get("brake_events", [])[-10:]}


def format_brake_paper(s: dict[str, Any], control: dict[str, Any] | None = None) -> str:
    def pct(v: Any) -> str:
        return "n/a" if v is None else f"{v * 100:+.2f}%"
    lines = [f"Brake paper account (100-day average on {s['benchmark']}, {s['universe']} top {s['top']}), since "
             f"{str(s.get('started') or '')[:10]}: {pct(s.get('strategy_return'))} vs {s['benchmark']} "
             f"{pct(s.get('benchmark_return'))} (gap {pct(s.get('gap'))}), worst fall "
             f"{pct(s.get('strategy_max_drawdown'))}, {s['days']} day(s), charges {s.get('charges_paid', 0):,.0f}.",
             f"  Brake: {'ON, in cash' if s.get('braked') else 'off, invested as the adopted rules'}."]
    ev = s.get("brake_event")
    if ev:
        lines.append(f"  Today the brake turned {ev['brake']}"
                     + (f": sold {len(ev.get('sold', []))} holding(s)." if ev["brake"] == "on" else
                        "; the account buys again at the next monthly rebalance."))
    if control and control.get("strategy_return") is not None and s.get("strategy_return") is not None:
        lines.append(f"  The adopted rules (forward test) over the same days, {control['from']} to {control['to']}: "
                     f"{pct(control['strategy_return'])}; brake minus the adopted rules: "
                     f"{pct(s['strategy_return'] - control['strategy_return'])}.")
    r = s.get("rebalanced")
    if r:
        ok = [t for t in r["trades"] if "error" not in t]
        lines.append(f"  Rebalanced {r['date']}: {len(r['picks'])} picks, {len(ok)} trades, charges {r['charges']:,.0f}.")
    lines.append("  Passed the India 2007-2016 test (+295% vs +120% for the adopted rules, worst fall -31.5% vs -73.9%) "
                 "but lost in 2020-2025: insurance for long bear markets. Practice money only; it changes nothing "
                 "live.")
    if s["days"] < 60:
        lines.append("  Too early to judge.")
    return "\n".join(lines)


def run_brake_paper_due(settings: Any, prices: Any, holidays: Any, *, today: date | None = None,
                        screen_fn: Callable[[], dict[str, Any]] | None = None,
                        closes_fn: Callable[[str], list[float]] | None = None) -> str:
    """One after-close run: nothing before BRAKE_PAPER_START; brake check, monthly rebalance when off, mark every
    trading day. Never raises (a failure is reported in the text)."""
    from . import killswitch
    from .costs import cost_model_for
    from .resmom_paper import control_over
    start = getattr(settings, "brake_paper_start", None)
    today = today or datetime.now(IST).date()
    if not start:
        return "Brake paper account: off (BRAKE_PAPER_START=off)."
    if today.isoformat() < start:
        return f"Brake paper account: starts {start}."
    halted = killswitch.reason(settings.state_dir, "run the brake paper account")
    if halted:
        return halted
    try:
        universe = settings.forward_universe.upper()
        bp = BrakePaper(settings.state_dir, universe=universe, capital=settings.paper_starting_cash,
                        price_fn=prices.latest_price, cost_model=cost_model_for("in"), holidays=holidays)
        if not bp.ft.due():
            return "Brake paper account: nothing due."

        def screen() -> dict[str, Any]:
            if screen_fn is not None:
                return screen_fn()
            from .bands import book_for
            from .indian_filters import filters_for
            from .screen import load_universe, run_screen
            return run_screen(load_universe(universe), prices, top=20, bands=book_for(settings),
                              filters=filters_for(settings))

        def closes(sym: str) -> list[float]:
            if closes_fn is not None:
                return closes_fn(sym)
            bars = prices.history(sym, "1y")
            return [float(b["close"]) for b in bars if b.get("close") and b["date"] <= today.isoformat()]

        summary = bp.run(screen, closes)
        return format_brake_paper(summary, control_over(settings.state_dir, universe, summary.get("started")))
    except Exception as e:  # noqa: BLE001 - the forward test's job must carry on
        log.exception("brake paper account failed")
        return f"Brake paper account failed: {type(e).__name__}: {e}"
