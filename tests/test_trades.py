"""trade_view: practice and real fills paired first in, first out into closed and open trades (trading_agent/trades.py). Fakes only."""
from __future__ import annotations

import pytest

from trading_agent.trades import normalise, trade_view


def o(sym, side, qty, px, fees, at, **extra):
    return {"id": f"{sym}-{side}-{at}", "symbol": sym, "side": side, "qty": qty, "filled_avg_price": px, "notional": qty * px, "fees": fees, "status": "filled", "filled_at": at, **extra}


# the owner's practice account, newest first as the server sends it
OWNER = [o("TCS", "buy", 4, 2490.40, 14.52, "2026-10-19T04:17:00+00:00"),
         o("VOGL", "sell", 200, 30.82, 11.40, "2026-10-19T03:57:00+00:00", stop_hit=True, stop_type="trailing", stop_level=30.9, avg_entry_price=30.5, realised_pl=52.6),
         o("KEI", "buy", 5, 4482.30, 58.30, "2026-10-17T08:32:00+00:00"), o("JUBLPHARMA", "buy", 32, 935, 80.12, "2026-10-16T05:45:00+00:00"),
         o("CPPLUS", "buy", 7, 4065, 71.44, "2026-10-15T04:35:00+00:00"), o("VOGL", "buy", 200, 30.50, 9.20, "2026-10-12T04:51:00+00:00"),
         o("ADANIPORTS", "buy", 2, 1761, 7.00, "2026-10-11T08:10:00+00:00")]
POS = [{"symbol": "TCS", "current_price": 2460.9}, {"symbol": "KEI", "current_price": 4520}, {"symbol": "JUBLPHARMA", "current_price": 915},
       {"symbol": "CPPLUS", "current_price": 4100}, {"symbol": "ADANIPORTS", "current_price": 1745}]


def test_the_owners_orders_become_one_closed_trade_and_five_open_ones():
    v = trade_view(OWNER, POS)
    s = v["summary"]
    assert (s["orders"], s["closed"], s["open"]) == (7, 1, 5) and s["charges"] == 251.98
    closed = [t for t in v["trades"] if t["kind"] == "closed"]
    (vogl,) = closed
    assert (vogl["symbol"], vogl["qty"], vogl["buy_px"], vogl["sell_px"]) == ("VOGL", 200, 30.5, 30.82)
    assert vogl["buy_at"] == "2026-10-12T04:51:00+00:00" and vogl["sell_at"] == "2026-10-19T03:57:00+00:00"
    assert vogl["gross"] == 64.0 and vogl["fees"] == 20.6 and vogl["net"] == 43.4 and vogl["pct"] == 0.71          # net of the buy and the sell charge
    assert vogl["reason"] == "stop-loss" and vogl["stop_type"] == "trailing" and vogl["broker_pl"] == 52.6 and vogl["paired"] is True
    assert s["realised"] == 43.4
    by = {t["symbol"]: t for t in v["trades"] if t["kind"] == "open"}
    assert by["TCS"]["gross"] == -118.0 and by["TCS"]["pct"] == -1.18 and by["TCS"]["now_px"] == 2460.9 and by["TCS"]["buy_px"] == 2490.4
    assert by["KEI"]["net"] == 188.5 and by["JUBLPHARMA"]["net"] == -640.0 and by["CPPLUS"]["net"] == 245.0 and by["ADANIPORTS"]["net"] == -32.0
    assert s["unrealised"] == -356.5 and s["unrealised_known"] is True
    assert [t["symbol"] for t in v["trades"]] == ["TCS", "VOGL", "KEI", "JUBLPHARMA", "CPPLUS", "ADANIPORTS"]      # newest event first


def test_a_sale_uses_the_oldest_lots_first_and_leaves_the_rest_open():
    orders = [o("ABC", "buy", 10, 100, 1.0, "2026-10-01T05:00:00+00:00"), o("ABC", "buy", 10, 120, 2.0, "2026-10-02T05:00:00+00:00"), o("ABC", "sell", 15, 130, 3.0, "2026-10-05T05:00:00+00:00")]
    v = trade_view(list(reversed(orders)), [{"symbol": "ABC", "current_price": 140}])
    (c,) = [t for t in v["trades"] if t["kind"] == "closed"]
    # 10 sh from the 100 lot and 5 from the 120 lot: cost 1600, proceeds 1950, charges 1.0 + 1.0 (half of the 2.0) + 3.0
    assert c["buy_px"] == 106.67 and c["gross"] == 350.0 and c["fees"] == 5.0 and c["net"] == 345.0 and c["buy_at"] == "2026-10-01T05:00:00+00:00"
    (rest,) = [t for t in v["trades"] if t["kind"] == "open"]
    assert (rest["qty"], rest["buy_px"], rest["net"], rest["fees"]) == (5, 120, 100.0, 1.0)
    assert v["summary"]["charges"] == 6.0 and v["summary"]["realised"] == 345.0


def test_losses_partial_sales_and_the_input_order_does_not_matter():
    orders = [o("XYZ", "buy", 100, 50, 5.0, "2026-09-01T05:00:00+00:00"), o("XYZ", "sell", 40, 45, 2.0, "2026-09-10T05:00:00+00:00"), o("XYZ", "sell", 60, 55, 3.0, "2026-09-12T05:00:00+00:00")]
    a, b = trade_view(orders), trade_view(list(reversed(orders)))
    assert a == b
    first, second = sorted([t for t in a["trades"] if t["kind"] == "closed"], key=lambda t: t["sell_at"])
    assert first["net"] == -204.0 and first["pct"] == -10.2 and second["net"] == 294.0 and a["summary"]["realised"] == 90.0 and a["summary"]["open"] == 0


def test_same_second_fills_keep_the_server_order():
    same = "2026-10-19T04:00:00+00:00"
    newest_first = [o("ABC", "sell", 5, 12, 1.0, same), o("ABC", "buy", 5, 10, 1.0, same)]       # the sale is listed first = it came last
    v = trade_view(newest_first)
    assert v["summary"]["closed"] == 1 and v["summary"]["open"] == 0 and v["trades"][0]["net"] == 8.0


def test_a_sale_the_history_cannot_pair_uses_the_orders_own_average_cost():
    v = trade_view([o("OLD", "sell", 10, 120, 2.0, "2026-10-19T04:00:00+00:00", avg_entry_price=100.0, realised_pl=198.0)])
    (t,) = v["trades"]
    assert t["paired"] is False and t["buy_at"] is None and t["buy_px"] == 100.0 and t["gross"] == 200.0 and t["net"] == 198.0 and t["fees"] == 2.0
    bare = trade_view([o("OLD", "sell", 10, 120, 2.0, "2026-10-19T04:00:00+00:00")])["trades"][0]
    assert bare["paired"] is False and bare["gross"] == 0.0 and bare["net"] == -2.0


def test_open_trades_without_a_price_have_no_unrealised_figure():
    v = trade_view([o("NOPX", "buy", 3, 100, 1.0, "2026-10-19T04:00:00+00:00")], [])
    (t,) = v["trades"]
    assert t["now_px"] is None and t["net"] is None and t["pct"] is None
    assert v["summary"]["unrealised"] == 0.0 and v["summary"]["unrealised_known"] is False
    assert trade_view([o("NOPX", "buy", 3, 100, 1.0, "2026-10-19T04:00:00+00:00")], [{"symbol": "NOPX", "current_price": None}])["trades"][0]["now_px"] is None


def live(sym, side, filled, qty, avg, at, status="complete", **extra):
    return {"live": True, "symbol": sym, "side": side, "filled_quantity": filled, "qty": qty, "average_fill_price": avg, "placed_at": at, "status": status, "order_status": status, **extra}


def test_real_groww_orders_use_the_same_pairing_with_charges_not_available():
    orders = [live("TCS", "sell", 5, 5, 3600, "2026-10-19T05:00:00+00:00"), live("TCS", "buy", 5, 5, 3500, "2026-10-10T05:00:00+00:00"),
              live("INFY", "buy", 2, 2, 1500, "2026-10-12T05:00:00+00:00"), live("ITC", "buy", 0, 10, None, "2026-10-13T05:00:00+00:00", status="failed"),
              live("SBIN", "buy", 0, 4, 800, "2026-10-14T05:00:00+00:00", status="open")]
    v = trade_view(orders, [{"symbol": "INFY", "current_price": 1450}])
    assert v["summary"]["orders"] == 3 and v["summary"]["charges"] is None and (v["summary"]["closed"], v["summary"]["open"]) == (1, 1)    # failed and unfilled orders are not trades
    (c,) = [t for t in v["trades"] if t["kind"] == "closed"]
    assert c["gross"] == 500.0 and c["fees"] is None and c["net"] == 500.0 and c["pct"] == 2.86 and c["live"] is True and c["reason"] == "manual"
    (op,) = [t for t in v["trades"] if t["kind"] == "open"]
    assert op["symbol"] == "INFY" and op["net"] == -100.0 and op["fees"] is None
    limit_only = live("TCS", "buy", 5, 5, None, "2026-10-10T05:00:00+00:00", limit_price=3400)
    assert normalise(limit_only)["px"] == 3400.0


def test_bad_rows_are_ignored_and_empty_input_is_fine():
    assert trade_view([], []) == {"trades": [], "summary": {"realised": 0.0, "unrealised": 0.0, "unrealised_known": True, "charges": 0.0, "orders": 0, "closed": 0, "open": 0}}
    assert trade_view(None)["summary"]["orders"] == 0
    junk = [{"symbol": "A"}, {"side": "hold", "qty": 1}, o("B", "buy", 0, 10, 0, "x"), o("C", "buy", 1, 0, 0, "x"), o("D", "buy", 1, "n/a", 0, "x"), {**o("E", "buy", 1, 10, 0, "x"), "status": "rejected"},
            o("F", "buy", 1, 10, "bad", "not a date")]
    v = trade_view(junk)
    assert v["summary"]["orders"] == 1 and [t["symbol"] for t in v["trades"]] == ["F"] and v["trades"][0]["fees"] is None
    assert normalise({"side": "buy", "symbol": "z", "qty": "2", "filled_avg_price": "10.5", "fees": "0.1", "filled_at": "2026-10-19T04:00:00+00:00"})["symbol"] == "Z"


@pytest.mark.parametrize("n", [1, 3])
def test_it_never_changes_its_input(n):
    import copy
    orders = copy.deepcopy(OWNER[:n + 3])
    before = copy.deepcopy(orders)
    trade_view(orders, POS)
    assert orders == before
