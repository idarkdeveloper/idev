"""Verify the live-trading assumptions against your real Groww account.

``python -m trading_agent groww-check`` only reads: token source and expiry, the
holdings fields used for sellable quantity, tick sizes, and the order / GTT lists.

``groww-check --live-test SYMBOL --i-understand-real-orders`` (needs
GROWW_LIVE_ORDERS=true) additionally places REAL but deliberately harmless orders:

* a 1-share DAY LIMIT BUY priced ``--offset-pct`` (default 3%) below the last price,
  so it should rest unfilled, then reads it back by id and by reference and cancels it;
* if you hold a free share of SYMBOL, a 1-share GTT SELL with its trigger 20% below the
  price, raised once, read back and cancelled.

Results are printed and saved in state.json under ``groww_checks`` so the coverage of
each documented-but-unproven assumption is recorded. Nothing here runs on a schedule.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Callable

from .groww import (IST, GrowwBroker, TokenCache, classify_status, limit_price, round_to_tick,
                    sellable_quantity)


class Check:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def add(self, name: str, ok: bool | None, detail: str) -> bool | None:
        self.rows.append({"check": name, "ok": ok, "detail": detail})
        return ok


def read_only_checks(broker: GrowwBroker, *, token_source: str, cache: TokenCache | None = None,
                     api_key: str | None = None, tick_fn: Callable[[str], float | None] | None = None) -> Check:
    c = Check()
    expiry = None
    if cache is not None and cache.path.exists():
        try:
            expiry = json.loads(cache.path.read_text()).get("expires_at")
        except ValueError:
            expiry = None
    c.add("token", True, f"source: {token_source}" + (f"; cached until {expiry}" if expiry else ""))
    try:
        rows = broker.holdings()
    except Exception as e:  # noqa: BLE001
        c.add("holdings", False, f"{type(e).__name__}: {e}")
        return c
    c.add("holdings", True, f"{len(rows)} holdings readable with this token")
    fields = ("demat_free_quantity", "t1_quantity", "pledge_quantity", "demat_locked_quantity",
              "groww_locked_quantity")
    missing = sorted({f for h in rows for f in fields[:2] if f not in h})
    if not rows:
        c.add("sellable fields", None, "no holdings to inspect")
    else:
        c.add("sellable fields", not missing,
              "demat_free_quantity and t1_quantity present on every holding" if not missing
              else f"missing on some holdings: {', '.join(missing)} (those count as 0 sellable)")
        for h in rows:
            c.add(f"sellable {h.get('trading_symbol')}", None,
                  f"held {h.get('quantity')}, sellable {sellable_quantity(h):g} "
                  + ", ".join(f"{f}={h.get(f)}" for f in fields if f in h))
    if tick_fn is not None:
        for h in rows[:20]:
            sym = str(h.get("trading_symbol", ""))
            t = tick_fn(sym)
            c.add(f"tick {sym}", t is not None, f"tick size {t}" if t is not None else "not in instrument list; 0.05 used")
    for name, path in (("order list", "order/list"), ("GTT list", "order-advance/list")):
        try:
            payload = broker._req("GET", path, params={"segment": "CASH"} if name == "order list"
                                  else {"smart_order_type": "GTT", "segment": "CASH"})
            n = len(payload.get("order_list") or payload.get("data") or payload.get("smart_orders") or [])
            c.add(name, True, f"readable ({n} rows; keys: {', '.join(sorted(payload)[:6])})")
        except Exception as e:  # noqa: BLE001
            c.add(name, False, f"{type(e).__name__}: {e}")
    return c


def live_test(broker: GrowwBroker, symbol: str, *, offset_pct: float = 3.0, c: Check | None = None) -> Check:
    """Real but harmless orders; see the module docstring. Refuses unless live."""
    c = c or Check()
    broker._require_live("run the live order test")
    symbol = symbol.upper()
    ltp = broker.latest_price(symbol)
    tick = broker.tick_size(symbol)
    price = round_to_tick(ltp * (1 - offset_pct / 100), tick, "down")
    c.add("live: limit price", True, f"{symbol} LTP {ltp}, tick {tick}, resting BUY at {price}")
    from .groww import make_reference_id
    ref = make_reference_id("VT")
    body = {"trading_symbol": symbol, "quantity": 1, "price": price, "trigger_price": None, "validity": "DAY",
            "exchange": broker.exchange, "segment": "CASH", "product": broker.product, "order_type": "LIMIT",
            "transaction_type": "BUY", "order_reference_id": ref}
    try:
        placed = broker._req("POST", "order/create", json=body)
    except Exception as e:  # noqa: BLE001
        c.add("live: place limit order", False, f"{type(e).__name__}: {e}")
        return c
    oid = placed.get("groww_order_id")
    c.add("live: place limit order", bool(oid), f"id {oid}, status {placed.get('order_status')}, "
          f"reference echoed: {placed.get('order_reference_id') == ref}")
    if not oid:
        return c
    try:
        st = broker.confirm_order(oid, tries=2)
        c.add("live: order status", st["order_status"] is not None,
              f"{st['order_status']} ({st['status']}), filled {st['filled_quantity']}, remark {st['remark']!r}")
        by_ref = broker.order_status_by_reference(ref)
        c.add("live: status by reference", by_ref.get("groww_order_id") == oid,
              f"reference {ref} -> {by_ref.get('groww_order_id')}")
        detail = broker.order_detail(oid)
        c.add("live: order detail fields", "average_fill_price" in detail and "filled_quantity" in detail,
              ", ".join(sorted(detail)[:12]))
    finally:
        try:
            now = (broker.order_status(oid) or {}).get("order_status")
        except Exception:  # noqa: BLE001 - assume it may still be open and try to cancel
            now = None
        if classify_status(now) == "open":
            try:
                broker.cancel_order(oid)
                after = broker.confirm_order(oid, tries=3)
                c.add("live: cancel order", after["order_status"] in ("CANCELLED", "CANCELLATION_REQUESTED"),
                      f"now {after['order_status']}")
            except Exception as e:  # noqa: BLE001
                c.add("live: cancel order", False, f"{type(e).__name__}: {e} - CANCEL {oid} IN THE GROWW APP")
        else:
            c.add("live: cancel order", None, "order was no longer open (filled or rejected); check the Groww app")

    free = broker.sellable_qty(symbol)
    if free < 1:
        c.add("live: GTT", None, f"no free {symbol} shares; GTT test skipped (hold one to test it)")
        return c
    trigger = round_to_tick(ltp * 0.80, tick, "down")
    try:
        g = broker.create_gtt_stop(symbol, 1, trigger, limit_price(trigger, "sell", broker.max_slippage_pct, tick))
        gid = g.get("smart_order_id")
        c.add("live: GTT create", bool(gid), f"id {gid}, status {g.get('status')}, trigger {trigger}")
    except Exception as e:  # noqa: BLE001
        c.add("live: GTT create", False, f"{type(e).__name__}: {e}")
        return c
    if not gid:
        return c
    try:
        higher = round_to_tick(ltp * 0.81, tick, "down")
        m = broker.modify_gtt_stop(gid, 1, higher, limit_price(higher, "sell", broker.max_slippage_pct, tick))
        got = broker.get_gtt(gid)
        c.add("live: GTT modify", str(got.get("trigger_price")) in (f"{higher:.2f}", str(higher)),
              f"modify answered {sorted(m)[:6]}; trigger now {got.get('trigger_price')}, status {got.get('status')}")
    except Exception as e:  # noqa: BLE001
        c.add("live: GTT modify", False, f"{type(e).__name__}: {e}")
    finally:
        try:
            out = broker.cancel_gtt(gid)
            c.add("live: GTT cancel", (out.get("status") or broker.get_gtt(gid).get("status")) == "CANCELLED",
                  f"status {out.get('status')}")
        except Exception as e:  # noqa: BLE001
            c.add("live: GTT cancel", False, f"{type(e).__name__}: {e} - CANCEL GTT {gid} IN THE GROWW APP")
    return c


def save(state: Any, c: Check, *, live: bool) -> None:
    hist = state.data.setdefault("groww_checks", [])
    hist.append({"at": datetime.now(IST).isoformat(timespec="seconds"), "live": live, "results": c.rows})
    state.data["groww_checks"] = hist[-20:]


def format_rows(c: Check) -> str:
    mark = {True: "PASS", False: "FAIL", None: "info"}
    return "\n".join(f"  [{mark[r['ok']]}] {r['check']}: {r['detail']}" for r in c.rows)
