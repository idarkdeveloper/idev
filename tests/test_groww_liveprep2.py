"""Fix round 2 for the live-order readiness build."""

import json
import threading
import time

from trading_agent.groww import is_authorisation_problem
from trading_agent.groww_check import live_test
from trading_agent.live_alerts import alert_once
from .conftest import FakeSession, Seq
from .test_groww_live import ok, routes
from .test_groww_liveprep import LIVE_ROUTES, live


def test_placement_answer_without_order_id_looks_up_and_cancels():
    sess = FakeSession({**routes(), **LIVE_ROUTES, ("POST", "/order/create"): ok({"order_status": "NEW"}),
                        ("GET", "/order/status/GMK1"): Seq(ok({"order_status": "CANCELLED"}))})
    res = {r["check"]: r for r in live_test(live(sess), "ITC").rows}
    assert res["live: place limit order"]["ok"] is False
    assert res["live: lookup after failed placement"]["ok"] is False
    assert any(x[1].endswith("/order/cancel") for x in sess.calls) and res["live: cancel order"]["ok"] is True
    # the lookup finds nothing: no cancel, an info row
    sess = FakeSession({**routes(), **LIVE_ROUTES, ("POST", "/order/create"): ok({"order_status": "NEW"}),
                        ("GET", "/order/status/reference/"): ok({})})
    res = {r["check"]: r for r in live_test(live(sess), "ITC").rows}
    assert res["live: lookup after failed placement"]["ok"] is None
    assert not any(x[1].endswith("/order/cancel") for x in sess.calls)


def test_alert_send_runs_outside_the_locks(tmp_path):
    p = tmp_path / "a.json"
    started, release = threading.Event(), threading.Event()
    sent = []

    def slow(s, b):
        sent.append(s)
        started.set()
        release.wait(5)

    t = threading.Thread(target=lambda: alert_once(p, "k", slow, "s", "b"))
    t.start()
    assert started.wait(5)
    t0 = time.time()
    # while the first send is in flight: another key is not blocked, the same key is skipped (claimed)
    assert alert_once(p, "other", lambda s, b: sent.append("other"), "o", "b") is True
    assert alert_once(p, "k", slow, "s", "b") is False
    assert time.time() - t0 < 1.0 and "other" in sent
    release.set()
    t.join(5)
    assert isinstance(json.loads(p.read_text())["k"], str)  # marked done (a day string) after the send
    assert alert_once(p, "k", slow, "s", "b") is False and sent.count("s") == 1


def test_failed_send_releases_the_claim(tmp_path):
    p = tmp_path / "a.json"

    def boom(s, b):
        raise RuntimeError("down")
    assert alert_once(p, "k", boom, "s", "b") is False
    assert "k" not in json.loads(p.read_text())
    assert alert_once(p, "k", lambda s, b: None, "s", "b") is True


def test_segment_and_permission_rejections_are_not_ddpi():
    for t in ("Sell order rejected: user not authorized for this segment",
              "Sell rejected: product not enabled for this account",
              "Sell authorization failed: segment not enabled for you"):
        assert not is_authorisation_problem(t), t
    assert is_authorisation_problem("Sell rejected: segment ok but TPIN authorisation pending")
    assert is_authorisation_problem("Sell authorization failed")


def test_filled_when_cancel_is_attempted_uses_filled_wording():
    open_ = ok({"order_status": "OPEN"})
    filled = ok({"order_status": "EXECUTED", "filled_quantity": 1})
    sess = FakeSession({**routes(), **LIVE_ROUTES, ("GET", "/order/status/GMK1"): Seq(open_, open_, open_, filled)})
    res = {r["check"]: r for r in live_test(live(sess), "ITC").rows}
    assert res["live: cancel order"]["ok"] is False
    assert "TEST ORDER FILLED: 1 share bought" in res["live: cancel order"]["detail"]
    assert "STILL BE OPEN" not in res["live: cancel order"]["detail"]
