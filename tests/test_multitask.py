"""Research tools run side by side (up to 3, one per kind); other jobs stay exclusive. Fake jobs gated by Events."""
import threading
import time

import pytest

from trading_agent.broker import LocalPaperBroker
from trading_agent.prices import atomic_write_text
from trading_agent.ui import App


@pytest.fixture
def app(settings, tmp_path):
    return App(settings, broker=LocalPaperBroker(tmp_path / "pb.json", starting_cash=1000, price_fn=lambda s: 1.0), dotenv=None)


class Gate:
    def __init__(self, result="done", boom=False):
        self.go, self.started, self.result, self.boom = threading.Event(), threading.Event(), result, boom

    def __call__(self, job):
        self.started.set()
        assert self.go.wait(10)
        if self.boom:
            raise RuntimeError("boom")
        return self.result


def wait(cond, secs=5):
    end = time.time() + secs
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_two_research_kinds_run_at_once_and_each_finishes_on_its_own(app):
    a, b = Gate("A done"), Gate("B done")
    ja, jb = app.run_background("screen", a), app.run_background("backtest", b)
    assert a.started.wait(5) and b.started.wait(5)
    assert app.busy and {j["kind"] for j in app.running_jobs()} == {"screen", "backtest"}
    st = app.snapshot()
    assert st["busy"] is True and len(st["running_jobs"]) == 2 and st["running"]["kind"] == "backtest"
    a.go.set()
    assert wait(lambda: ja.finished_at) and ja.ok and ja.message == "A done"
    assert jb.finished_at is None and app.busy                         # the other is still running
    b.go.set()
    assert wait(lambda: jb.finished_at and not app.busy) and jb.message == "B done"


def test_same_kind_twice_is_refused_with_a_clear_message_and_not_recorded(app):
    a = Gate()
    app.run_background("screen", a)
    assert a.started.wait(5)
    n = len(app.jobs)
    j = app.run_background("screen", Gate())
    assert j.ok is False and "factor screen is still running" in j.message and len(app.jobs) == n
    a.go.set()
    assert wait(lambda: not app.busy)


def test_a_fourth_research_job_beyond_the_cap_is_refused(app):
    gates = {k: Gate() for k in ("screen", "backtest", "signal_lab")}
    for k, g in gates.items():
        assert app.run_background(k, g).ok is None
    for g in gates.values():
        assert g.started.wait(5)
    j = app.run_background("factor_backtest", Gate())
    assert j.ok is False and "at most 3" in j.message
    for g in gates.values():
        g.go.set()
    assert wait(lambda: not app.busy)
    assert app.run_background("factor_backtest", g2 := Gate()).ok is None
    g2.go.set()
    assert wait(lambda: not app.busy)


def test_exclusive_jobs_serialize_and_block_research_both_ways(app):
    ex = Gate()
    app.run_background("replay_step", ex)
    assert ex.started.wait(5)
    r = app.run_background("screen", Gate())
    assert r.ok is False and "still running" in r.message                # research waits for an exclusive job
    assert app.run_background("replay_create", Gate()).ok is False       # exclusive vs exclusive
    ex.go.set()
    assert wait(lambda: not app.busy)
    rs = Gate()
    app.run_background("screen", rs)
    assert rs.started.wait(5)
    e = app.run_background("replay_step", Gate())
    assert e.ok is False and "factor screen is still running" in e.message   # an exclusive job waits for research
    rs.go.set()
    assert wait(lambda: not app.busy)


def test_one_job_failing_does_not_touch_the_others(app):
    bad, good = Gate(boom=True), Gate("fine")
    jb, jg = app.run_background("screen", bad), app.run_background("signal_lab", good)
    assert bad.started.wait(5) and good.started.wait(5)
    bad.go.set()
    assert wait(lambda: jb.finished_at) and jb.ok is False and "boom" in jb.message
    assert jg.finished_at is None and app.busy
    good.go.set()
    assert wait(lambda: jg.finished_at) and jg.ok and not app.busy


def test_the_market_check_does_not_hold_a_slot(app, settings):
    settings.market = "us"
    j = app.start_signal_lab("NIFTY50", 3, [20])
    assert j.ok is False and "NSE indices" in j.message and not app.busy and j in app.jobs


def test_cache_writes_are_atomic(tmp_path):
    p = tmp_path / "c.json"
    ts = [threading.Thread(target=atomic_write_text, args=(p, "[" + "1," * 5000 + "1]")) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert p.read_text().endswith("1]") and not list(tmp_path.glob("*.tmp"))
