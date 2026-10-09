import re
import threading
import time

import pytest

from trading_agent.ui import App
from trading_agent.replay.web import ReplayApp
from .replay_fakes import FakeUniverse, market, top_by_6m


class FakeNews:
    def announcement_history(self, symbol):
        return [{"at": "2021-03-10 10:00:00", "category": "Updates", "text": f"{symbol} update", "file": ""}]


def wait(app, job):
    for _ in range(200):
        if job.finished_at:
            return job
        time.sleep(0.02)
    raise AssertionError("job did not finish")


@pytest.fixture
def rapp(settings, tmp_path):
    settings.market = "in"
    app = App(settings, dotenv=None)
    r = ReplayApp(app, source=market(), universe_factory=lambda name: FakeUniverse(), news_client=FakeNews(),
                  client_factory=None, today_fn=lambda: "2026-10-09", screen_fn=top_by_6m)
    app._replay = r
    return app, r


def create(app, r, **kw):
    body = {"name": "Test run", "start": "2021-03-15", "cash": 100000, "universe": "NIFTYMIDCAP150",
            "top": 3, "dividends": "reinvest", **kw}
    job = wait(app, r.create(body))
    assert job.ok, job.message
    return job.result["slug"]


def test_create_list_and_snapshot_has_no_future_dates(rapp):
    app, r = rapp
    slug = create(app, r)
    assert r.list()[0]["slug"] == slug
    snap = r.snapshot(slug)
    assert snap["trial"]["clock"] == "2021-03-15" and snap["race"]["dates"] == ["2021-03-15"]
    assert snap["picks"]["will_buy"] == [] and set(snap["agent"]["next_rebalance"]) <= set("0123456789-")
    import json
    dates = re.findall(r"\d{4}-\d{2}-\d{2}", json.dumps(snap, default=str))
    assert dates and all(d <= "2021-03-15" for d in dates)


def test_order_step_end_via_route(rapp):
    app, r = rapp
    slug = create(app, r)
    st, o = r.route("POST", f"/replay/api/trial/{slug}/order", {}, {"symbol": "D", "side": "buy", "qty": 5})
    assert st == 200 and o["order"]["qty"] == 5
    st, j = r.route("POST", f"/replay/api/trial/{slug}/step", {}, {"by": "month"})
    assert st == 202
    wait(app, app.jobs[-1])
    assert app.jobs[-1].ok, app.jobs[-1].message
    snap = r.snapshot(slug)
    assert snap["trial"]["clock"] == "2021-04-15" and len(snap["race"]["dates"]) > 20
    st, s = r.route("POST", f"/replay/api/trial/{slug}/end", {}, {})
    assert st == 200 and s["scorecard"]["you"]["trades"] == 1 and s["next"]["dates"][0] == "2021-04-15"


def test_lookup_is_as_of_the_clock(rapp):
    app, r = rapp
    slug = create(app, r)
    st, lk = r.route("GET", f"/replay/api/trial/{slug}/lookup", {"ticker": "A"}, None)
    assert st == 200 and lk["ticker"] == "A" and lk["history"][-1]["d"] == "2021-03-15"
    assert lk["announcements"][0]["text"] == "A update" and lk["today"] == "2021-03-15"


def test_second_step_while_busy_is_refused(rapp):
    app, r = rapp
    slug = create(app, r)
    gate = threading.Event()
    blocker = app.run_background("check", lambda job: gate.wait(5) and "done")
    st, j = r.route("POST", f"/replay/api/trial/{slug}/step", {}, {"by": "week"})
    assert st == 202 and j["ok"] is False and "still running" in j["message"]
    gate.set()
    wait(app, blocker)
    assert r.snapshot(slug)["trial"]["clock"] == "2021-03-15"


def test_snapshot_after_restart_needs_no_network(rapp, settings):
    app, r = rapp
    slug = create(app, r)
    st, _ = r.route("POST", f"/replay/api/trial/{slug}/order", {}, {"symbol": "D", "side": "buy", "qty": 5})
    assert st == 200

    class NoNetwork:
        def history(self, *a, **k):
            raise ConnectionError("offline")
        dividends = history
    fresh = ReplayApp(App(settings, dotenv=None), source=NoNetwork(), universe_factory=lambda n: FakeUniverse(),
                      news_client=FakeNews(), today_fn=lambda: "2026-10-09", screen_fn=top_by_6m)
    snap = fresh.snapshot(slug)
    assert snap["trial"]["slug"] == slug and snap["you"]["cash"] < 100000
    pos = snap["you"]["positions"]
    assert [p["symbol"] for p in pos] == ["D"] and pos[0]["current_price"] > 0 and pos[0]["stop"] is None


def test_ask_needs_a_key(rapp):
    app, r = rapp
    slug = create(app, r)
    app.settings.anthropic_api_key = None
    st, body = r.route("POST", f"/replay/api/trial/{slug}/ask", {}, {})
    assert st == 403 and "ANTHROPIC_API_KEY" in body["error"]


def test_replay_never_builds_a_groww_broker(rapp, monkeypatch):
    import trading_agent.groww as g
    monkeypatch.setattr(g.GrowwBroker, "__init__", lambda *a, **k: (_ for _ in ()).throw(AssertionError("groww")))
    app, r = rapp
    slug = create(app, r)
    r.route("POST", f"/replay/api/trial/{slug}/order", {}, {"symbol": "D", "side": "buy", "qty": 1})
    r.snapshot(slug)


@pytest.fixture
def gated(settings):
    settings.market = "in"
    app = App(settings, dotenv=None)
    entered, release, armed = threading.Event(), threading.Event(), []

    def screen(members, prices, top):
        if armed:
            entered.set()
            release.wait(5)
        return top_by_6m(members, prices, top)
    r = ReplayApp(app, source=market(), universe_factory=lambda name: FakeUniverse(), news_client=FakeNews(),
                  today_fn=lambda: "2026-10-09", screen_fn=screen)
    app._replay = r
    return app, r, entered, release, armed


def test_actions_are_409_while_a_step_runs(gated):
    app, r, entered, release, armed = gated
    slug = create(app, r)
    armed.append(1)
    st, j = r.route("POST", f"/replay/api/trial/{slug}/step", {}, {"by": "month"})
    assert st == 202 and entered.wait(5)
    st, body = r.route("POST", f"/replay/api/trial/{slug}/order", {}, {"symbol": "D", "side": "buy", "qty": 1})
    assert st == 409 and "step is running" in body["error"]
    assert r.route("GET", f"/replay/api/trial/{slug}", {}, None)[0] == 409
    release.set()
    wait(app, app.jobs[-1])
    assert app.jobs[-1].ok, app.jobs[-1].message
    st, _ = r.route("POST", f"/replay/api/trial/{slug}/order", {}, {"symbol": "D", "side": "buy", "qty": 1})
    assert st == 200


def test_lock_released_after_refused_step(rapp):
    app, r = rapp
    slug = create(app, r)
    gate = threading.Event()
    blocker = app.run_background("check", lambda job: gate.wait(5) and "done")
    st, j = r.route("POST", f"/replay/api/trial/{slug}/step", {}, {"by": "week"})
    assert st == 202 and j["ok"] is False
    st, _ = r.route("POST", f"/replay/api/trial/{slug}/order", {}, {"symbol": "D", "side": "buy", "qty": 1})
    assert st == 200
    gate.set()
    wait(app, blocker)


def test_unknown_slug_is_404_and_bad_body_is_400(rapp):
    app, r = rapp
    assert r.route("GET", "/replay/api/trial/nope", {}, None)[0] == 404
    slug = create(app, r)
    st, body = r.route("POST", f"/replay/api/trial/{slug}/order", {}, ["x"])
    assert st == 400 and body["error"] == "request body must be a JSON object"


def test_unexpected_error_is_500(rapp):
    app, r = rapp
    slug = create(app, r)
    app.settings.anthropic_api_key = "k"

    def boom():
        raise RuntimeError("kaput")
    r.client_factory = boom
    st, body = r.route("POST", f"/replay/api/trial/{slug}/ask", {}, {})
    assert st == 500 and "RuntimeError: kaput" in body["error"]
