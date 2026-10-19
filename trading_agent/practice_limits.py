"""Practice limit orders: a buy or sell at a price you choose, on the practice account only.

A limit order fills like a real one: a buy when the price is at or below your limit, a sell when it is at or above it,
at the latest price (which is your limit or better). If the price is not there yet the order waits in
``state/practice_limits.json`` and the dashboard's practice checker (stops.PracticeStopChecker) looks at it every minute
in NSE market hours. A "day" order expires at the close of the day it was placed; a "gtc" order waits until it fills
or you cancel it. Nothing here has a Groww client: fills go through the practice ``LocalPaperBroker`` the caller gives.
The kill switch stops fills too (the checker does not run while HALT is set).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any, Callable

from .timezones import IST

log = logging.getLogger(__name__)

FILE = "practice_limits.json"
VALIDITY = ("day", "gtc")
CLOSE = dtime(15, 30)
KEEP_CLOSED = 50          # filled / cancelled / expired orders kept for the page
MAX_OPEN = 50             # a practice account does not need more waiting orders than this

_LOCK = threading.RLock()


def marketable(side: str, price: float, limit: float) -> bool:
    """A buy fills at or below its limit, a sell at or above it."""
    return price <= limit if side == "buy" else price >= limit


class LimitBook:
    def __init__(self, state_dir: Path, now_fn: Callable[[], datetime] | None = None):
        self.path = Path(state_dir) / FILE
        self._now_fn = now_fn or (lambda: datetime.now(IST))

    # -- storage --------------------------------------------------------------
    def _load(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as e:
            log.warning("practice limit orders unreadable (%s): starting empty", e)
            return []
        return [o for o in data if isinstance(o, dict)] if isinstance(data, list) else []

    def _save(self, orders: list[dict[str, Any]]) -> None:
        live = [o for o in orders if o["status"] == "open"]
        done = [o for o in orders if o["status"] != "open"][-KEEP_CLOSED:]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(live + done, indent=1))
        os.replace(tmp, self.path)

    def _now(self) -> datetime:
        n = self._now_fn()
        return n.astimezone(IST) if n.tzinfo is not None else n

    def _expire(self, orders: list[dict[str, Any]], now: datetime) -> bool:
        """Day orders end at the close of the day they were placed. Returns whether anything changed."""
        changed = False
        for o in orders:
            if o["status"] == "open" and o["validity"] == "day":
                if now.date().isoformat() > o["day"] or (now.date().isoformat() == o["day"] and now.time() > CLOSE):
                    o["status"], o["closed_at"] = "expired", now.isoformat(timespec="seconds")
                    changed = True
        return changed

    # -- the page's calls -----------------------------------------------------
    def add(self, symbol: str, side: str, limit: float, *, qty: float | None = None, notional: float | None = None,
            validity: str = "day", stop: dict[str, Any] | None = None) -> dict[str, Any]:
        symbol = str(symbol).strip().upper()
        if side not in ("buy", "sell"):
            raise ValueError("side must be buy or sell")
        try:
            limit = float(limit)
        except (TypeError, ValueError):
            raise ValueError("enter a limit price") from None
        if not limit > 0:
            raise ValueError("the limit price must be above zero")
        if validity not in VALIDITY:
            raise ValueError("validity must be 'day' or 'gtc'")
        if (qty is None) == (notional is None):
            raise ValueError("enter an amount or a number of shares")
        if (qty is not None and not qty > 0) or (notional is not None and not notional > 0):
            raise ValueError("the amount or number of shares must be above zero")
        now = self._now()
        with _LOCK:
            orders = self._load()
            self._expire(orders, now)
            if sum(1 for o in orders if o["status"] == "open") >= MAX_OPEN:
                raise ValueError(f"at most {MAX_OPEN} practice orders can wait at once; cancel one first")
            order = {"id": uuid.uuid4().hex[:10], "symbol": symbol, "side": side, "limit": round(limit, 2),
                     "qty": qty, "notional": notional, "validity": validity, "stop": stop, "status": "open",
                     "placed_at": now.isoformat(timespec="seconds"), "day": now.date().isoformat()}
            orders.append(order)
            self._save(orders)
        return order

    def cancel(self, order_id: str) -> dict[str, Any]:
        now = self._now()
        with _LOCK:
            orders = self._load()
            self._expire(orders, now)
            o = next((x for x in orders if x["id"] == str(order_id)), None)
            if o is None:
                raise LookupError("no such practice order")
            if o["status"] != "open":
                self._save(orders)
                raise ValueError(f"this order is already {o['status']}")
            o["status"], o["closed_at"] = "cancelled", now.isoformat(timespec="seconds")
            self._save(orders)
        return o

    def orders(self) -> list[dict[str, Any]]:
        """Open orders first (oldest first), then the most recent closed ones (newest first)."""
        now = self._now()
        with _LOCK:
            orders = self._load()
            if self._expire(orders, now):
                self._save(orders)
        live = [o for o in orders if o["status"] == "open"]
        done = sorted((o for o in orders if o["status"] != "open"), key=lambda o: o.get("closed_at") or "",
                      reverse=True)
        return live + done[:10]

    # -- the checker's call -----------------------------------------------------
    def sweep(self, broker: Any, now: datetime | None = None) -> list[dict[str, Any]]:
        """Fill every open order whose limit the latest price has reached; expire finished day orders. Returns the
        orders filled. A failed fill (a price outage, not enough cash, nothing to sell) leaves the order waiting, with
        the reason kept on it, and is tried again next minute."""
        now = now.astimezone(IST) if now is not None and now.tzinfo is not None else (now or self._now())
        with _LOCK:
            orders = self._load()
            changed = self._expire(orders, now)
            pending = [dict(o) for o in orders if o["status"] == "open"]
        filled: list[dict[str, Any]] = []
        results: dict[str, dict[str, Any]] = {}
        for o in pending:
            try:
                price = float(broker.latest_price(o["symbol"]))
            except Exception as e:  # noqa: BLE001 - no price this minute: try next minute
                results[o["id"]] = {"note": f"no price: {e}"[:160]}
                continue
            if not price > 0 or not marketable(o["side"], price, o["limit"]):
                continue
            try:
                kw: dict[str, Any] = {"qty": o["qty"]} if o["qty"] is not None else {"notional": o["notional"]}
                if o["side"] == "buy" and o.get("stop"):
                    kw["stop"] = o["stop"]
                fill = broker.submit_order(o["symbol"], o["side"], **kw,
                                           extra={"limit": o["limit"], "limit_order": o["id"]})
            except Exception as e:  # noqa: BLE001 - e.g. not enough cash or shares: it keeps waiting, reason shown
                results[o["id"]] = {"note": str(e)[:160]}
                continue
            done = {"status": "filled", "closed_at": now.isoformat(timespec="seconds"),
                    "fill_qty": fill.get("qty"), "fill_price": fill.get("filled_avg_price"), "note": None}
            results[o["id"]] = done
            filled.append({**o, **done})
        if results or changed:
            with _LOCK:   # merge into the file as it is now: a cancel made meanwhile wins over a note, not over a fill
                orders = self._load()
                self._expire(orders, now)
                for cur in orders:
                    r = results.get(cur["id"])
                    if r is None:
                        continue
                    if r.get("status") == "filled" or cur["status"] == "open":
                        cur.update(r)
                self._save(orders)
        return filled
