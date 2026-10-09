"""Position sizing and exits the way trend followers and disciplined traders do it.

* Size by volatility: risk a fixed fraction of equity per position, where "risk" is a
  2x ATR(14) adverse move, capped at a maximum share of equity.
* Trailing stop: the wider of 3x ATR or a fixed percentage below the high since entry.
"""

from __future__ import annotations

import math
from typing import Any


def atr(bars: list[dict[str, Any]], n: int = 14) -> float | None:
    """Average true range from daily bars. Uses close-to-close range when high/low absent."""
    if len(bars) < n + 1:
        return None
    trs = []
    for i in range(1, len(bars)):
        hi = bars[i].get("high", bars[i]["close"])
        lo = bars[i].get("low", bars[i]["close"])
        prev = bars[i - 1]["close"]
        if hi == lo == bars[i]["close"]:  # no intraday range in the data: fall back to |close - prev|
            tr = abs(bars[i]["close"] - prev)
        else:
            tr = max(hi - lo, abs(hi - prev), abs(lo - prev))
        trs.append(tr)
    window = trs[-n:]
    return sum(window) / len(window) if window else None


def position_size(equity: float, price: float, atr_value: float | None, *, risk_pct: float = 0.01,
                  atr_mult: float = 2.0, max_pct: float = 0.10, whole_shares: bool = True) -> dict[str, Any]:
    """How many shares so that a ``atr_mult`` x ATR move costs ``risk_pct`` of equity."""
    if price <= 0 or equity <= 0:
        return {"qty": 0, "notional": 0.0, "reason": "no price or equity"}
    cap_notional = equity * max_pct
    if not atr_value or atr_value <= 0:
        notional = cap_notional / 2  # unknown volatility: half the cap
        basis = "no ATR; half of max position"
    else:
        risk_rupees = equity * risk_pct
        per_share_risk = atr_value * atr_mult
        notional = min(cap_notional, (risk_rupees / per_share_risk) * price)
        basis = f"risk {risk_pct*100:.1f}% of equity on a {atr_mult:g}x ATR move"
    qty = math.floor(notional / price) if whole_shares else notional / price
    return {"qty": qty, "notional": round(qty * price, 2), "price": price, "atr": atr_value,
            "atr_pct": (atr_value / price) if atr_value else None, "max_notional": round(cap_notional, 2),
            "stop": round(price - (atr_value * 3 if atr_value else price * 0.15), 2), "basis": basis}


def trailing_stop(high_since_entry: float, atr_value: float | None, *, atr_mult: float = 3.0,
                  pct: float = 0.15) -> float:
    """Stop level: the higher (tighter) of 3x ATR or 15% below the high-water mark."""
    by_pct = high_since_entry * (1 - pct)
    by_atr = high_since_entry - atr_value * atr_mult if atr_value else by_pct
    return max(by_pct, by_atr)


STOP_TYPES = ("trailing", "fixed", "percent", "none")
AT_ONCE = "that stop is at or above today's price, so it would sell at once"


def normalize_stop(kind: Any, value: Any = None, price: float | None = None) -> dict[str, Any]:
    """A practice stop-loss setting as stored with the position: {"type", "value"}. Raises ValueError with a
    plain message. ``price`` (today's price) is checked for a fixed stop."""
    kind = str(kind or "trailing").strip().lower()
    if kind not in STOP_TYPES:
        raise ValueError("stop type must be trailing, fixed, percent or none")
    if kind in ("trailing", "none"):
        return {"type": kind, "value": None}
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ValueError("enter the stop price in rupees" if kind == "fixed" else "enter how many percent below your buy price") from None
    if not math.isfinite(v):
        raise ValueError("enter a number for the stop")
    if kind == "percent":
        if not 0.5 <= v <= 50:
            raise ValueError("the stop must be between 0.5% and 50% below your buy price")
        return {"type": "percent", "value": v}
    if v <= 0:
        raise ValueError("the stop price must be above zero")
    if price is not None and v >= price:
        raise ValueError(AT_ONCE)
    return {"type": "fixed", "value": v}


def _field(obj: Any, name: str, default: Any = None) -> Any:
    got = obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)
    return default if got is None else got


def position_stop(pos: Any, bars: list[dict[str, Any]] | None = None, *, atr_mult: float = 3.0,
                  pct: float = 0.15) -> dict[str, Any]:
    """The one stop rule for a practice position (a dict or an object with .stop_type, .stop_value,
    .avg_entry_price, .high_water, .current_price). Returns {"type", "value", "level", "label"}; ``level`` is
    None for "none". No stop setting means trailing: the tighter of 3x ATR or 15% below the highest price since entry."""
    kind = _field(pos, "stop_type", "trailing")
    value = _field(pos, "stop_value")
    if kind == "none":
        return {"type": "none", "value": None, "level": None, "label": "none"}
    if kind == "fixed":
        return {"type": "fixed", "value": value, "level": float(value), "label": "fixed"}
    if kind == "percent":
        return {"type": "percent", "value": value, "level": _field(pos, "avg_entry_price") * (1 - float(value) / 100),
                "label": f"−{float(value):g}% from buy"}
    price = _field(pos, "current_price")
    avg = _field(pos, "avg_entry_price")
    high = _field(pos, "high_water") or max(price if price is not None else avg, avg)
    try:
        a = atr(bars) if bars else None
    except Exception:  # noqa: BLE001
        a = None
    return {"type": "trailing", "value": None, "level": trailing_stop(high, a, atr_mult=atr_mult, pct=pct),
            "label": "trailing"}


def check_stops(positions: list[Any], bars_fn: Any, *, atr_mult: float = 3.0, pct: float = 0.15) -> list[dict[str, Any]]:
    """Positions whose current price is at or below their stop (``position_stop``: trailing unless the position
    has its own setting; "none" never hits).

    ``positions`` need .symbol, .qty, .current_price and optionally .high_water, .stop_type, .stop_value;
    ``bars_fn(symbol)`` returns daily bars for the ATR.
    """
    hits = []
    for p in positions:
        price = getattr(p, "current_price", None)
        if price is None:
            continue
        kind = getattr(p, "stop_type", None) or "trailing"
        bars: list[dict[str, Any]] = []
        if kind == "trailing":
            try:
                bars = bars_fn(p.symbol)
            except Exception:  # noqa: BLE001
                bars = []
        s = position_stop(p, bars, atr_mult=atr_mult, pct=pct)
        if s["level"] is None or price > s["level"]:
            continue
        high = getattr(p, "high_water", None) or max(price, getattr(p, "avg_entry_price", price))
        hits.append({"symbol": p.symbol, "qty": p.qty, "price": price, "high_water": high,
                     "stop": round(s["level"], 2), "level": s["level"], "type": s["type"], "label": s["label"],
                     "stop_value": s["value"], "drawdown_from_high": price / high - 1.0})
    return hits
