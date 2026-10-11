"""Groww is used mainly while the market is live: the window, the saved close snapshot, the saved copy after hours.

Fakes only: a counting Groww client, a fake clock, a fake holiday calendar. No network, no .env, no orders."""
import dataclasses
import json
from datetime import date, datetime, timedelta

import pytest

import trading_agent.groww as groww_mod
import trading_agent.groww_hours as gh
import trading_agent.runner as runner
import trading_agent.watch as watch_mod
from trading_agent.broker import Position
from trading_agent.groww import GrowwTokenUnavailable
from trading_agent.groww_hours import CloseSnapshot, snapshot_covers, window_open
from trading_agent.timezones import IST
from trading_agent.watch import Watcher

CALLS: list[str] = []


def at(day, hh, mm=0):
    """day is 'YYYY-MM-DD' (2026-06-08 is a Monday)."""
    y, m, d = map(int, day.split("-"))
    return datetime(y, m, d, hh, mm, tzinfo=IST)


MON, TUE, FRI, SAT = "2026-06-08", "2026-06-09", "2026-06-12", "2026-06-13"


class Holidays:
    def __init__(self, closed=()):
        self.closed = {date.fromisoformat(c) for c in closed}

    def is_trading_day(self, d):
        return d.weekday() < 5 and d not in self.closed


class FakeGroww:
    def __init__(self, token, **kw):
        CALLS.append("GrowwBroker")

    def positions(self):
        CALLS.append("positions")
        return [Position("TCS", 10, 3000.0, 3300.0, sellable_qty=10)]


class NoYahoo:
    def __init__(self, *a, **k):
        pass

    def latest_price(self, symbol):
        raise LookupError("no BSE quote")


class Free:
    def latest_price(self, symbol):
        return 3500.0


@pytest.fixture(autouse=True)
def _reset():
    CALLS.clear()


@pytest.fixture
def linked(settings, monkeypatch):
    settings.groww_api_key, settings.groww_api_secret = "KEY-SECRET-VALUE", "s3cret-value"
    monkeypatch.setattr(groww_mod, "GrowwBroker", FakeGroww)
    monkeypatch.setattr(runner, "resolve_groww_token", lambda s, **k: "TOKEN")
    monkeypatch.setattr(runner, "YahooPrices", NoYahoo)
    return settings


def save_snap(settings, when, **kw):
    """A saved snapshot as the agent writes it, stamped ``when`` (the file's own saved_at)."""
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    body = {"saved_at": when.isoformat(timespec="seconds"),
            "holdings": [{"symbol": "TCS", "qty": 10, "avg_price": 3000.0, "sellable_qty": 10}], **kw}
    (settings.state_dir / runner.SNAPSHOT_FILE).write_text(json.dumps(body), encoding="utf-8")


def read(settings, now, **kw):
    return runner.read_groww_portfolio(settings, Free(), "stamp", now=now, holidays=kw.pop("holidays", Holidays()), **kw)


# --------------------------------------------------------------------------- the window
def test_window_edges_on_a_trading_day(settings):
    h = Holidays()
    assert not window_open(settings, at(TUE, 8, 29), h)
    assert window_open(settings, at(TUE, 8, 30), h)
    assert window_open(settings, at(TUE, 15, 40), h)
    assert window_open(settings, at(TUE, 16, 0), h)
    assert not window_open(settings, at(TUE, 16, 1), h)
    assert not window_open(settings, at(TUE, 23, 0), h)


def test_window_closed_on_weekend_and_holiday(settings):
    assert not window_open(settings, at(SAT, 11), Holidays())
    assert not window_open(settings, at(TUE, 11), Holidays(closed=[TUE]))


def test_window_follows_the_settings(settings):
    s = dataclasses.replace(settings, groww_window_start="09:00", groww_window_end="15:00")
    assert not window_open(s, at(TUE, 8, 45), Holidays())
    assert window_open(s, at(TUE, 9, 0), Holidays())
    assert not window_open(s, at(TUE, 15, 30), Holidays())


def test_window_settings_from_the_environment(monkeypatch, tmp_path):
    from trading_agent.config import load_settings
    monkeypatch.setenv("GROWW_WINDOW_START", "08:45")
    monkeypatch.setenv("GROWW_WINDOW_END", "15:45")
    s = load_settings(None)
    assert (s.groww_window_start, s.groww_window_end) == ("08:45", "15:45")
    monkeypatch.setenv("GROWW_WINDOW_END", "08:00")
    with pytest.raises(SystemExit):
        load_settings(None)
    monkeypatch.setenv("GROWW_WINDOW_END", "late")
    with pytest.raises(SystemExit):
        load_settings(None)
    monkeypatch.delenv("GROWW_WINDOW_START")
    monkeypatch.delenv("GROWW_WINDOW_END")
    s = load_settings(None)
    assert (s.groww_window_start, s.groww_window_end) == ("08:30", "16:00")


# --------------------------------------------------------------------------- the snapshot covers the latest session
def test_snapshot_coverage(settings):
    h = Holidays()
    now = at(TUE, 18, 0)   # the latest completed session is Tuesday itself
    assert snapshot_covers({"saved_at": at(TUE, 15, 40).isoformat()}, now, h)
    assert not snapshot_covers({"saved_at": at(TUE, 12, 0).isoformat()}, now, h)           # saved before the close
    assert snapshot_covers({"saved_at": at(TUE, 12, 0).isoformat(), "kind": "close", "session": TUE}, now, h)
    assert not snapshot_covers({"saved_at": at(MON, 15, 40).isoformat(), "kind": "close", "session": MON}, now, h)
    assert not snapshot_covers({"saved_at": at(MON, 15, 40).isoformat()}, now, h)         # yesterday's
    assert not snapshot_covers(None, now, h)


def test_weekend_uses_fridays_close(settings):
    h = Holidays()
    assert snapshot_covers({"saved_at": at(FRI, 15, 40).isoformat()}, at(SAT, 12), h)
    assert not snapshot_covers({"saved_at": at(FRI, 12, 0).isoformat()}, at(SAT, 12), h)


def test_holiday_monday_still_uses_fridays_close(settings):
    h = Holidays(closed=[MON])
    assert snapshot_covers({"saved_at": at("2026-06-05", 15, 40).isoformat()}, at(MON, 11), h)   # Friday 5th
    assert gh.latest_session(at(MON, 11), h) == date(2026, 6, 5)


# --------------------------------------------------------------------------- read_groww_portfolio outside the window
def test_inside_the_window_it_reads_groww_and_saves(linked):
    out = read(linked, at(TUE, 11))
    assert CALLS.count("positions") == 1 and out["value"] == 33000.0
    assert "source" not in out
    assert runner.load_groww_snapshot(linked) is not None


def test_after_hours_with_a_covering_snapshot_never_calls_groww(linked):
    save_snap(linked, at(TUE, 15, 41), kind="close", session=TUE)
    out = read(linked, at(TUE, 19, 0))
    assert CALLS == []
    assert out["source"] == "saved" and out["market_closed"] is True and "error" not in out
    assert out["reason"] == "market closed — holdings saved at the 09 Jun 2026 close"
    assert out["prices"] == "yahoo (delayed)"
    assert out["holdings"][0]["price"] == 3500.0   # priced from the free source
    assert out["linked"] is True


def test_weekend_with_fridays_close_never_calls_groww(linked):
    save_snap(linked, at(FRI, 15, 41))
    out = read(linked, at(SAT, 10))
    assert CALLS == [] and out["source"] == "saved"
    assert "12 Jun 2026" in out["reason"]


def test_holiday_never_calls_groww_when_the_snapshot_is_current(linked):
    save_snap(linked, at("2026-06-05", 15, 41), kind="close", session="2026-06-05")
    out = read(linked, at(MON, 11), holidays=Holidays(closed=[MON]))
    assert CALLS == [] and out["source"] == "saved" and out["market_closed"]


def test_no_covering_snapshot_allows_one_read_a_day(linked):
    save_snap(linked, at(TUE, 12, 0))                    # saved mid-session: does not stand for the close
    first = read(linked, at(TUE, 18, 0))
    assert CALLS.count("positions") == 1 and "source" not in first and first["value"] == 33000.0
    assert (linked.state_dir / gh.OFFHOURS_FILE).exists()
    # the fresh read was saved after the close, so it now covers; and even without that the day's read is spent
    second = read(linked, at(TUE, 20, 0))
    assert CALLS.count("positions") == 1 and second["source"] == "saved"


def test_the_daily_fallback_is_spent_even_when_it_fails(linked, monkeypatch):
    save_snap(linked, at(TUE, 12, 0))

    def boom(*a, **k):
        CALLS.append("positions")
        raise RuntimeError("down")
    monkeypatch.setattr(FakeGroww, "positions", boom)
    first = read(linked, at(TUE, 18, 0))
    assert first["source"] == "saved" and "down" in first["reason"]
    second = read(linked, at(TUE, 18, 30))
    assert CALLS.count("positions") == 1               # not asked again the same day
    assert second["source"] == "saved" and second["market_closed"] is True
    third = read(linked, at("2026-06-10", 20, 0))      # the next day it may try again
    assert CALLS.count("positions") == 2 and third["source"] == "saved"


def test_no_snapshot_at_all_after_a_spent_fallback_is_a_plain_message(linked, monkeypatch):
    monkeypatch.setattr(FakeGroww, "positions", lambda self: (_ for _ in ()).throw(RuntimeError("down")))
    first = read(linked, at(TUE, 18, 0))
    assert "error" in first
    again = read(linked, at(TUE, 19, 0))
    assert again["linked"] is True and "next open" in again["error"]


def test_force_bypasses_the_gate(linked):
    save_snap(linked, at(TUE, 15, 41), kind="close", session=TUE)
    out = read(linked, at(TUE, 22, 0), force=True)
    assert CALLS.count("positions") == 1 and "source" not in out
    assert not (linked.state_dir / gh.OFFHOURS_FILE).exists()   # a forced read does not use up the daily one


def test_force_still_respects_the_cooldown(linked, monkeypatch):
    save_snap(linked, at(TUE, 15, 41), kind="close", session=TUE)
    until = at(TUE, 23, 0) + timedelta(hours=2)

    def blocked(settings, **k):
        raise GrowwTokenUnavailable("cooling down", until=until)
    monkeypatch.setattr(runner, "resolve_groww_token", blocked)
    out = read(linked, at(TUE, 22, 0), force=True)
    assert CALLS == []                                      # no client, so no Groww call
    assert out["source"] == "saved" and out["blocked_until"]


def test_not_linked_is_unchanged(settings):
    assert read(settings, at(TUE, 22))["linked"] is False


# --------------------------------------------------------------------------- the close snapshot
def make_close(linked, results):
    reads = []

    def read_fn(day):
        reads.append(day)
        r = results[min(len(reads) - 1, len(results) - 1)]
        if isinstance(r, Exception):
            raise r
        return r
    return CloseSnapshot(linked, read_fn, Holidays()), reads


GOOD = {"linked": True, "holdings": []}


def test_close_snapshot_only_in_its_slice(linked):
    c, reads = make_close(linked, [GOOD])
    assert c.tick(at(TUE, 15, 34)) is None and c.tick(at(TUE, 15, 51)) is None and c.tick(at(TUE, 11)) is None
    assert c.tick(at(SAT, 15, 40)) is None
    assert reads == []
    assert c.tick(at(MON, 15, 40))["done"] is True
    assert reads == [date(2026, 6, 8)]       # Monday 15:40 was the first (and only) valid tick


def test_close_snapshot_once_per_session(linked):
    c, reads = make_close(linked, [GOOD])
    assert c.tick(at(TUE, 15, 35))["done"] is True
    for m in (36, 40, 49):
        assert c.tick(at(TUE, 15, m)) == {"session": TUE, "done": True}
    assert reads == [date(2026, 6, 9)]
    c2, reads2 = make_close(linked, [GOOD])         # a restarted service reads the record from disk
    c2.tick(at(TUE, 15, 45))
    assert reads2 == []
    c2.tick(at("2026-06-10", 15, 40))               # next session: once more
    assert reads2 == [date(2026, 6, 10)]


def test_close_snapshot_retries_then_gives_up_after_three(linked):
    bad = {"linked": True, "error": "boom"}
    c, reads = make_close(linked, [bad])
    assert c.tick(at(TUE, 15, 35))["done"] is False
    assert c.tick(at(TUE, 15, 36)).get("waiting")          # too soon after the last try
    assert len(reads) == 1
    c.tick(at(TUE, 15, 39))
    c.tick(at(TUE, 15, 43))
    assert len(reads) == 3
    assert c.tick(at(TUE, 15, 47)).get("gave_up")
    assert len(reads) == 3                                  # never more than 3 attempts


def test_close_snapshot_retry_succeeds(linked):
    c, reads = make_close(linked, [RuntimeError("down"), {"linked": True, "source": "saved", "reason": "x"}, GOOD])
    c.tick(at(TUE, 15, 35))
    c.tick(at(TUE, 15, 39))       # a saved copy returned instead of a live read is not a success
    assert c.tick(at(TUE, 15, 43))["done"] is True
    assert len(reads) == 3
    c.tick(at(TUE, 15, 48))
    assert len(reads) == 3


def test_close_snapshot_needs_credentials_and_a_trading_day(settings):
    c, reads = make_close(settings, [GOOD])
    assert c.tick(at(TUE, 15, 40)) is None and reads == []
    settings.groww_api_key, settings.groww_api_secret = "K", "S"
    c = CloseSnapshot(settings, lambda d: reads.append(d) or GOOD, Holidays(closed=[TUE]))
    assert c.tick(at(TUE, 15, 40)) is None and reads == []


def test_close_snapshot_outside_a_narrowed_window_does_nothing(linked):
    s = dataclasses.replace(linked, groww_window_end="15:30")
    c, reads = make_close(s, [GOOD])
    assert c.tick(at(TUE, 15, 40)) is None and reads == []


def test_the_close_read_is_saved_as_a_close_snapshot(linked):
    out = runner.read_groww_portfolio(linked, Free(), "s", force=True, close_session=date(2026, 6, 9),
                                      now=at(TUE, 15, 40), holidays=Holidays())
    assert out["value"] == 33000.0
    snap = runner.load_groww_snapshot(linked)
    assert snap["kind"] == "close" and snap["session"] == TUE
    snap["saved_at"] = at(TUE, 15, 40).isoformat()      # the file's own stamp is the real clock; pin it to the fake one
    assert snapshot_covers(snap, at(SAT, 12), Holidays()) is False       # Tuesday's close; Friday is the latest
    assert snapshot_covers(snap, at("2026-06-10", 8, 0), Holidays())      # covers until the next close
    runner.read_groww_portfolio(linked, Free(), "s", now=at(TUE, 11), holidays=Holidays())
    assert "kind" not in runner.load_groww_snapshot(linked)               # an ordinary read is not a close snapshot


# --------------------------------------------------------------------------- other Groww callers
class Clock(datetime):
    fixed = at(TUE, 18, 0)

    @classmethod
    def now(cls, tz=None):
        return cls.fixed.astimezone(tz) if tz else cls.fixed


class LiveBroker:
    name = "groww"
    live_orders = True

    def __init__(self):
        self.n = 0

    def positions(self):
        self.n += 1
        return [Position("TCS", 10, 3000.0, 100.0)]


def live_settings(settings):
    return dataclasses.replace(settings, broker="groww", groww_live_orders=True)


def test_watch_pauses_a_live_broker_outside_the_window(settings, monkeypatch):
    s = live_settings(settings)
    monkeypatch.setattr(watch_mod, "datetime", Clock)
    b, checks = LiveBroker(), []
    w = Watcher(s, every=15, awake=None, broker=b, window=("08:45", "18:30"), holidays=Holidays(),
                check_fn=lambda: checks.append(1), prices=None)
    info = w.tick()
    assert info["in_window"] is True and "groww_paused" in info
    assert b.n == 0 and checks == []                      # no stop check, no order sync, no check, no positions
    assert info["stop_hits"] == [] and "live" not in info


def test_watch_still_uses_a_live_broker_inside_the_window(settings, monkeypatch):
    s = live_settings(settings)
    Clock.fixed = at(TUE, 11, 0)
    try:
        monkeypatch.setattr(watch_mod, "datetime", Clock)
        b, checks = LiveBroker(), []
        w = Watcher(s, every=15, awake=None, broker=b, window=("08:45", "18:30"), holidays=Holidays(),
                    check_fn=lambda: checks.append(1), prices=None)
        info = w.tick()
        assert "groww_paused" not in info and b.n >= 1 and checks == [1]
    finally:
        Clock.fixed = at(TUE, 18, 0)


def test_watch_ticks_the_close_snapshot_even_if_a_tick_is_otherwise_skipped(settings, monkeypatch):
    class Spy:
        def __init__(self):
            self.at = []

        def tick(self, now):
            self.at.append(now)
            return {"done": True}
    Clock.fixed = at(TUE, 15, 40)
    try:
        monkeypatch.setattr(watch_mod, "datetime", Clock)
        spy = Spy()
        w = Watcher(settings, every=15, awake=None, close_snapshot=spy, window=("09:00", "10:00"), holidays=Holidays())
        info = w.tick()
        assert info["skipped"] is True and info["close_snapshot"] == {"done": True} and len(spy.at) == 1
    finally:
        Clock.fixed = at(TUE, 18, 0)


def test_practice_broker_prices_from_the_free_source_outside_the_window(linked, monkeypatch):
    linked.broker = "groww"
    made = []

    class G:
        def latest_price(self, symbol):
            made.append(symbol)
            return 111.0
    monkeypatch.setattr(runner, "make_groww", lambda *a, **k: made.append("client") or G())
    monkeypatch.setattr(gh, "now_ist", lambda: at(TUE, 19, 0))
    b = runner.make_practice_broker(linked, lambda sym: 222.0)
    assert b.latest_price("TCS") == 222.0 and made == []         # no client built, no Groww price asked
    monkeypatch.setattr(gh, "now_ist", lambda: at(TUE, 11, 0))
    assert b.latest_price("TCS") == 111.0 and made == ["client", "TCS"]


def test_the_integration_canary_skips_groww_outside_the_window(settings, monkeypatch):
    from tests.test_integration import (FakeBSE, FakeNSE, FakePrices, FakeSession, Holidays as IH, MON, by_name)
    from trading_agent import integration
    s = dataclasses.replace(settings, market="in")
    asked = []
    monkeypatch.setattr(integration, "cached_groww", lambda st, **k: asked.append(1))
    late = MON.replace(hour=18, minute=0)
    r = integration.run_check(s, now=late, holidays=IH(), session=FakeSession(), nse=FakeNSE(), bse=FakeBSE(),
                              prices=FakePrices(), claude=lambda x: pytest.fail("no Claude"), timeout=5)
    steps = by_name(r)
    assert asked == []
    assert "Groww window" in steps["Groww (read-only)"]["detail"] and steps["Groww (read-only)"]["ok"]
    monkeypatch.setattr(gh, "now_ist", lambda: late)
    early = MON.replace(hour=8, minute=36)
    integration.run_check(s, now=early, holidays=IH(), session=FakeSession(), nse=FakeNSE(), bse=FakeBSE(),
                          prices=FakePrices(), claude=lambda x: pytest.fail("no Claude"), timeout=5)
    assert asked          # inside the window it asks for the cached client as before


def test_the_digest_reads_groww_through_the_gate(linked):
    """The digest's default holdings source is runner.read_groww_portfolio: after hours it serves the saved copy."""
    import inspect

    from trading_agent import digest
    assert "read_groww_portfolio" in inspect.getsource(digest.make_context)
    save_snap(linked, at(TUE, 15, 41), kind="close", session=TUE)
    out = read(linked, at(TUE, 15, 46) + timedelta(hours=3))
    assert CALLS == [] and out["source"] == "saved"
