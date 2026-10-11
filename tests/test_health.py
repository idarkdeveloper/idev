"""Data health (health.py, GET /api/health, the Groww buttons): each status rule from fake state files and a fixed clock
(a weekend or holiday is never late), redaction, the run record, failing first, the endpoint, "Refresh from Groww" and
the Groww test. Fakes only: no network, no Groww, no orders."""
import dataclasses
import json
import os
from datetime import date, datetime, timedelta

import pytest

from trading_agent import groww_logins, health, runner
from trading_agent.groww import GrowwTokenUnavailable
from trading_agent.timezones import IST

from .test_ui import _get, _post, server, two_pages  # noqa: F401  (fixtures)

MON_11 = datetime(2026, 10, 12, 11, 0, tzinfo=IST)      # a Monday, market open
SAT = datetime(2026, 10, 10, 11, 0, tzinfo=IST)


class Cal:
    def __init__(self, closed=()):
        self.closed = set(closed)

    def is_trading_day(self, d):
        return d.weekday() < 5 and d not in self.closed


@pytest.fixture
def s(settings):
    settings.data_source = "nse"
    settings.watch_source = "deals"
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    return settings


def by_id(rep):
    return {r["id"]: r for r in rep["sources"]}


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def iso(dt):
    return dt.isoformat(timespec="seconds")


# ---------- redaction ----------
def test_redaction_hides_anything_secret_looking():
    raw = ("GET https://hc-ping.com/abcd-1234-secret-uuid?x=1 failed; Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIx.sig "
           "token=abc123 api_key: K9 bot 123456789:AAH_fake-token-for-tests-0123456789abcd mail me@x.com "
           "blob 0123456789abcdef0123456789abcdef01234567 password='hunter2'")
    out = health.redact(raw)
    for leak in ("abcd-1234-secret-uuid", "eyJhbGci", "abc123", "K9", "AAH_fake", "me@x.com", "0123456789abcdef0123", "hunter2", "x=1"):
        assert leak not in out, leak
    assert "https://hc-ping.com/…" in out and "token=[redacted]" in out
    assert len(health.redact("word " * 300, 50)) == 50 and health.redact(None) == ""


# ---------- the run record ----------
def test_recorded_notes_rows_and_errors_and_never_breaks_the_fetch(s):
    assert health.recorded(s.state_dir, "deals", lambda: [1, 2, 3]) == [1, 2, 3]
    r = health.read_runs(s.state_dir)["deals"]
    assert r["rows"] == 3 and r["ok"] is True and r["last_ok"] == r["last_try"]
    with pytest.raises(ValueError):
        health.recorded(s.state_dir, "deals", lambda: (_ for _ in ()).throw(ValueError("token=SECRET boom")))
    r = health.read_runs(s.state_dir)["deals"]
    assert r["ok"] is False and "SECRET" not in r["error"] and r["rows"] == 3 and "last_ok" in r
    health.note_run(None, "x", ok=True)                                         # no state dir: a no-op
    health.note_run(s.state_dir / "no\0such", "x", ok=True)                     # a bad path: swallowed
    (s.state_dir / health.RUNS_FILE).write_text("not json")
    health.note_run(s.state_dir, "news", ok=True, rows=1)
    assert set(health.read_runs(s.state_dir)) == {"news"}                       # an unreadable record starts over


# ---------- status rules ----------
def test_intraday_sources_ok_late_failing_and_a_weekend_is_never_late(s):
    cal = Cal()
    health.note_run(s.state_dir, "deals", ok=True, rows=40, now=MON_11 - timedelta(minutes=10))
    health.note_run(s.state_dir, "news", ok=True, rows=5, now=MON_11 - timedelta(hours=3))
    health.note_run(s.state_dir, "announcements", ok=True, rows=2, now=MON_11 - timedelta(minutes=5))
    health.note_run(s.state_dir, "announcements", ok=False, error="NSE 403", now=MON_11 - timedelta(minutes=1))
    rep = health.report(s, MON_11, cal)
    r = by_id(rep)
    assert r["deals"]["status"] == "ok" and r["deals"]["rows"] == 40 and r["deals"]["age"] == "10 min ago"
    assert r["news"]["status"] == "late" and r["news"]["delay"]
    assert r["announcements"]["status"] == "failing" and r["announcements"]["error"] == "NSE 403"
    # Saturday: Friday's success is enough; Monday 08:00 too (the window has not opened)
    fri = datetime(2026, 10, 9, 15, 0, tzinfo=IST)
    for src in ("deals", "news", "announcements"):
        health.note_run(s.state_dir, src, ok=True, rows=1, now=fri)
    r = by_id(health.report(s, SAT, cal))
    assert r["deals"]["status"] == r["news"]["status"] == r["announcements"]["status"] == "ok"
    assert by_id(health.report(s, datetime(2026, 10, 12, 8, 0, tzinfo=IST), cal))["deals"]["status"] == "ok"
    # Friday a holiday: Thursday's success covers the long weekend
    thu = datetime(2026, 10, 8, 15, 0, tzinfo=IST)
    health.note_run(s.state_dir, "deals", ok=True, rows=1, now=thu)
    assert by_id(health.report(s, SAT, Cal({date(2026, 10, 9)})))["deals"]["status"] == "ok"
    assert by_id(health.report(s, SAT, cal))["deals"]["status"] == "late"


def test_not_configured_sources(s):
    s.bse_deals = False
    s.data_source = "quiver"
    r = by_id(health.report(s, MON_11, Cal()))
    for sid in ("bse", "announcements", "insider", "groww"):
        assert r[sid]["status"] == "off" and r[sid]["status_text"] == "not configured", sid
    assert r["deals"]["name"] == "Disclosed deals (QuiverQuant)"


def test_evening_stores_flows_and_breadth(s):
    cal = Cal()
    fri = "2026-10-09"
    write(s.state_dir / "flows.json", {"days": {fri: {"date": fri, "fii_net": -1}},
                                         "attempts": {fri: {"tries": 1, "done": True, "last": "2026-10-09T19:05:00+05:30", "outcome": "ok"}}})
    write(s.state_dir / "breadth.json", {"days": {"2026-10-08": {"n": 498}}, "attempts": {
        fri: {"tries": 3, "done": False, "last": "2026-10-09T20:00:00+05:30", "outcome": "error: HTTPError"}}})
    r = by_id(health.report(s, MON_11, cal))
    assert r["flows"]["status"] == "ok" and r["flows"]["detail"] == "latest day stored: 9 Oct"
    assert r["breadth"]["status"] == "failing" and r["breadth"]["error"] == "error: HTTPError" and r["breadth"]["rows"] == 498
    # Monday 20:00: today's figures are not due until 21:00
    assert by_id(health.report(s, datetime(2026, 10, 12, 20, 0, tzinfo=IST), cal))["flows"]["status"] == "ok"
    late = by_id(health.report(s, datetime(2026, 10, 12, 21, 30, tzinfo=IST), cal))["flows"]
    assert late["status"] == "late" and "12 Oct" in late["delay"]
    # "still <date>": NSE had not published yet, not an error
    write(s.state_dir / "flows.json", {"days": {fri: {}}, "attempts": {"2026-10-12": {"last": "2026-10-12T19:20:00+05:30", "outcome": "still 2026-10-09"}}})
    assert by_id(health.report(s, datetime(2026, 10, 12, 20, 0, tzinfo=IST), cal))["flows"]["status"] == "ok"


def test_the_nse_breaker_marks_nse_sources_failing(s):
    import time
    health.note_run(s.state_dir, "deals", ok=True, rows=1, now=MON_11)
    write(s.state_dir / "nse_breaker.json", {"name": "NSE", "degraded": True, "since": "x", "retry_at": time.time() + 300, "written": time.time()})
    r = by_id(health.report(s, MON_11, Cal()))
    assert r["deals"]["status"] == "failing" and "paused until" in r["deals"]["backoff"]
    assert r["news"]["backoff"] is None


def test_bands_yahoo_canary_and_watch(s):
    cal = Cal()
    write(s.state_dir / "price_bands.json", {"date": "2026-10-12", "bands": {"AAA": 5, "BBB": None}})
    cache = s.state_dir / "cache"
    write(cache / "yahoo_AAA_1y.json", [])
    write(s.state_dir / "integration_check.json", {"at": "2026-10-12T08:35:00+05:30", "last_trading_day": "2026-10-09", "ok": False,
                                                   "steps": [{"name": "NSE deals", "ok": False, "ms": 1, "detail": "x"},
                                                             {"name": "Yahoo prices", "ok": True, "ms": 1, "detail": "y"}]})
    write(s.state_dir / "canary_failed.json", {"at": "x", "steps": ["NSE deals"]})
    r = by_id(health.report(s, MON_11, cal))
    assert r["bands"]["status"] == "ok" and r["bands"]["rows"] == 2
    assert r["yahoo"]["status"] == "ok" and r["yahoo"]["rows"] == 1                 # refreshed just now
    assert r["canary"]["status"] == "failing" and "NSE deals" in r["canary"]["error"] and "paused" in r["canary"]["backoff"]
    assert r["watch"]["status"] == "off"                                            # never seen on this machine
    old = (MON_11 - timedelta(days=4)).timestamp()
    os.utime(cache / "yahoo_AAA_1y.json", (old, old))
    write(s.state_dir / "price_bands.json", {"date": "2026-10-09", "bands": {}})
    write(s.state_dir / "watch_alive.json", {"at": iso(MON_11 - timedelta(minutes=30)), "every": 60})
    r = by_id(health.report(s, MON_11, cal))
    assert r["yahoo"]["status"] == "late" and r["bands"]["status"] == "late" and r["watch"]["status"] == "failing"
    write(s.state_dir / "price_bands_tries.json", {"2026-10-12": {"tries": 6, "last": "x"}})
    assert by_id(health.report(s, MON_11, cal))["bands"]["status"] == "failing"


def test_groww_row_and_panel_read_files_only_and_never_show_the_token(s):
    s = dataclasses.replace(s, groww_api_key="K", groww_api_secret="S")
    write(s.state_dir / "groww_holdings.json", {"saved_at": "2026-10-09T15:40:00+05:30", "kind": "close", "session": "2026-10-09",
                                                "holdings": [{"symbol": "TCS"}]})
    write(s.state_dir / "groww_token.json", {"token": "TOPSECRETTOKEN", "key": "fp", "expires_at": "2026-10-13T06:00:00+05:30",
                                             "created_at": "2026-10-12T08:31:00+05:30"})
    rep = health.report(s, SAT.replace(day=10), Cal())
    r = by_id(rep)
    assert r["groww"]["status"] == "ok" and r["groww"]["rows"] == 1
    assert "TOPSECRETTOKEN" not in json.dumps(rep)
    assert rep["groww"]["token"]["expires_at"].startswith("2026-10-13T06:00") and rep["groww"]["logins"]["limit"] == 150
    assert "150 a day is the Groww Trade API token limit" in rep["groww"]["note"]
    # Monday after 16:00 without a Monday close snapshot: late; a cool-down: failing with the end time
    assert by_id(health.report(s, datetime(2026, 10, 12, 17, 0, tzinfo=IST), Cal()))["groww"]["status"] == "late"
    write(s.state_dir / "groww_token.block", {"until": "2026-10-12T11:45:00+05:30", "status": 429, "reason": "429 Too Many Requests"})
    rep = health.report(s, MON_11, Cal())
    g = by_id(rep)["groww"]
    assert g["status"] == "failing" and "11:45 IST" in g["backoff"] and rep["groww"]["cooldown"]["status"] == 429
    log = groww_logins.log_path(s.state_dir)
    for _ in range(145):
        groww_logins.record(log, MON_11 - timedelta(minutes=5), "laptop", "ok")
    g = by_id(health.report(s, MON_11 + timedelta(hours=1), Cal()))["groww"]
    assert g["status"] == "failing" and "145 token requests" in g["backoff"]


def test_failing_first_and_counts(s):
    health.note_run(s.state_dir, "news", ok=False, error="boom", now=MON_11)
    rep = health.report(s, MON_11, Cal())
    order = [r["status"] for r in rep["sources"]]
    rank = {"failing": 0, "late": 1, "ok": 2, "off": 3}
    assert order == sorted(order, key=rank.get) and order[0] == "failing"
    assert rep["counts"]["failing"] == order.count("failing") and sum(rep["counts"].values()) == len(order)
    ids = {r["id"] for r in rep["sources"]}
    assert ids == {"deals", "bse", "insider", "announcements", "yahoo", "flows", "breadth", "bands", "filters", "corp_actions", "clearing", "news", "groww", "canary", "watch"}
    for r in rep["sources"]:
        assert r["feeds"] and r["schedule"] and r["status_text"]


# ---------- the endpoint and the Groww buttons ----------
def test_health_endpoint_is_read_only_json(server):  # noqa: F811
    base, app = server
    status, j = _get(base + "/api/health")
    assert status == 200 and {"sources", "counts", "groww", "at"} <= set(j)
    assert _post(base + "/api/health", {})[0] == 404                                   # no POST


def test_refresh_from_groww_forces_the_read_but_never_the_cool_down(server, monkeypatch):  # noqa: F811
    base, app = server
    seen = []
    monkeypatch.setattr("trading_agent.ui.read_groww_portfolio",
                        lambda settings, prices, stamp="", force=False, **kw: seen.append(force) or {"linked": True, "at": "x", "holdings": []})
    monkeypatch.setattr(runner, "token_block", lambda settings: None)
    status, j = _post(base + "/api/groww/refresh", {})
    assert status == 200 and seen == [True] and j["linked"]
    until = datetime.now(IST) + timedelta(minutes=20)
    monkeypatch.setattr(runner, "token_block", lambda settings: GrowwTokenUnavailable("cooling", until, 429))
    status, j = _post(base + "/api/groww/refresh", {})
    assert status == 409 and "cooling down" in j["error"] and seen == [True]           # nothing sent during the cool-down
    _, mp = _get(base + "/api/my-portfolio")
    assert mp["cooldown_until"] == until.isoformat()


def test_refresh_from_groww_is_a_live_page_action(two_pages):  # noqa: F811
    base, app, fake, settings = two_pages
    status, j = _post(base + "/demo/api/groww/refresh", {})
    assert status == 403 and "Live page" in j["error"]


def test_groww_test_never_forces_past_the_cool_down(server, monkeypatch):  # noqa: F811
    base, app = server
    app.settings = dataclasses.replace(app.settings, groww_api_key="K", groww_api_secret="S")
    calls = []

    def resolve(settings, **kw):
        calls.append(kw)
        raise GrowwTokenUnavailable("Groww refused a new login token (429). Next try after 11:45 IST.", None, 429)
    monkeypatch.setattr(runner, "resolve_groww_token", resolve)
    status, j = _post(base + "/api/groww-test", {})
    assert status == 200 and j["ok"] is False and "429" in j["message"]
    assert calls == [{}]                                                               # no force=True


# ---------- the page ----------
def test_the_health_tab_badge_palette_command_and_portfolio_buttons_are_on_the_live_page():
    import re
    from pathlib import Path
    ui = Path(__file__).resolve().parents[1] / "trading_agent" / "ui"
    page = (ui / "index.html").read_text(encoding="utf-8")
    assert 'id="wst-health" data-ws-tab="health" aria-controls="ws-health"' in page and 'id="ws-health-count"' in page
    assert '["health", "Health"]' in page   # WORKSPACES feeds the Ctrl+K "Go to Health" command
    assert 'api("/api/health")' in page and 'badge.hidden = !c.failing' in page
    assert 'id="mp-groww-refresh"' in page and 'api("/api/groww/refresh", {})' in page and "m.cooldown_until" in page
    assert 'm.source === "saved" && m.market_closed' in page   # a plain note, not the error styling
    css = (ui / "nocturne.css").read_text(encoding="utf-8")
    block = css[css.index("/* ---- Health tab"):]
    assert not re.findall(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(", block)                    # tokens only
