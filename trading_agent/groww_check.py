"""Verify the live-trading assumptions against your real Groww account.

``python -m trading_agent groww-check`` only reads: token source and expiry, the
holdings fields used for sellable quantity, tick sizes, and the order / GTT lists.

``groww-check --live-test SYMBOL --i-understand-real-orders`` (needs
GROWW_LIVE_ORDERS=true) additionally places REAL but deliberately harmless orders:

* first today's order list (count, open orders, and any order the agent did not place), so you can
  compare it with the Groww app;
* a 1-share DAY LIMIT BUY priced ``--offset-pct`` (default 3%) below the last price,
  so it should rest unfilled, then reads it back by id and by reference, moves its limit price up
  0.5% through Groww's modify-order endpoint (staying at least 2% below the last price, so it cannot
  fill), reads the new price back and cancels it (the cancel is attempted even if the modify fails);
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

DEFAULT_LIVE_TEST_SYMBOL = "ITC"  # liquid large cap, 0.05 tick
AGENT_REFERENCE_PREFIXES = ("TA-", "VT-", "SL-")  # make_reference_id prefixes used by this agent
ORDER_PAGE_SIZE = 100  # Groww's maximum
ORDER_PAGES = 5
MODIFY_UP_PCT = 0.5
MODIFY_SKIP_IF_LTP_DROP_PCT = 1.0  # the market fell this much since the order was priced: do not modify
MODIFY_READ_BACKOFF = (0.5, 1.0, 2.0)
MODIFY_MAX_PCT_OF_LTP = 98.0  # the modified price must stay at least 2% below LTP so it cannot fill

DDPI_MANUAL_CHECK = ("confirm in the Groww app (Profile -> Settings -> Demat / DDPI authorisation) that DDPI is "
                     "active; without it automated sells are rejected. Groww's API has no DDPI status call, so "
                     "record your confirmation with GROWW_DDPI_CONFIRMED=true (Settings page).")


class Check:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def add(self, name: str, ok: bool | None, detail: str) -> bool | None:
        self.rows.append({"check": name, "ok": ok, "detail": detail})
        return ok


def read_only_checks(broker: GrowwBroker, *, token_source: str, cache: TokenCache | None = None,
                     api_key: str | None = None, tick_fn: Callable[[str], float | None] | None = None,
                     ddpi_confirmed: bool | None = None) -> Check:
    c = Check()
    state = {True: "you have confirmed it (GROWW_DDPI_CONFIRMED=true)",
             False: "NOT yet confirmed (GROWW_DDPI_CONFIRMED=false)",
             None: "confirmation not recorded"}[ddpi_confirmed]
    c.add("DDPI (manual check)", None, f"{DDPI_MANUAL_CHECK} Status: {state}.")
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
            n = len(payload.get("order_list") or payload.get("orders") or payload.get("data") or [])
            c.add(name, True, f"readable ({n} rows; keys: {', '.join(sorted(payload)[:6])})")
        except Exception as e:  # noqa: BLE001
            c.add(name, False, f"{type(e).__name__}: {e}")
    return c


FILLED_TEXT = "TEST ORDER FILLED: 1 share bought \u2014 sell it in the Groww app if unwanted"
STILL_OPEN = "TEST ORDER MAY STILL BE OPEN: CANCEL IT IN THE GROWW APP NOW"


def _cancel_if_placed(broker: GrowwBroker, c: Check, ref: str) -> None:
    """The placement call failed or timed out: it may have reached Groww. Look it up by reference; cancel if so."""
    try:
        found = broker.order_status_by_reference(ref)
    except Exception as e:  # noqa: BLE001
        c.add("live: lookup after failed placement", None,
              f"could not look up reference {ref} ({type(e).__name__}: {e}); check the Groww app for it")
        return
    oid = found.get("groww_order_id")
    if not oid:
        c.add("live: lookup after failed placement", None, f"reference {ref} not found at Groww: nothing was placed")
        return
    c.add("live: lookup after failed placement", False,
          f"the order WAS placed (id {oid}, {found.get('order_status')}); cancelling it")
    try:
        broker.cancel_order(oid)
        after = broker.confirm_order(oid, tries=3)
        gone = after["order_status"] in ("CANCELLED", "CANCELLATION_REQUESTED")
        c.add("live: cancel order", gone, f"now {after['order_status']}" if gone else f"{STILL_OPEN} (id {oid})")
    except Exception as e:  # noqa: BLE001
        c.add("live: cancel order", False, f"{type(e).__name__}: {e}. {STILL_OPEN} (id {oid})")


def _order_list_rows(broker: GrowwBroker, c: Check) -> None:
    """Print today's order list: count, open orders, and orders not placed by the agent."""
    rows: list[dict[str, Any]] = []
    capped = False
    try:
        for page in range(ORDER_PAGES):
            got = broker.order_list(page, ORDER_PAGE_SIZE)
            rows += got
            if len(got) < ORDER_PAGE_SIZE:
                break
        else:
            capped = True
    except Exception as e:  # noqa: BLE001
        c.add("live: today's orders", False, f"{type(e).__name__}: {e}")
        return
    open_rows = [r for r in rows if classify_status(r.get("order_status")) == "open"]
    c.add("live: today's orders", True, f"{len(rows)} order(s) today{f' (first {ORDER_PAGES * ORDER_PAGE_SIZE} only)' if capped else ''}, "
          f"{len(open_rows)} open (compare with the Groww app)")
    for r in open_rows:
        ref = str(r.get("order_reference_id") or "")
        mine = ref.startswith(AGENT_REFERENCE_PREFIXES)
        c.add("live: open order", None,
              f"{r.get('transaction_type')} {r.get('quantity')} {r.get('trading_symbol')} @ {r.get('price')} "
              f"{r.get('order_status')} ref {ref or 'none'}" + ("" if mine else " - NOT placed by this agent"))
    foreign = [r for r in rows if not str(r.get("order_reference_id") or "").startswith(AGENT_REFERENCE_PREFIXES)]
    if foreign:
        c.add("live: orders not from the agent", None,
              f"{len(foreign)} of {len(rows)} today's order(s) carry no agent reference (placed in the app or elsewhere; "
              "orders fired by a GTT may also lack the agent prefix)")


def _modify_price(price: float, ltp: float, tick: float) -> float | None:
    """The resting price raised 0.5% (to the tick), capped at 98% of LTP; None when it cannot move up."""
    new = round_to_tick(price * (1 + MODIFY_UP_PCT / 100), tick, "down")
    cap = round_to_tick(ltp * MODIFY_MAX_PCT_OF_LTP / 100, tick, "down")
    new = min(new, cap)
    if new <= price + 1e-9:
        new = round_to_tick(price + tick, tick, "down")  # a coarse tick: move one tick instead
        if new > cap + 1e-9:
            return None
    return new


def _modify_step(broker: GrowwBroker, c: Check, oid: str, price: float, ltp: float, tick: float,
                 symbol: str) -> None:
    try:
        fresh = broker.latest_price(symbol)  # the market may have moved since the order was priced
    except Exception as e:  # noqa: BLE001
        c.add("live: modify order", None, f"could not fetch a fresh price ({type(e).__name__}: {e}); modify skipped")
        return
    if fresh <= ltp * (1 - MODIFY_SKIP_IF_LTP_DROP_PCT / 100) + 1e-9:
        c.add("live: modify order", None, f"{symbol} fell from {ltp} to {fresh} (1% or more) since the order "
              "was priced; modify skipped, cancelling")
        return
    ref_ltp = min(ltp, fresh)
    new = _modify_price(price, ref_ltp, tick)
    if new is None:
        c.add("live: modify order", None, f"no room to raise {price} and stay 2% below LTP {ref_ltp}; skipped")
        return
    try:
        broker.modify_order(oid, new, 1)
        seen = None
        for wait in MODIFY_READ_BACKOFF:  # the status call has no price: only order detail does
            broker.sleep(wait)
            try:
                d = broker.order_detail(oid) or {}
            except Exception:  # noqa: BLE001
                continue
            seen = d.get("price")
            if seen is not None and abs(float(seen) - new) < 1e-6:
                break  # MODIFICATION_REQUESTED in between is fine: keep polling until the price shows
        ok = None if seen is None else abs(float(seen) - new) < 1e-6
        c.add("live: modify order", ok,
              f"limit {price} -> {new} (+{MODIFY_UP_PCT:g}%, {new / ref_ltp * 100:.2f}% of LTP); "
              f"price read back from order detail: {seen}")
    except Exception as e:  # noqa: BLE001 - the cancel below must still run
        c.add("live: modify order", False, f"{type(e).__name__}: {e}")


def live_test(broker: GrowwBroker, symbol: str = DEFAULT_LIVE_TEST_SYMBOL, *, offset_pct: float = 3.0,
              c: Check | None = None) -> Check:
    """Real but harmless orders; see the module docstring. Refuses unless live."""
    c = c or Check()
    broker._require_live("run the live order test")
    symbol = (symbol or DEFAULT_LIVE_TEST_SYMBOL).upper()
    _order_list_rows(broker, c)
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
    except Exception as e:  # noqa: BLE001 - it may still have reached Groww: look it up by reference
        c.add("live: place limit order", False, f"{type(e).__name__}: {e}")
        _cancel_if_placed(broker, c, ref)
        return c
    oid = placed.get("groww_order_id")
    c.add("live: place limit order", bool(oid), f"id {oid}, status {placed.get('order_status')}, "
          f"reference echoed: {placed.get('order_reference_id') == ref}")
    if not oid:
        _cancel_if_placed(broker, c, ref)  # no id in the answer: it may still be resting
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
        if st["status"] == "open":
            _modify_step(broker, c, oid, price, ltp, tick, symbol)
        else:
            c.add("live: modify order", None, f"order is {st['order_status']}, not open; modify skipped")
    finally:
        try:
            now = (broker.order_status(oid) or {}).get("order_status")
        except Exception:  # noqa: BLE001 - assume it may still be open and try to cancel
            now = None
        if classify_status(now) == "open":
            try:
                broker.cancel_order(oid)
                after = broker.confirm_order(oid, tries=3)
                gone = after["order_status"] in ("CANCELLED", "CANCELLATION_REQUESTED")
                c.add("live: cancel order", gone,
                      f"now {after['order_status']}" if gone else
                      FILLED_TEXT if after["status"] == "filled" else
                      f"now {after['order_status']}. {STILL_OPEN} (id {oid})")
            except Exception as e:  # noqa: BLE001
                c.add("live: cancel order", False, f"{type(e).__name__}: {e}. {STILL_OPEN} (id {oid})")
        else:
            c.add("live: cancel order", False,
                  f"the test buy is {now}, not open: it was not left resting. If it FILLED you now own 1 share "
                  f"of {symbol}; check the Groww app (id {oid})")

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
