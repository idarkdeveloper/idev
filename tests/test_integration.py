"""The live integration check (integration.py), run entirely on fakes: no network, no real keys, no orders.

Covers: each step ok / fail / timeout and independence; the Groww step skipped without a cached token and never
generating one; the read-only guards; the scheduler's claim and holiday guards; one alert a day; the dashboard line;
the settings keys, the CLI and the dashboard endpoints."""
import dataclasses
import json
import threading
from datetime import date, datetime, timedelta

import pytest

from trading_agent import integration
from trading_agent.groww import GrowwBroker, TokenCache
from trading_agent.integration import (IntegrationScheduler, OrderBlocked, ReadOnlyGroww, ReadOnlySession,
                                       cached_groww, run_check, run_and_record, status_line)
from trading_agent.timezones import IST

from tests.conftest import FakeResponse
from tests.test_ui import server  # noqa: F401  (the offline dashboard fixture)

MON = datetime(2026, 10, 12, 8, 36, tzinfo=IST)     # a Monday: the last trading day is Friday 2026-10-09
LAST = "2026-10-09"


class Holidays:
    def __init__(self, closed=()):
        self.closed = set(closed)

    def is_trading_day(self, d):
        return d.weekday() < 5 and d not in self.closed


BANDS = "Symbol,Series,Security Name,Band,Remarks\nAAA,EQ,Aaa Ltd,5,\nBBB,EQ,Bbb Ltd,No Band,\n"
BHAV = ("SYMBOL, SERIES, DATE1, PREV_CLOSE, CLOSE_PRICE\n"
        "AAA, EQ, 09-Oct-2026, 100.0, 101.0\nBBB, EQ, 09-Oct-2026, 50.0, 49.0\n")


class FakeSession:
    """GET-only archive host: routes by a word in the URL."""

    def __init__(self, bands=BANDS, bhav=BHAV):
        self.bands, self.bhav, self.calls = bands, bhav, []

    def get(self, url, **kw):
        self.calls.append(url)
        if "sec_list" in url:
            return FakeResponse(self.bands)
        if "sec_bhavdata_full" in url:
            return FakeResponse(self.bhav)
        return FakeResponse("", 404)

    def request(self, method, url, **kw):
        assert method == "GET", "the integration check must only GET"
        return self.get(url, **kw)

    def post(self, *a, **k):
        raise AssertionError("the integration check must never POST")


class FakeNSE:
    def __init__(self, deals_error=None, ann=None, hang=None):
        self.deals_error, self.hang = deals_error, hang
        self.ann = [{"symbol": "ABC", "at": "2026-10-09 17:00"}] if ann is None else ann

    def probe_deals(self, day):
        assert day == date(2026, 10, 9)
        if self.deals_error:
            raise ValueError(self.deals_error)
        return {"bulk": 3, "block": 1}

    def announcements(self, symbol=None, limit=20):
        if self.hang is not None:
            self.hang.wait(5)
        return self.ann


class FakeBSE:
    def __init__(self, error=None):
        self.error, self.asked = error, []

    def fetch_range(self, start, end):
        self.asked.append((start, end))
        if self.error:
            raise RuntimeError(self.error)
        return [{"date": "2026-10-09"}]


class FakePrices:
    def __init__(self, missing=None):
        self.missing = missing

    def history(self, symbol, range_="1mo"):
        if symbol == self.missing:
            return [{"date": "2026-10-08", "close": 1.0}]
        return [{"date": "2026-10-08", "close": 1.0}, {"date": LAST, "close": 2.0}]


class FakeGrowwReads:
    def __init__(self, holdings=None, orders=None, error=None, cash_error=None):
        self.error, self.cash_error = error, cash_error
        self._h = [{"trading_symbol": "ITC", "quantity": 4, "average_price": 400.0}] if holdings is None else holdings
        self._o = [{"groww_order_id": "G1", "order_status": "OPEN"}] if orders is None else orders

    def holdings(self):
        if self.error:
            raise RuntimeError(self.error)
        return self._h

    def order_list(self, page=0, page_size=100):
        return self._o

    def available_cash(self):
        if self.cash_error:
            raise RuntimeError(self.cash_error)
        return 12345.0


def good(settings, **over):
    kw = dict(now=MON, holidays=Holidays(), session=FakeSession(), nse=FakeNSE(), bse=FakeBSE(), prices=FakePrices(),
              groww=lambda: FakeGrowwReads(), claude=lambda s: pytest.fail("Claude must not be called"), timeout=5)
    kw.update(over)
    return run_check(settings, **kw)


def by_name(result):
    return {s["name"]: s for s in result["steps"]}


@pytest.fixture
def s(settings):
    s = dataclasses.replace(settings, market="in", resend_api_key="re_fake", notify_email_to="me@example.com")
    return s


# --------------------------------------------------------------------------- steps
def test_every_step_ok_with_good_answers(s):
    r = good(s)
    steps = by_name(r)
    assert r["ok"] is True and r["last_trading_day"] == LAST
    for name in ("NSE deals", "NSE announcements", "BSE deals", "Yahoo prices", "NSE price bands", "NSE bhavcopy",
                 "Groww (read-only)", "Groww cash", "Resend"):
        assert steps[name]["ok"] and not steps[name].get("skipped"), name
    assert steps["Claude"]["skipped"] and steps["Telegram"]["skipped"]       # off / empty: ok but skipped
    assert steps["Resend"]["detail"] == "filled" and "empty" in steps["Telegram"]["detail"]
    assert all(set(x) >= {"name", "ok", "ms", "detail"} for x in r["steps"])
    assert integration.counts(r) == (9, 9)
    assert "3 bulk, 1 block" in steps["NSE deals"]["detail"]


@pytest.mark.parametrize("name,over,needle", [
    ("NSE deals", {"nse": FakeNSE(deals_error="bulk deals CSV has no 'client' column")}, "client"),
    ("NSE announcements", {"nse": FakeNSE(ann=[])}, "no announcements"),
    ("NSE announcements", {"nse": FakeNSE(ann=[{"symbol": "", "at": "x"}])}, "lacks"),
    ("BSE deals", {"bse": FakeBSE(error="BSE deals page layout changed")}, "layout changed"),
    ("Yahoo prices", {"prices": FakePrices(missing="^NSEI")}, "^NSEI has no bar"),
    ("NSE price bands", {"session": FakeSession(bands="Foo,Bar\n1,2\n")}, "Symbol / Band"),
    ("NSE bhavcopy", {"session": FakeSession(bhav="A,B\n1,2\n")}, "bhavcopy"),
    ("NSE bhavcopy", {"session": FakeSession(bhav=BHAV.replace("09-Oct", "08-Oct"))}, "not 2026-10-09"),
    ("Groww (read-only)", {"groww": lambda: FakeGrowwReads(holdings=[{"trading_symbol": "ITC"}])}, "lack"),
    ("Groww (read-only)", {"groww": lambda: FakeGrowwReads(error="HTTP 500")}, "HTTP 500"),
    ("Groww (read-only)", {"groww": lambda: FakeGrowwReads(orders=[{"groww_order_id": "G1"}])}, "order_status"),
    ("Groww cash", {"groww": lambda: FakeGrowwReads(cash_error="the margin reply has none of")}, "margin reply"),
])
def test_a_failing_step_is_reported_and_only_that_step(s, name, over, needle):
    r = good(s, **over)
    steps = by_name(r)
    assert r["ok"] is False and steps[name]["ok"] is False and needle in steps[name]["detail"]
    assert [n for n, x in steps.items() if not x["ok"]] == [name]          # every other step still ran and passed


def test_a_hung_step_times_out_without_stopping_the_others(s):
    release = threading.Event()
    try:
        r = good(s, nse=FakeNSE(hang=release), timeout=0.3)
    finally:
        release.set()
    steps = by_name(r)
    assert steps["NSE announcements"]["ok"] is False and "timed out" in steps["NSE announcements"]["detail"]
    assert steps["NSE deals"]["ok"] and steps["Yahoo prices"]["ok"] and steps["NSE bhavcopy"]["ok"]


def test_bse_is_skipped_when_switched_off_and_asked_for_the_last_trading_day_only(s):
    assert by_name(good(s, bse=None))["BSE deals"]["skipped"]
    bse = FakeBSE()
    good(s, bse=bse)
    assert bse.asked == [(date(2026, 10, 9), date(2026, 10, 9))]


def test_last_trading_day_skips_holidays(s):
    r = good(s, holidays=Holidays({date(2026, 10, 9)}), nse=FakeNSE(deals_error="x"))   # Friday closed -> Thursday
    assert r["last_trading_day"] == "2026-10-08"


def test_claude_only_when_asked_and_with_the_configured_key(s):
    calls = []
    s.integration_claude = True
    s.anthropic_api_key = None
    r = good(s, claude=lambda st: calls.append(1) or {"model": "m", "ms": 1})
    assert not calls and "ANTHROPIC_API_KEY" in by_name(r)["Claude"]["detail"] and not by_name(r)["Claude"]["ok"]
    s.anthropic_api_key = "sk-fake"
    r = good(s, claude=lambda st: calls.append(st.digest_claude_model) or {"model": "claude-haiku-x", "ms": 420})
    assert calls == ["claude-haiku-4-5"] and by_name(r)["Claude"]["detail"] == "claude-haiku-x answered in 420 ms"
    s.integration_claude = False
    assert by_name(good(s))["Claude"]["skipped"]


def test_resend_and_telegram_are_configuration_checks_only(s):
    s.resend_api_key, s.telegram_bot_token, s.telegram_chat_id = None, "123456:" + "a" * 30, "42"
    steps = by_name(good(s))
    assert steps["Resend"]["skipped"] and steps["Telegram"]["detail"] == "filled" and not steps["Telegram"].get("skipped")


# --------------------------------------------------------------------------- Groww: cached token only, read only
def _groww_settings(s, tmp_path):
    s.groww_api_key, s.groww_api_secret, s.groww_access_token = "k" * 20, "sec", None
    s.state_dir = tmp_path / "state"
    return s


def test_groww_is_skipped_without_a_cached_token_and_never_generates_one(s, tmp_path, monkeypatch):
    import trading_agent.groww as g
    s = _groww_settings(s, tmp_path)

    def boom(*a, **k):
        raise AssertionError("the integration check asked Groww for a new token")
    monkeypatch.setattr(g, "request_access_token", boom)
    monkeypatch.setattr(g, "cached_access_token", boom)
    monkeypatch.setattr("trading_agent.runner.resolve_groww_token", boom)
    assert cached_groww(s) is None
    r = run_check(s, now=MON, holidays=Holidays(), session=FakeSession(), nse=FakeNSE(), bse=FakeBSE(),
                  prices=FakePrices(), timeout=5)
    step = by_name(r)["Groww (read-only)"]
    assert step["ok"] and step["skipped"] and step["detail"] == "skipped (no cached token)"
    assert r["ok"] is True


def test_groww_is_skipped_while_the_token_cool_down_file_is_active(s, tmp_path, monkeypatch):
    s = _groww_settings(s, tmp_path)
    cache = TokenCache(s.state_dir / "groww_token.json")
    cache.put(s.groww_api_key, "cached-token", datetime.now(IST) + timedelta(hours=5))
    cache.write_block({"until": (datetime.now(IST) + timedelta(hours=1)).isoformat(timespec="seconds"), "status": 429,
                       "reason": "429 Too Many Requests", "at": "x", "strikes": 1, "last_429": None,
                       "key": cache.fingerprint(s.groww_api_key)})
    assert cached_groww(s) is None


def test_groww_reads_with_a_cached_token_through_get_only(s, tmp_path):
    s = _groww_settings(s, tmp_path)
    TokenCache(s.state_dir / "groww_token.json").put(s.groww_api_key, "cached-token", datetime.now(IST) + timedelta(hours=5))
    seen = []

    class Sess:
        def request(self, method, url, **kw):
            seen.append((method, url.rsplit("/", 2)[-2:], kw["headers"]["Authorization"]))
            if "holdings" in url:
                return FakeResponse({"payload": {"holdings": [{"trading_symbol": "ITC", "quantity": 1, "average_price": 1}]}})
            return FakeResponse({"payload": {"order_list": [{"groww_order_id": "G", "order_status": "OPEN"}]}})

    client = cached_groww(s, session=Sess())
    assert isinstance(client, ReadOnlyGroww)
    r = run_check(s, now=MON, holidays=Holidays(), session=FakeSession(), nse=FakeNSE(), bse=FakeBSE(),
                  prices=FakePrices(), groww=lambda: client, timeout=5)
    assert by_name(r)["Groww (read-only)"]["ok"] and not by_name(r)["Groww (read-only)"].get("skipped")
    assert {m for m, _, _ in seen} == {"GET"} and all(a == "Bearer cached-token" for _, _, a in seen)


def test_the_order_method_guards_raise(s):
    sess = ReadOnlySession(FakeSession())
    with pytest.raises(OrderBlocked):
        sess.request("POST", "https://api.groww.in/v1/order/create", json={})
    for m in ("post", "put", "patch", "delete"):
        with pytest.raises(OrderBlocked):
            getattr(sess, m)("https://x.invalid")
    with pytest.raises(OrderBlocked):
        sess.request("DELETE", "https://x.invalid")
    assert sess.get("https://x/sec_list.csv").status_code == 200       # a GET passes through
    # the Groww wrapper exposes the two reads only
    ro = ReadOnlyGroww(GrowwBroker("tok", live_orders=True, session=sess))
    for name in ("submit_order", "cancel_order", "modify_order", "place_gtt_stop", "cancel_gtt", "account", "latest_price"):
        with pytest.raises(OrderBlocked):
            getattr(ro, name)
    # and even the real broker behind the session cannot send an order: its POST is refused by the session
    broker = GrowwBroker("tok", live_orders=True, session=ReadOnlySession(FakeSession()))
    with pytest.raises(OrderBlocked):
        broker._req("POST", "order/create", json={})


# --------------------------------------------------------------------------- scheduler: claim and holiday guards
def at(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=IST)


def test_scheduler_runs_once_per_trading_day_from_0835(tmp_path):
    runs = []
    sch = IntegrationScheduler(tmp_path, lambda: runs.append(1), holidays=Holidays(), threaded=False)
    assert sch.tick(at(2026, 10, 12, 8, 29))["due"] is False and not runs          # too early
    assert sch.tick(at(2026, 10, 12, 8, 30))["started"] and runs == [1]
    sch.tick(at(2026, 10, 12, 9, 30))
    assert runs == [1]                                                              # not twice the same day
    again = IntegrationScheduler(tmp_path, lambda: runs.append(2), holidays=Holidays(), threaded=False)
    assert again.tick(at(2026, 10, 12, 9, 40)).get("claimed") is False and runs == [1]   # a restart finds the claim
    assert (tmp_path / "integration_2026-10-12.claim").exists()
    assert not list(tmp_path.glob("forward_*.claim"))                               # its own claim files
    assert sch.tick(at(2026, 10, 13, 8, 40))["started"] and runs == [1, 1]          # next day runs again
    assert sch.tick(at(2026, 10, 14, 12, 30))["due"] is False                       # after 12:00 the day is skipped


def test_scheduler_skips_weekends_and_exchange_holidays(tmp_path):
    runs = []
    sch = IntegrationScheduler(tmp_path, lambda: runs.append(1), holidays=Holidays({date(2026, 10, 20)}), threaded=False)
    assert sch.tick(at(2026, 10, 17, 8, 40))["due"] is False        # Saturday
    assert sch.tick(at(2026, 10, 20, 8, 40))["due"] is False        # a holiday
    assert not runs and not list(tmp_path.glob("integration_*.claim"))


def test_scheduler_releases_a_crashed_run_for_a_retry_and_prunes_old_claims(tmp_path):
    now = [0.0]
    attempts = []

    def boom():
        attempts.append(1)
        raise RuntimeError("bug")
    (tmp_path / "integration_2026-10-01.claim").write_text("1 1")
    (tmp_path / "forward_2026-10-01.claim").write_text("1 1")
    sch = IntegrationScheduler(tmp_path, boom, holidays=Holidays(), threaded=False, clock=lambda: now[0])
    sch.tick(at(2026, 10, 12, 8, 40))
    assert len(attempts) == 1 and not (tmp_path / "integration_2026-10-12.claim").exists()
    assert not (tmp_path / "integration_2026-10-01.claim").exists()        # pruned
    assert (tmp_path / "forward_2026-10-01.claim").exists()                # the forward job's files are left to it


def test_make_scheduler_respects_the_switch_and_the_market(s):
    assert isinstance(integration.make_scheduler(s, None, Holidays()), IntegrationScheduler)
    s.integration_check = False
    assert integration.make_scheduler(s, None, Holidays()) is None
    s.integration_check, s.market = True, "us"
    assert integration.make_scheduler(s, None, Holidays()) is None


# --------------------------------------------------------------------------- result file and the once-a-day alert
class Notifier:
    def __init__(self):
        self.sent = []

    def send(self, subject, body):
        self.sent.append((subject, body))


def test_result_is_stored_and_a_clean_run_sends_nothing(s):
    n = Notifier()
    r = run_and_record(s, notifier=n, now=MON, holidays=Holidays(), session=FakeSession(), nse=FakeNSE(), bse=FakeBSE(),
                       prices=FakePrices(), groww=lambda: FakeGrowwReads(), timeout=5)
    stored = json.loads((s.state_dir / "integration_check.json").read_text())
    assert stored == r and stored["ok"] is True and set(stored) >= {"at", "steps", "ok"}
    assert n.sent == []


def test_a_failure_alerts_once_a_day_and_lists_the_failed_steps(s):
    n = Notifier()
    kw = dict(notifier=n, holidays=Holidays(), session=FakeSession(), bse=FakeBSE("BSE is down"), prices=FakePrices(),
              groww=lambda: None, timeout=5)
    run_and_record(s, now=MON, nse=FakeNSE(deals_error="NSE changed"), **kw)
    run_and_record(s, now=MON + timedelta(hours=2), nse=FakeNSE(deals_error="NSE changed"), **kw)   # same day: no second alert
    assert len(n.sent) == 1
    subject, body = n.sent[0]
    assert subject.startswith("[INTEGRATION] 2 live checks failed")
    assert "NSE deals: ValueError: NSE changed" in body and "BSE deals" in body and "Yahoo" not in body
    run_and_record(s, now=MON + timedelta(days=1), nse=FakeNSE(deals_error="NSE changed"), **kw)     # next day: alerts again
    assert len(n.sent) == 2
    assert sorted(p.name for p in s.state_dir.glob("integration_alert_*.sent")) == ["integration_alert_2026-10-13.sent"]


def test_an_alert_that_cannot_be_sent_does_not_break_the_run(s):
    class Broken:
        def send(self, *a):
            raise RuntimeError("smtp down")
    r = run_and_record(s, notifier=Broken(), now=MON, holidays=Holidays(), session=FakeSession(), bse=None,
                       nse=FakeNSE(deals_error="x"), prices=FakePrices(), groww=lambda: None, timeout=5)
    assert r["ok"] is False


# --------------------------------------------------------------------------- the dashboard line
def write(s, ok, at_dt, failed=()):
    steps = [{"name": f"step{i}", "ok": True, "ms": 1, "detail": "fine"} for i in range(6)]
    for i in failed:
        steps[i]["ok"] = False
    s.state_dir.mkdir(parents=True, exist_ok=True)
    (s.state_dir / "integration_check.json").write_text(json.dumps({"at": at_dt.isoformat(), "steps": steps, "ok": ok}))


def test_dashboard_line_states(s):
    h = Holidays()
    never = status_line(s.state_dir, MON, h)
    assert never["level"] == "idle" and never["text"] == "Integration: not run yet"
    write(s, True, at(2026, 10, 12, 8, 35))
    ok = status_line(s.state_dir, MON, h)
    assert ok["level"] == "ok" and ok["text"] == "Integration: ok 08:35 · 6/6"
    write(s, False, at(2026, 10, 12, 8, 35), failed=[2])
    bad = status_line(s.state_dir, MON, h)
    assert bad["level"] == "bad" and bad["text"] == "Integration: failed 08:35 · 5/6" and "step2" in bad["title"]
    # two trading days old is still fine, three is red; a weekend does not count as trading days
    write(s, True, at(2026, 10, 9, 8, 35))
    assert status_line(s.state_dir, at(2026, 10, 13, 10, 0), h)["level"] == "ok"        # Fri -> Tue: Mon, Tue
    assert status_line(s.state_dir, at(2026, 10, 14, 10, 0), h)["level"] == "bad"       # Fri -> Wed: 3 trading days
    stale = status_line(s.state_dir, at(2026, 10, 14, 10, 0), h)
    assert "not run for 3 trading days" in stale["text"] and stale["text"].startswith("Integration: ok 09 Oct 08:35")
    assert status_line(s.state_dir, at(2026, 10, 14, 10, 0), Holidays({date(2026, 10, 12)}))["level"] == "ok"
    (s.state_dir / "integration_check.json").write_text("{not json")
    assert status_line(s.state_dir, MON, h)["level"] == "idle"


# --------------------------------------------------------------------------- settings, CLI, dashboard
def test_settings_defaults_and_the_validated_writer(s):
    from trading_agent.config import load_settings
    from trading_agent.ui import App
    assert s.integration_check is True and s.integration_claude is False
    from trading_agent.broker import LocalPaperBroker
    app = App(s, broker=LocalPaperBroker(s.state_dir / "pb.json", starting_cash=1000, price_fn=lambda x: 1.0),
              dotenv=s.state_dir / ".env_int")
    out = app.update_settings({"integration_check": False, "integration_claude": "true"})
    assert out == {"INTEGRATION_CHECK": "false", "INTEGRATION_CLAUDE": "true"}
    assert s.integration_check is False and s.integration_claude is True
    env = (s.state_dir / ".env_int").read_text()
    assert "INTEGRATION_CHECK=false" in env and "INTEGRATION_CLAUDE=true" in env
    snap = app.snapshot()["settings"]
    assert snap["integration_check"] is False and snap["integration_claude"] is True
    for bad in (["x"], {"a": 1}, "sometimes"):
        with pytest.raises(ValueError):
            app.update_settings({"integration_claude": bad})
    import os
    os.environ.pop("INTEGRATION_CHECK", None)
    os.environ.pop("INTEGRATION_CLAUDE", None)
    loaded = load_settings(None)
    assert loaded.integration_check is True and loaded.integration_claude is False


def test_cli_prints_the_steps_and_exit_code_follows_the_result(s, monkeypatch, capsys):
    from trading_agent import cli
    monkeypatch.setattr(cli, "_settings", lambda args: s)
    monkeypatch.setattr(cli, "_market_holidays", lambda settings: Holidays())
    monkeypatch.setattr("trading_agent.runner.make_notifier", lambda settings: Notifier())
    result = {"at": "2026-10-12T08:36:00+05:30", "last_trading_day": LAST, "ok": False,
              "steps": [{"name": "NSE deals", "ok": False, "ms": 12, "detail": "ValueError: nope"},
                        {"name": "Claude", "ok": True, "ms": 0, "detail": "skipped (INTEGRATION_CLAUDE is off)", "skipped": True}]}
    seen = {}

    def fake_run(settings, notifier=None, **kw):
        seen["notifier"], seen["claude"] = notifier, settings.integration_claude
        return result
    monkeypatch.setattr(integration, "run_and_record", fake_run)
    args = cli.build_parser().parse_args(["integration-check", "--claude"])
    assert args.func(args) == 2
    out = capsys.readouterr().out
    assert "[FAIL] NSE deals" in out and "[skip] Claude" in out and "FAILED: 0/1 checks passed" in out
    assert seen["claude"] is True and seen["notifier"] is not None
    args = cli.build_parser().parse_args(["integration-check", "--no-alert"])
    result["ok"] = True
    assert args.func(args) == 0 and seen["notifier"] is None


def test_dashboard_endpoints_show_the_last_result_and_start_a_background_run(server, monkeypatch):
    from tests.test_ui import _get, _post
    base, app = server
    app.settings.market = "in"
    status, j = _get(base + "/api/integration")
    assert status == 200 and j["line"]["level"] == "idle" and j["last"] is None and j["enabled"] is True and j["running"] is False
    started = threading.Event()
    release = threading.Event()
    calls = []

    def fake_run(settings, notifier=None, **kw):
        calls.append(notifier)
        started.set()
        release.wait(5)
        return {}
    monkeypatch.setattr(integration, "run_and_record", fake_run)
    monkeypatch.setattr("trading_agent.ui.make_notifier", lambda settings: Notifier())
    status, j = _post(base + "/api/integration/run", {})
    assert status == 202 and j == {"started": True, "running": True}
    assert started.wait(5)
    status, j = _post(base + "/api/integration/run", {})            # one at a time
    assert j == {"started": False, "running": True} and len(calls) == 1
    assert _get(base + "/api/integration")[1]["running"] is True
    release.set()
    for _ in range(100):
        if not _get(base + "/api/integration")[1]["running"]:
            break
        threading.Event().wait(0.05)
    assert _get(base + "/api/integration")[1]["running"] is False


def test_the_page_has_the_line_the_button_and_the_switches(tmp_path):
    from pathlib import Path
    html = (Path(__file__).resolve().parents[1] / "trading_agent" / "ui" / "index.html").read_text(encoding="utf-8")
    for needle in ('id="integ-chip"', 'id="btn-integ"', "Run integration check", 'id="f-integ"', 'id="f-integ-claude"',
                   "/api/integration/run", "body.integration_check"):
        assert needle in html, needle


# --------------------------------------------------------------------------- the NSE probe and the watch loop
DEALS_CSV = ('"Date ","Symbol ","Security Name ","Client Name ","Buy / Sell ","Quantity Traded ",'
             '"Trade Price / Wght. Avg. Price ","Remarks "\n'
             '"09-Oct-2026","ABC","Abc Ltd","SOME CLIENT","BUY","3,50,000","101.5","-"\n')


def test_nse_probe_reads_both_csvs_strictly():
    from trading_agent.nse import NSEClient
    urls = []

    class Sess:
        def get(self, url, headers=None, params=None, timeout=None):
            urls.append((url, (params or {}).get("optionType")))
            return FakeResponse(DEALS_CSV)

    got = NSEClient(session=Sess()).probe_deals(date(2026, 10, 9))
    assert got == {"bulk": 1, "block": 1}
    assert [k for _, k in urls if k] == ["bulk_deals", "block_deals"]

    class NoClient(Sess):
        def get(self, url, headers=None, params=None, timeout=None):
            return FakeResponse('"Date ","Symbol "\n"09-Oct-2026","ABC"\n')

    with pytest.raises(ValueError, match="column"):
        NSEClient(session=NoClient()).probe_deals(date(2026, 10, 9))

    class Html(Sess):
        def get(self, url, headers=None, params=None, timeout=None):
            return FakeResponse("<html>Access denied</html>")

    with pytest.raises(ValueError):   # not the CSV: no silent fallback to the capped JSON
        NSEClient(session=Html()).probe_deals(date(2026, 10, 9))


def test_the_watch_loop_ticks_the_integration_scheduler_and_survives_its_errors(s):
    from trading_agent.watch import Watcher
    ticks = []

    class Sched:
        def tick(self, now):
            ticks.append(now)
            if len(ticks) == 2:
                raise RuntimeError("scheduler bug")
            return {"due": False}

    w = Watcher(s, integration=Sched(), awake=None, weekdays_only=False)
    assert w.tick()["integration"] == {"due": False}
    assert "integration" not in w.tick() and len(ticks) == 2        # an error is logged, the tick carries on
