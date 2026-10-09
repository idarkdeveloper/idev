"""Bookkeeping for real (live) Groww orders and the GTT stop-losses that protect them.

* ``record_order`` stores every live order in ``state.json`` under ``live_orders`` and
  sends failures (REJECTED / FAILED / CANCELLED, or a placement error) to the notifier.
* ``refresh_open_orders`` re-checks orders still "open" (``orders --refresh``).
* ``GttStopManager`` keeps one GTT SELL (trigger DOWN) per live CNC holding at the
  trailing-stop level: created when missing, moved up as the stop rises (never down),
  cancelled when the holding is gone.

Nothing here runs unless the broker has ``live_orders`` enabled (GROWW_LIVE_ORDERS=true);
the GTT manager additionally needs GROWW_GTT_STOPS=true.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .risk import atr, trailing_stop
from .state import State

log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

ORDER_FIELDS = ("id", "groww_order_id", "order_reference_id", "symbol", "side", "qty", "order_type",
                "limit_price", "ltp", "order_status", "filled_quantity", "average_fill_price", "remark",
                "status", "placed_at", "checked_at", "error", "source")


def _now() -> str:
    return datetime.now(IST).isoformat(timespec="seconds")


def is_live_broker(broker: Any) -> bool:
    return getattr(broker, "name", "") == "groww" and bool(getattr(broker, "live_orders", False))


# --------------------------------------------------------------------------- #
# Orders
# --------------------------------------------------------------------------- #
def record_order(state: State, order: dict[str, Any], notifier: Any | None = None,
                 *, source: str | None = None) -> dict[str, Any]:
    """Upsert a live order into state (by groww_order_id or reference) and alert on failure."""
    row = {k: order.get(k) for k in ORDER_FIELDS if order.get(k) is not None}
    if source and "source" not in row:
        row["source"] = source
    orders = state.data.setdefault("live_orders", [])
    key = row.get("groww_order_id") or row.get("order_reference_id")
    prev = next((o for o in orders if key and (o.get("groww_order_id") or o.get("order_reference_id")) == key), None)
    was = prev.get("status") if prev else None
    if prev is not None:
        prev.update(row)
        row = prev
    else:
        orders.append(row)
        state.data["live_orders"] = orders[-500:]
    if row.get("status") == "failed" and was != "failed":
        notify_failure(notifier, row)
    return row


def record_order_error(state: State, symbol: str, side: str, error: str, notifier: Any | None = None,
                       *, source: str | None = None, qty: Any = None) -> dict[str, Any]:
    """A live order that never got a Groww order id (API error at placement)."""
    return record_order(state, {"symbol": symbol.upper(), "side": side, "qty": qty, "status": "failed",
                                "order_status": "NOT_PLACED", "error": error, "placed_at": _now(),
                                "order_reference_id": f"err-{datetime.now(IST).strftime('%H%M%S%f')}"},
                        notifier, source=source)


def notify_failure(notifier: Any | None, o: dict[str, Any]) -> None:
    if notifier is None:
        return
    subject = f"[ORDER {o.get('order_status') or 'FAILED'}] {str(o.get('side', '')).upper()} {o.get('symbol')}"
    body = (f"Live Groww order did not go through.\n"
            f"Symbol: {o.get('symbol')}  side: {o.get('side')}  qty: {o.get('qty')}\n"
            f"Limit: {o.get('limit_price')}  status: {o.get('order_status')}  "
            f"filled: {o.get('filled_quantity', 0)}\n"
            f"Groww order id: {o.get('groww_order_id') or 'n/a'}  reference: {o.get('order_reference_id')}\n"
            f"Reason: {o.get('remark') or o.get('error') or 'n/a'}\n")
    try:
        notifier.send(subject, body)
    except Exception as e:  # noqa: BLE001
        log.warning("failure notification not delivered: %s", e)


def refresh_open_orders(broker: Any, state: State, notifier: Any | None = None) -> list[dict[str, Any]]:
    """Re-check every live order still "open"; returns the updated rows. Read-only calls."""
    updated = []
    for o in state.data.get("live_orders", []):
        if o.get("status") != "open" or not o.get("groww_order_id"):
            continue
        try:
            st = broker.confirm_order(o["groww_order_id"], tries=1)
        except Exception as e:  # noqa: BLE001
            log.warning("status for %s unavailable: %s", o["groww_order_id"], e)
            continue
        updated.append(record_order(state, {**o, **st}, notifier))
    return updated


# --------------------------------------------------------------------------- #
# GTT stop-losses
# --------------------------------------------------------------------------- #
class GttStopManager:
    """One GTT stop-loss per live holding, kept in ``state.json`` under ``gtt_stops``."""

    def __init__(self, broker: Any, state: State, *, bars_fn: Callable[[str], list] | None = None,
                 notifier: Any | None = None, enabled: bool = True):
        self.broker = broker
        self.state = state
        self.bars_fn = bars_fn or (lambda sym: [])
        self.notifier = notifier
        self.enabled = enabled

    @property
    def stops(self) -> dict[str, dict[str, Any]]:
        return self.state.data.setdefault("gtt_stops", {})

    def _require(self) -> None:
        if not is_live_broker(self.broker):
            raise PermissionError("GTT stops need the live Groww broker (GROWW_LIVE_ORDERS=true).")
        if not self.enabled:
            raise PermissionError("GTT stops are off (set GROWW_GTT_STOPS=true).")

    def _stop_for(self, sym: str, high: float) -> float:
        try:
            a = atr(self.bars_fn(sym))
        except Exception:  # noqa: BLE001
            a = None
        return trailing_stop(high, a)

    def sync(self, positions: list[Any] | None = None) -> list[dict[str, Any]]:
        """Create / raise / cancel GTTs so they match the holdings. Returns the actions."""
        self._require()
        positions = self.broker.positions() if positions is None else positions
        held = {p.symbol.upper(): p for p in positions if (p.qty or 0) > 0}
        actions: list[dict[str, Any]] = []

        for sym in [s for s in self.stops if s not in held]:
            actions.append(self.cancel(sym, reason="holding sold"))

        for sym, p in held.items():
            qty = int(p.free_qty)
            rec = self.stops.get(sym)
            price = p.current_price
            if qty < 1:  # everything pledged / locked: a GTT could not sell anything
                if rec:
                    actions.append(self.cancel(sym, reason="no free shares"))
                continue
            if price is None:
                continue
            high = max(x for x in (rec.get("high_water") if rec else None, price, p.avg_entry_price) if x)
            trigger, limit = self.broker.gtt_stop_prices(sym, self._stop_for(sym, high))
            if rec is None or not rec.get("smart_order_id"):
                if trigger >= price:
                    actions.append({"symbol": sym, "action": "skip", "reason": f"stop {trigger:.2f} at/above price {price:.2f}"})
                    continue
                try:
                    out = self.broker.create_gtt_stop(sym, qty, trigger, limit)
                except Exception as e:  # noqa: BLE001
                    actions.append(self._error(sym, "create", e))
                    continue
                self.stops[sym] = {"smart_order_id": out.get("smart_order_id"), "reference_id": out.get("reference_id"),
                                   "trigger": trigger, "limit": limit, "qty": qty, "high_water": high,
                                   "status": out.get("status") or "ACTIVE", "updated_at": _now()}
                actions.append({"symbol": sym, "action": "create", "trigger": trigger, "limit": limit, "qty": qty})
                continue

            rec["high_water"] = max(rec.get("high_water") or 0, high)
            old = float(rec.get("trigger") or 0)
            new_trigger, new_limit = (trigger, limit) if trigger > old else (old, float(rec.get("limit") or limit))
            if new_trigger == old and qty == rec.get("qty"):
                continue  # never move a stop down; nothing changed
            try:
                out = self.broker.modify_gtt_stop(rec["smart_order_id"], qty, new_trigger, new_limit)
            except Exception as e:  # noqa: BLE001
                actions.append(self._error(sym, "modify", e))
                continue
            actions.append({"symbol": sym, "action": "modify", "from": old, "trigger": new_trigger,
                            "limit": new_limit, "qty": qty})
            rec.update(trigger=new_trigger, limit=new_limit, qty=qty, updated_at=_now(),
                       status=out.get("status") or rec.get("status"))
        return actions

    def cancel(self, symbol: str, *, reason: str = "") -> dict[str, Any]:
        self._require()
        sym = symbol.upper()
        rec = self.stops.get(sym)
        if not rec:
            return {"symbol": sym, "action": "none"}
        status = None
        try:
            if rec.get("smart_order_id"):
                status = self.broker.cancel_gtt(rec["smart_order_id"]).get("status") or "CANCELLED"
        except Exception as e:  # noqa: BLE001 - maybe it already triggered / expired
            try:
                status = self.broker.get_gtt(rec["smart_order_id"]).get("status")
            except Exception:  # noqa: BLE001
                status = None
            if status in (None, "ACTIVE"):
                return self._error(sym, "cancel", e)
        hist = self.state.data.setdefault("gtt_history", [])
        hist.append({**rec, "symbol": sym, "status": status, "closed_at": _now(), "reason": reason})
        self.state.data["gtt_history"] = hist[-200:]
        self.stops.pop(sym, None)
        return {"symbol": sym, "action": "cancel", "status": status, "reason": reason}

    def _error(self, sym: str, what: str, e: Exception) -> dict[str, Any]:
        msg = f"{type(e).__name__}: {e}"
        log.warning("GTT %s for %s failed: %s", what, sym, msg)
        if self.notifier is not None:
            try:
                self.notifier.send(f"[GTT {what.upper()} FAILED] {sym}",
                                   f"Could not {what} the stop-loss GTT for {sym}: {msg}")
            except Exception:  # noqa: BLE001
                pass
        if sym in self.stops:
            self.stops[sym]["last_error"] = msg
        return {"symbol": sym, "action": "error", "op": what, "error": msg}


def gtt_enabled(settings: Any, broker: Any) -> bool:
    return bool(getattr(settings, "groww_gtt_stops", False)) and is_live_broker(broker)


def sync_gtt_stops(settings: Any, broker: Any, state: State, *, bars_fn: Callable | None = None,
                   notifier: Any | None = None) -> list[dict[str, Any]] | None:
    """Run a GTT sync if (and only if) live orders and GROWW_GTT_STOPS are both on."""
    if not gtt_enabled(settings, broker):
        return None
    try:
        return GttStopManager(broker, state, bars_fn=bars_fn, notifier=notifier).sync()
    except Exception as e:  # noqa: BLE001
        log.warning("GTT sync failed: %s", e)
        return [{"action": "error", "error": str(e)}]
