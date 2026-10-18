"""Variant B (top 10) on paper: its 12-month forward test beside the adopted rules.

The backward check, rerun once on daily prices on 18 Oct 2026 (``Backward-batch-v2``; the plan is
``docs/research/preregistered-batch-2026-10-16.md`` with the rerun addendum ``rerun-addendum-2026-10-18c.md``),
passed variant B: the adopted rules holding 10 names instead of 20. The pre-registration says a passing variant must
still beat the control over 12 months of forward paper trading before it is adopted. This is that account.

Everything is the forward test's except the size of the portfolio, exactly as pre-registered for B:

* the same screen, ranking, price bands and Indian filters (ASM/GSM, results blackout, pledges) as the forward test;
* top 10; keep a holding while it ranks 18 or better (25 for a profitable holding aged 335-365 days, for long-term
  tax); the same 25% disaster floor below the buy price; no stop;
* monthly, after the close, Indian delivery charges, marked daily against MID150BEES.

The overfitting probability across the six backward variants was 0.79, so B's pass may be luck; this account decides.
It starts on ``TOP10_PAPER_START`` (default 19 Oct 2026; ``off`` disables it), keeps its own files
(``state/forward/top10_<universe>.json``) and practice money only. Nothing is retuned on what it shows.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

from .timezones import IST

log = logging.getLogger(__name__)

TOP = 10
RULES_B = {"buffer": 18, "tax_aware_floor": 25, "floor": 0.25}   # research_backward.variants(): B
VARIANT = "B: top 10 (preregistered-batch-2026-10-16.md; passed Backward-batch-v2)"


def account_name(universe: str) -> str:
    return f"top10_{universe.lower()}"


class Top10Paper:
    """Variant B's paper account: a ForwardTest under its own name, top 10 with B's buffer."""

    def __init__(self, state_dir: Path, *, universe: str = "NIFTYMIDCAP150", capital: float = 500_000.0,
                 price_fn: Callable[[str], float] | None, cost_model: Any | None = None,
                 now: Callable[[], datetime] | None = None, holidays: Any | None = None):
        from .forward import ForwardTest
        self.universe = universe.upper()
        self.ft = ForwardTest(state_dir, universe=self.universe, top=TOP, capital=capital, price_fn=price_fn,
                              cost_model=cost_model, now=now, holidays=holidays, rules=dict(RULES_B),
                              name=account_name(self.universe))

    def run(self, screen_fn: Callable[[], dict[str, Any]] | None) -> dict[str, Any]:
        self.ft.data.setdefault("variant", VARIANT)
        return self.ft.run(screen_fn)


def format_top10_paper(s: dict[str, Any], control: dict[str, Any] | None = None) -> str:
    def pct(v: Any) -> str:
        return "n/a" if v is None else f"{v * 100:+.2f}%"
    lines = [f"Top-10 paper account (variant B, {s['universe']} top {s['top']}), since "
             f"{str(s.get('started') or '')[:10]}: {pct(s.get('strategy_return'))} vs {s['benchmark']} "
             f"{pct(s.get('benchmark_return'))} (gap {pct(s.get('gap'))}), worst fall "
             f"{pct(s.get('strategy_max_drawdown'))}, {s['days']} day(s), charges {s.get('charges_paid', 0):,.0f}."]
    if control and control.get("strategy_return") is not None and s.get("strategy_return") is not None:
        lines.append(f"  The adopted rules (forward test) over the same days, {control['from']} to {control['to']}: "
                     f"{pct(control['strategy_return'])}; B minus the adopted rules: "
                     f"{pct(s['strategy_return'] - control['strategy_return'])}.")
    r = s.get("rebalanced")
    if r:
        ok = [t for t in r["trades"] if "error" not in t]
        lines.append(f"  Rebalanced {r['date']}: {len(r['picks'])} picks of {r.get('eligible')} eligible, {len(ok)} "
                     f"trades, charges {r['charges']:,.0f}"
                     + (f", kept {len(r['kept'])}" if r.get("kept") is not None else "") + ".")
        if r["picks"]:
            lines.append("  Holding: " + ", ".join(r["picks"][:10]))
    lines.append("  Same screen, ranking and filters as the forward test; 10 names, kept while ranked 18 or better. "
                 "Pre-registered: B replaces the adopted rules only if it beats them over 12 months here. "
                 "Practice money only.")
    if s["days"] < 60:
        lines.append("  Too early to judge.")
    return "\n".join(lines)


def run_top10_paper_due(settings: Any, prices: Any, holidays: Any, *, today: date | None = None,
                        screen_fn: Callable[[], dict[str, Any]] | None = None) -> str:
    """One after-close run: nothing before TOP10_PAPER_START; on the first run of a month screen and trade; mark
    every trading day. Never raises (a failure is reported in the text)."""
    from . import killswitch
    from .costs import cost_model_for
    from .resmom_paper import control_over
    start = getattr(settings, "top10_paper_start", None)
    today = today or datetime.now(IST).date()
    if not start:
        return "Top-10 paper account: off (TOP10_PAPER_START=off)."
    if today.isoformat() < start:
        return f"Top-10 paper account: starts {start}."
    halted = killswitch.reason(settings.state_dir, "run the top-10 paper account")
    if halted:
        return halted
    try:
        universe = settings.forward_universe.upper()
        tp = Top10Paper(settings.state_dir, universe=universe, capital=settings.paper_starting_cash,
                        price_fn=prices.latest_price, cost_model=cost_model_for("in"), holidays=holidays)
        if not tp.ft.due():
            return "Top-10 paper account: nothing due."

        def screen() -> dict[str, Any]:
            if screen_fn is not None:
                return screen_fn()
            from .bands import book_for
            from .indian_filters import filters_for
            from .screen import load_universe, run_screen
            return run_screen(load_universe(universe), prices, top=TOP, bands=book_for(settings),
                              filters=filters_for(settings))

        summary = tp.run(screen)
        return format_top10_paper(summary, control_over(settings.state_dir, universe, summary.get("started")))
    except Exception as e:  # noqa: BLE001 - the forward test's job must carry on
        log.exception("top-10 paper account failed")
        return f"Top-10 paper account failed: {type(e).__name__}: {e}"
