"""Live-order readiness: DDPI / e-DIS handling, free-only sells (GROWW_SELL_T1), and the
groww-check --live-test order list + modify drill. Fake Groww only; nothing leaves the machine."""

import json

import pytest

from trading_agent.broker import Position
from trading_agent.groww import (AUTH_MESSAGE, GrowwAuthorisationError, GrowwBroker, LiveOrdersDisabled,
                                 is_authorisation_problem, sellable_quantity)
from trading_agent.groww_check import DDPI_MANUAL_CHECK, format_rows, live_test, read_only_checks
from trading_agent.live import GttStopManager
from trading_agent.live_alerts import alert_once
from trading_agent.state import State
from .conftest import FakeSession, Seq
from .test_groww_live import CHECK_ROUTES, GTT_ROUTES, ok, routes

T1_ONLY = ok({"holdings": [
    {"trading_symbol": "TCS", "quantity": 4, "average_price": 3000.0, "demat_free_quantity": 0,
     "t1_quantity": 4, "pledge_quantity": 0, "demat_locked_quantity": 0, "groww_locked_quantity": 0}]})


def live(sess, **kw):
    alerts = []
    b = GrowwBroker("tok", session=sess, live_orders=True, sleep=lambda s: None,
                    alert_fn=lambda k, s, body: alerts.append((k, s, body)), **kw)
    b.alerts = alerts
    return b


# ---- 1. DDPI / e-DIS ---------------------------------------------------------
def test_authorisation_text_detection():
    for t in ("TPIN required", "e-DIS authorisation failed", "edis pending", "DDPI not enabled",
              "CDSL rejected", "Authorization required", "authorisation pending"):
        assert is_authorisation_problem(t), t
    for t in ("Insufficient quantity", "", None, "price out of range"):
        assert not is_authorisation_problem(t), t


def test_groww_check_prints_manual_ddpi_line_and_state():
    sess = FakeSession({**routes(), **CHECK_ROUTES})
    for confirmed, word in ((False, "NOT yet confirmed"), (True, "you have confirmed")):
        text = format_rows(read_only_checks(GrowwBroker("tok", session=sess), token_source="env",
                                            ddpi_confirmed=confirmed))
        assert "DDPI (manual check)" in text and "Groww app" in text and "automated sells are rejected" in text
        assert word in text
    assert "no DDPI status call" in DDPI_MANUAL_CHECK


def test_sell_without_ddpi_confirmed_still_goes_but_alerts():
    sess = FakeSession(routes())
    b = live(sess)  # ddpi_confirmed defaults to False
    order = b.submit_order("RELIANCE", "sell", qty=2)
    assert order["side"] == "sell" and any(c[1].endswith("/order/create") for c in sess.calls)
    assert [a[0] for a in b.alerts] == ["ddpi-unconfirmed"]
    assert "DDPI not confirmed: sells may be rejected" in b.alerts[0][2]


def test_no_ddpi_alert_when_confirmed_or_for_buys():
    b = live(FakeSession(routes()), ddpi_confirmed=True)
    b.submit_order("RELIANCE", "sell", qty=2)
    assert b.alerts == []
    b2 = live(FakeSession(routes()))
    b2.submit_order("RELIANCE", "buy", qty=1)
    assert b2.alerts == []


def test_gtt_create_without_ddpi_alerts_and_rejection_raises():
    b = live(FakeSession({**GTT_ROUTES}))
    b.create_gtt_stop("TCS", 1, 100.0, 99.0)
    assert [a[0] for a in b.alerts] == ["ddpi-unconfirmed"]
    bad = {("POST", "/order-advance/create"): {"status": "FAILURE",
                                               "error": {"code": "GA001", "message": "e-DIS authorisation required"}}}
    b = live(FakeSession(bad), ddpi_confirmed=True)
    with pytest.raises(GrowwAuthorisationError, match="demat authorisation"):
        b.create_gtt_stop("TCS", 1, 100.0, 99.0)
    assert [a[0] for a in b.alerts] == ["auth-TCS"]
    assert AUTH_MESSAGE in b.alerts[0][2]


def test_sell_rejected_for_missing_authorisation_error_response():
    bad = {("POST", "/order/create"): {"status": "FAILURE",
                                        "error": {"code": "GA999", "message": "CDSL TPIN authorisation pending"}}}
    b = live(FakeSession(routes(bad)), ddpi_confirmed=True)
    with pytest.raises(GrowwAuthorisationError) as e:
        b.submit_order("RELIANCE", "sell", qty=1)
    assert "DDPI/e-DIS" in str(e.value) and "Enable DDPI in the Groww app" in str(e.value)
    assert [a[0] for a in b.alerts] == ["auth-RELIANCE"]


def test_sell_rejected_after_placement_is_flagged_from_status_remark():
    r = routes({("GET", "/order/status/GMK1"): ok({"groww_order_id": "GMK1", "order_status": "REJECTED",
                                                   "remark": "e-DIS TPIN not authorised"})})
    b = live(FakeSession(r), ddpi_confirmed=True)
    order = b.submit_order("RELIANCE", "sell", qty=1)
    assert order["status"] == "failed" and order["authorisation_error"] is True
    assert order["remark"] == AUTH_MESSAGE
    assert [a[0] for a in b.alerts] == ["auth-RELIANCE"]


def test_http_error_text_also_detected_for_writes_only():
    class Resp:
        content = b"x"
        text = "TPIN needed"
        status_code = 403

        def json(self):
            return {}

        def raise_for_status(self):
            import requests
            raise requests.HTTPError("403 TPIN needed")

    class Sess:
        def request(self, *a, **k):
            return Resp()

    b = live(Sess(), ddpi_confirmed=True)
    with pytest.raises(GrowwAuthorisationError):
        b._req("POST", "order/create", json={})
    import requests
    with pytest.raises(requests.HTTPError):  # a read keeps its normal error
        b._req("GET", "holdings/user")


def test_alert_once_per_day_per_key(tmp_path):
    from datetime import datetime
    from trading_agent.timezones import IST
    sent = []
    p = tmp_path / "a.json"
    d1 = datetime(2026, 10, 12, 10, 0, tzinfo=IST)
    d2 = datetime(2026, 10, 13, 10, 0, tzinfo=IST)
    send = lambda s, b: sent.append(s)
    assert alert_once(p, "auth-TCS", send, "s", "b", now=d1) is True
    assert alert_once(p, "auth-TCS", send, "s", "b", now=d1) is False
    assert alert_once(p, "auth-INFY", send, "s2", "b", now=d1) is True  # per symbol
    assert alert_once(p, "auth-TCS", send, "s", "b", now=d2) is True  # next day
    assert len(sent) == 3 and json.loads(p.read_text()) == {"auth-TCS": "2026-10-13"}


# ---- 2. free-only sells ------------------------------------------------------
def test_live_sell_free_only_by_default_and_t1_when_enabled():
    sess = FakeSession(routes())  # RELIANCE: 5 free + 3 T1
    b = live(sess, ddpi_confirmed=True)
    assert b.sellable_qty("RELIANCE") == 5
    assert b.positions()[0].sellable_qty == 5
    with pytest.raises(ValueError, match="free shares"):
        b.submit_order("RELIANCE", "sell", qty=6)
    assert sess.writes() == []
    sess2 = FakeSession(routes())
    b2 = live(sess2, ddpi_confirmed=True, sell_t1=True)
    assert b2.sellable_qty("RELIANCE") == 8
    b2.submit_order("RELIANCE", "sell", qty=8)
    assert any(c[1].endswith("/order/create") for c in sess2.calls)


def test_paper_and_readonly_brokers_still_count_t1():
    b = GrowwBroker("tok", session=FakeSession(routes()))  # live_orders False
    assert b.sellable_qty("RELIANCE") == 8 and b.positions()[0].sellable_qty == 8
    assert sellable_quantity({"quantity": 10, "demat_free_quantity": 5, "t1_quantity": 3}) == 5


def test_t1_only_holding_is_not_sold_and_alerts():
    sess = FakeSession(routes({("GET", "/holdings/user"): T1_ONLY, ("GET", "/live-data/ltp"): ok({"NSE_TCS": 3999.0})}))
    b = live(sess, ddpi_confirmed=True)
    with pytest.raises(ValueError, match="only T1 shares; not sold until they settle"):
        b.submit_order("TCS", "sell", qty=2)
    assert sess.writes() == []
    assert [a[0] for a in b.alerts] == ["t1-TCS"]
    assert "only T1 shares; not sold until they settle" in b.alerts[0][2]


def test_gtt_manager_alerts_for_t1_only_and_does_not_create(tmp_path):
    sess = FakeSession(routes({("GET", "/holdings/user"): T1_ONLY, ("GET", "/live-data/ltp"): ok({"NSE_TCS": 3999.0}),
                               **GTT_ROUTES}))
    b = live(sess, ddpi_confirmed=True)
    acts = GttStopManager(b, State(tmp_path / "s.json")).sync()
    assert acts == [] and not [c for c in sess.calls if c[1].endswith("/order-advance/create")]
    assert [a[0] for a in b.alerts] == ["t1-TCS"]


def test_settings_load_and_save_new_switches(monkeypatch):
    from trading_agent.config import load_settings
    from trading_agent.ui import EDITABLE_ENV_KEYS as ENV_KEYS
    monkeypatch.delenv("GROWW_DDPI_CONFIRMED", raising=False)
    monkeypatch.delenv("GROWW_SELL_T1", raising=False)
    s = load_settings()
    assert s.groww_ddpi_confirmed is False and s.groww_sell_t1 is False
    monkeypatch.setenv("GROWW_DDPI_CONFIRMED", "true")
    monkeypatch.setenv("GROWW_SELL_T1", "true")
    s = load_settings()
    assert s.groww_ddpi_confirmed is True and s.groww_sell_t1 is True
    assert ENV_KEYS["groww_ddpi_confirmed"] == "GROWW_DDPI_CONFIRMED" and ENV_KEYS["groww_sell_t1"] == "GROWW_SELL_T1"


def test_make_groww_passes_the_switches(tmp_path, settings):
    from trading_agent import runner
    settings.groww_access_token = "tok"
    settings.groww_sell_t1 = True
    settings.groww_ddpi_confirmed = True
    settings.state_dir = tmp_path
    b = runner.make_groww(settings)
    assert b.sell_t1 is True and b.ddpi_confirmed is True and callable(b.alert_fn)


# ---- 3. groww-check --live-test ---------------------------------------------
LIVE_ROUTES = {
    ("GET", "/order/list"): ok({"order_list": [
        {"groww_order_id": "A1", "trading_symbol": "ITC", "order_status": "OPEN", "transaction_type": "BUY",
         "quantity": 1, "price": 400.0, "order_reference_id": "TA-1234567890AB"},
        {"groww_order_id": "A2", "trading_symbol": "SBIN", "order_status": "OPEN", "transaction_type": "BUY",
         "quantity": 5, "price": 700.0, "order_reference_id": ""},
        {"groww_order_id": "A3", "trading_symbol": "TCS", "order_status": "EXECUTED", "transaction_type": "SELL",
         "quantity": 1, "price": 3999.0, "order_reference_id": "app-xyz"}]}),
    ("GET", "/live-data/ltp"): ok({"NSE_ITC": 400.0}),
    ("GET", "/order/status/reference/"): ok({"groww_order_id": "GMK1", "order_status": "OPEN"}),
    ("POST", "/order/cancel"): ok({"groww_order_id": "GMK1", "order_status": "CANCELLATION_REQUESTED"}),
    ("GET", "/holdings/user"): ok({"holdings": []}),
}


def status_seq(price=None):
    open_ = {"groww_order_id": "GMK1", "order_status": "OPEN"}
    # confirm_order x2, the read-back after the modify, the pre-cancel check, then the post-cancel confirm
    return Seq(ok(open_), ok(open_), ok({**open_, **({"price": price} if price else {})}), ok(open_),
               ok({"groww_order_id": "GMK1", "order_status": "CANCELLED"}))


def test_live_test_prints_order_list_and_flags_foreign_orders():
    sess = FakeSession({**routes(), **LIVE_ROUTES, ("GET", "/order/status/GMK1"): status_seq()})
    c = live_test(live(sess), "ITC")
    text = format_rows(c)
    assert "3 order(s) today, 2 open" in text
    assert "SBIN" in text and "NOT placed by this agent" in text
    assert "1 of 3" not in text and "2 of 3" in text  # A2 (blank ref) and A3 (app ref) are foreign
    first = next(i for i, call in enumerate(sess.calls) if call[1].endswith("/order/list"))
    assert first < next(i for i, call in enumerate(sess.calls) if call[1].endswith("/order/create"))


def test_live_test_modifies_up_half_percent_rounded_to_tick_then_cancels():
    # LTP 400, tick 0.05: rests at 388.00 (3% below); +0.5% = 389.94 -> 389.90; cap 98% of LTP = 392.00
    sess = FakeSession({**routes(), **LIVE_ROUTES,
                        ("POST", "/order/modify"): ok({"groww_order_id": "GMK1", "order_status": "MODIFICATION_REQUESTED"}),
                        ("GET", "/order/status/GMK1"): status_seq(price=389.90)})
    c = live_test(live(sess), "ITC")
    res = {r["check"]: r for r in c.rows}
    create = next(x[2]["json"] for x in sess.calls if x[1].endswith("/order/create"))
    mod = next(x[2]["json"] for x in sess.calls if x[1].endswith("/order/modify"))
    assert create["trading_symbol"] == "ITC" and create["price"] == 388.0
    assert mod == {"quantity": 1, "order_type": "LIMIT", "segment": "CASH", "groww_order_id": "GMK1", "price": 389.9}
    assert mod["price"] <= 400.0 * 0.98
    assert res["live: modify order"]["ok"] is True, res["live: modify order"]
    assert res["live: cancel order"]["ok"] is True
    order = [x[1].rsplit("/", 2)[-2:] for x in sess.calls if x[0] == "POST"]
    assert order.index(["order", "modify"]) < order.index(["order", "cancel"])


def test_live_test_cancels_even_when_modify_fails():
    sess = FakeSession({**routes(), **LIVE_ROUTES,
                        ("POST", "/order/modify"): {"status": "FAILURE", "error": {"code": "GA003", "message": "boom"}},
                        ("GET", "/order/status/GMK1"): status_seq()})
    c = live_test(live(sess), "ITC")
    res = {r["check"]: r for r in c.rows}
    assert res["live: modify order"]["ok"] is False and "boom" in res["live: modify order"]["detail"]
    assert any(x[1].endswith("/order/cancel") for x in sess.calls)
    assert res["live: cancel order"]["ok"] is True


def test_live_test_cancels_when_modify_raises_unexpectedly():
    sess = FakeSession({**routes(), **LIVE_ROUTES, ("POST", "/order/modify"): RuntimeError("net down"),
                        ("GET", "/order/status/GMK1"): status_seq()})
    c = live_test(live(sess), "ITC")
    assert any(x[1].endswith("/order/cancel") for x in sess.calls)
    assert {r["check"]: r for r in c.rows}["live: modify order"]["ok"] is False


def test_live_test_default_symbol_is_itc_and_refuses_when_not_live():
    sess = FakeSession({**routes(), **LIVE_ROUTES, ("GET", "/order/status/GMK1"): status_seq()})
    live_test(live(sess))
    create = next(x[2]["json"] for x in sess.calls if x[1].endswith("/order/create"))
    assert create["trading_symbol"] == "ITC"
    sess2 = FakeSession({**routes(), **LIVE_ROUTES})
    with pytest.raises(LiveOrdersDisabled):
        live_test(GrowwBroker("tok", session=sess2, live_orders=False), "ITC")
    assert sess2.calls == []


def test_modify_order_refuses_unless_live():
    sess = FakeSession(routes())
    with pytest.raises(LiveOrdersDisabled):
        GrowwBroker("tok", session=sess).modify_order("GMK1", 100.0, 1)
    assert sess.calls == []
    with pytest.raises(LiveOrdersDisabled):
        GrowwBroker("tok", session=sess).create_gtt_stop("TCS", 1, 100.0, 99.0)


def test_modify_price_never_reaches_ltp():
    from trading_agent.groww_check import _modify_price
    assert _modify_price(388.0, 400.0, 0.05) == 389.9
    assert _modify_price(391.95, 400.0, 0.05) == 392.0  # capped at 98% of LTP
    assert _modify_price(392.0, 400.0, 0.05) is None  # no room left
    assert _modify_price(95.0, 100.0, 0.5) == 95.5  # coarse tick: one tick up


def test_cli_live_test_symbol_flags(tmp_path, monkeypatch, capsys):
    from trading_agent import cli
    from .test_groww_live import _isolated_env
    _isolated_env(tmp_path, monkeypatch, GROWW_ACCESS_TOKEN="tok", GROWW_LIVE_ORDERS="false")
    # bare --live-test parses (SYMBOL optional) and still refuses without live orders
    assert cli.main(["groww-check", "--live-test", "--i-understand-real-orders"]) == 1
    assert cli.main(["groww-check", "--live-test", "--symbol", "TCS", "--i-understand-real-orders"]) == 1
    assert "REAL 1-share" in capsys.readouterr().out
