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


def _dividends(trial: Any, after: str, day: str, reinvest: bool = False) -> list[dict[str, Any]]:
    """Dividends with an ex-date in (after, day] on the shares held. Cash: qty x amount is credited. Reinvest: the same
    credit, then as many whole shares as it buys at the day's raw close (charges applied), the rest stays as cash.
    ``trial.prices.dividends`` only shows ex-dates up to the replay clock."""
    out = []
    for who in ("you", "agent", "nifty"):
        broker = trial.broker(who)
        for p in broker.positions():
            for d in trial.prices.dividends(p.symbol):
                if not after < d["date"] <= day:
                    continue
                amount = round(d["amount"] * p.qty, 2)
                broker.credit(amount, f"dividend {p.symbol}", day)
                row = {"date": day, "who": who, "symbol": p.symbol, "amount": amount}
                if reinvest:
                    try:
                        price = float(trial.prices.latest_price(p.symbol))
                        n = int(amount // price)
                        while n > 0 and n * price + trial.cost_model.charges("buy", n * price) > amount:
                            n -= 1
                        if n > 0:
                            broker.submit_order(p.symbol, "buy", qty=n)
                            row.update(reinvested_shares=n, price=price)
                    except Exception as e:  # noqa: BLE001 - the cash stays; the failure is shown
                        row["error"] = str(e)
                out.append(row)
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
        d = trial.data
        if "rebalance_count" not in d or "agent_stop_count" not in d:  # an older save: seed from the (trimmed) lists
            d.setdefault("rebalance_count", max(len(d["rebalances"]) - 1, 0))
            d.setdefault("agent_stop_count", sum(1 for x in d.get("stops", []) if x.get("who") == "agent" and "error" not in x))
            d["counts_exact"] = False
        prev = start
        raw = getattr(trial, "raw_basis", False)
        for i, day in enumerate(days):
            trial.clock.advance_to(day)
            if raw:   # entitlement is the shares held at the previous close, so this runs before today's trades and stops
                report["dividends"] += _dividends(trial, prev, day, reinvest=trial.data["dividends"] == "reinvest")
            if day[:7] != trial.data["last_rebalance_month"]:
                if progress:
                    progress(f"rebalancing the agent on {day}")
                trial.rebalance_agent()
                report["rebalances"].append(day)
                trial.data["rebalance_count"] = trial.data.get("rebalance_count", 0) + 1
            stops = _stops(trial, "agent", day)
            if trial.data["auto_stop"]:
                stops += _stops(trial, "you", day)
            trial.data["stops"] = (trial.data["stops"] + stops)[-500:]
            trial.data["agent_stop_count"] = trial.data.get("agent_stop_count", 0) + sum(
                1 for x in stops if x["who"] == "agent" and "error" not in x)
            report["stops"] += stops
            if not raw and trial.data["dividends"] == "cash":   # an older save: its original rule
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
