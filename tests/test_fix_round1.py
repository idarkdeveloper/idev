"""Fix round 1: the overdue/in-flight race, retries before 09:00, the live-orders failsafe without the watch service,
heartbeat progress and the cached calendar, start-up resilience, clock parser, breaker scope, backups.
Fakes only: no network, no commands, no real keys."""
import dataclasses
import json
import sqlite3
import threading
import time
from datetime import date
from pathlib import Path

import pytest

from trading_agent import backup, clockcheck, integration
from trading_agent.circuit import CircuitBreaker, CircuitOpen
from trading_agent.clockcheck import check_clock
from trading_agent.holidays import NSEHolidays
from trading_agent.integration import IntegrationScheduler, buy_block, mark_overdue, run_and_record
from trading_agent.prices import YahooPrices

from tests.conftest import FakeResponse
from tests.test_integration import (FakeBSE, FakeNSE, FakePrices, FakeSession, Holidays, Notifier, at)
from tests.test_resilience import SYNC_YES, fake_run


@pytest.fixture
def s(settings):
    return dataclasses.replace(settings, market="in", resend_api_key="re_fake", notify_email_to="me@example.com")


def write_result(s, ok, when):
    s.state_dir.mkdir(parents=True, exist_ok=True)
    (s.state_dir / "integration_check.json").write_text(
        json.dumps({"at": when.isoformat(), "ok": ok, "steps": [{"name": "x", "ok": ok, "ms": 1, "detail": "d"}]}))


# ------------------------------------------------------------------ 1. overdue vs a run in flight; own alert marker
def test_overdue_is_not_checked_while_a_run_is_in_flight(tmp_path):
    release = threading.Event()
    calls = []
    sch = IntegrationScheduler(tmp_path, lambda: release.wait(5), holidays=Holidays(),
                               overdue_fn=lambda now: calls.append(now) or False)
    out = sch.tick(at(2026, 10, 12, 9, 15))
    assert out.get("started") and calls == []                   # the thread is alive: no "did not run" yet
    sch.tick(at(2026, 10, 12, 9, 16))
    assert calls == []
    release.set()
    sch.wait(5)
    sch.tick(at(2026, 10, 12, 9, 17))
    assert len(calls) == 1                                      # finished: the check applies again


def test_overdue_is_checked_when_the_claim_belongs_to_a_crashed_process(tmp_path):
    (tmp_path / "integration_2026-10-12.claim").write_text("1 1")
    calls = []
    sch = IntegrationScheduler(tmp_path, lambda: None, holidays=Holidays(), threaded=False,
                               overdue_fn=lambda now: calls.append(1) or False)
    sch.tick(at(2026, 10, 12, 9, 15))                           # the claim is not ours: day marked done, nothing running
    assert calls == [1]


def test_the_overdue_alert_has_its_own_marker_so_both_alerts_go_out(s):
    n = Notifier()
    assert mark_overdue(s, n, at(2026, 10, 12, 9, 20), Holidays()) is True
    run_and_record(s, notifier=n, now=at(2026, 10, 12, 9, 40), holidays=Holidays(), session=FakeSession(),
                   nse=FakeNSE(deals_error="x"), bse=FakeBSE(), prices=FakePrices(), groww=lambda: None, timeout=5)
    assert len(n.sent) == 2
    assert sorted(p.name for p in s.state_dir.glob("integration_*.sent")) == [
        "integration_alert_2026-10-12.sent", "integration_overdue_2026-10-12.sent"]


# ------------------------------------------------------------------ 2. a failure before 09:00 is retried
def fake_run_check(results):
    def run_check(settings, **kw):
        return results.pop(0) if len(results) > 1 else results[0]
    return run_check


def result(ok):
    return {"at": "2026-10-12T08:30:00+05:30", "last_trading_day": "2026-10-09", "ok": ok,
            "steps": [{"name": "NSE deals", "ok": ok, "ms": 1, "detail": "d"}]}


def test_a_failure_before_0900_raises_so_the_scheduler_retries_and_a_pass_clears_the_block(s, monkeypatch):
    monkeypatch.setattr(integration, "run_check", fake_run_check([result(False), result(False), result(True)]))
    clock = {"t": 0.0}
    now = [at(2026, 10, 12, 8, 30)]
    n = Notifier()
    sch = integration.make_scheduler(s, n, Holidays(), now_fn=lambda: now[0])
    sch.threaded, sch._clock = False, lambda: clock["t"]
    sch.tick(now[0])
    assert buy_block(s) == "pre-market check failed: NSE deals"          # failed once: buys paused meanwhile
    assert not (s.state_dir / "integration_2026-10-12.claim").exists()    # released for a retry
    now[0], clock["t"] = at(2026, 10, 12, 8, 35), 300
    assert sch.tick(now[0]).get("waiting")                               # not before the 10 minute gap
    now[0], clock["t"] = at(2026, 10, 12, 8, 40), 600
    sch.tick(now[0])                                                     # second try fails too
    now[0], clock["t"] = at(2026, 10, 12, 8, 50), 1200
    sch.tick(now[0])                                                     # third try passes
    assert buy_block(s) is None and sch._tries["2026-10-12"] == 3


def test_a_failure_after_0900_stands_and_is_not_retried(s, monkeypatch):
    monkeypatch.setattr(integration, "run_check", fake_run_check([result(False)]))
    now = [at(2026, 10, 12, 9, 5)]
    sch = integration.make_scheduler(s, Notifier(), Holidays(), now_fn=lambda: now[0])
    sch.threaded = False
    sch.tick(now[0])
    assert buy_block(s) == "pre-market check failed: NSE deals"
    assert (s.state_dir / "integration_2026-10-12.claim").exists()       # kept: no retry
    sch._clock = lambda: 10_000.0
    sch.tick(at(2026, 10, 12, 9, 30))
    assert sch._tries["2026-10-12"] == 1


# ------------------------------------------------------------------ 3. live orders need a passing result today
def test_live_orders_need_a_passing_result_today_after_0910_without_the_watch_service(s):
    s.groww_live_orders = True
    tue = lambda h, m: at(2026, 10, 13, h, m)                          # noqa: E731
    assert buy_block(s, tue(9, 9)) is None                             # before 09:10 nothing is required yet
    assert buy_block(s, tue(9, 10)) == "pre-market check has not passed today"
    write_result(s, True, at(2026, 10, 12, 8, 30))                     # yesterday's pass does not count
    assert buy_block(s, tue(10, 0)) == "pre-market check has not passed today"
    write_result(s, False, tue(8, 30))
    assert buy_block(s, tue(10, 0)) == "pre-market check has not passed today"
    write_result(s, True, tue(8, 30))
    assert buy_block(s, tue(10, 0)) is None
    assert buy_block(s, at(2026, 10, 17, 11, 0)) is None               # a Saturday
    s.groww_live_orders = False
    (s.state_dir / "integration_check.json").unlink()
    assert buy_block(s, tue(11, 0)) is None                            # paper and practice are unaffected
    s.groww_live_orders, s.integration_check = True, False
    assert buy_block(s, tue(11, 0)) is None


def test_the_live_gate_leaves_the_state_alone(s):
    s.groww_live_orders = True
    before = sorted(p.name for p in s.state_dir.glob("*")) if s.state_dir.exists() else []
    buy_block(s, at(2026, 10, 13, 11, 0))
    assert (sorted(p.name for p in s.state_dir.glob("*")) if s.state_dir.exists() else []) == before


# ------------------------------------------------------------------ 4. progress stamps and the cached calendar
def test_a_long_tick_that_keeps_moving_stamps_progress_between_its_steps(s, monkeypatch):
    from trading_agent import watch
    from trading_agent.watch import Watcher
    ticker = iter(range(1, 10_000))

    class T:
        def __getattr__(self, name):
            return getattr(time, name)

        @staticmethod
        def monotonic():
            return float(next(ticker))

    monkeypatch.setattr(watch, "time", T())
    w = Watcher(s, awake=None, weekdays_only=False, window=("00:00", "23:59"))
    seen = {}
    w.poll_announcements = lambda: seen.setdefault("ann", w._progress) and []
    w.poll_news = lambda: seen.setdefault("news", w._progress) and []
    w.check_trailing_stops = lambda: seen.setdefault("stops", w._progress) and []
    w.sync_live = lambda: seen.setdefault("live", w._progress) and None
    w._check_fn = lambda: seen.setdefault("check", w._progress)
    w._progress = 0.0
    w.tick(force=True)
    assert 0 < seen["ann"] < seen["stops"] < seen["live"] <= w._progress      # stamped between the sub-steps


class ExplodingClient:
    def _get(self, *a, **k):
        raise AssertionError("the network was touched")


def test_the_window_test_uses_cached_holidays_only(tmp_path):
    from trading_agent.watch import _CachedCalendar
    h = NSEHolidays(client=ExplodingClient(), cache_dir=tmp_path)
    cal = _CachedCalendar(h)
    assert cal.is_trading_day(date(2026, 10, 12)) is True              # nothing cached: weekdays, no fetch
    assert cal.is_trading_day(date(2026, 10, 10)) is False             # Saturday
    (tmp_path / "nse_holidays.json").write_text(json.dumps({"2026-10-20": "Diwali"}))
    old = time.time() - 30 * 86400
    import os
    os.utime(tmp_path / "nse_holidays.json", (old, old))              # even an old cache is used, not refreshed
    assert _CachedCalendar(NSEHolidays(client=ExplodingClient(), cache_dir=tmp_path)).is_trading_day(date(2026, 10, 20)) is False
    assert _CachedCalendar(None).is_trading_day(date(2026, 10, 12)) is True


def test_the_watch_hands_the_heartbeat_the_cached_calendar(s):
    from trading_agent.watch import Watcher
    got = {}

    class Real:
        def is_trading_day(self, d):
            raise AssertionError("the heartbeat thread must not call the fetching method")

        def peek_trading_day(self, d):
            return True

    class HB:
        def start(self, progress, *, stall_s, stop, window=None, loop_every=None):
            got["inside"] = window()
            w._stop.set()

        def stop(self):
            pass

    w = Watcher(s, every=60, awake=None, heartbeat=HB(), holidays=Real())
    w.run_forever()
    assert isinstance(got["inside"], bool)


# ------------------------------------------------------------------ 5. the watch always starts
def test_clock_command_errors_are_swallowed(monkeypatch):
    import subprocess
    for exc in (UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad"), ValueError("embedded null"), OSError("nope"),
                subprocess.TimeoutExpired("x", 5)):
        def boom(*a, exc=exc, **k):
            raise exc
        monkeypatch.setattr(clockcheck.subprocess, "run", boom)
        assert clockcheck._run(["timedatectl"]) is None


def test_the_watch_starts_even_if_the_clock_check_blows_up(s, monkeypatch, capsys):
    from trading_agent import cli
    monkeypatch.setattr(cli, "_settings", lambda args: s)
    s.broker, s.data_source = "local", "nse"
    s.state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("trading_agent.watch.Watcher.run_forever", lambda self: ran.append(1))
    monkeypatch.setattr("trading_agent.clockcheck.startup_check", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(cli, "_market_holidays", lambda settings: Holidays())
    ran = []
    args = cli.build_parser().parse_args(["watch"])
    assert args.func(args) == 0 and ran == [1]
    assert "Watching" in capsys.readouterr().out


# ------------------------------------------------------------------ 6. clock parser
@pytest.mark.parametrize("line,expected", [("+1min 2.345s", 62.345), ("-1min 30s", -90.0), ("+250us", 0.00025),
                                           ("-12ms", -0.012), ("+3.2ms", 0.0032)])
def test_clock_offset_units_include_minutes(line, expected):
    r = check_clock(fake_run({"timedatectl show -p NTPSynchronized": SYNC_YES,
                              "timedatectl timesync-status": (0, f"   Offset: {line}\n")}), "linux")
    assert abs(r["offset_s"] - expected) < 1e-9
    assert r["ok"] is (abs(expected) <= 1.0)


def test_an_offset_line_that_cannot_be_read_fails_the_check():
    r = check_clock(fake_run({"timedatectl show -p NTPSynchronized": SYNC_YES,
                              "timedatectl timesync-status": (0, "Offset: wobbly\n")}), "linux")
    assert r["ok"] is False and r["detail"] == "offset unreadable"
    r = check_clock(fake_run({"timedatectl show -p NTPSynchronized": SYNC_YES, "timedatectl timesync-status": (1, ""),
                              "chronyc tracking": (0, "System time     : lots of seconds off\n")}), "linux")
    assert r["ok"] is False and r["detail"] == "offset unreadable"
    r = check_clock(fake_run({"timedatectl show -p NTPSynchronized": SYNC_YES,
                              "timedatectl timesync-status": (0, "Server: x\n")}), "linux")   # no Offset line at all: fine
    assert r["ok"] is True


# ------------------------------------------------------------------ 7. paper prices are not behind the Yahoo breaker
def test_yahoo_latest_price_ignores_the_breaker_and_does_not_feed_it():
    class Sess:
        def __init__(self):
            self.n = 0

        def get(self, url, **kw):
            self.n += 1
            if "range" in kw.get("params", {}) and kw["params"]["range"] == "1d":
                return FakeResponse({"chart": {"result": [{"meta": {"regularMarketPrice": 123.5}}]}})
            return FakeResponse({}, 500)

    inner = Sess()
    p = YahooPrices(session=inner)
    for _ in range(3):
        with pytest.raises(Exception):
            p.history("ABC", "1mo")
    assert p.breaker.degraded
    with pytest.raises(CircuitOpen):
        p.history("ABC", "1mo")                                        # history is paused ...
    assert p("ABC") == 123.5 and p.latest_price("ABC") == 123.5        # ... the paper price is not
    assert p.breaker.degraded                                          # and it did not close the breaker either


def test_the_breaker_docstring_describes_what_it_does():
    import trading_agent.circuit as c
    assert "no single probe" in c.__doc__ and "one call is let through" not in c.__doc__
    b = CircuitBreaker("X", clock=lambda: 0.0)
    for _ in range(3):
        b.failure()
    with pytest.raises(CircuitOpen):
        b.before()


# ------------------------------------------------------------------ 8. only the watch publishes the breaker state
def test_only_the_watch_passes_a_breaker_file(s, tmp_path):
    from trading_agent.runner import make_data_source
    s.data_source, s.bse_deals, s.state_dir = "nse", False, tmp_path
    assert make_data_source(s).breaker.state_file is None             # the dashboard's client
    f = tmp_path / "nse_breaker.json"
    assert make_data_source(s, breaker_file=f).breaker.state_file == f
    import inspect
    from trading_agent import cli
    src = inspect.getsource(cli.cmd_watch)
    assert 'breaker_file=settings.state_dir / "nse_breaker.json"' in src
    assert "breaker_file" not in inspect.getsource(cli.cmd_ui)


# ------------------------------------------------------------------ 10. pinned dev tools
def test_dev_requirements_are_pinned_exactly():
    lines = [x.strip() for x in (Path(__file__).resolve().parents[1] / "requirements-dev.txt").read_text().splitlines() if x.strip()]
    assert lines and all("==" in x and not any(c in x for c in "<>~") for x in lines)
    assert {x.split("==")[0] for x in lines} == {"ruff", "mypy", "types-requests", "pytest-xdist"}


# ------------------------------------------------------------------ 11. backups
def make_state(tmp_path):
    d = tmp_path / "state"
    (d / "prices").mkdir(parents=True)
    (d / "forward").mkdir()
    (d / "state.json").write_text("{}")
    (d / "forward" / "NIFTYMIDCAP150.json").write_text('{"a": 1}')
    (d / "forward" / "notes.txt").write_text("ignored")
    con = sqlite3.connect(d / "prices" / "archive.sqlite")
    con.execute("create table b (x)")
    con.commit()
    con.close()
    return d


def test_backups_include_the_forward_account_files(tmp_path):
    d = make_state(tmp_path)
    r = backup.backup_now(d, date(2026, 10, 12))
    day = d / "backups" / "20261012"
    assert (day / "forward" / "NIFTYMIDCAP150.json").read_text() == '{"a": 1}' and not (day / "forward" / "notes.txt").exists()
    assert "forward/NIFTYMIDCAP150.json" in r["files"]


def test_price_archive_copies_go_after_seven_days_but_the_json_stays_thirty(tmp_path):
    d = make_state(tmp_path)
    for day in ("20260920", "20261004", "20261010"):
        (d / "backups" / day).mkdir(parents=True)
        (d / "backups" / day / "state.json").write_text("{}")
        (d / "backups" / day / "archive.sqlite").write_text("x")
    backup.backup_now(d, date(2026, 10, 12))                           # archive cutoff 20261005, json cutoff 20260912
    assert not (d / "backups" / "20260920" / "archive.sqlite").exists() and (d / "backups" / "20260920" / "state.json").exists()
    assert not (d / "backups" / "20261004" / "archive.sqlite").exists()
    assert (d / "backups" / "20261010" / "archive.sqlite").exists() and (d / "backups" / "20261012" / "archive.sqlite").exists()


def test_a_failed_copy_leaves_no_tmp_file(tmp_path, monkeypatch):
    import shutil

    def half(src, tmp, *a, **k):
        Path(tmp).write_text("partial")
        raise OSError("disk full")
    monkeypatch.setattr(shutil, "copy2", half)
    src = tmp_path / "a.json"
    src.write_text("{}")
    with pytest.raises(OSError):
        backup._copy_atomic(src, tmp_path / "b.json")
    assert not list(tmp_path.glob("*.tmp")) and not (tmp_path / "b.json").exists()
    d = make_state(tmp_path / "x")
    backup.backup_now(d, date(2026, 10, 12))                           # a failing copy is logged, never raised
    assert not list((d / "backups").rglob("*.tmp"))
