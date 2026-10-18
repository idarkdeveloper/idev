"""Residual momentum (variant E) on paper: its 12-month forward test beside the adopted rules.

The pre-registered backward check (``docs/research/preregistered-batch-2026-10-16b.md``, run once on 18 Oct 2026)
passed variant E: the adopted rules with only the ranking changed to residual momentum. The pre-registration says a
passing variant becomes a candidate for 12 months of forward paper trading beside the control, and is adopted only if
it beats the control there. This is that account.

Everything is the forward test's except the ranking:

* the same eligible names on the same day: the factor screen with the same price-band and Indian filters (ASM/GSM,
  results blackout, pledges) as the forward test, so the two accounts differ only in how they order those names;
* those names ordered by residual momentum exactly as pre-registered (``research_backward.residual_momentum_ranker``,
  unchanged): daily log returns over sessions t-252 .. t-21, a fit through the origin on MID150BEES (the index fund
  is the benchmark in any run after the backward check), score = sum of residuals / (their sd x sqrt(n)), at least
  150 days; an eligible name with too little history is left unranked;
* the forward test's research rules (``forward.RULES``): top 20, keep a holding while it ranks 35 or better (50 for
  a profitable holding aged 335-365 days, for long-term tax), a 25% disaster floor below the buy price, no stop;
* monthly, after the close, Indian delivery charges, marked daily against MID150BEES.

It starts on ``RESMOM_PAPER_START`` (default 2 Nov 2026, the next monthly rebalance; ``off`` disables it), keeps its
own files (``state/forward/resmom_<universe>.json``) and practice money only. Nothing is retuned on what it shows.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

from .timezones import IST

log = logging.getLogger(__name__)

TOP = 20
VARIANT = "E: residual momentum (preregistered-batch-2026-10-16b.md)"


def account_name(universe: str) -> str:
    return f"resmom_{universe.lower()}"


def reorder(result: dict[str, Any], ranked: list[str], top: int = TOP) -> dict[str, Any]:
    """The screen's result with its eligible names put in ``ranked`` order (residual momentum). An eligible name the
    residual ranking does not score is left out; a name it scores that the screen's filters exclude stays out."""
    rows = {r["symbol"].upper(): r for r in result.get("all", []) if r.get("symbol")}
    eligible = {s for s, r in rows.items() if r.get("eligible")}
    order = [s.upper() for s in ranked if s.upper() in eligible]
    unranked = sorted(eligible - set(order))
    out = dict(result)
    out["all"] = [{**rows[s], "eligible": True, "residual_rank": i + 1} for i, s in enumerate(order)] + \
                 [{**r, "eligible": False} for s, r in rows.items() if s not in order]
    out["top"] = [rows[s] for s in order[:top]]
    out["eligible"] = len(order)
    out["unranked"] = unranked
    return out


class ResidualPaper:
    """Variant E's paper account: a ForwardTest under its own name with the forward test's research rules."""

    def __init__(self, state_dir: Path, *, universe: str = "NIFTYMIDCAP150", capital: float = 500_000.0,
                 price_fn: Callable[[str], float] | None, cost_model: Any | None = None,
                 now: Callable[[], datetime] | None = None, holidays: Any | None = None):
        from .forward import RULES, ForwardTest
        self.universe = universe.upper()
        self.ft = ForwardTest(state_dir, universe=self.universe, top=TOP, capital=capital, price_fn=price_fn,
                              cost_model=cost_model, now=now, holidays=holidays, rules=RULES,
                              name=account_name(self.universe))

    def run(self, screen_fn: Callable[[], dict[str, Any]] | None) -> dict[str, Any]:
        self.ft.data.setdefault("variant", VARIANT)
        return self.ft.run(screen_fn)


def control_over(state_dir: Path, universe: str, since: str | None) -> dict[str, Any] | None:
    """The adopted rules' forward test over E's own period: its equity from the last point on or before ``since`` to
    its latest point (the two accounts started on different days, so only the same days are compared)."""
    if not since:
        return None
    path = Path(state_dir) / "forward" / f"{universe.lower()}.json"
    try:
        hist = json.loads(path.read_text(encoding="utf-8"))["history"]
        base = [p for p in hist if p["date"] <= since[:10]]
        if not base or not hist:
            return None
        return {"from": base[-1]["date"], "to": hist[-1]["date"],
                "strategy_return": hist[-1]["equity"] / base[-1]["equity"] - 1}
    except (OSError, ValueError, KeyError, TypeError, ZeroDivisionError):
        return None


def format_resmom_paper(s: dict[str, Any], control: dict[str, Any] | None = None) -> str:
    def pct(v: Any) -> str:
        return "n/a" if v is None else f"{v * 100:+.2f}%"
    lines = [f"Residual momentum paper account (variant E, {s['universe']} top {s['top']}), since "
             f"{str(s.get('started') or '')[:10]}: {pct(s.get('strategy_return'))} vs {s['benchmark']} "
             f"{pct(s.get('benchmark_return'))} (gap {pct(s.get('gap'))}), worst fall "
             f"{pct(s.get('strategy_max_drawdown'))}, {s['days']} day(s), charges {s.get('charges_paid', 0):,.0f}."]
    if control and control.get("strategy_return") is not None and s.get("strategy_return") is not None:
        lines.append(f"  The adopted rules (forward test) over the same days, {control['from']} to {control['to']}: "
                     f"{pct(control['strategy_return'])}; E minus the adopted rules: "
                     f"{pct(s['strategy_return'] - control['strategy_return'])}.")
    r = s.get("rebalanced")
    if r:
        ok = [t for t in r["trades"] if "error" not in t]
        lines.append(f"  Rebalanced {r['date']}: {len(r['picks'])} picks of {r.get('eligible')} ranked, {len(ok)} trades, "
                     f"charges {r['charges']:,.0f}" + (f", kept {len(r['kept'])}" if r.get("kept") is not None else "")
                     + ".")
        if r["picks"]:
            lines.append("  Top by residual momentum: " + ", ".join(r["picks"][:10]))
    lines.append("  Same names, filters and rules as the forward test; only the ranking differs. Pre-registered: E "
                 "replaces the adopted rules only if it beats them over 12 months here. Practice money only.")
    if s["days"] < 60:
        lines.append("  Too early to judge.")
    return "\n".join(lines)


def _residual_ranking(settings: Any, prices: Any, universe: str) -> list[str]:
    """Today's eligible members ordered by residual momentum on MID150BEES (point-in-time membership)."""
    from .factor_backtest import INDEX_FUNDS
    from .index_history import point_in_time
    from .research_backward import residual_momentum_ranker
    from .research_sim import load_market
    from .screen import load_universe
    members = [r["symbol"] for r in load_universe(universe)]
    membership = point_in_time(universe, members, settings.state_dir)
    m = load_market(members, prices, regime=INDEX_FUNDS.get(universe, "MID150BEES"), years=2, until=None,
                    membership=membership)
    return residual_momentum_ranker()(m, len(m.dates) - 1)


def run_resmom_paper_due(settings: Any, prices: Any, holidays: Any, *, today: date | None = None,
                         ranking_fn: Callable[[], list[str]] | None = None,
                         screen_fn: Callable[[], dict[str, Any]] | None = None) -> str:
    """One after-close run: nothing before RESMOM_PAPER_START; on the first run of a month rank and trade; mark every
    trading day. Never raises (a failure is reported in the text)."""
    from . import killswitch
    from .costs import cost_model_for
    start = getattr(settings, "resmom_paper_start", None)
    today = today or datetime.now(IST).date()
    if not start:
        return "Residual momentum paper account: off (RESMOM_PAPER_START=off)."
    if today.isoformat() < start:
        return f"Residual momentum paper account: starts {start}."
    halted = killswitch.reason(settings.state_dir, "run the residual momentum paper account")
    if halted:
        return halted
    try:
        universe = settings.forward_universe.upper()
        rp = ResidualPaper(settings.state_dir, universe=universe, capital=settings.paper_starting_cash,
                           price_fn=prices.latest_price, cost_model=cost_model_for("in"), holidays=holidays)
        if not rp.ft.due():
            return "Residual momentum paper account: nothing due."

        def screen() -> dict[str, Any]:
            if screen_fn is not None:
                result = screen_fn()
            else:
                from .bands import book_for
                from .indian_filters import filters_for
                from .screen import load_universe, run_screen
                result = run_screen(load_universe(universe), prices, top=TOP, bands=book_for(settings),
                                    filters=filters_for(settings))
            ranked = ranking_fn() if ranking_fn is not None else _residual_ranking(settings, prices, universe)
            return reorder(result, ranked)

        summary = rp.run(screen)
        return format_resmom_paper(summary, control_over(settings.state_dir, universe, summary.get("started")))
    except Exception as e:  # noqa: BLE001 - the forward test's job must carry on
        log.exception("residual momentum paper account failed")
        return f"Residual momentum paper account failed: {type(e).__name__}: {e}"
