"""Order history as trades: the fills a person made, paired up (first in, first out) into closed trades with a buy price, a sell price and a
profit or loss, plus the open buys with an unrealised profit or loss at today's price.

``trade_view(orders, positions)`` is a pure function over what the dashboard already has: the practice account's orders (symbol, side, qty,
filled_avg_price, fees, filled_at, stop_hit ...) or Groww's real orders (``live: True``; filled_quantity, average_fill_price, placed_at; charges are
not stored, so they are None and shown as n/a). Read-only: it never touches a broker.

A closed trade's P/L is FIFO and net of charges (the buy charges of the lots it used and the sell charge). The broker's own ``realised_pl`` on a sale is
average-cost and net of the sale charge only, so the two can differ by the buy charges and by lot prices; the broker's number is passed through as
``broker_pl`` for reference. An open trade's P/L is gross (price move only), at the position's current price.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

EPS = 1e-9


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) != float("inf") else None


def _when(v: Any) -> datetime:
    try:
        d = datetime.fromisoformat(str(v))
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def normalise(o: dict[str, Any]) -> dict[str, Any] | None:
    """One filled order in a common shape, or None when it is not a fill (a rejected, cancelled or empty order)."""
    side = str(o.get("side") or "").lower()
    if side not in ("buy", "sell"):
        return None
    live = bool(o.get("live"))
    if live:
        qty = _num(o.get("filled_quantity")) or 0.0
        px = _num(o.get("average_fill_price")) or _num(o.get("limit_price"))
        at, fees = o.get("placed_at") or o.get("at"), None
    else:
        if str(o.get("status") or "filled").lower() != "filled":
            return None
        qty, px = _num(o.get("qty")) or 0.0, _num(o.get("filled_avg_price"))
        at, fees = o.get("filled_at") or o.get("at"), _num(o.get("fees"))
    if qty <= EPS or not px or px <= 0:
        return None
    return {"id": str(o.get("id") or o.get("groww_order_id") or ""), "symbol": str(o.get("symbol") or "").upper(), "side": side, "qty": qty, "px": px, "at": str(at or ""),
            "when": _when(at), "fees": fees, "live": live, "stop_hit": bool(o.get("stop_hit")), "stop_type": o.get("stop_type"),
            "avg_entry": _num(o.get("avg_entry_price")), "broker_pl": _num(o.get("realised_pl"))}


def _r(v: float | None, d: int = 2) -> float | None:
    return None if v is None else round(v, d)


def trade_view(orders: list[dict[str, Any]], positions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """{"trades": [...newest first], "summary": {...}}. ``orders`` in any order; ``positions``: [{symbol, current_price?}] for the "now" price of open trades
    (a trade without one has now_px None and no unrealised P/L)."""
    fills = [n for n in (normalise(o) for o in orders or []) if n]
    # oldest first; the list is usually newest first, so a tie keeps the later position in the list as the earlier fill
    order = sorted(range(len(fills)), key=lambda i: (fills[i]["when"], -i))
    now_px = {str(p.get("symbol") or "").upper(): _num(p.get("current_price")) for p in positions or []}
    lots: dict[str, list[dict[str, Any]]] = {}
    trades: list[dict[str, Any]] = []
    for i in order:
        f = fills[i]
        sym = f["symbol"]
        if f["side"] == "buy":
            lots.setdefault(sym, []).append({"f": f, "left": f["qty"]})
            continue
        need, cost, buy_fee, fee_known, first, matched = f["qty"], 0.0, 0.0, True, None, 0.0
        for lot in lots.get(sym, []):
            if need <= EPS:
                break
            take = min(lot["left"], need)
            if take <= EPS:
                continue
            lf = lot["f"]
            cost += take * lf["px"]
            if lf["fees"] is None:
                fee_known = False
            else:
                buy_fee += lf["fees"] * take / lf["qty"]
            lot["left"] -= take
            need -= take
            matched += take
            first = first or lf
        if need > EPS:   # a sale the history cannot fully pair: the rest is valued at the order's own average cost when it has one
            basis = f["avg_entry"] if f["avg_entry"] else f["px"]
            cost += need * basis
            matched += need
        gross = f["qty"] * f["px"] - cost
        sell_fee = f["fees"]
        fees = (buy_fee + sell_fee) if fee_known and sell_fee is not None else None
        net = gross - (fees or 0.0)
        trades.append({"kind": "closed", "symbol": sym, "qty": f["qty"], "buy_at": first["at"] if first else None, "buy_px": cost / f["qty"],
                       "sell_at": f["at"], "sell_px": f["px"], "now_px": None, "gross": gross, "fees": fees, "net": net, "pct": net / cost * 100 if cost > EPS else None,
                       "reason": "stop-loss" if f["stop_hit"] else "manual", "stop_type": f["stop_type"] if f["stop_hit"] else None, "live": f["live"],
                       "paired": need <= EPS, "broker_pl": f["broker_pl"], "at": f["at"], "ids": [f["id"]]})
    for sym, ls in lots.items():
        for lot in ls:
            if lot["left"] <= EPS:
                continue
            lf, now = lot["f"], now_px.get(sym)
            cost = lot["left"] * lf["px"]
            gross = lot["left"] * (now - lf["px"]) if now else None
            trades.append({"kind": "open", "symbol": sym, "qty": lot["left"], "buy_at": lf["at"], "buy_px": lf["px"], "sell_at": None, "sell_px": None, "now_px": now,
                           "gross": gross, "fees": None if lf["fees"] is None else lf["fees"] * lot["left"] / lf["qty"], "net": gross,
                           "pct": gross / cost * 100 if gross is not None and cost > EPS else None, "reason": None, "stop_type": None, "live": lf["live"],
                           "paired": True, "broker_pl": None, "at": lf["at"], "ids": [lf["id"]]})
    trades.sort(key=lambda t: _when(t["at"]), reverse=True)
    for t in trades:
        for k in ("qty", "buy_px", "sell_px", "now_px", "gross", "fees", "net", "pct", "broker_pl"):
            if t[k] is not None:
                t[k] = _r(t[k], 4 if k in ("qty",) else 2)
    closed = [t for t in trades if t["kind"] == "closed"]
    opened = [t for t in trades if t["kind"] == "open"]
    all_fees = [f["fees"] for f in fills]
    return {"trades": trades,
            "summary": {"realised": _r(sum(t["net"] for t in closed)), "unrealised": _r(sum(t["net"] for t in opened if t["net"] is not None)),
                        "unrealised_known": all(t["net"] is not None for t in opened), "charges": None if any(x is None for x in all_fees) else _r(sum(all_fees)),
                        "orders": len(fills), "closed": len(closed), "open": len(opened)}}
