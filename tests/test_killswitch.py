"""Kill switch (state/HALT): the five steps, and every order path refusing while the file exists.

Fakes only: a FakeSession stands in for Groww and records every call, so a test can show that the switch only lists
and cancels, and that nothing places an order. No network, no real Groww, no notifier.
"""
import dataclasses
import json
from datetime import datetime

import pytest

from trading_agent import killswitch, runner
from trading_agent.groww import GrowwBroker, GrowwTokenUnavailable
from trading_agent.killswitch import Halted
from trading_agent.state import State
from trading_agent.timezones import IST
from trading_agent.watch import Watcher

from .conftest import FakeSession
from .test_ui import _get, _post, server, two_pages  # noqa: F401  (fixtures)

AT = datetime(2026, 10, 12, 10, 5, tzinfo=IST)


def ok(payload):
    return {"status": "SUCCESS", "payload": payload}


ORDERS = ok({"order_list": [
    {"groww_order_id": "G1", "order_reference_id": "TA-AAA111", "order_status": "OPEN", "transaction_type": "BUY",
     "quantity": 3, "trading_symbol": "RELIANCE"},
    {"groww_order_id": "G2", "order_reference_id": "TA-BBB222", "order_status": "EXECUTED", "transaction_type": "BUY",
     "quantity": 1, "trading_symbol": "TCS"},
    {"groww_order_id": "G3", "order_reference_id": "APP-123", "order_status": "OPEN", "transaction_type": "SELL",
     "quantity": 5, "trading_symbol": "INFY"},     # placed in the Groww app: never touched
]})
GTTS = ok({"order_list": [
    {"smart_order_id": "S1", "reference_id": "SL-111", "status": "ACTIVE", "trading_symbol": "RELIANCE",
     "quantity": 3, "trigger_price": "2700.00"},
    {"smart_order_id": "S9", "reference_id": "MANUAL-9", "status": "ACTIVE", "trading_symbol": "ITC",
     "quantity": 10, "trigger_price": "400.00"},   # the user's own GTT: never cancelled
]})


def groww_routes():
    return {("GET", "/order/list"): ORDERS, ("GET", "/order-advance/list"): GTTS,
            ("POST", "/order/cancel"): ok({"order_status": "CANCELLATION_REQUESTED"}),
            ("POST", "/order-advance/cancel"): ok({"status": "CANCELLED"})}


class Notes:
    def __init__(self):
        self.sent = []

    def send(self, subject, body, **kw):
        self.sent.append((subject, body))
        return ["telegram", "email"]


@pytest.fixture
def live(settings):
    settings.market, settings.broker, settings.groww_live_orders = "in", "groww", True
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    return settings


def halt(settings, by="test"):
    killswitch.engage(dataclasses.replace(settings, groww_live_orders=False), by=by, now=AT)


# --------------------------------------------------------------------------- #
# the five steps
# --------------------------------------------------------------------------- #
def test_engage_writes_halt_cancels_only_open_agent_orders_keeps_gtts_deletes_token_and_alerts(live):
    (live.state_dir / "groww_token.json").write_text("{}")
    sess = FakeSession(groww_routes())
    stopped, notes = [], Notes()
    rep = killswitch.engage(live, by="cli", broker_fn=lambda: GrowwBroker("tok", session=sess, live_orders=True),
                            stop_watch=lambda: stopped.append(1), notifier=notes, now=AT)
    rec = killswitch.status(live.state_dir)
    assert rec == {"at": "2026-10-12T10:05:00+05:30", "by": "cli", "cancel_gtt": False}          # 1
    assert stopped == [1] and rep["watch"].startswith("stopped in this process")                  # 2
    writes = sess.writes()
    assert [(m, u.split("/v1/")[-1], kw.get("json")) for m, u, kw in writes] == [
        ("POST", "order/cancel", {"segment": "CASH", "groww_order_id": "G1"})]                    # 3: only G1
    assert rep["orders_cancelled"] == ["BUY 3 RELIANCE (id G1, ref TA-AAA111)"]
    assert len(rep["gtts"]) == 2 and rep["gtts_cancelled"] == []                                  # listed, kept
    assert not (live.state_dir / "groww_token.json").exists() and rep["token"] == "cached Groww token deleted"  # 4
    assert notes.sent[0][0] == "[HALT] kill switch engaged" and rep["alerted"] == ["telegram", "email"]  # 5
    body = notes.sent[0][1]
    assert "GTT stop-losses left active (2)" in body and "kill-switch --release" in body
    assert not any("order/create" in u or "order/modify" in u or "order-advance/create" in u for _, u, _ in sess.calls)


def test_cancel_gtt_cancels_only_the_agents_gtts_and_forgets_them(live):
    st = State(live.state_dir / "state.json")
    st.data["gtt_stops"] = {"RELIANCE": {"smart_order_id": "S1", "trigger": 2700.0, "qty": 3}}
    st.save()
    sess = FakeSession(groww_routes())
    rep = killswitch.engage(live, by="cli", cancel_gtt=True,
                            broker_fn=lambda: GrowwBroker("tok", session=sess, live_orders=True), now=AT)
    cancels = [u for m, u, _ in sess.writes() if "order-advance/cancel" in u]
    assert len(cancels) == 1 and cancels[0].endswith("/S1")                                       # not the user's S9
    assert rep["gtts_cancelled"] == ["RELIANCE qty 3 trigger 2700.00 (id S1)"]
    st = State(live.state_dir / "state.json")
    assert st.data["gtt_stops"] == {} and st.data["gtt_history"][-1]["reason"] == "kill switch"


def test_engage_never_stops_half_way(live):
    def no_groww():
        raise GrowwTokenUnavailable("cooling down")

    def bad_watch():
        raise RuntimeError("stuck")

    def bad_notes(*a, **k):
        raise RuntimeError("telegram down")
    notes = Notes()
    notes.send = bad_notes
    rep = killswitch.engage(live, by="cli", broker_fn=no_groww, stop_watch=bad_watch, notifier=notes, now=AT)
    assert killswitch.status(live.state_dir) is not None
    assert "could not reach Groww" in rep["groww"] and "CHECK THE GROWW APP" in rep["groww"]
    assert "could not stop" in rep["watch"] and rep["alerted"] == [] and rep["token"] == "no cached Groww token"


def test_with_live_orders_off_nothing_at_groww_is_touched(settings):
    called = []
    rep = killswitch.engage(settings, by="cli", broker_fn=lambda: called.append(1), now=AT)
    assert called == [] and "live orders are off" in rep["groww"]


def test_engaging_again_keeps_the_first_time_and_release_removes_it(live):
    notes = Notes()
    halt(live, by="dashboard")
    killswitch.engage(dataclasses.replace(live, groww_live_orders=False), by="cli",
                      now=datetime(2026, 10, 12, 11, 0, tzinfo=IST))
    assert killswitch.status(live.state_dir)["at"] == "2026-10-12T10:05:00+05:30"
    assert killswitch.status(live.state_dir)["by"] == "dashboard"
    assert "since 12 Oct 10:05 IST" in killswitch.reason(live.state_dir)
    assert killswitch.release(live, by="cli", notifier=notes) is True
    assert killswitch.status(live.state_dir) is None and notes.sent[0][0] == "[HALT released] trading may resume"
    assert killswitch.release(live, by="cli") is False
    log = [json.loads(x) for x in (live.state_dir / "halt_log.jsonl").read_text().splitlines()]
    assert [e["event"] for e in log] == ["engage", "engage", "release"]


def test_an_unreadable_halt_file_still_halts(settings):
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    (settings.state_dir / "HALT").write_text("not json")
    assert killswitch.status(settings.state_dir) == {"at": None, "by": "unknown"}
    with pytest.raises(Halted):
        killswitch.refuse(settings.state_dir, "trade")


# --------------------------------------------------------------------------- #
# every order path refuses while halted
# --------------------------------------------------------------------------- #
def test_the_live_broker_refuses_every_order_change_but_still_cancels(live):
    halt(live)
    sess = FakeSession(groww_routes())
    b = GrowwBroker("tok", session=sess, live_orders=True, sleep=lambda s: None,
                    halt_fn=lambda what: killswitch.reason(live.state_dir, what))
    for call in (lambda: b.submit_order("RELIANCE", "buy", qty=1), lambda: b.submit_order("RELIANCE", "sell", qty=1),
                 lambda: b.modify_order("G1", 100.0, 1), lambda: b.create_gtt_stop("RELIANCE", 1, 90.0, 89.5),
                 lambda: b.modify_gtt_stop("S1", 1, 90.0, 89.5)):
        with pytest.raises(Halted, match="HALT is set"):
            call()
    assert sess.calls == []                                                   # refused before any network call
    b.cancel_order("G1")
    b.cancel_gtt("S1")
    assert len(sess.writes()) == 2


def test_make_groww_wires_the_halt_check_and_no_token_is_used_while_halted(live):
    live.groww_access_token = "tok"                    # even a token from .env is not used
    assert runner.make_groww(live).halt_fn("x") is None
    halt(live)
    with pytest.raises(GrowwTokenUnavailable, match="HALT is set"):
        runner.resolve_groww_token(live)
    assert isinstance(runner.token_block(live), GrowwTokenUnavailable)
    b = runner.make_groww(live, halt_ok=True)          # the switch's own client: lists and cancels
    with pytest.raises(Halted):
        b.submit_order("RELIANCE", "buy", qty=1)


def test_the_practice_account_and_its_stop_sells_refuse(settings):
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    pb = runner.make_practice_broker(settings, price_fn=lambda s: 100.0)
    pb.submit_order("AAA", "buy", qty=5)
    halt(settings)
    with pytest.raises(PermissionError, match="practice buy"):
        pb.submit_order("AAA", "buy", qty=1)
    with pytest.raises(PermissionError, match="practice sell"):
        pb.submit_order("AAA", "sell", qty=1)
    with pytest.raises(PermissionError):
        pb.sell_if_stopped("AAA", qty=5, level=1000.0, stop={"type": "trailing", "value": None})
    assert [p.qty for p in pb.positions()] == [5]


def test_the_practice_stop_checker_does_nothing(settings):
    from trading_agent.stops import PracticeStopChecker
    halt(settings)

    def broker():
        raise AssertionError("the broker was opened")
    chk = PracticeStopChecker(broker, settings.state_dir / "state.json")
    assert chk.check_once(datetime(2026, 10, 12, 11, 0, tzinfo=IST)) == []


def test_the_forward_test_refuses(settings):
    from trading_agent.forward import ForwardTest
    from trading_agent.forward_schedule import run_forward_due
    settings.market = "in"
    halt(settings)
    assert run_forward_due(settings, prices=None, holidays=None).startswith("HALT is set")
    ft = ForwardTest(settings.state_dir, universe="NIFTY50", capital=100_000, price_fn=lambda s: 10.0)
    with pytest.raises(PermissionError, match="forward-test buy"):
        ft.broker.submit_order("AAA", "buy", qty=1)


def test_the_agent_check_refuses(settings):
    halt(settings)
    with pytest.raises(Halted):
        runner.check(settings, trades=[], broker=object(), notifier=object())


def test_the_agent_order_tool_refuses(settings):
    from trading_agent.agent import AgentContext, RunResult, build_tools
    settings.auto_trade = True
    pb = runner.make_practice_broker(settings, price_fn=lambda s: 100.0)
    halt(settings)
    ctx = AgentContext(settings=settings, broker=pb, data=None, state=State(settings.state_dir / "state.json"),
                       notifier=Notes(), result=RunResult(investor="x", new_trades=[]))
    tool = next(t for t in build_tools(ctx) if t.name == "place_paper_order")
    out = json.loads(tool(symbol="AAA", side="buy", notional_usd=500.0))
    assert "HALT is set" in out["error"] and ctx.result.orders == []


def test_the_watch_idles_and_resumes_after_release(settings):
    calls = []
    w = Watcher(settings, check_fn=lambda: calls.append("check"), awake=None, weekdays_only=False,
                window=("00:00", "23:59"))
    halt(settings)
    info = w.tick(force=True)
    assert info["skipped"] and "HALT is set" in info["halted"] and calls == []
    killswitch.release(settings, by="test")
    info = w.tick(force=True)
    assert "halted" not in info and calls == ["check"]


def test_the_cli_refuses_with_exit_1_except_the_switch_and_offline_commands(tmp_path, monkeypatch, capsys):
    from trading_agent import cli
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("STATE_DIR", str(tmp_path / "state"))
    for k in ("GROWW_ACCESS_TOKEN", "GROWW_API_KEY", "BROKER", "MARKET", "GROWW_LIVE_ORDERS"):
        monkeypatch.delenv(k, raising=False)
    assert cli.main(["kill-switch", "--status"]) == 0 and "Not halted" in capsys.readouterr().out
    assert cli.main(["kill-switch"]) == 0
    out = capsys.readouterr().out
    assert "Trading HALTED" in out and "live orders are off" in out
    for argv in (["check"], ["loop"], ["forward", "--if-due"], ["orders"], ["gtt", "--sync"], ["holdings"],
                 ["groww-token"], ["groww-check"], ["integration-check"], ["size", "RELIANCE"], ["portfolio"],
                 ["reset"], ["digest", "morning"]):
        assert cli.main(argv) == 1, argv
        assert "HALT is set" in capsys.readouterr().err
    assert cli.main(["kill-switch", "--status"]) == 0 and "HALT is set" in capsys.readouterr().out
    assert cli.main(["kill-switch", "--release"]) == 0 and "released" in capsys.readouterr().out
    assert cli.main(["forward", "--status"]) == 0                              # works again


def test_every_halt_allowed_cli_command_exists():
    from trading_agent import cli
    names = set(cli.build_parser()._subparsers._group_actions[0].choices)   # type: ignore[union-attr]
    assert cli.HALT_ALLOWED <= names


# --------------------------------------------------------------------------- #
# the dashboard
# --------------------------------------------------------------------------- #
def test_the_dashboard_button_needs_the_typed_word_and_the_live_page(two_pages, monkeypatch):  # noqa: F811
    base, app, fake, settings = two_pages
    notes = Notes()
    monkeypatch.setattr("trading_agent.ui.make_notifier", lambda s: notes)
    monkeypatch.setattr(runner, "make_groww", lambda s, price_fn=None, halt_ok=False: (_ for _ in ()).throw(
        GrowwTokenUnavailable("no token in tests")))
    status, j = _post(base + "/api/kill-switch", {"confirm": "halt"})
    assert status == 400 and "Type HALT" in j["error"] and killswitch.status(settings.state_dir) is None
    status, j = _post(base + "/demo/api/kill-switch", {"confirm": "HALT"})
    assert status == 403 and "Live page" in j["error"]
    status, j = _post(base + "/api/kill-switch", {"confirm": "HALT", "cancel_gtt": True})
    assert status == 200 and j["halt"]["by"] == "dashboard" and j["halt"]["cancel_gtt"] is True
    assert "Trading HALTED" in j["summary"] and notes.sent[0][0] == "[HALT] kill switch engaged"
    _, st = _get(base + "/api/state")
    assert st["halt"]["by"] == "dashboard" and "kill-switch --release" in st["halt"]["text"]
    _, h = _get(base + "/api/health")
    assert h["halt"]["by"] == "dashboard"
    assert fake.writes == []


def test_the_dashboard_refuses_trading_routes_while_halted(server):  # noqa: F811
    base, app = server
    halt(app.settings)
    for path, body in (("/api/order", {"symbol": "NVDA", "side": "buy", "notional": 1000}), ("/api/check", {}),
                       ("/api/close", {"symbol": "NVDA"}), ("/api/groww/refresh", {}), ("/api/reset", {}),
                       ("/api/practice/sell", {"symbol": "NVDA", "qty": 1})):
        status, j = _post(base + path, body)
        assert status == 403 and "HALT is set" in j["error"], path
    assert app.broker.positions() == []


def test_the_page_has_the_button_and_the_strip_knows_the_halt():
    from pathlib import Path
    ui = Path(__file__).resolve().parents[1] / "trading_agent" / "ui"
    page = (ui / "index.html").read_text(encoding="utf-8")
    assert 'id="ks-word"' in page and 'id="ks-go" disabled' in page and 'value !== "HALT"' in page
    assert 'api("/api/kill-switch", {confirm:' in page and "halt: S.halt" in page
    js = (ui / "common.js").read_text(encoding="utf-8")
    assert 'kind: "halted"' in js and "kill-switch --release" in js
