"""Opening collar: no buy in the noisy first minutes, and no buy chasing a spike above the opening price.

* Time: no live buy before OPEN_COLLAR_START (09:17 IST). Practice buys, for realism, are refused from 09:00 (the
  pre-open) to OPEN_COLLAR_START on trading days; outside those minutes they work as before (evenings included).
* Price (live buys, from OPEN_COLLAR_START to OPEN_COLLAR_END, 09:17 to 09:45 IST): limit = min(the existing
  MAX_SLIPPAGE_PCT cap above LTP, today's opening price + OPEN_COLLAR_BPS basis points), rounded down to the tick.
  After OPEN_COLLAR_END only the MAX_SLIPPAGE_PCT cap applies. Today's opening price is the price NSE's pre-open
  call auction discovered; it comes from Groww's quote, else from today's Yahoo bar. With neither, the buy is refused
  (never priced blind).
* Time out (live buys): a buy still open BUY_TIMEOUT_S (120 s) after it was placed is cancelled; a partial fill keeps
  what filled (GrowwBroker.submit_order does this).

Sells are never collared.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time as dtime
from typing import Any

from .timezones import IST

PRACTICE_FROM = dtime(9, 0)


def _now() -> datetime:
    return datetime.now(IST)


def start_time(settings: Any) -> dtime:
    return dtime.fromisoformat(str(getattr(settings, "open_collar_start", "09:17") or "09:17"))


def end_time(settings: Any) -> dtime:
    return dtime.fromisoformat(str(getattr(settings, "open_collar_end", "09:45") or "09:45"))


def price_cap_active(settings: Any, now: datetime | None = None) -> bool:
    """True from OPEN_COLLAR_START up to (not including) OPEN_COLLAR_END: buys are capped at the opening price."""
    t = (now or _now()).astimezone(IST).time()
    return start_time(settings) <= t < end_time(settings)


def live_buy_block(settings: Any, now: datetime | None = None) -> str | None:
    """The reason a live buy is refused right now, or None."""
    n = (now or _now()).astimezone(IST)
    start = start_time(settings)
    if n.time() < start:
        return f"opening collar: no buys before {start:%H:%M} IST (it is {n:%H:%M})"
    return None


def practice_buy_block(settings: Any, now: datetime | None = None, calendar: Any | None = None) -> str | None:
    """The reason a practice buy is refused right now, or None: only from 09:00 to the collar start on a trading day."""
    n = (now or _now()).astimezone(IST)
    start = start_time(settings)
    if not PRACTICE_FROM <= n.time() < start:
        return None
    trading = n.weekday() < 5 if calendar is None else calendar.is_trading_day(n.date())
    if not trading:
        return None
    return f"opening collar: no practice buys from 09:00 to {start:%H:%M} IST (it is {n:%H:%M})"


def collar_limit(cap: float, opening: float, bps: float, tick: float) -> float:
    """min(cap, opening x (1 + bps / 10,000)) rounded down to the tick."""
    from .groww import round_to_tick
    return min(cap, round_to_tick(opening * (1 + float(bps) / 10_000.0), tick, "down"))


_OPEN_IN_TEXT = re.compile(r"open\s*[:=]\s*([0-9.]+)", re.I)


def opening_from_quote(payload: Any) -> float | None:
    """Today's opening price from a Groww quote payload. ``ohlc`` comes as a dict or as the text
    "{open: 149.50,high: ...}" (both are seen in Groww's documentation). None when absent or not positive."""
    if not isinstance(payload, dict):
        return None
    o = payload.get("ohlc")
    val: Any = None
    if isinstance(o, dict):
        val = o.get("open")
    elif isinstance(o, str):
        m = _OPEN_IN_TEXT.search(o)
        val = m.group(1) if m else None
    if val is None:
        val = payload.get("open_price") or payload.get("open")
    try:
        x = float(val)
    except (TypeError, ValueError):
        return None
    return x if x > 0 else None


def yahoo_opening(prices: Any, symbol: str, today: date | None = None) -> float | None:
    """Today's opening price from Yahoo's daily bar (cached a minute), or None when there is no bar for today."""
    if prices is None or not hasattr(prices, "history_ohlc"):
        return None
    day = (today or _now().date()).isoformat()
    try:
        bars = prices.history_ohlc(symbol, "5d", ttl=60.0)
    except Exception:  # noqa: BLE001 - no fallback price: the caller refuses the buy
        return None
    for b in reversed(bars or []):
        if b.get("date") == day:
            try:
                x = float(b["open"])
            except (KeyError, TypeError, ValueError):
                return None
            return x if x > 0 else None
    return None
