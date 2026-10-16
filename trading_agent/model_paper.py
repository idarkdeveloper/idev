"""The prediction model on paper: an out-of-sample test of the sizing model, in its own practice account.

Research (``research-model``) found that the model which sizes the momentum picks does not beat the simple rules on
the development years (skill odds 0.61 against a 0.95 bar). Paper trading is the honest next look: real months it never
saw, no money at risk, nothing shared with the practice account or the forward test.

The model is fitted ONCE, on the development years only (before ``HOLDOUT_START``, as it was tested), and frozen in
``state/forward/model_<universe>_fit.json``: the sealed holdout is not used to fit it, and nothing is retuned on what
the paper account shows. Then, on the first run of each month after the close:

1. the momentum top 20 of the index's point-in-time members (the research ranking, no other filters);
2. each pick's features as known that day (price signals, and earnings and promoter signals from the caches);
3. the model's probability for each pick is its size: weight = probability / 20; the rest stays in cash;
4. the paper account trades to those weights (whole shares, Indian delivery charges).

Every trading day after the close the account is marked against MID150BEES bought on the first day. No stop and no
floor: the account tests the model's sizing and nothing else. It starts on ``MODEL_PAPER_START`` (default 19 Oct 2026;
``off`` disables it) and runs in the same after-close job as the forward test.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

from .timezones import IST

log = logging.getLogger(__name__)

TOP = 20
TRIM_ABOVE = 1.25
MAX_DAYS = 60   # the triple barrier's time limit: a sample is complete only when this many sessions have passed


def account_name(universe: str) -> str:
    return f"model_{universe.lower()}"


# --------------------------------------------------------------------------- #
# the frozen model
# --------------------------------------------------------------------------- #
def fit_frozen(m: Any, ev: Any, cost: float) -> dict[str, Any]:
    """The plain model fitted on every complete development sample of ``m`` (no stop, +3R target, 60 days)."""
    from .research_ml import fit_final, meta_samples
    from .research_studies import _first_ranked_day
    start, end = _first_ranked_day(m), len(m.dates) - 1
    samples, names = meta_samples(m, start, end, rule="none", cost=cost, ev=ev)
    complete = [s for s in samples if s["t"] + 1 + MAX_DAYS <= end]   # a label cut short by the data's end is dropped
    if len(complete) < 100:
        raise ValueError(f"only {len(complete)} complete samples to fit the model on")
    beta = fit_final("plain", complete, cost)
    return {"beta": beta, "features": names, "samples": len(complete), "from": m.dates[start], "to": m.dates[end],
            "fitted_at": datetime.now(IST).isoformat(timespec="seconds"), "objective": "plain"}


def score(m: Any, ev: Any, model: dict[str, Any], t: int | None = None, top: int = TOP) -> list[dict[str, Any]]:
    """The momentum top ``top`` on day t (default: the last day) with the model's probability as each pick's size."""
    from .research_ml import Features, predict
    t = len(m.dates) - 1 if t is None else t
    feats = Features(m, ev)
    if feats.names() + ["regime_above_ema50", "breadth"] != model["features"]:
        raise ValueError("the features available now differ from the ones the model was fitted on")
    z = feats.z(t)
    c, e50 = m.fund_close[t], m.fund_ema50[t]
    bt = m.breadth()[t]
    regime = [1.0 if c is not None and e50 is not None and c >= e50 else 0.0, (bt - 0.5) * 4 if bt is not None else 0.0]
    picks = [s for s in m.momentum_ranking(t)[:top] if s in z]
    probs = predict(model["beta"], [[z[s][f] for f in feats.names()] + regime for s in picks])
    return [{"symbol": s, "rank": i + 1, "size": round(p, 4), "weight": round(p / top, 5)}
            for i, (s, p) in enumerate(zip(picks, probs))]


# --------------------------------------------------------------------------- #
# the paper account
# --------------------------------------------------------------------------- #
def trade_to_weights(broker: Any, weights: dict[str, float], *, price_fn: Callable[[str], float],
                     cost_model: Any | None) -> list[dict[str, Any]]:
    """Sell what left the list, trim a holding above 125% of its target, then buy up to each target while cash lasts."""
    weights = {k.upper(): w for k, w in weights.items() if w > 0}
    trades: list[dict[str, Any]] = []

    def trade(sym: str, side: str, qty: int) -> None:
        if qty < 1:
            return
        try:
            o = broker.submit_order(sym, side, qty=qty)
            trades.append({"symbol": sym, "side": side, "qty": o["qty"], "price": o["filled_avg_price"]})
        except Exception as e:  # noqa: BLE001 - a missing price or cash skips one trade
            trades.append({"symbol": sym, "side": side, "qty": qty, "error": str(e)})

    held = {p.symbol: p for p in broker.positions()}
    for sym, p in held.items():
        if sym not in weights:
            trade(sym, "sell", int(p.qty))
    equity = broker.account().equity
    for sym, w in weights.items():
        p = held.get(sym)
        target = equity * w
        if p is not None and p.current_price and p.qty * p.current_price > target * TRIM_ABOVE:
            trade(sym, "sell", int(math.floor((p.qty * p.current_price - target) / p.current_price)))
    cash = broker.account().cash
    for sym, w in sorted(weights.items(), key=lambda kv: -kv[1]):
        target = equity * w
        p = next((x for x in broker.positions() if x.symbol == sym), None)
        have = p.qty * p.current_price if p and p.current_price else 0.0
        if have >= target * 0.95:
            continue
        try:
            price = float(price_fn(sym))
        except Exception as e:  # noqa: BLE001
            trades.append({"symbol": sym, "side": "buy", "qty": 0, "error": f"no price: {e}"})
            continue
        budget = min(target - have, cash)
        charges = float(cost_model.charges("buy", budget)) if cost_model else 0.0
        qty = int(math.floor((budget - charges) / price)) if price > 0 else 0
        if qty >= 1:
            trade(sym, "buy", qty)
            cash = broker.account().cash
    return trades


class ModelPaper:
    """The model's paper account: a ForwardTest under its own name (``model_<universe>``), without research rules."""

    def __init__(self, state_dir: Path, *, universe: str = "NIFTYMIDCAP150", capital: float = 500_000.0,
                 price_fn: Callable[[str], float] | None, cost_model: Any | None = None,
                 now: Callable[[], datetime] | None = None, holidays: Any | None = None):
        from .forward import ForwardTest
        self.universe = universe.upper()
        self.ft = ForwardTest(state_dir, universe=self.universe, top=TOP, capital=capital, price_fn=price_fn,
                              cost_model=cost_model, now=now, holidays=holidays, rules=None,
                              name=account_name(self.universe))
        self.fit_path = self.ft.dir / f"{account_name(self.universe)}_fit.json"

    def model(self) -> dict[str, Any] | None:
        try:
            return json.loads(self.fit_path.read_text(encoding="utf-8")) if self.fit_path.exists() else None
        except (OSError, ValueError):
            return None

    def save_model(self, model: dict[str, Any]) -> None:
        from .state import atomic_write
        self.fit_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(self.fit_path, json.dumps(model, indent=1))

    def rebalance(self, picks: list[dict[str, Any]]) -> dict[str, Any]:
        ft = self.ft
        fees_before = ft.broker.performance()["fees_paid"]
        equity_before = ft.broker.account().equity
        trades = trade_to_weights(ft.broker, {p["symbol"]: p["weight"] for p in picks}, price_fn=ft.price_fn,
                                  cost_model=ft.cost_model)
        entry = {"date": ft.now().date().isoformat(), "month": ft._month(), "picks": [p["symbol"] for p in picks],
                 "sizes": {p["symbol"]: p["size"] for p in picks}, "trades": trades,
                 "invested": round(sum(p["weight"] for p in picks), 4),
                 "charges": round(ft.broker.performance()["fees_paid"] - fees_before, 2),
                 "equity": round(equity_before, 2)}
        ft.data["rebalances"] = (ft.data["rebalances"] + [entry])[-60:]
        ft.data["last_rebalance"] = ft._month()
        return entry

    def run(self, picks_fn: Callable[[], list[dict[str, Any]]] | None) -> dict[str, Any]:
        """Start if needed, size and trade if a month's rebalance is due, record today's point."""
        ft = self.ft
        if not ft.started:
            ft.start()
        rebalanced = None
        if ft.rebalance_due() and picks_fn is not None:
            rebalanced = self.rebalance(picks_fn())
        ft.mark()
        ft.save()
        return {**ft.summary(), "rebalanced": rebalanced, "model": self.model()}


def format_model_paper(s: dict[str, Any], forward: dict[str, Any] | None = None) -> str:
    def pct(v: Any) -> str:
        return "n/a" if v is None else f"{v * 100:+.2f}%"
    m = s.get("model") or {}
    lines = [f"Model paper account ({s['universe']} momentum top {s['top']}, each sized by the frozen prediction model), "
             f"since {str(s.get('started') or '')[:10]}: {pct(s.get('strategy_return'))} vs "
             f"{s['benchmark']} {pct(s.get('benchmark_return'))} (gap {pct(s.get('gap'))}), {s['days']} day(s), "
             f"charges {s.get('charges_paid', 0):,.0f}."]
    if forward and forward.get("strategy_return") is not None:
        lines.append(f"  The forward test (plain screen) over its own period: {pct(forward.get('strategy_return'))}.")
    r = s.get("rebalanced")
    if r:
        lines.append(f"  Rebalanced {r['date']}: {len(r['picks'])} picks, {r['invested'] * 100:.0f}% invested by the "
                     f"model's sizes, charges {r['charges']:,.0f}.")
        top = sorted(r["sizes"].items(), key=lambda kv: -kv[1])
        lines.append("  Sizes: " + ", ".join(f"{k} {v:.2f}" for k, v in top))
    if m:
        lines.append(f"  Model fitted on {m.get('samples')} samples, {m.get('from')} to {m.get('to')} (the development "
                     "years; the holdout was not used), frozen.")
    lines.append("  Research verdict: the model did not beat the simple rules (skill odds 0.61); this account is its "
                 "out-of-sample test on paper. Practice money only.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# the scheduled run
# --------------------------------------------------------------------------- #
def _market_and_events(settings: Any, prices: Any, *, years: int, until: str | None) -> tuple[Any, Any]:
    from .factor_backtest import INDEX_FUNDS
    from .index_history import point_in_time
    from .research_sim import load_market
    from .screen import load_universe
    universe = settings.forward_universe.upper()
    members = [r["symbol"] for r in load_universe(universe)]
    membership = point_in_time(universe, members, settings.state_dir)
    m = load_market(members, prices, regime=INDEX_FUNDS.get(universe, "MID150BEES"), years=years, until=until,
                    membership=membership)
    from .event_signals import EventData, InsiderHistory
    from .fundamentals_history import ResultsHistory
    src = ResultsHistory(settings.state_dir / "cache", max_new_downloads=0)
    results: dict[str, Any] = {}
    for sym in m.raw:
        try:
            results[sym] = src.history(sym)
        except Exception:  # noqa: BLE001 - no cached listing: no earnings signals for it
            results[sym] = []
    insider = InsiderHistory(settings.state_dir / "cache").trades(m.dates[0], m.dates[-1])
    return m, EventData(m, results, insider)


def forward_return(settings: Any) -> dict[str, Any] | None:
    """The forward test's own return so far, read from its file (for one comparison line)."""
    path = Path(settings.state_dir) / "forward" / f"{settings.forward_universe.lower()}.json"
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        last = d["history"][-1]
        return {"strategy_return": last["equity"] / d["capital"] - 1}
    except (OSError, ValueError, KeyError, IndexError, TypeError, ZeroDivisionError):
        return None


def run_model_paper_due(settings: Any, prices: Any, holidays: Any, *, today: date | None = None) -> str:
    """One after-close run: nothing before MODEL_PAPER_START; fit the frozen model once; size and trade on the first
    run of a month; mark every trading day. Never raises (a failure is reported in the text)."""
    from . import killswitch
    from .costs import cost_model_for
    from .research_studies import HOLDOUT_START
    start = getattr(settings, "model_paper_start", None)
    today = today or datetime.now(IST).date()
    if not start:
        return "Model paper account: off (MODEL_PAPER_START=off)."
    if today.isoformat() < start:
        return f"Model paper account: starts {start}."
    halted = killswitch.reason(settings.state_dir, "run the model paper account")
    if halted:
        return halted
    try:
        cost_model = cost_model_for("in")
        mp = ModelPaper(settings.state_dir, universe=settings.forward_universe, capital=settings.paper_starting_cash,
                        price_fn=prices.latest_price, cost_model=cost_model, holidays=holidays)
        if not mp.ft.due():
            return "Model paper account: nothing due."
        cost = cost_model.round_trip_bps(25_000) / 10_000

        def picks() -> list[dict[str, Any]]:
            model = mp.model()
            if model is None:
                m_dev, ev_dev = _market_and_events(settings, prices, years=5, until=HOLDOUT_START)
                model = fit_frozen(m_dev, ev_dev, cost)
                mp.save_model(model)
            m_now, ev_now = _market_and_events(settings, prices, years=2, until=None)
            return score(m_now, ev_now, model)

        summary = mp.run(picks)
        return format_model_paper(summary, forward_return(settings))
    except Exception as e:  # noqa: BLE001 - the forward test's job must carry on
        log.exception("model paper account failed")
        return f"Model paper account failed: {type(e).__name__}: {e}"
