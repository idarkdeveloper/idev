"""Live-trading safety for Groww: limit pricing, references, confirmation, GTT stops,
token caching, sellable quantity, and refusal of every live path when disabled.

Everything runs against FakeSession; no request ever leaves the machine.
"""

import json
import os
import stat
import sys
from datetime import datetime

import pytest
import requests

from trading_agent.agent import AgentContext, RunResult, build_tools
from trading_agent.broker import LocalPaperBroker, Position
from trading_agent.groww import (IST, GrowwBroker, InstrumentTicks, LiveOrdersDisabled, TokenCache,
                                 cached_access_token, limit_price, make_reference_id, next_token_expiry,
                                 round_to_tick, sellable_quantity, valid_reference_id)
from trading_agent.live import GttStopManager, record_order, refresh_open_orders, sync_gtt_stops
from trading_agent.notify import Notifier
from trading_agent.runner import make_broker, resolve_groww_token
from trading_agent.state import State
from trading_agent.watch import Watcher
from .conftest import FakeSession, Seq


def ok(payload):
    return {"status": "SUCCESS", "payload": payload}


HOLDINGS = ok({"holdings": [
    # 10 held: 5 free in demat + 3 bought yesterday (T1) + 2 pledged -> 8 sellable
    {"trading_symbol": "RELIANCE", "quantity": 10, "average_price": 2500.0, "demat_free_quantity": 5,
     "t1_quantity": 3, "pledge_quantity": 2, "demat_locked_quantity": 0, "groww_locked_quantity": 0},
]})


def routes(extra=None):
    r = {
        ("GET", "/holdings/user"): HOLDINGS,
        ("GET", "/margins/detail/user"): ok({"clear_cash": 50000.0}),
        ("GET", "/live-data/ltp"): ok({"NSE_RELIANCE": 2862.0, "NSE_TCS": 3999.0}),
        ("POST", "/order/create"): ok({"groww_order_id": "GMK1", "order_status": "NEW",
                                      "remark": "Order placed successfully"}),
        ("GET", "/order/status/GMK1"): ok({"groww_order_id": "GMK1", "order_status": "OPEN", "filled_quantity": 0}),
        ("GET", "/order/detail/GMK1"): ok({"groww_order_id": "GMK1", "average_fill_price": 2870.5,
                                          "filled_quantity": 3}),
    }
    r.update(extra or {})
    return r


def live_broker(sess, **kw):
    sleeps = []
    b = GrowwBroker("tok", session=sess, live_orders=True, sleep=sleeps.append, **kw)
    b.sleeps = sleeps
    return b


class RecordingNotifier(Notifier):
    def send(self, subject, body):  # no console noise, no network
        self.sent.append({"subject": subject, "body": body})
        return ["test"]


# --------------------------------------------------------------------------- #
# 1. limit price and tick rounding
# --------------------------------------------------------------------------- #
def test_limit_price_and_tick_rounding():
    assert limit_price(2862.0, "buy", 0.5) == 2876.30   # 2876.31 rounded down to 0.05
    assert limit_price(2862.0, "sell", 0.5) == 2847.70  # 2847.69 rounded up to 0.05
    assert limit_price(2862.0, "buy", 0.5, 0.10) == 2876.30
    assert limit_price(2862.0, "buy", 0.5, 1.0) == 2876.0
    assert limit_price(2862.0, "sell", 0.5, 1.0) == 2848.0
    assert limit_price(10.0, "buy", 1.0, 0.01) == 10.10
    # never more than MAX_SLIPPAGE away from the LTP
    for ltp in (99.99, 346.2, 1234.56, 2862.0):
        assert limit_price(ltp, "buy", 0.5) <= ltp * 1.005 + 1e-9
        assert limit_price(ltp, "sell", 0.5) >= ltp * 0.995 - 1e-9
    assert round_to_tick(100.03, 0.05) == 100.05 and round_to_tick(100.02, 0.05) == 100.0
    assert round_to_tick(100.04, None, "down") == 100.0  # unknown tick -> 0.05
    with pytest.raises(ValueError):
        limit_price(0, "buy", 0.5)


def test_submit_order_sends_rounded_limit_and_uses_tick_size():
    sess = FakeSession(routes())
    b = live_broker(sess, max_slippage_pct=0.5, tick_size_fn=lambda s: 0.10 if s == "RELIANCE" else None)
    order = b.submit_order("reliance", "buy", qty=3)
    body = next(c[2]["json"] for c in sess.calls if c[1].endswith("/order/create"))
    assert body["order_type"] == "LIMIT" and body["validity"] == "DAY"
    assert body["product"] == "CNC" and body["segment"] == "CASH" and body["exchange"] == "NSE"
    assert body["price"] == 2876.3 and order["limit_price"] == 2876.3 and order["tick_size"] == 0.10
    assert live_broker(FakeSession(routes())).tick_size("UNKNOWN") == 0.05  # default when unknown


def test_instrument_ticks_from_groww_csv(tmp_path):
    csv_text = ("exchange,exchange_token,trading_symbol,groww_symbol,name,instrument_type,segment,series,isin,"
                "underlying_symbol,underlying_exchange_token,expiry_date,strike_price,lot_size,tick_size,"
                "freeze_quantity,is_reserved,buy_allowed,sell_allowed\n"
                "NSE,2885,RELIANCE,NSE-RELIANCE,Reliance,EQ,CASH,EQ,INE002A01018,,,,,1,0.10,,0,1,1\n"
                "NSE,49445,BANKNIFTY25DEC27000PE,x,,PE,FNO,,,BANKNIFTY,26009,2025-12-24,27000,35,0.05,601,1,1,1\n")
    sess = FakeSession({("GET", "instrument.csv"): csv_text})
    ticks = InstrumentTicks(tmp_path, session=sess)
    assert ticks.tick_size("reliance") == 0.10 and ticks.tick_size("BANKNIFTY25DEC27000PE") is None
    assert ticks.tick_size("TCS") is None
    assert len(sess.calls) == 1  # downloaded once, then cached
    assert InstrumentTicks(tmp_path, session=FakeSession({})).tick_size("RELIANCE") == 0.10  # from disk


# --------------------------------------------------------------------------- #
# 2. order_reference_id
# --------------------------------------------------------------------------- #
def test_order_reference_id_format():
    refs = {make_reference_id() for _ in range(200)}
    assert len(refs) == 200
    for r in refs:
        assert 8 <= len(r) <= 20 and r.count("-") <= 2 and r.replace("-", "").isalnum()
        assert valid_reference_id(r)
    for bad in ("short", "x" * 21, "a-b-c-d-eeee", "has space1", "-leading12", "trail1234-", "bad_char12"):
        assert not valid_reference_id(bad), bad
    assert valid_reference_id("Ab-654321234-162819")


def test_reference_sent_and_invalid_reference_refused():
    sess = FakeSession(routes())
    order = live_broker(sess).submit_order("RELIANCE", "buy", qty=1, reference_id="MyRef-0001")
    body = next(c[2]["json"] for c in sess.calls if c[1].endswith("/order/create"))
    assert body["order_reference_id"] == "MyRef-0001" == order["order_reference_id"]
    sess = FakeSession(routes())
    with pytest.raises(ValueError):
        live_broker(sess).submit_order("RELIANCE", "buy", qty=1, reference_id="a-b-c-d-1234")
    assert sess.writes() == []


def test_network_retry_reuses_reference_and_does_not_duplicate():
    # The POST times out but actually reached Groww: the lookup by reference finds it.
    sess = FakeSession(routes({
        ("POST", "/order/create"): Seq(requests.Timeout("read timed out"),
                                       ok({"groww_order_id": "DUP", "order_status": "NEW"})),
        ("GET", "/order/status/reference/"): ok({"groww_order_id": "GMK1", "order_status": "OPEN"}),
    }))
    order = live_broker(sess).submit_order("RELIANCE", "buy", qty=1, confirm=False)
    assert order["groww_order_id"] == "GMK1"
    assert len([c for c in sess.calls if c[1].endswith("/order/create")]) == 1
    # Not found by reference -> one retry, with the same reference.
    sess = FakeSession(routes({
        ("POST", "/order/create"): Seq(requests.ConnectionError("reset"), ok({"groww_order_id": "GMK2"})),
        ("GET", "/order/status/reference/"): {"status": "FAILURE", "error": {"code": "GA004", "message": "nf"}},
    }))
    order = live_broker(sess).submit_order("RELIANCE", "buy", qty=1, confirm=False)
    posts = [c[2]["json"] for c in sess.calls if c[1].endswith("/order/create")]
    assert order["groww_order_id"] == "GMK2" and len(posts) == 2
    assert posts[0]["order_reference_id"] == posts[1]["order_reference_id"]


# --------------------------------------------------------------------------- #
# 3. status polling
# --------------------------------------------------------------------------- #
def test_status_polling_executed(tmp_path):
    sess = FakeSession(routes({("GET", "/order/status/GMK1"): Seq(
        ok({"groww_order_id": "GMK1", "order_status": "ACKED", "filled_quantity": 0}),
        ok({"groww_order_id": "GMK1", "order_status": "EXECUTED", "filled_quantity": 3, "remark": "done"}))}))
    b = live_broker(sess)
    order = b.submit_order("RELIANCE", "buy", qty=3)
    assert order["status"] == "filled" and order["order_status"] == "EXECUTED"
    assert order["filled_quantity"] == 3 and order["average_fill_price"] == 2870.5
    assert order["groww_order_id"] == "GMK1" and order["remark"] == "done"
    assert b.sleeps == [0.5, 1.0]  # stopped polling once executed
    st, n = State(tmp_path / "state.json"), RecordingNotifier()
    record_order(st, order, n)
    assert st.data["live_orders"][0]["status"] == "filled" and n.sent == []


def test_status_polling_rejected_reaches_notifier(tmp_path):
    sess = FakeSession(routes({("GET", "/order/status/GMK1"): ok(
        {"groww_order_id": "GMK1", "order_status": "REJECTED", "filled_quantity": 0,
         "remark": "Insufficient funds"})}))
    order = live_broker(sess).submit_order("RELIANCE", "buy", qty=3)
    assert order["status"] == "failed" and order["remark"] == "Insufficient funds"
    st, n = State(tmp_path / "state.json"), RecordingNotifier()
    record_order(st, order, n)
    record_order(st, order, n)  # same order recorded twice: one alert, one row
    assert len(n.sent) == 1 and "REJECTED" in n.sent[0]["subject"] and "Insufficient funds" in n.sent[0]["body"]
    assert len(st.data["live_orders"]) == 1
    for status in ("FAILED", "CANCELLED"):
        sess = FakeSession(routes({("GET", "/order/status/GMK1"): ok({"order_status": status})}))
        assert live_broker(sess).submit_order("RELIANCE", "buy", qty=1)["status"] == "failed"


def test_status_polling_still_open_then_refresh(tmp_path):
    sess = FakeSession(routes())  # always OPEN
    b = live_broker(sess)
    order = b.submit_order("RELIANCE", "sell", qty=2)
    assert order["status"] == "open" and order["order_status"] == "OPEN"
    assert len([c for c in sess.calls if "/order/status/GMK1" in c[1]]) == 4 and b.sleeps == [0.5, 1.0, 2.0, 3.0]
    st, n = State(tmp_path / "state.json"), RecordingNotifier()
    record_order(st, order, n)
    assert n.sent == []
    # later: `orders --refresh` finds it executed
    sess.routes[("GET", "/order/status/GMK1")] = ok({"groww_order_id": "GMK1", "order_status": "EXECUTED",
                                                    "filled_quantity": 2})
    updated = refresh_open_orders(b, st, n)
    assert updated[0]["status"] == "filled" and st.data["live_orders"][0]["average_fill_price"] == 2870.5
    assert refresh_open_orders(b, st, n) == []  # nothing open any more


def test_cli_orders_refresh(tmp_path, monkeypatch, capsys):
    from trading_agent import cli, runner
    _isolated_env(tmp_path, monkeypatch, GROWW_ACCESS_TOKEN="tok")
    st = State(tmp_path / "state" / "state.json")
    record_order(st, {"groww_order_id": "GMK1", "symbol": "RELIANCE", "side": "buy", "qty": 3,
                      "status": "open", "order_status": "OPEN", "placed_at": "2026-10-09T10:00:00+05:30"})
    st.save()
    sess = FakeSession(routes({("GET", "/order/status/GMK1"): ok({"order_status": "EXECUTED",
                                                                     "filled_quantity": 3})}))
    monkeypatch.setattr(runner, "make_groww", lambda settings, price_fn=None: GrowwBroker(
        "tok", session=sess, sleep=lambda s: None))
    assert cli.main(["orders", "--refresh"]) == 0
    out = capsys.readouterr().out
    assert "Re-checked 1 open order" in out and "EXECUTED" in out
    assert sess.writes() == []  # refreshing only reads
    assert State(tmp_path / "state" / "state.json").data["live_orders"][0]["status"] == "filled"


# --------------------------------------------------------------------------- #
# 4. GTT stop-losses
# --------------------------------------------------------------------------- #
GTT_ROUTES = {
    ("POST", "/order-advance/create"): ok({"smart_order_id": "gtt_91a7f4", "smart_order_type": "GTT",
                                          "status": "ACTIVE"}),
    ("PUT", "/order-advance/modify/gtt_91a7f4"): ok({"smart_order_id": "gtt_91a7f4", "status": "ACTIVE"}),
    ("POST", "/order-advance/cancel/CASH/GTT/gtt_91a7f4"): ok({"smart_order_id": "gtt_91a7f4",
                                                              "status": "CANCELLED"}),
}


def pos(price, qty=10, free=10):
    return Position("TCS", qty, 900.0, current_price=price, sellable_qty=free)


def test_gtt_create_modify_up_only_and_cancel_on_sell(tmp_path):
    sess = FakeSession(dict(GTT_ROUTES))
    st = State(tmp_path / "state.json")
    mgr = GttStopManager(live_broker(sess, max_slippage_pct=0.5), st)

    # create: stop 15% below the high (no ATR data) = 850; limit 0.5% below = 845.75
    acts = mgr.sync([pos(1000.0)])
    assert acts == [{"symbol": "TCS", "action": "create", "trigger": 850.0, "limit": 845.75, "qty": 10}]
    body = sess.calls[-1][2]["json"]
    assert sess.calls[-1][1].endswith("/order-advance/create")
    assert body["smart_order_type"] == "GTT" and body["trigger_direction"] == "DOWN"
    assert body["trigger_price"] == "850.00" and body["order"] == {"order_type": "LIMIT", "price": "845.75",
                                                                  "transaction_type": "SELL"}
    assert body["product_type"] == "CNC" and body["segment"] == "CASH" and body["quantity"] == 10
    assert valid_reference_id(body["reference_id"])
    assert st.data["gtt_stops"]["TCS"]["smart_order_id"] == "gtt_91a7f4"

    # price rises -> stop moves up -> modify
    acts = mgr.sync([pos(1200.0)])
    assert acts[0]["action"] == "modify" and acts[0]["trigger"] == 1020.0 and acts[0]["from"] == 850.0
    method, url, kw = sess.calls[-1]
    assert method == "PUT" and url.endswith("/order-advance/modify/gtt_91a7f4")
    assert kw["json"]["trigger_price"] == "1020.00" and kw["json"]["order"]["price"] == "1014.90"

    # price falls -> the stop is never moved down: no request at all
    n = len(sess.calls)
    assert mgr.sync([pos(1100.0)]) == [] and mgr.sync([pos(1030.0)]) == []
    assert len(sess.calls) == n and st.data["gtt_stops"]["TCS"]["trigger"] == 1020.0

    # partial sell -> quantity follows, trigger stays
    acts = mgr.sync([pos(1100.0, qty=6, free=6)])
    assert acts[0]["action"] == "modify" and acts[0]["trigger"] == 1020.0 and acts[0]["qty"] == 6

    # holding sold -> cancel, removed from state
    acts = mgr.sync([])
    assert acts == [{"symbol": "TCS", "action": "cancel", "status": "CANCELLED", "reason": "holding sold"}]
    assert sess.calls[-1][1].endswith("/order-advance/cancel/CASH/GTT/gtt_91a7f4")
    assert "TCS" not in st.data["gtt_stops"] and st.data["gtt_history"][-1]["status"] == "CANCELLED"


def test_gtt_skips_breached_stop_and_pledged_holding(tmp_path):
    sess = FakeSession(dict(GTT_ROUTES))
    mgr = GttStopManager(live_broker(sess), State(tmp_path / "s.json"))
    # high-water 1000 (from avg cost) but price 800 is already below the 850 stop
    p = Position("TCS", 10, 1000.0, current_price=800.0, sellable_qty=10)
    assert mgr.sync([p])[0]["action"] == "skip"
    assert mgr.sync([pos(1000.0, qty=10, free=0)]) == []  # all pledged: nothing to protect
    assert sess.writes() == []


def test_agent_live_sell_cancels_gtt(settings, tmp_path):
    settings.auto_trade = True
    settings.groww_live_orders = True
    settings.groww_gtt_stops = True
    sess = FakeSession(routes({**GTT_ROUTES,
        ("GET", "/order/status/GMK1"): ok({"order_status": "EXECUTED", "filled_quantity": 8}),
    }))
    b = live_broker(sess)
    st = State(settings.state_dir / "state.json")
    st.data["gtt_stops"] = {"RELIANCE": {"smart_order_id": "gtt_91a7f4", "trigger": 2400.0, "limit": 2388.0,
                                         "qty": 8, "status": "ACTIVE"}}
    # holdings reads: account(), the free-share check, then the GTT sync after the fill,
    # by which time Groww reports no RELIANCE holding any more
    sess.routes[("GET", "/holdings/user")] = Seq(HOLDINGS, HOLDINGS, ok({"holdings": []}))
    n = RecordingNotifier()
    ctx = AgentContext(settings=settings, broker=b, data=None, notifier=n, state=st,
                       result=RunResult(investor="x", new_trades=[]))
    tool = next(t for t in build_tools(ctx) if t.name == "place_paper_order")
    out = json.loads(tool.call({"symbol": "RELIANCE", "side": "sell", "notional_usd": 2862.0 * 1}))
    assert out["ok"] and out["order"]["status"] == "filled"
    assert st.data["live_orders"][0]["source"] == "agent"
    assert any(c[1].endswith("/order-advance/cancel/CASH/GTT/gtt_91a7f4") for c in sess.calls)
    assert "RELIANCE" not in st.data["gtt_stops"]


def test_agent_live_rejection_is_notified(settings):
    settings.auto_trade = True
    sess = FakeSession(routes({("GET", "/order/status/GMK1"): ok({"order_status": "REJECTED",
                                                                     "remark": "price band"})}))
    st, n = State(settings.state_dir / "state.json"), RecordingNotifier()
    ctx = AgentContext(settings=settings, broker=live_broker(sess), data=None, notifier=n, state=st,
                       result=RunResult(investor="x", new_trades=[]))
    tool = next(t for t in build_tools(ctx) if t.name == "place_paper_order")
    out = json.loads(tool.call({"symbol": "RELIANCE", "side": "buy", "notional_usd": 5000}))
    assert out["ok"] is False and out["order"]["status"] == "failed"
    assert len(n.sent) == 1 and "price band" in n.sent[0]["body"]
    # an API error at placement is recorded and notified too
    sess.routes[("POST", "/order/create")] = {"status": "FAILURE", "error": {"code": "GA001", "message": "boom"}}
    out = json.loads(tool.call({"symbol": "RELIANCE", "side": "buy", "notional_usd": 5000}))
    assert "GA001" in out["error"] and len(n.sent) == 2 and st.data["live_orders"][-1]["status"] == "failed"


# --------------------------------------------------------------------------- #
# 5. token cache
# --------------------------------------------------------------------------- #
def test_next_token_expiry_is_6am_ist():
    assert next_token_expiry(datetime(2026, 10, 9, 10, 0, tzinfo=IST)) == datetime(2026, 10, 10, 6, 0, tzinfo=IST)
    assert next_token_expiry(datetime(2026, 10, 9, 5, 59, tzinfo=IST)) == datetime(2026, 10, 9, 6, 0, tzinfo=IST)
    assert next_token_expiry(datetime(2026, 10, 9, 6, 0, tzinfo=IST)) == datetime(2026, 10, 10, 6, 0, tzinfo=IST)


def test_token_cache_reuse_and_expiry_at_6am(tmp_path):
    sess = FakeSession({("POST", "/token/api/access"): Seq({"token": "T1"}, {"token": "T2"}, {"token": "T3"})})
    cache = TokenCache(tmp_path / "groww_token.json")
    tok = lambda now: cached_access_token("APIKEY", cache, secret="s3cret", now=now, session=sess)
    assert tok(datetime(2026, 10, 9, 10, 0, tzinfo=IST)) == "T1"
    assert tok(datetime(2026, 10, 9, 15, 0, tzinfo=IST)) == "T1"     # reused, no new generation
    assert tok(datetime(2026, 10, 10, 5, 59, tzinfo=IST)) == "T1"    # still valid before 06:00
    assert len(sess.calls) == 1
    assert tok(datetime(2026, 10, 10, 6, 0, tzinfo=IST)) == "T2"     # expired at 06:00 IST
    assert len(sess.calls) == 2
    text = (tmp_path / "groww_token.json").read_text()
    assert "APIKEY" not in text and "s3cret" not in text           # only a key fingerprint
    assert json.loads(text)["expires_at"] == "2026-10-11T06:00:00+05:30"
    # another API key never reuses this token
    assert cached_access_token("OTHERKEY", cache, secret="x", now=datetime(2026, 10, 10, 7, 0, tzinfo=IST),
                               session=sess) == "T3"
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(tmp_path / "groww_token.json").st_mode) == 0o600


def test_token_cache_respects_earlier_reported_expiry(tmp_path):
    sess = FakeSession({("POST", "/token/api/access"): Seq({"token": "T1", "expiry": "2026-10-09T12:00:00+05:30"},
                                                           {"token": "T2"})})
    cache = TokenCache(tmp_path / "t.json")
    tok = lambda now: cached_access_token("K", cache, secret="s", now=now, session=sess)
    assert tok(datetime(2026, 10, 9, 10, 0, tzinfo=IST)) == "T1"
    assert tok(datetime(2026, 10, 9, 12, 1, tzinfo=IST)) == "T2"


def test_resolve_token_env_wins_and_cache_reused(settings):
    settings.groww_api_key, settings.groww_api_secret = "APIKEY", "s3cret"
    sess = FakeSession({("POST", "/token/api/access"): Seq({"token": "GEN1"}, {"token": "GEN2"})})
    assert resolve_groww_token(settings, session=sess) == "GEN1"
    assert resolve_groww_token(settings, session=sess) == "GEN1" and len(sess.calls) == 1
    assert (settings.state_dir / "groww_token.json").exists()
    settings.groww_access_token = "FROM_ENV"
    assert resolve_groww_token(settings, session=sess) == "FROM_ENV" and len(sess.calls) == 1
    settings.groww_access_token = None
    assert resolve_groww_token(settings, session=sess, fresh=True) == "GEN2"


# --------------------------------------------------------------------------- #
# 6. sellable quantity
# --------------------------------------------------------------------------- #
def test_sellable_quantity_excludes_pledged_and_locked():
    assert sellable_quantity({"quantity": 10, "demat_free_quantity": 5, "t1_quantity": 3, "pledge_quantity": 2}) == 8
    assert sellable_quantity({"quantity": 10}) == 0  # fields missing: sell nothing rather than guess
    assert sellable_quantity({"quantity": 4, "demat_free_quantity": 5, "t1_quantity": 3}) == 4  # capped
    b = GrowwBroker("tok", session=FakeSession(routes()))
    p = b.positions()[0]
    assert p.qty == 10 and p.sellable_qty == 8 and p.to_dict()["sellable_qty"] == 8
    assert b.sellable_qty("reliance") == 8 and b.sellable_qty("TCS") == 0
    assert Position("X", 5, 1.0).to_dict()["sellable_qty"] == 5  # paper: everything is free


def test_live_sell_limited_to_free_shares():
    sess = FakeSession(routes())
    b = live_broker(sess)
    with pytest.raises(ValueError, match="only 8 free shares"):
        b.submit_order("RELIANCE", "sell", qty=9)
    assert sess.writes() == []
    order = b.submit_order("RELIANCE", "sell", qty=8)
    body = next(c[2]["json"] for c in sess.calls if c[1].endswith("/order/create"))
    assert body["transaction_type"] == "SELL" and body["quantity"] == 8 and body["price"] == 2847.7
    assert order["side"] == "sell"


# --------------------------------------------------------------------------- #
# 7. every live path refuses when GROWW_LIVE_ORDERS is false
# --------------------------------------------------------------------------- #
def test_every_live_call_refuses_when_live_orders_off():
    sess = FakeSession({**routes(), **GTT_ROUTES})
    b = GrowwBroker("tok", session=sess, live_orders=False)
    calls = [
        lambda: b.submit_order("RELIANCE", "buy", notional=10000),
        lambda: b.submit_order("RELIANCE", "sell", qty=1),
        lambda: b.submit_order("RELIANCE", "buy", qty=1, order_type="MARKET"),
        lambda: b.cancel_order("GMK1"),
        lambda: b.create_gtt_stop("TCS", 1, 850.0, 845.0),
        lambda: b.modify_gtt_stop("gtt_91a7f4", 1, 900.0, 895.0),
        lambda: b.cancel_gtt("gtt_91a7f4"),
    ]
    for c in calls:
        with pytest.raises(LiveOrdersDisabled):
            c()
    assert sess.calls == []  # refused before any request, not even a price lookup


def test_gtt_manager_and_sync_refuse_without_live(settings, tmp_path):
    sess = FakeSession({**routes(), **GTT_ROUTES})
    st = State(tmp_path / "s.json")
    paper = GrowwBroker("tok", session=sess, live_orders=False)
    with pytest.raises(PermissionError):
        GttStopManager(paper, st).sync([pos(1000.0)])
    with pytest.raises(PermissionError):
        GttStopManager(live_broker(sess), st, enabled=False).sync([pos(1000.0)])
    settings.groww_gtt_stops = True
    assert sync_gtt_stops(settings, paper, st) is None
    assert sync_gtt_stops(settings, LocalPaperBroker(tmp_path / "pb.json"), st) is None
    settings.groww_gtt_stops = False
    assert sync_gtt_stops(settings, live_broker(sess), st) is None  # live, but GTT toggle off
    assert sess.writes() == []


def test_watch_does_no_live_work_on_paper(settings, tmp_path):
    w = Watcher(settings, broker=LocalPaperBroker(tmp_path / "pb.json"))
    assert w.sync_live() is None
    sess = FakeSession({**routes(), **GTT_ROUTES})
    assert Watcher(settings, broker=GrowwBroker("tok", session=sess, live_orders=False)).sync_live() is None
    assert sess.calls == []


def test_make_broker_stays_paper_and_agent_has_no_order_tool(settings, monkeypatch):
    settings.broker, settings.market, settings.groww_access_token = "groww", "in", "tok"
    from trading_agent import groww
    monkeypatch.setattr(groww.requests, "Session", lambda: FakeSession(routes()))
    b = make_broker(settings, price_fn=lambda s: 100.0)
    assert isinstance(b, LocalPaperBroker)  # GROWW_LIVE_ORDERS=false -> simulated fills only
    st = State(settings.state_dir / "state.json")
    ctx = AgentContext(settings=settings, broker=live_broker(FakeSession(routes())), data=None,
                       notifier=RecordingNotifier(), state=st, result=RunResult(investor="x", new_trades=[]))
    assert "place_paper_order" not in [t.name for t in build_tools(ctx)]  # AUTO_TRADE=false


def _isolated_env(tmp_path, monkeypatch, **env):
    monkeypatch.chdir(tmp_path)  # no real .env is read
    for k in ("GROWW_ACCESS_TOKEN", "GROWW_API_KEY", "GROWW_API_SECRET", "GROWW_TOTP_SECRET",
              "GROWW_LIVE_ORDERS", "GROWW_GTT_STOPS", "AUTO_TRADE", "BROKER", "RESEND_API_KEY",
              "NOTIFY_WEBHOOK_URL", "MARKET"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("STATE_DIR", str(tmp_path / "state"))
    for k, v in env.items():
        monkeypatch.setenv(k, v)


def test_cli_gtt_sync_refuses_without_live(tmp_path, monkeypatch, capsys):
    from trading_agent import cli
    _isolated_env(tmp_path, monkeypatch, GROWW_ACCESS_TOKEN="tok", GROWW_GTT_STOPS="true")
    assert cli.main(["gtt", "--sync"]) == 1
    assert "GROWW_LIVE_ORDERS is not true" in capsys.readouterr().out


def test_config_reads_slippage_and_gtt_toggle(tmp_path, monkeypatch):
    from trading_agent.config import load_settings
    _isolated_env(tmp_path, monkeypatch)
    s = load_settings()
    assert s.max_slippage_pct == 0.5 and s.groww_gtt_stops is False and s.groww_live_orders is False
    monkeypatch.setenv("MAX_SLIPPAGE_PCT", "0.25")
    monkeypatch.setenv("GROWW_GTT_STOPS", "true")
    s = load_settings()
    assert s.max_slippage_pct == 0.25 and s.groww_gtt_stops is True
    monkeypatch.setenv("MAX_SLIPPAGE_PCT", "12")
    with pytest.raises(SystemExit):
        load_settings()


# --------------------------------------------------------------------------- #
# 8. groww-check: verification you run yourself
# --------------------------------------------------------------------------- #
CHECK_ROUTES = {
    ("GET", "/order/list"): ok({"order_list": []}),
    ("GET", "/order-advance/list"): ok({"data": []}),
}


def test_groww_check_read_only_makes_no_writes(tmp_path):
    from trading_agent.groww_check import format_rows, read_only_checks
    sess = FakeSession({**routes(), **CHECK_ROUTES})
    c = read_only_checks(GrowwBroker("tok", session=sess), token_source="cached token",
                         tick_fn=lambda s: 0.10)
    text = format_rows(c)
    assert "[PASS] holdings" in text and "[PASS] sellable fields" in text and "sellable 8" in text
    assert "[PASS] tick RELIANCE: tick size 0.1" in text and "[PASS] GTT list" in text
    assert sess.writes() == []


def test_groww_check_flags_missing_sellable_fields():
    from trading_agent.groww_check import read_only_checks
    sess = FakeSession({**routes({("GET", "/holdings/user"): ok({"holdings": [
        {"trading_symbol": "TCS", "quantity": 3, "average_price": 1.0}]})}), **CHECK_ROUTES})
    c = read_only_checks(GrowwBroker("tok", session=sess), token_source="env")
    row = next(r for r in c.rows if r["check"] == "sellable fields")
    assert row["ok"] is False and "demat_free_quantity" in row["detail"]


def test_groww_check_live_test_refuses_unless_live():
    from trading_agent.groww_check import live_test
    sess = FakeSession({**routes(), **GTT_ROUTES})
    with pytest.raises(LiveOrdersDisabled):
        live_test(GrowwBroker("tok", session=sess, live_orders=False), "RELIANCE")
    assert sess.calls == []


def test_groww_check_live_test_places_and_cleans_up():
    from trading_agent.groww_check import live_test
    sess = FakeSession({**routes({
        ("GET", "/order/status/reference/"): ok({"groww_order_id": "GMK1", "order_status": "OPEN"}),
        ("GET", "/order/status/GMK1"): Seq(*[ok({"groww_order_id": "GMK1", "order_status": "OPEN"})] * 3,
                                           ok({"groww_order_id": "GMK1", "order_status": "CANCELLED"})),
        ("POST", "/order/cancel"): ok({"groww_order_id": "GMK1", "order_status": "CANCELLATION_REQUESTED"}),
        ("GET", "/order-advance/status/CASH/GTT/internal/gtt_91a7f4"): ok(
            {"smart_order_id": "gtt_91a7f4", "status": "ACTIVE", "trigger_price": "2318.20"}),
    }), **GTT_ROUTES})
    c = live_test(live_broker(sess), "RELIANCE", offset_pct=3)
    res = {r["check"]: r for r in c.rows}
    order = next(c_[2]["json"] for c_ in sess.calls if c_[1].endswith("/order/create"))
    assert order["quantity"] == 1 and order["order_type"] == "LIMIT" and order["price"] == 2776.10  # 3% below
    for name in ("live: place limit order", "live: status by reference", "live: order detail fields",
                 "live: cancel order", "live: GTT create", "live: GTT modify", "live: GTT cancel"):
        assert res[name]["ok"] is True, (name, res[name])
    gtt = next(c_[2]["json"] for c_ in sess.calls if c_[1].endswith("/order-advance/create"))
    assert gtt["quantity"] == 1 and gtt["trigger_price"] == "2289.60"  # 20% below the price
    assert any(c_[1].endswith("/order/cancel") for c_ in sess.calls)
    assert any(c_[1].endswith("/order-advance/cancel/CASH/GTT/gtt_91a7f4") for c_ in sess.calls)


def test_cli_groww_check_refuses_live_test_without_both_switches(tmp_path, monkeypatch, capsys):
    from trading_agent import cli
    _isolated_env(tmp_path, monkeypatch, GROWW_ACCESS_TOKEN="tok", GROWW_LIVE_ORDERS="true")
    assert cli.main(["groww-check", "--live-test", "RELIANCE"]) == 1  # missing the acknowledgement flag
    assert "REAL 1-share" in capsys.readouterr().out
    monkeypatch.setenv("GROWW_LIVE_ORDERS", "false")
    assert cli.main(["groww-check", "--live-test", "RELIANCE", "--i-understand-real-orders"]) == 1


def test_cli_baseline_marks_seen_without_claude(tmp_path, monkeypatch, capsys):
    from trading_agent import cli
    _isolated_env(tmp_path, monkeypatch, MARKET="in")
    assert cli.main(["check", "--demo", "--baseline"]) == 0
    st = State(tmp_path / "state" / "state.json")
    assert len(st.data["seen"]) == 3 and st.data["runs"][-1]["baseline"] is True
    assert "baseline" in capsys.readouterr().out
    assert cli.main(["check", "--demo", "--dry-run"]) == 0
    assert "No new disclosed trades" in capsys.readouterr().out


def test_gtt_skips_holdings_that_are_not_exchange_traded(tmp_path):
    sess = FakeSession(dict(GTT_ROUTES))
    b = live_broker(sess, tick_size_fn=lambda s: None if s == "NSE" else 0.05)  # unlisted NSE Ltd shares
    mgr = GttStopManager(b, State(tmp_path / "s.json"))
    acts = mgr.sync([Position("NSE", 8, 1000.0, current_price=1100.0, sellable_qty=8), pos(1000.0)])
    assert {"symbol": "NSE", "action": "skip", "reason": "not exchange-traded"} in acts
    assert [a["symbol"] for a in acts if a["action"] == "create"] == ["TCS"]
    assert GrowwBroker("tok", session=sess).is_listed("ANY")  # no instrument list: don't block


def test_anthropic_client_sends_workspace_header(settings):
    from trading_agent.agent import make_client
    assert "anthropic-workspace-id" not in make_client(settings).default_headers
    settings.anthropic_workspace_id = "wrkspc_test"
    assert make_client(settings).default_headers["anthropic-workspace-id"] == "wrkspc_test"


def test_cli_holdings_prints_buy_current_and_pl(tmp_path, monkeypatch, capsys):
    from trading_agent import cli, groww
    _isolated_env(tmp_path, monkeypatch, GROWW_ACCESS_TOKEN="tok")
    monkeypatch.setattr(groww.requests, "Session", lambda: FakeSession(routes()))
    assert cli.main(["holdings"]) == 0
    out = capsys.readouterr().out
    # RELIANCE: 10 @ 2,500 bought, 2,862 now -> +3,620 (+14.48%)
    assert "RELIANCE" in out and "2,500.00" in out and "2,862.00" in out and "+3,620" in out and "+14.48%" in out
    _isolated_env(tmp_path, monkeypatch)
    assert cli.main(["holdings"]) == 1
