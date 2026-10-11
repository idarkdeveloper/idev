"""Clearing holidays: on a day NSE's clearing corporation does not settle, the proceeds of the previous trading day's
sells (T-1) are not paid out, so a buy is never sized from that money (live and practice).

NSE publishes the list beside the trading holidays (``api/holiday-master?type=clearing``); it differs from them (bank
holidays such as 1 April). It is fetched through the NSE client once a year (and again after 30 days, in case NSE adds
a day), cached in ``state/cache/nse_clearing_holidays.json``. Fail safe: while the list for the year is unknown,
every day counts as a clearing holiday, so T-1 proceeds are never counted until it is known.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from .state import atomic_write
from .timezones import IST

log = logging.getLogger(__name__)

FILE = "nse_clearing_holidays.json"
REFRESH_S = 30 * 86400
RETRY_S = 3600


def _now() -> datetime:
    return datetime.now(IST)


def _parse(d: str) -> str | None:
    try:
        return datetime.strptime(d.strip(), "%d-%b-%Y").date().isoformat()
    except (ValueError, AttributeError):
        return None


def parse(payload: Any) -> dict[str, str]:
    """{ISO date: description} from NSE's holiday-master answer (equities: the "CM" segment)."""
    out = {}
    for row in (payload or {}).get("CM", []) if isinstance(payload, dict) else []:
        day = _parse(str(row.get("tradingDate") or ""))
        if day:
            out[day] = str(row.get("description") or "clearing holiday").strip().rstrip("*").strip()
    return out


class ClearingHolidays:
    def __init__(self, state_dir: Path):
        self.path = Path(state_dir) / "cache" / FILE

    def load(self) -> dict[str, Any]:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return d if isinstance(d, dict) else {}

    def due(self, now: datetime) -> bool:
        d = self.load()
        clock = time.time()
        if d.get("failed_at") and clock - float(d["failed_at"]) < RETRY_S:
            return False
        if d.get("year") != now.astimezone(IST).year:
            return True
        return clock - float(d.get("fetched_at_s") or 0) >= REFRESH_S

    def tick(self, client: Any, now: datetime) -> int | None:
        """Fetch when due; never raises. Returns the days stored, or None."""
        if client is None or not hasattr(client, "_get") or not self.due(now):
            return None
        from . import health
        sd = self.path.parent.parent
        try:
            days = parse(client._get("api/holiday-master", params={"type": "clearing"},
                                     referer="https://www.nseindia.com/resources/exchange-communication-holidays"))
            year = now.astimezone(IST).year
            if not any(k.startswith(str(year)) for k in days):
                raise ValueError(f"NSE returned no clearing holidays for {year}")
        except Exception as e:  # noqa: BLE001 - unknown list: T-1 proceeds are held back every day (fail safe)
            d = self.load()
            d["failed_at"], d["error"] = time.time(), f"{type(e).__name__}: {e}"[:200]
            atomic_write(self.path, json.dumps(d))
            health.note_run(sd, "clearing", ok=False, error=f"{type(e).__name__}: {e}")
            log.warning("NSE clearing holidays unavailable: %s", e)
            return None
        atomic_write(self.path, json.dumps({"year": year, "fetched_at": now.isoformat(timespec="seconds"),
                                            "fetched_at_s": time.time(), "days": days}))
        health.note_run(sd, "clearing", ok=True, rows=len(days))
        return len(days)

    def status(self, day: date) -> bool | None:
        """True / False for a clearing holiday, None when the list for that year is unknown."""
        d = self.load()
        if d.get("year") != day.year or not isinstance(d.get("days"), dict):
            return None
        return day.isoformat() in d["days"]


def previous_trading_day(day: date, calendar: Any | None) -> date:
    d = day - timedelta(days=1)
    for _ in range(10):
        if (d.weekday() < 5) if calendar is None else calendar.is_trading_day(d):
            return d
        d -= timedelta(days=1)
    return d


def hold(state_dir: Path, sells: Iterable[tuple[date, float]], today: date, calendar: Any | None = None) -> tuple[float, str]:
    """(amount not to count as cash today, why) from ``sells`` = (trade date, proceeds). Non-zero only on a clearing
    holiday (or while the list is unknown) and only for the previous trading day's sells."""
    st = ClearingHolidays(state_dir).status(today)
    if st is False:
        return 0.0, ""
    t1 = previous_trading_day(today, calendar)
    amount = round(sum(float(v) for d, v in sells if d == t1 and v), 2)
    if amount <= 0:
        return 0.0, ""
    why = ("today is an NSE clearing holiday" if st else "NSE's clearing-holiday list is unknown") + \
          f": proceeds of {t1:%d %b} sells ({amount:,.2f}) are not settled yet and are not counted"
    return amount, why


def practice_cash_hold(settings: Any) -> Any:
    """cash_hold(orders) for the practice account (India only): (amount, why) not to count as cash today."""
    if str(getattr(settings, "market", "in") or "in") != "in":
        return None
    from .groww_hours import default_calendar
    cal = default_calendar(settings)
    return lambda orders: hold(Path(settings.state_dir), practice_sells(orders), _now().date(), cal)


def _ist_date(stamp: Any) -> date | None:
    try:
        return datetime.fromisoformat(str(stamp)).astimezone(IST).date()
    except (TypeError, ValueError):
        return None


def practice_sells(orders: list[dict[str, Any]]) -> list[tuple[date, float]]:
    """(trade date, net proceeds) of the filled sells of a paper account."""
    out = []
    for o in orders:
        if o.get("side") != "sell" or o.get("status") != "filled":
            continue
        d = _ist_date(o.get("filled_at"))
        if d is not None:
            out.append((d, float(o.get("notional") or 0) - float(o.get("fees") or 0)))
    return out


def live_sells(orders: list[dict[str, Any]]) -> list[tuple[date, float]]:
    """(trade date, proceeds) of the live sells recorded in state.json (``live_orders``) that filled, even partly."""
    out = []
    for o in orders:
        if o.get("side") != "sell":
            continue
        q = float(o.get("filled_quantity") or 0)
        px = o.get("average_fill_price") or o.get("limit_price")
        d = _ist_date(o.get("placed_at"))
        if q > 0 and px and d is not None:
            out.append((d, q * float(px)))
    return out
