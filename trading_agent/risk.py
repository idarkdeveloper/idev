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


def check_stops(positions: list[Any], bars_fn: Any, *, atr_mult: float = 3.0, pct: float = 0.15) -> list[dict[str, Any]]:
    """Positions whose current price is at or below their trailing stop.

    ``positions`` need .symbol, .qty, .current_price and optionally .high_water;
    ``bars_fn(symbol)`` returns daily bars for the ATR.
    """
    hits = []
    for p in positions:
        price = getattr(p, "current_price", None)
        if price is None:
            continue
        high = getattr(p, "high_water", None) or max(price, getattr(p, "avg_entry_price", price))
        try:
            a = atr(bars_fn(p.symbol))
        except Exception:  # noqa: BLE001
            a = None
        stop = trailing_stop(high, a, atr_mult=atr_mult, pct=pct)
        if price <= stop:
            hits.append({"symbol": p.symbol, "qty": p.qty, "price": price, "high_water": high,
                         "stop": round(stop, 2), "drawdown_from_high": price / high - 1.0})
    return hits
