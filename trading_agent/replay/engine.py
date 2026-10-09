"""One replay step: walk every trading day up to the target date.

Each day, in order: the agent rebalances on the first trading day of a month, stops are
checked (the agent's always, yours when auto-sell is on), dividends are credited in cash
mode, and the three portfolio values are recorded. The whole step is one transaction.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Callable

from ..risk import check_stops
from .clock import add_months

CALENDAR = "^NSEI"


def step_target(clock: str, by: str, today: str) -> str:
    if by == "week":
        target = (date.fromisoformat(clock) + timedelta(days=7)).isoformat()
    elif by == "month":
        target = add_months(clock, 1)
    elif by == "year":
        target = add_months(clock, 12)
    else:
        raise ValueError("step by week, month or year")
    return min(target, today)


def _stops(trial: Any, who: str, day: str) -> list[dict[str, Any]]:
    broker = trial.broker(who)
    out = []
    for h in check_stops(broker.positions(), lambda s: trial.prices.history(s, "1y")):
        try:
            o = broker.submit_order(h["symbol"], "sell", qty=h["qty"])
            out.append({"date": day, "who": who, "symbol": h["symbol"], "qty": o["qty"],
                        "price": o["filled_avg_price"], "stop": h["stop"]})
        except Exception as e:  # noqa: BLE001 - a stop that can't fill is logged, not fatal
            out.append({"date": day, "who": who, "symbol": h["symbol"], "error": str(e)})
    return out


def _dividends(trial: Any, after: str, day: str) -> list[dict[str, Any]]:
    out = []
    for who in ("you", "agent", "nifty"):
        broker = trial.broker(who)
        for p in broker.positions():
            for d in trial.prices.dividends(p.symbol):
                if after < d["date"] <= day:
                    amount = round(d["amount"] * p.qty, 2)
                    broker.credit(amount, f"dividend {p.symbol}", day)
                    out.append({"date": day, "who": who, "symbol": p.symbol, "amount": amount})
    return out


def step(trial: Any, until: str, *, today: str | None = None,
         progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    if trial.data["ended"]:
        raise ValueError("this replay has ended; it is read-only")
    today = today or date.today().isoformat()
    start = trial.clock.today
    until = min(until, today)
    if until <= start:
        raise ValueError(f"the replay is already at {start}")
    trial.prices.take_fetch_errors()  # start clean
    days = trial.prices.calendar(CALENDAR, start, until)
    report: dict[str, Any] = {"from": start, "to": until, "days": len(days), "rebalances": [],
                              "stops": [], "dividends": []}
    with trial.transaction():
        prev = start
        for i, day in enumerate(days):
            trial.clock.advance_to(day)
            if day[:7] != trial.data["last_rebalance_month"]:
                if progress:
                    progress(f"rebalancing the agent on {day}")
                trial.rebalance_agent()
                report["rebalances"].append(day)
            stops = _stops(trial, "agent", day)
            if trial.data["auto_stop"]:
                stops += _stops(trial, "you", day)
            trial.data["stops"] = (trial.data["stops"] + stops)[-500:]
            report["stops"] += stops
            if trial.data["dividends"] == "cash":
                report["dividends"] += _dividends(trial, prev, day)
            trial.data["equity"].append(trial.point())
            prev = day
            if progress and i % 20 == 0:
                progress(f"{day}: {i + 1} of {len(days)} trading days")
        trial.clock.advance_to(until)
        trial.data["clock"] = until
        trial.refresh_picks()
        # The broker and the screens fall back to stored prices when Yahoo fails; a step built
        # on those would look fine and be wrong, so any failed fetch undoes the whole step.
        failed = trial.prices.take_fetch_errors()
        if failed:
            sym, err = next(iter(failed.items()))
            raise RuntimeError(f"Price data could not be loaded for {sym}: {err}; nothing was changed — try again")
        trial.save()
    return report
