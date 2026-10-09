"""Forward test of the factor screen: paper-trade it month by month, against an index fund.

The factor backtest found one place where the screen beat its index fund after charges
(the Midcap 150, over one five-year window). A backtest can be fitted to its own past;
the real test is months it has never seen. This runs the same screen forward in its own
paper account, separate from the investor-following one:

* On the first run of each month (after the 15:30 close when run by the routine) it
  ranks today's index members with the same score as the screen and the backtest, and
  trades to the top N in equal weights: dropped names are sold, new names bought, and a
  kept name is trimmed only when it is more than 25% over its target weight, so small
  trims don't each pay a ₹20 DP charge. Fills are whole shares at the latest price with
  real Indian delivery charges.
* On the same day it started, the same capital was "invested" in the index fund
  (MID150BEES for the Midcap 150), charges included, as the benchmark.
* Each run records one point a day for both, so the gap between them builds up over
  months the backtest never saw.

State lives in ``state/forward/<universe>.json`` (with the paper fills in a separate
broker file). Nothing here can reach a real broker.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any, Callable

from .broker import LocalPaperBroker
from .factor_backtest import INDEX_FUNDS
from .timezones import IST

log = logging.getLogger(__name__)
CLOSE_DONE = dtime(15, 40)  # NSE closes 15:30; prices settle a few minutes later
TRIM_ABOVE = 1.25  # trim a kept name only above 125% of its target weight


def _now() -> datetime:
    return datetime.now(IST)


class ForwardTest:
    def __init__(self, state_dir: Path, *, universe: str = "NIFTYMIDCAP150", top: int = 20,
                 capital: float = 500_000.0, benchmark: str | None = None,
                 price_fn: Callable[[str], float] | None, cost_model: Any | None = None,
                 now: Callable[[], datetime] | None = None):
        self.universe = universe.upper()
        self.dir = Path(state_dir) / "forward"
        self.path = self.dir / f"{self.universe.lower()}.json"
        self.price_fn = price_fn
        self.cost_model = cost_model
        self.now = now or (lambda: _now())  # looked up at call time, so tests can patch it
        self.data: dict[str, Any] = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        if not self.data:
            self.data = {"universe": self.universe, "top": int(top), "capital": float(capital),
                         "benchmark": (benchmark or INDEX_FUNDS.get(self.universe) or "^NSEI").upper(),
                         "started": None, "bench_units": None, "bench_cost": None,
                         "last_rebalance": None, "rebalances": [], "history": []}
        self.broker = LocalPaperBroker(self.dir / f"{self.universe.lower()}_broker.json",
                                       starting_cash=self.data["capital"], price_fn=price_fn,
                                       currency="INR", whole_shares=True, cost_model=cost_model)

    # -- state ------------------------------------------------------------------
    @property
    def started(self) -> bool:
        return bool(self.data.get("started"))

    def save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")

    def _month(self) -> str:
        return self.now().strftime("%Y-%m")

    def rebalance_due(self) -> bool:
        return self.data.get("last_rebalance") != self._month()

    def due(self) -> bool:
        """For the scheduled routine: work only on weekdays after the close, and only
        when a rebalance is due or today's point is missing."""
        now = self.now()
        if now.weekday() >= 5 or now.time() < CLOSE_DONE:
            return False
        today = now.date().isoformat()
        has_today = any(p["date"] == today for p in self.data["history"])
        return self.rebalance_due() or not has_today

    # -- actions ------------------------------------------------------------------
    def start(self) -> None:
        """Put the same capital into the benchmark fund, charges included."""
        price = float(self.price_fn(self.data["benchmark"]))
        capital = self.data["capital"]
        cost = float(self.cost_model.charges("buy", capital)) if self.cost_model else 0.0
        self.data.update(started=self.now().isoformat(timespec="seconds"),
                         bench_units=(capital - cost) / price, bench_cost=round(cost, 2),
                         bench_start_price=price)

    def rebalance(self, picks: list[str], *, eligible: int | None = None) -> dict[str, Any]:
        """Trade the paper account to equal weights in ``picks``."""
        top = self.data["top"]
        picks = [p.upper() for p in picks][:top]
        held = {p.symbol: p for p in self.broker.positions()}
        trades: list[dict[str, Any]] = []
        fees_before = self.broker.performance()["fees_paid"]

        def trade(sym: str, side: str, qty: int) -> None:
            if qty < 1:
                return
            try:
                o = self.broker.submit_order(sym, side, qty=qty)
                trades.append({"symbol": sym, "side": side, "qty": o["qty"], "price": o["filled_avg_price"]})
            except Exception as e:  # noqa: BLE001 - a missing price or cash skips one trade
                trades.append({"symbol": sym, "side": side, "qty": qty, "error": str(e)})

        for sym, p in held.items():  # 1. sell what dropped out
            if sym not in picks:
                trade(sym, "sell", int(p.qty))
        equity = self.broker.account().equity
        target = equity / top
        for sym in picks:  # 2. trim what grew far past its weight
            p = held.get(sym)
            if p is not None and p.current_price and p.qty * p.current_price > target * TRIM_ABOVE:
                trade(sym, "sell", int(math.floor((p.qty * p.current_price - target) / p.current_price)))
        cash = self.broker.account().cash
        for sym in picks:  # 3. buy up to the target, while cash lasts
            p = next((x for x in self.broker.positions() if x.symbol == sym), None)
            have = p.qty * p.current_price if p and p.current_price else 0.0
            if have >= target * 0.95:
                continue
            try:
                price = float(self.price_fn(sym))
            except Exception as e:  # noqa: BLE001
                trades.append({"symbol": sym, "side": "buy", "qty": 0, "error": f"no price: {e}"})
                continue
            budget = min(target - have, cash)
            charges = float(self.cost_model.charges("buy", budget)) if self.cost_model else 0.0
            qty = int(math.floor((budget - charges) / price))
            if qty >= 1:
                trade(sym, "buy", qty)
                cash = self.broker.account().cash
        entry = {"date": self.now().date().isoformat(), "month": self._month(), "picks": picks,
                 "eligible": eligible, "trades": trades,
                 "charges": round(self.broker.performance()["fees_paid"] - fees_before, 2)}
        self.data["rebalances"] = (self.data["rebalances"] + [entry])[-60:]
        self.data["last_rebalance"] = self._month()
        return entry

    def mark(self) -> dict[str, Any]:
        """Record today's value of the strategy and the benchmark (one point a day)."""
        acct = self.broker.account()
        bench = None
        try:
            bench = round(self.data["bench_units"] * float(self.price_fn(self.data["benchmark"])), 2)
        except Exception as e:  # noqa: BLE001
            log.warning("benchmark price unavailable: %s", e)
        point = {"date": self.now().date().isoformat(), "equity": round(acct.equity, 2),
                 "cash": round(acct.cash, 2), "bench": bench, "positions": len(self.broker.positions())}
        self.broker._save()  # keep the prices just seen, for read-only views like the dashboard
        hist = [p for p in self.data["history"] if p["date"] != point["date"]]
        self.data["history"] = (hist + [point])[-2000:]
        return point

    def run(self, screen_fn: Callable[[], dict[str, Any]] | None = None, *, force_rebalance: bool = False) -> dict[str, Any]:
        """Start if needed, rebalance if due (or forced), record today's point."""
        if not self.started:
            self.start()
        rebalanced = None
        if (force_rebalance or self.rebalance_due()) and screen_fn is not None:
            result = screen_fn()
            picks = [r["symbol"] for r in result.get("top", [])]
            rebalanced = self.rebalance(picks, eligible=result.get("eligible"))
        self.mark()
        self.save()
        return {**self.summary(), "rebalanced": rebalanced}

    # -- reporting ----------------------------------------------------------------
    def summary(self) -> dict[str, Any]:
        d, hist = self.data, self.data["history"]
        cap = d["capital"]
        last = hist[-1] if hist else None

        def dd(key: str) -> float | None:
            vals = [p[key] for p in hist if p.get(key)]
            if not vals:
                return None
            peak, worst = vals[0], 0.0
            for v in vals:
                peak = max(peak, v)
                worst = min(worst, v / peak - 1)
            return worst

        strat = last["equity"] / cap - 1 if last else None
        bench = last["bench"] / cap - 1 if last and last.get("bench") else None
        holdings = []
        if last:
            for p in self.broker.positions():
                mv = p.market_value or 0.0
                holdings.append({"symbol": p.symbol, "qty": p.qty, "avg": p.avg_entry_price, "price": p.current_price,
                                 "value": round(mv, 2), "weight": mv / last["equity"] if last["equity"] else None,
                                 "pl": p.unrealized_pl})
            holdings.sort(key=lambda h: -h["value"])
        return {"universe": d["universe"], "top": d["top"], "benchmark": d["benchmark"], "capital": cap,
                "started": d.get("started"), "days": len(hist), "strategy_return": strat, "benchmark_return": bench,
                "gap": (strat - bench) if strat is not None and bench is not None else None,
                "strategy_max_drawdown": dd("equity"), "benchmark_max_drawdown": dd("bench"),
                "charges_paid": self.broker.performance()["fees_paid"], "last_rebalance": d.get("last_rebalance"),
                "rebalances": len(d["rebalances"]), "last_picks": d["rebalances"][-1]["picks"] if d["rebalances"] else [],
                "holdings": holdings, "history": hist}


def format_forward(s: dict[str, Any]) -> str:
    pct = lambda v: "n/a" if v is None else f"{v*100:+.2f}%"  # noqa: E731
    lines = [f"Forward test: top {s['top']} of {s['universe']} vs {s['benchmark']}, "
             f"₹{s['capital']:,.0f} each, started {(s['started'] or 'not yet')[:10]}, {s['days']} daily point(s)"]
    if s.get("rebalanced"):
        r = s["rebalanced"]
        ok = [t for t in r["trades"] if "error" not in t]
        lines.append(f"Rebalanced {r['date']}: {len(r['picks'])} picks of {r.get('eligible')} eligible, "
                     f"{len(ok)} trades, charges ₹{r['charges']:,.2f}"
                     + (f", {len(r['trades']) - len(ok)} skipped" if len(ok) < len(r["trades"]) else ""))
    lines.append(f"Strategy {pct(s['strategy_return'])} (worst fall {pct(s['strategy_max_drawdown'])}) · "
                 f"{s['benchmark']} {pct(s['benchmark_return'])} (worst fall {pct(s['benchmark_max_drawdown'])}) · "
                 f"gap {pct(s['gap'])} · charges paid ₹{s['charges_paid']:,.2f}")
    for h in s["holdings"][:25]:
        w = f"{h['weight']*100:4.1f}%" if h.get("weight") is not None else "  n/a"
        lines.append(f"  {h['symbol']:<12} {h['qty']:>6g} sh  ₹{h['value']:>11,.0f}  {w}")
    if s["days"] < 60:
        lines.append("Too early to judge: give it at least a few months before reading anything into the gap.")
    return "\n".join(lines)
