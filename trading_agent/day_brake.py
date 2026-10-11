"""Portfolio day brake: when the live account falls DAY_BRAKE_PCT (3.5%) or more from the previous close during the
session, no new buys and no rebalancing for the rest of that session. Stops already at Groww (GTTs) still act; one
alert. The brake lifts by itself at the next session.

The account value is cash plus every live holding at its latest price; the previous close values the same holdings at
each stock's last close before today (Yahoo daily bars), with the same cash. A holding without a previous close counts
at its latest price on both sides, so it can never trip the brake. Read-only: nothing here places or cancels an order.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, time as dtime
from pathlib import Path
from typing import Any, Callable

from .state import atomic_write
from .timezones import IST

log = logging.getLogger(__name__)

FILE = "day_brake.json"
OPEN, CLOSE = dtime(9, 15), dtime(15, 30)


def _now() -> datetime:
    return datetime.now(IST)


def _read(state_dir: Path) -> dict[str, Any]:
    try:
        d = json.loads((Path(state_dir) / FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def active(state_dir: Path | None, now: datetime | None = None) -> dict[str, Any] | None:
    """Today's brake record ({date, at, fall_pct, value, prev_value}), or None."""
    if state_dir is None:
        return None
    rec = _read(state_dir).get("brake")
    today = (now or _now()).astimezone(IST).date().isoformat()
    return rec if isinstance(rec, dict) and rec.get("date") == today else None


def reason(state_dir: Path | None, now: datetime | None = None) -> str | None:
    rec = active(state_dir, now)
    if rec is None:
        return None
    return (f"day brake: the live account is down {float(rec.get('fall_pct') or 0):.1f}% from the previous close today: "
            "no new buys or rebalancing until the next session (GTT stops at Groww still act)")


def prev_close_fn(prices: Any, today: date) -> Callable[[str], float | None]:
    """The last daily close before ``today`` from Yahoo (``prices.history``), or None."""
    def get(symbol: str) -> float | None:
        if prices is None or not hasattr(prices, "history"):
            return None
        try:
            bars = prices.history(symbol, "1mo")
        except Exception:  # noqa: BLE001 - that holding counts at its latest price on both sides
            return None
        for b in reversed(bars or []):
            if str(b.get("date") or "") < today.isoformat():
                try:
                    x = float(b["close"])
                except (KeyError, TypeError, ValueError):
                    return None
                return x if x > 0 else None
        return None
    return get


def measure(positions: list[Any], cash: float, prev_close: Callable[[str], float | None]) -> dict[str, Any]:
    """{value, prev_value, fall_pct} for live positions (objects with symbol, qty, current_price)."""
    now_v = prev_v = float(cash)
    for p in positions:
        price = p.current_price
        if price is None or not p.qty:
            continue
        before = prev_close(p.symbol) or price
        now_v += p.qty * price
        prev_v += p.qty * before
    fall = (prev_v - now_v) / prev_v * 100 if prev_v > 0 else 0.0
    return {"value": round(now_v, 2), "prev_value": round(prev_v, 2), "fall_pct": round(fall, 2)}


def evaluate(settings: Any, broker: Any, prev_close: Callable[[str], float | None], now: datetime | None = None,
             calendar: Any | None = None, alert: Callable[[str, str, str], Any] | None = None) -> dict[str, Any] | None:
    """One check (the watch calls it each tick on the live account in market hours). Returns today's brake record
    when the brake is on (set now or earlier today), else None. Never raises."""
    n = (now or _now()).astimezone(IST)
    sd = Path(settings.state_dir)
    rec = active(sd, n)
    if rec is not None:
        return rec
    if not OPEN <= n.time() <= CLOSE:
        return None
    if calendar is not None and not calendar.is_trading_day(n.date()):
        return None
    try:
        m = measure(broker.positions(), broker.available_cash(), prev_close)
    except Exception as e:  # noqa: BLE001 - no reading this tick: the brake stays as it was
        log.warning("day brake: account value unavailable (%s)", e)
        return None
    limit = float(getattr(settings, "day_brake_pct", 3.5) or 3.5)
    if m["fall_pct"] < limit:
        return None
    rec = {"date": n.date().isoformat(), "at": n.isoformat(timespec="seconds"), "limit_pct": limit, **m}
    data = _read(sd)
    data["brake"] = rec
    atomic_write(sd / FILE, json.dumps(data))
    log.warning("day brake on: account down %.2f%% from the previous close", m["fall_pct"])
    if alert is not None:
        try:
            alert("day-brake", f"[DAY BRAKE] account down {m['fall_pct']:.1f}% today: no new buys",
                  f"The live account is worth {m['value']:,.0f} against {m['prev_value']:,.0f} at the previous close "
                  f"({m['fall_pct']:.1f}% down; the brake is at {limit:g}%).\n"
                  "No new buys and no rebalancing for the rest of this session. The GTT stop-losses at Groww still act. "
                  "The brake lifts by itself at the next session.")
        except Exception as e:  # noqa: BLE001
            log.warning("day brake alert not delivered: %s", e)
    return rec
