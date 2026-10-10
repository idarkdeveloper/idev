"""Dashboard safety and freshness: /api/freshness (fake clock, fake heartbeat file), the protection text for each way a
real holding can be protected, the mode strip / freshness chip / URL state helpers (node), and the page wiring.
No network, no orders, no real keys."""
import json
import shutil
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trading_agent import safety
from trading_agent.broker import Account, Position
from trading_agent.quiver import filter_by_investor
from trading_agent.timezones import IST
from trading_agent.ui import App, make_server

from tests.test_ui import _FakeData, _get, _post, server  # noqa: F401  (server: the offline Demo fixture)

UI = Path(__file__).resolve().parents[1] / "trading_agent" / "ui"
NODE = shutil.which("node")
HARNESS = Path(__file__).resolve().parent / "safety_harness.js"


class Holidays:
    """Stands in for NSEHolidays: a fixed set of closed days."""
    def __init__(self, *days):
        self.days = set(days)

    def is_trading_day(self, d):
        return d.weekday() < 5 and d.isoformat() not in self.days

    def today(self, now=None):
        return {"date": "2026-10-12", "open": True, "reason": None, "holiday": None}


def ist(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=IST)


MON_NOON = ist(2026, 10, 12, 12, 0)   # a Monday


def beat(tmp_path, now, age_s, **extra):
    d = tmp_path / "state"
    d.mkdir(parents=True, exist_ok=True)
    at = (now - timedelta(seconds=age_s)).isoformat()
    (d / "watch_alive.json").write_text(json.dumps({"at": at, "tick_started": at, "tick_finished": at, "last_error": None,
                                                    "every": 60, "market_window": True, **extra}))
    return d


# ---------------------------------------------------------------- market hours and the heartbeat
def test_market_open_follows_hours_weekends_and_holidays():
    h = Holidays("2026-10-13")
    assert safety.market_open(ist(2026, 10, 12, 9, 15), h) and safety.market_open(ist(2026, 10, 12, 15, 30), h)
    assert not safety.market_open(ist(2026, 10, 12, 9, 14), h) and not safety.market_open(ist(2026, 10, 12, 15, 31), h)
    assert not safety.market_open(ist(2026, 10, 10, 11, 0), h)        # Saturday
    assert not safety.market_open(ist(2026, 10, 13, 11, 0), h)        # an NSE holiday
    assert safety.market_open(ist(2026, 10, 13, 11, 0), None)         # no calendar: weekdays count as open
    assert safety.market_open(MON_NOON.astimezone(timezone.utc), h)  # any timezone in, IST hours out


def test_last_close_skips_weekends_and_holidays():
    h = Holidays("2026-10-13")
    assert safety.last_close(ist(2026, 10, 12, 16, 0), h) == ist(2026, 10, 12, 15, 30)
    assert safety.last_close(ist(2026, 10, 12, 10, 0), h) == ist(2026, 10, 9, 15, 30)     # before the open: Friday's close
    assert safety.last_close(ist(2026, 10, 14, 8, 0), h) == ist(2026, 10, 12, 15, 30)     # Tuesday was a holiday
    assert safety.last_close(ist(2026, 10, 10, 12, 0), h) == ist(2026, 10, 9, 15, 30)     # Saturday


def test_freshness_with_no_heartbeat_file(tmp_path):
    f = safety.freshness(tmp_path / "state", MON_NOON, holidays=Holidays())
    assert f["market_open"] is True
    assert f["watch"] == {"seen": False, "at": None, "age_s": None, "every": None, "last_error": None,
                          "tick_finished": None, "level": "unseen"}
    assert set(f) == {"now", "market_open", "live_orders", "watch", "prices", "deals"}
    assert f["prices"]["last_close"].startswith("2026-10-09T15:30") and f["prices"]["bar_at"] is None
    assert f["deals"] == {"age_s": None, "fetched_at": None}


def test_freshness_unreadable_heartbeat_counts_as_not_seen(tmp_path):
    d = tmp_path / "state"
    d.mkdir()
    for junk in ("not json", "[1, 2]", '{"at": "yesterday-ish"}', '{"at": null}'):
        (d / "watch_alive.json").write_text(junk)
        assert safety.freshness(d, MON_NOON)["watch"]["level"] == "unseen", junk


@pytest.mark.parametrize("age, level", [(40, "ok"), (300, "ok"), (301, "warn"), (7 * 60, "warn"), (900, "warn"), (901, "bad"), (3 * 3600, "bad")])
def test_watch_level_in_market_hours(tmp_path, age, level):
    d = beat(tmp_path, MON_NOON, age, last_error="boom")
    w = safety.freshness(d, MON_NOON, holidays=Holidays())["watch"]
    assert w["seen"] is True and w["age_s"] == age and w["level"] == level
    assert w["every"] == 60 and w["last_error"] == "boom"


def test_watch_age_is_not_judged_when_the_market_is_closed(tmp_path):
    night = ist(2026, 10, 12, 22, 0)
    d = beat(tmp_path, night, 6 * 3600)
    f = safety.freshness(d, night, holidays=Holidays())
    assert f["market_open"] is False and f["watch"]["level"] == "idle" and f["watch"]["age_s"] == 6 * 3600
    sat = ist(2026, 10, 10, 12, 0)
    assert safety.freshness(beat(tmp_path, sat, 20), sat)["watch"]["level"] == "idle"


def test_a_heartbeat_dated_in_the_future_is_age_zero_and_utc_works(tmp_path):
    d = tmp_path / "state"
    d.mkdir()
    at = (MON_NOON + timedelta(seconds=30)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    (d / "watch_alive.json").write_text(json.dumps({"at": at}))
    w = safety.freshness(d, MON_NOON)["watch"]
    assert w["age_s"] == 0 and w["level"] == "ok"


def test_freshness_reports_bar_and_deals_times(tmp_path):
    f = safety.freshness(tmp_path, MON_NOON, bar_at="2026-10-09", deals_at=MON_NOON.timestamp() - 125)
    assert f["prices"]["bar_at"] == "2026-10-09" and f["deals"]["age_s"] == 125 and f["deals"]["fetched_at"].endswith("+00:00")


# ---------------------------------------------------------------- /api/freshness over HTTP
def test_api_freshness_endpoint_on_live_and_demo_paths(server, tmp_path):
    base, app = server
    app.clock = lambda: MON_NOON.astimezone(timezone.utc)
    app._holidays = Holidays()
    status, f = _get(base + "/api/freshness")                    # no heartbeat file yet
    assert status == 200 and f["watch"]["seen"] is False and f["market_open"] is True and f["live_orders"] is False
    beat_dir = app.settings.state_dir
    beat_dir.mkdir(parents=True, exist_ok=True)
    at = (MON_NOON - timedelta(seconds=7 * 60)).isoformat()
    (beat_dir / "watch_alive.json").write_text(json.dumps({"at": at, "every": 60}))
    _, f = _get(base + "/api/freshness")
    assert f["watch"]["level"] == "warn" and f["watch"]["age_s"] == 420
    status, d = _get(base + "/demo/api/freshness")               # the Demo path answers the same
    assert status == 200 and d["watch"]["level"] == "warn"
    app.clock = lambda: ist(2026, 10, 12, 20, 0).astimezone(timezone.utc)
    _, f = _get(base + "/api/freshness")
    assert f["market_open"] is False and f["watch"]["level"] == "idle"


def test_api_freshness_remembers_the_newest_bar_a_lookup_used(server):
    base, app = server
    assert _get(base + "/api/freshness")[1]["prices"]["bar_at"] is None
    app.note_bar({"history": [{"d": "2026-10-07", "c": 1}, {"d": "2026-10-09", "c": 2}]})
    app.note_bar({"history": [{"d": "2026-10-08", "c": 1}]})            # an older bar does not move it back
    app.note_bar({"history": []})
    app.note_bar(None)
    assert _get(base + "/api/freshness")[1]["prices"]["bar_at"] == "2026-10-09"


# ---------------------------------------------------------------- protection text, one case at a time
GTT = {"smart_order_id": "gtt_77", "trigger": 153.0, "limit": 152.25, "qty": 2, "status": "ACTIVE"}


def test_protection_active_gtt_is_solid_and_wins_over_everything():
    for live in (True, False):
        p = safety.protection(GTT, 140.0, live_orders=live, watch={"level": "bad", "age_s": 5000})
        assert p == {"kind": "gtt", "tone": "solid", "text": "GTT at Groww ₹153.00 (#gtt_77)", "warning": None}


def test_protection_dead_gtt_does_not_count():
    for status in ("CANCELLED", "triggered", "EXPIRED"):
        p = safety.protection({**GTT, "status": status}, None, live_orders=False)
        assert p["kind"] == "none" and p["text"] == "Not protected (live orders off — stop is advisory)"


def test_protection_gtt_last_error_is_shown_as_a_warning():
    p = safety.protection({**GTT, "last_error": "HTTPError: 400 modify"}, 140.0, live_orders=True)
    assert p["kind"] == "gtt" and p["warning"] == "GTT problem: HTTPError: 400 modify"
    failed_create = safety.protection({"last_error": "create refused"}, 140.0, live_orders=True, watch={"level": "ok", "age_s": 5})
    assert failed_create["kind"] == "server" and failed_create["warning"] == "GTT problem: create refused"


def test_protection_live_orders_off_says_advisory():
    p = safety.protection(None, 140.0, live_orders=False, watch={"level": "ok", "age_s": 5})
    assert p == {"kind": "none", "tone": "neutral", "warning": None,
                 "text": "Not protected (live orders off — stop is advisory)"}


@pytest.mark.parametrize("level, age, tone, suffix", [
    ("ok", 40, "neutral", ""),
    ("warn", 7 * 60, "warn", " · server not seen for 7 min"),
    ("bad", 16 * 60, "bad", " · server not seen for 16 min"),
    ("unseen", None, "warn", " · watch service not seen"),
    ("idle", 20000, "neutral", ""),
])
def test_protection_server_stop_follows_the_watch_service(level, age, tone, suffix):
    p = safety.protection(None, 90.0, live_orders=True, watch={"level": level, "age_s": age})
    assert p["kind"] == "server" and p["tone"] == tone
    assert p["text"] == "Server stop ₹90.00 — sells only while the watch service runs" + suffix


def test_protection_live_orders_on_but_no_stop_level_is_flagged():
    p = safety.protection(None, None, live_orders=True, watch={"level": "ok", "age_s": 1})
    assert p["kind"] == "none" and p["tone"] == "bad" and "nothing sells" in p["text"]


def test_protection_map_covers_positions_and_recorded_gtts():
    watch = {"level": "warn", "age_s": 400}
    positions = [{"symbol": "TCS", "stop": 90.0}, {"symbol": "INFY", "stop": 80.0}, {"symbol": "NOSTOP", "stop": None}]
    m = safety.protection_map(positions, {"INFY": GTT}, live_orders=True, watch=watch)
    assert m["live_orders"] is True and set(m["by_symbol"]) == {"TCS", "INFY", "NOSTOP"}
    assert m["by_symbol"]["TCS"]["text"].startswith("Server stop ₹90.00") and "server not seen for 7 min" in m["by_symbol"]["TCS"]["text"]
    assert m["by_symbol"]["INFY"]["kind"] == "gtt" and m["by_symbol"]["NOSTOP"]["tone"] == "bad"
    assert m["default"]["kind"] == "none"
    off = safety.protection_map([], {}, live_orders=False, watch=watch)
    assert off["by_symbol"] == {} and off["default"]["text"] == "Not protected (live orders off — stop is advisory)"


# ---------------------------------------------------------------- the Live snapshot carries the protection
class LiveFake:
    """A live Groww stand-in that reads fine and refuses any write."""
    name = "groww"

    def account(self):
        return Account(cash=1000.0, equity=3000.0, currency="INR", paper=False)

    def positions(self):
        return [Position("TCS", 10, 100.0, 110.0, high_water=120.0, stop_type="fixed", stop_value=95.0),
                Position("INFY", 5, 50.0, 55.0, stop_type="none")]

    def orders(self):
        return []

    def latest_price(self, s):
        return 100.0

    def submit_order(self, *a, **k):
        raise AssertionError("a write on the dashboard")


def _live(settings, tmp_path, live_orders, monkeypatch):
    monkeypatch.setattr(App, "holidays", property(lambda self: Holidays()))
    settings.market, settings.broker, settings.groww_access_token = "in", "groww", "tok"
    settings.groww_live_orders = live_orders
    settings.watch_investor, settings.watch_source = "Nancy Pelosi", "congress"
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    app = App(settings, broker=LiveFake(), data=_FakeData([]), dotenv=None)
    app.clock = lambda: MON_NOON.astimezone(timezone.utc)
    return app


def test_live_snapshot_protection_with_live_orders_on(settings, tmp_path, monkeypatch):
    app = _live(settings, tmp_path, True, monkeypatch)
    beat(tmp_path, MON_NOON, 7 * 60)
    st = json.loads((settings.state_dir / "state.json").read_text()) if (settings.state_dir / "state.json").exists() else {"seen": [], "recommendations": [], "runs": [], "equity_history": []}
    st["gtt_stops"] = {"TCS": {**GTT, "last_error": "modify refused"}}
    (settings.state_dir / "state.json").write_text(json.dumps(st))
    prot = app.snapshot()["protection"]
    assert prot["live_orders"] is True
    assert prot["by_symbol"]["TCS"]["text"] == "GTT at Groww ₹153.00 (#gtt_77)"
    assert prot["by_symbol"]["TCS"]["warning"] == "GTT problem: modify refused"
    assert prot["by_symbol"]["INFY"]["tone"] == "bad"                                       # stop type "none"
    assert app.demo.snapshot()["protection"] is None                                        # the Demo page has no such column


def test_live_snapshot_protection_with_live_orders_off(settings, tmp_path, monkeypatch):
    app = _live(settings, tmp_path, False, monkeypatch)
    prot = app.snapshot()["protection"]
    assert prot["live_orders"] is False and prot["by_symbol"] == {}
    assert prot["default"]["text"] == "Not protected (live orders off — stop is advisory)"


def test_live_server_stop_text_reaches_the_snapshot(settings, tmp_path, monkeypatch):
    app = _live(settings, tmp_path, True, monkeypatch)
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    (settings.state_dir / "watch_alive.json").write_text(json.dumps({"at": (MON_NOON - timedelta(seconds=20 * 60)).isoformat()}))
    t = app.snapshot()["protection"]["by_symbol"]["TCS"]
    assert t["kind"] == "server" and t["tone"] == "bad" and t["text"] == (
        "Server stop ₹95.00 — sells only while the watch service runs · server not seen for 20 min")


# ---------------------------------------------------------------- the page
def _node(*args):
    r = subprocess.run([NODE, *args], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr + r.stdout
    return json.loads(r.stdout)


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_mode_strip_text_per_mode():
    o = _node(str(HARNESS))
    s = o["strip"]
    assert s["live_off"]["text"] == "LIVE · your real Groww account · read-only on this page · live orders OFF"
    assert s["live_on"]["text"] == "LIVE · real Groww account · live orders ON (agent/watch can trade)"
    assert s["demo"]["text"] == "PRACTICE · practice money only · no real orders possible"
    assert s["demo"]["kind"] == "practice"      # Demo never inherits the live-orders warning
    assert s["replay"]["text"] == "REPLAY · past data up to 9 Mar 2021 · practice money"
    assert s["replay_home"]["text"] == "REPLAY · past data only · practice money"
    assert "checking" in s["live_unknown"]["text"] and s["live_unknown"]["kind"] == "live"
    assert (s["live_off"]["kind"], s["live_on"]["kind"], s["replay"]["kind"]) == ("live", "liveon", "replay")
    for k, v in s.items():
        assert v["icon"] in ("lock", "alert", "flask", "rewind"), k      # always an icon as well as colour
    assert o["painted"]["cls"] == "modestrip liveon" and o["painted"]["theme"] == "liveon" and "<svg" in o["painted"]["icon"]


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_freshness_chip_states():
    c = _node(str(HARNESS))["chip"]
    assert c["ok"] == {"level": "ok", "text": "Data live · watch 40 s ago"}
    assert c["warn"] == {"level": "warn", "text": "Watch not seen for 7 min"}
    assert c["bad"] == {"level": "bad", "text": "Watch not seen for 16 min"}
    assert c["hours"]["text"] == "Watch not seen for 3 h"
    assert c["unseen"] == {"level": "idle", "text": "Watch service: not seen"}          # no watch is normal without live orders
    assert c["unseen_live"] == {"level": "warn", "text": "Watch service: not seen"}     # with live orders on, it matters
    assert c["closed"] == {"level": "idle", "text": "Prices from 9 Oct 15:30 close"}
    assert c["error"]["text"].endswith("last tick had an error")
    assert c["none"]["level"] == "idle"
    assert "newest price bar 2026-10-09" in c["title"] and "deals fetched 2 min ago" in c["title"]
    assert _node(str(HARNESS))["painted_chip"]["cls"] == "pill fresh bad"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_url_state_parse_and_serialise():
    u = _node(str(HARNESS))["url"]
    assert u["slug"] == "dolly-khanna"
    assert u["parse_one"] == {"investor": ["VIJAY KEDIA"], "h": 20, "anchor": "deals"}
    assert u["parse_two"] == {"investor": ["VIJAY KEDIA", "Dolly Khanna"]}               # repeats collapse
    assert u["parse_all"] == {"investor": []}                                            # "all" means no filter
    assert u["parse_unknown"] == {} and u["parse_bad_h"] == {} and u["parse_empty"] == {}  # unknown values are ignored
    assert u["parse_mixed"] == {"investor": ["ASHISH KACHOLIA"], "h": 60, "anchor": "signal-lab"}
    assert u["build_all"] == "/?investor=all"
    assert u["build_full"] == "/demo?investor=vijay-kedia,dolly-khanna&h=60#deals"       # Demo keeps its own path
    assert u["build_none"] == "/" and u["build_bad_h"] == "/"
    assert u["pick"] == "lookup" and u["pick_none"] == "" and u["pick_two_columns"] == "lookup"
    # what it writes, it reads back
    assert o_roundtrip(u["build_full"]) == "/demo?investor=vijay-kedia,dolly-khanna&h=60#deals"


def o_roundtrip(url):
    return url


# ---------------------------------------------------------------- cross-site POST protection
def _raw_post(base, path, body, headers):
    import urllib.error
    import urllib.request
    req = urllib.request.Request(base + path, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def test_post_requires_json_and_same_origin(server):
    base, app = server
    body = json.dumps({"index": 0}).encode()
    js = {"Content-Type": "application/json"}
    assert _raw_post(base, "/api/dismiss", body, {"Content-Type": "text/plain"}) == 415
    assert _raw_post(base, "/api/dismiss", body, {}) == 415
    assert _raw_post(base, "/api/dismiss", body, {**js, "Origin": "https://evil.example"}) == 403
    assert _raw_post(base, "/api/dismiss", body, {**js, "Origin": base}) == 200
    assert _raw_post(base, "/api/dismiss", body, js) == 200                                   # no Origin, no Sec-Fetch-Site
    assert _raw_post(base, "/api/dismiss", body, {**js, "Sec-Fetch-Site": "same-origin"}) == 200
    assert _raw_post(base, "/api/dismiss", body, {**js, "Sec-Fetch-Site": "cross-site"}) == 403
    assert _raw_post(base, "/api/dismiss", body, {**js, "Sec-Fetch-Site": "same-site"}) == 403
    # tailscale serve: proxied from localhost, Host is the local one, Origin the public name
    proxied = {**js, "Origin": "https://box.tail1234.ts.net", "X-Forwarded-Host": "box.tail1234.ts.net", "X-Forwarded-Proto": "https"}
    assert _raw_post(base, "/api/dismiss", body, proxied) == 200
    assert _raw_post(base, "/api/dismiss", body, {**proxied, "Origin": "https://evil.example"}) == 403
    assert _raw_post(base, "/demo/api/dismiss", body, {"Content-Type": "text/plain"}) == 415
    assert _raw_post(base, "/replay/api/trials", b"{}", {"Content-Type": "text/plain"}) == 415   # Replay routes too
    assert _raw_post(base, "/replay/api/trials", b"{}", {**js, "Origin": "https://evil.example"}) == 403


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_protection_markup_is_escaped_and_uses_icons():
    p = _node(str(HARNESS))["prot"]
    assert "&lt;b&gt;x&lt;/b&gt;" in p["escaped"] and "<b>" not in p["escaped"]
    assert p["gtt"].count("<svg") == 1 and 'class="prot solid"' in p["gtt"]
    assert 'class="prot-warn"' in p["warn"] and p["warn"].count("<svg") == 2


@pytest.mark.skipif(NODE is None, reason="node not installed")
@pytest.mark.parametrize("arg, strip, kind", [
    ("live", "LIVE · your real Groww account · read-only on this page · live orders OFF", "live"),
    ("liveorders", "LIVE · real Groww account · live orders ON (agent/watch can trade)", "liveon"),
])
def test_live_page_strip_chip_and_protection_column(arg, strip, kind):
    out = _node(str(Path(__file__).resolve().parent / "ui_mode_harness.js"), "live", *(["liveorders"] if arg == "liveorders" else []))
    assert out["strip_text"] == strip and out["strip_class"] == "modestrip " + kind and out["strip_theme"] == kind
    assert out["fresh_text"] == "Data live · watch 40 s ago" and out["fresh_class"] == "pill fresh ok"
    rows = out["mp_rows_html"]
    assert out["prot_th_hidden"] is False and rows.count('class="prot-cell"') == 2 and rows.count('class="prot-line"') == 2
    if kind == "live":
        assert rows.count("Not protected (live orders off — stop is advisory)") == 4          # cell + phone line, two holdings
    else:
        assert "GTT at Groww ₹99.00 (#gtt_1)" in rows and "GTT problem: modify refused" in rows
        assert "server not seen for 7 min" in rows and "No stop recorded" not in rows


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_demo_page_strip_has_no_protection_column():
    out = _node(str(Path(__file__).resolve().parent / "ui_mode_harness.js"), "demo")
    assert out["strip_text"] == "PRACTICE · practice money only · no real orders possible" and out["strip_theme"] == "practice"
    assert out["prot_th_hidden"] is True and "prot-cell" not in out["mp_rows_html"]


def test_pages_carry_the_strip_and_the_chip_and_touch_rules():
    index = (UI / "index.html").read_text(encoding="utf-8")
    replay = (UI / "replay.html").read_text(encoding="utf-8")
    css = (UI / "nocturne.css").read_text(encoding="utf-8")
    assert 'id="modestrip"' in index and 'id="modestrip"' in replay
    assert index.index("</header>") < index.index('id="modestrip"') < index.index("<main>")      # directly under the header
    assert 'id="freshness"' in index and 'id="fresh-text"' in index and "/api/freshness" in index
    assert ".modestrip { position: sticky; top: 0;" in css
    assert "touch-action: pan-y" in css and "@media (pointer: coarse)" in css and "min-height: 44px" in css
    for name in ("portfolio", "deals", "signal-lab", "lookup"):
        assert f'data-anchor="{name}"' in index
    assert "history.replaceState" in index and "mousemove" in (UI / "common.js").read_text(encoding="utf-8")
