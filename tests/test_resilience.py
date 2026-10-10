"""Heartbeat SLA in the market window, the NSE/Yahoo circuit breaker, the clock check, and the state backups.
Fakes only: no network, no commands run, no real keys."""
import json
import logging
import sqlite3
import threading
import time
from datetime import date, datetime

import pytest
import requests

from trading_agent import backup
from trading_agent.circuit import CircuitBreaker, CircuitOpen, GuardedSession, read_degraded
from trading_agent.clockcheck import check_clock, startup_check
from trading_agent.heartbeat import Heartbeat
from trading_agent.integration import run_check
from trading_agent.nse import NSEClient
from trading_agent.prices import YahooPrices
from trading_agent.safety import freshness
from trading_agent.state import State, atomic_write
from trading_agent.timezones import IST

from tests.conftest import FakeResponse
from tests.test_integration import (FakeBSE, FakeGrowwReads, FakeNSE, FakePrices, FakeSession, Holidays, MON, Notifier,
                                    by_name)

URL = "https://hc-ping.com/abcd-1234-token"


# ------------------------------------------------------------------ heartbeat SLA
class Pings:
    def __init__(self):
        self.calls = []

    def get(self, url, timeout=None):
        self.calls.append(("GET", url))
        return FakeResponse("")

    def post(self, url, data=None, timeout=None):
        self.calls.append(("POST", url))
        return FakeResponse("")


def beat_setup(in_window, loop_every=60.0):
    now = [1000.0]
    s = Pings()
    h = Heartbeat(URL, session=s, clock=lambda: now[0])
    h._progress, h._stall, h._loop_every = (lambda: 1000.0), 1200.0, loop_every
    h._window = lambda: in_window[0]
    return h, s, now


def test_in_the_market_window_the_loop_is_stalled_after_three_minutes():
    win = [True]
    h, s, now = beat_setup(win)
    assert h.every == 60.0 and h._stall_now() == 180.0
    now[0] = 1000.0 + 179
    assert h.beat() == "ok"                      # 179 s without progress: still fine in the window
    now[0] = 1000.0 + 181
    assert h.beat() == "fail" and s.calls[-1] == ("POST", URL + "/fail")
    now[0] += 30
    assert h.beat() == "skip"                    # still stalled: one fail per minute at most
    now[0] += 300
    assert h.beat() == "fail"


def test_outside_the_window_the_stall_limit_is_twenty_minutes_but_the_ping_is_still_every_minute():
    win = [False]
    h, s, now = beat_setup(win)
    assert h.every == 60.0 and h._stall_now() == 1200.0
    now[0] = 1000.0 + 600                        # 10 minutes of silence overnight is fine
    assert h.beat() == "ok"
    now[0] = 1000.0 + 1300
    assert h.beat() == "fail"


def test_a_slow_loop_interval_stretches_the_window_stall_to_three_intervals():
    h, s, now = beat_setup([True], loop_every=120.0)
    assert h._stall_now() == 360.0


def test_the_ping_period_defaults_to_a_minute_at_every_hour():
    import trading_agent.heartbeat as hb
    assert hb.PING_EVERY_S == 60.0 and Heartbeat(URL, session=Pings()).every == 60.0


def test_the_thread_keeps_pinging_whatever_the_hour():
    s = Pings()
    h = Heartbeat(URL, session=s, every=0.05)
    stop = threading.Event()
    h.start(lambda: time.monotonic(), stall_s=1200, stop=stop, window=lambda: False, loop_every=60)
    try:
        end = time.time() + 3
        while time.time() < end and len([c for c in s.calls if c[0] == "GET"]) < 3:
            time.sleep(0.01)
        assert len([c for c in s.calls if c[0] == "GET"]) >= 3
    finally:
        stop.set()
        h.stop()


def test_the_watch_loop_hands_the_heartbeat_a_market_window_test(settings):
    from trading_agent.watch import Watcher
    got = {}

    class HB:
        def start(self, progress, *, stall_s, stop, window=None, loop_every=None):
            got.update(window=window, loop_every=loop_every)
            w._stop.set()

        def stop(self):
            pass

    w = Watcher(settings, every=60, awake=None, heartbeat=HB(), holidays=Holidays())
    w.run_forever()
    assert got["loop_every"] == 60 and isinstance(got["window"](), bool)


# ------------------------------------------------------------------ circuit breaker
class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_breaker_opens_after_three_refusals_with_30_60_300_back_off_and_resets_on_success(caplog):
    clk = Clock()
    b = CircuitBreaker("NSE", clock=clk)
    with caplog.at_level(logging.INFO, logger="trading_agent.circuit"):
        b.failure(); b.failure()
        b.before()                               # two refusals: still closed
        b.failure()                              # the third opens it for 30 s
        with pytest.raises(CircuitOpen):
            b.before()
        clk.t = 29.0
        with pytest.raises(CircuitOpen):
            b.before()
        clk.t = 31.0
        b.before()                               # a trial is let through
        b.failure()                              # it failed: 60 s
        clk.t = 31.0 + 59
        with pytest.raises(CircuitOpen):
            b.before()
        clk.t = 31.0 + 61
        b.before(); b.failure()                  # 300 s (the cap)
        clk.t += 299
        with pytest.raises(CircuitOpen):
            b.before()
        clk.t += 2
        b.before(); b.failure()                  # and it stays at 300 s
        assert b.open_until - clk.t == 300.0
        b.success()
        b.before()
        assert b.failures == 0 and not b.degraded
    msgs = [r.getMessage() for r in caplog.records]
    assert msgs.count("NSE connection degraded") == 1 and msgs.count("NSE connection recovered") == 1


class Scripted:
    """A session whose GET answers come from a list (a status code, or an exception to raise)."""

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), 0

    def get(self, url, **kw):
        self.calls += 1
        a = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(a, Exception):
            raise a
        return FakeResponse({"ok": 1}, a)


@pytest.mark.parametrize("bad", [403, 429, 500, 503, requests.Timeout("slow"), requests.ConnectionError("down")])
def test_guarded_session_counts_refusals_and_skips_calls_while_open(bad):
    clk = Clock()
    inner = Scripted(bad)
    g = GuardedSession(inner, CircuitBreaker("NSE", clock=clk))
    for _ in range(3):
        try:
            g.get("https://x")
        except requests.RequestException:
            pass
    assert inner.calls == 3 and g.breaker.degraded
    with pytest.raises(CircuitOpen):
        g.get("https://x")
    assert inner.calls == 3                      # skipped, not retried
    assert isinstance(CircuitOpen("x"), requests.RequestException)


def test_a_404_or_a_success_is_not_a_refusal_and_resets_the_count():
    inner = Scripted(500, 500, 404, 500, 500, 200)
    g = GuardedSession(inner, CircuitBreaker("NSE", clock=Clock()))
    for _ in range(6):
        g.get("https://x")
    assert not g.breaker.degraded and g.breaker.failures == 0   # 404 reset the run, then 2 more refusals, then a 200


def test_the_nse_client_uses_the_breaker_and_publishes_its_state(tmp_path):
    f = tmp_path / "nse_breaker.json"
    inner = Scripted(403)
    c = NSEClient(session=inner, breaker_file=f)
    for _ in range(3):
        with pytest.raises(Exception):
            c.announcements()
    assert c.breaker.degraded
    n = inner.calls
    with pytest.raises(CircuitOpen):
        c.announcements()
    assert inner.calls == n                      # no request while open
    assert read_degraded(f) is not None
    # the dashboard's freshness reads it: "NSE degraded"
    now = datetime.now(IST)
    assert freshness(tmp_path, now)["nse_degraded"] is True
    assert read_degraded(f, now=time.time() + 3600) is None                # a stale file (watch gone) is ignored
    c.breaker.success()
    assert freshness(tmp_path, now)["nse_degraded"] is False


def test_the_warm_up_403_does_not_count_against_nse():
    inner = Scripted(403)
    c = NSEClient(session=inner)
    c._ensure_warm({})
    assert c.breaker.failures == 0 and inner.calls == 1


def test_yahoo_history_goes_through_a_breaker_too():
    inner = Scripted(500)
    p = YahooPrices(session=inner)
    for _ in range(3):
        with pytest.raises(Exception):
            p.history("ABC", "1mo")
    n = inner.calls
    with pytest.raises(CircuitOpen):
        p.history("ABC", "1mo")
    assert inner.calls == n and p.breaker.degraded


def test_the_chip_script_mentions_nse_degraded():
    from pathlib import Path
    js = (Path(__file__).resolve().parents[1] / "trading_agent" / "ui" / "common.js").read_text(encoding="utf-8")
    assert "NSE degraded" in js and "nse_degraded" in js


# ------------------------------------------------------------------ clock check
def fake_run(table):
    def run(cmd):
        return table.get(" ".join(cmd))
    return run


SYNC_YES = (0, "NTPSynchronized=yes\n")
SYNC_NO = (0, "NTPSynchronized=no\n")


def test_clock_ok_with_a_small_offset_from_timesyncd():
    r = check_clock(fake_run({"timedatectl show -p NTPSynchronized": SYNC_YES,
                              "timedatectl timesync-status": (0, "       Offset: +3.2ms\n  Packet count: 4\n")}), "linux")
    assert r["checked"] and r["ok"] is True and abs(r["offset_s"] - 0.0032) < 1e-9 and "offset 3 ms" in r["detail"]


@pytest.mark.parametrize("offset,sign", [("+1.5s", 1), ("-2.1s", -1)])
def test_clock_fails_when_the_offset_is_over_a_second(offset, sign):
    r = check_clock(fake_run({"timedatectl show -p NTPSynchronized": SYNC_YES,
                              "timedatectl timesync-status": (0, f"Offset: {offset}\n")}), "linux")
    assert r["ok"] is False and "limit 1 s" in r["detail"] and r["offset_s"] * sign > 1


def test_clock_reads_chrony_when_timesyncd_has_no_offset():
    table = {"timedatectl show -p NTPSynchronized": SYNC_YES, "timedatectl timesync-status": (1, ""),
             "chronyc tracking": (0, "System time     : 2.500000000 seconds slow of NTP time\n")}
    r = check_clock(fake_run(table), "linux")
    assert r["ok"] is False and r["offset_s"] == -2.5
    table["chronyc tracking"] = (0, "System time     : 0.000012345 seconds fast of NTP time\n")
    assert check_clock(fake_run(table), "linux")["ok"] is True


def test_clock_fails_when_ntp_is_not_synchronised_and_skips_where_it_cannot_look():
    r = check_clock(fake_run({"timedatectl show -p NTPSynchronized": SYNC_NO}), "linux")
    assert r["ok"] is False and r["detail"] == "NTP is not synchronised"
    assert check_clock(fake_run({}), "linux")["checked"] is False                    # no timedatectl
    w = check_clock(fake_run({"timedatectl show -p NTPSynchronized": SYNC_YES}), "win32")
    assert w == {"checked": False, "ok": None, "offset_s": None, "detail": "not checked (Linux only)"}
    only_flag = check_clock(fake_run({"timedatectl show -p NTPSynchronized": SYNC_YES}), "linux")
    assert only_flag["ok"] is True and only_flag["offset_s"] is None


def test_startup_check_alerts_once_a_day_when_the_clock_is_wrong(settings):
    n = Notifier()
    bad = fake_run({"timedatectl show -p NTPSynchronized": SYNC_NO})
    noon = datetime(2026, 10, 12, 12, tzinfo=IST)
    assert startup_check(settings, n, run=bad, now=noon, platform="linux")["ok"] is False
    startup_check(settings, n, run=bad, now=noon, platform="linux")
    assert len(n.sent) == 1 and "TOTP" in n.sent[0][1]
    startup_check(settings, n, run=bad, now=datetime(2026, 10, 13, 9, tzinfo=IST), platform="linux")
    assert len(n.sent) == 2
    good = fake_run({"timedatectl show -p NTPSynchronized": SYNC_YES})
    startup_check(settings, n, run=good, now=datetime(2026, 10, 14, 9, tzinfo=IST), platform="linux")
    startup_check(settings, n, run=lambda c: None, now=datetime(2026, 10, 14, 9, tzinfo=IST), platform="linux")
    assert len(n.sent) == 2                      # fine, or unknown: nothing sent


def test_the_integration_check_has_a_clock_step_that_fails_on_a_bad_clock(settings):
    s = settings
    s.market = "in"
    kw = dict(now=MON, holidays=Holidays(), session=FakeSession(), nse=FakeNSE(), bse=FakeBSE(), prices=FakePrices(),
              groww=lambda: FakeGrowwReads(), timeout=5)
    steps = by_name(run_check(s, clock=lambda: {"checked": True, "ok": True, "detail": "NTP synchronised, offset 3 ms"}, **kw))
    assert steps["Server clock"]["ok"] and "offset 3 ms" in steps["Server clock"]["detail"]
    steps = by_name(run_check(s, clock=lambda: {"checked": False, "ok": None, "detail": "not checked (Linux only)"}, **kw))
    assert steps["Server clock"]["skipped"] and steps["Server clock"]["detail"] == "not checked (Linux only)"
    r = run_check(s, clock=lambda: {"checked": True, "ok": False, "detail": "NTP is not synchronised"}, **kw)
    assert r["ok"] is False and "TOTP" in by_name(r)["Server clock"]["detail"]


# ------------------------------------------------------------------ backups
def test_saving_state_json_keeps_the_previous_file_as_a_bak(tmp_path):
    st = State(tmp_path / "state.json")
    st.data["marker"] = 1
    st.save()
    assert not (tmp_path / "state.json.bak").exists()           # nothing to keep the first time
    st.data["marker"] = 2
    st.save()
    assert json.loads((tmp_path / "state.json.bak").read_text())["marker"] == 1
    assert json.loads((tmp_path / "state.json").read_text())["marker"] == 2
    atomic_write(tmp_path / "other.json", "{}")
    atomic_write(tmp_path / "other.json", "{}")
    assert not (tmp_path / "other.json.bak").exists()           # only state.json
    assert not list(tmp_path.glob("*.tmp"))


def make_state(tmp_path):
    d = tmp_path / "state"
    (d / "prices").mkdir(parents=True)
    for name in ("state.json", "forward_x.json", "breadth.json", "groww_token.json", "groww_token.block", "tg_secret.json"):
        (d / name).write_text(json.dumps({"n": name}))
    (d / ".env").write_text("GROWW_API_KEY=should-never-be-copied")
    con = sqlite3.connect(d / "prices" / "archive.sqlite")
    con.execute("create table bars (s text, d text, c real)")
    con.execute("insert into bars values ('A','2026-10-09',1.5)")
    con.commit()
    con.close()
    return d


def test_daily_backup_copies_json_and_the_sqlite_archive_but_never_secrets(tmp_path):
    d = make_state(tmp_path)
    r = backup.backup_now(d, date(2026, 10, 12))
    day = d / "backups" / "20261012"
    assert sorted(p.name for p in day.iterdir()) == ["archive.sqlite", "breadth.json", "forward_x.json", "state.json"]
    assert r["archive"] is True and sorted(r["files"]) == ["breadth.json", "forward_x.json", "state.json"]
    assert not any("token" in p.name or "secret" in p.name or p.name.startswith(".env") for p in (d / "backups").rglob("*"))
    con = sqlite3.connect(day / "archive.sqlite")
    assert con.execute("select c from bars").fetchall() == [(1.5,)]
    con.close()
    assert not list(day.glob("*.tmp"))


def test_backups_keep_thirty_days(tmp_path):
    d = make_state(tmp_path)
    for day in ("20260901", "20260911", "20260912", "20261001"):
        (d / "backups" / day).mkdir(parents=True)
        (d / "backups" / day / "state.json").write_text("{}")
    (d / "backups" / "notes").mkdir()
    r = backup.backup_now(d, date(2026, 10, 12))                 # cutoff = 2026-09-12
    left = sorted(p.name for p in (d / "backups").iterdir())
    assert left == ["20260912", "20261001", "20261012", "notes"] and r["pruned"] == ["20260901", "20260911"]


def at(d, h, m):
    return datetime(2026, 10, d, h, m, tzinfo=IST)


def test_backup_scheduler_runs_once_after_the_close_on_trading_days(tmp_path):
    runs = []
    sch = backup.BackupScheduler(tmp_path, lambda: runs.append(1), holidays=Holidays({date(2026, 10, 20)}), threaded=False)
    assert sch.tick(at(12, 15, 59))["due"] is False
    assert sch.tick(at(12, 16, 0))["started"] and runs == [1]
    sch.tick(at(12, 17, 0))
    assert runs == [1] and (tmp_path / "backup_2026-10-12.claim").exists()
    assert sch.tick(at(20, 16, 30))["due"] is False                       # an exchange holiday
    assert sch.tick(at(17, 16, 30))["due"] is False                       # a Saturday


def test_the_watch_loop_ticks_the_backup_scheduler_and_survives_its_errors(settings):
    from trading_agent.watch import Watcher
    seen = []

    class Boom:
        def tick(self, now):
            seen.append(now)
            raise RuntimeError("disk full")

    w = Watcher(settings, backup=Boom(), awake=None, weekdays_only=False)
    assert "backup" not in w.tick() and len(seen) == 1
