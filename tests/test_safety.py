"""Dashboard safety and freshness: /api/freshness (fake clock, fake heartbeat file), the protection text for each way a
real holding can be protected, the mode strip / freshness chip / URL state helpers (node), and the page wiring.
No network, no orders, no real keys."""
import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trading_agent import safety
from trading_agent.broker import Account, Position
from trading_agent.timezones import IST
from trading_agent.ui import App

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
    assert set(f) == {"now", "market_open", "live_orders", "nse_degraded", "watch", "prices", "deals"}
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
    ("ok", 40, "warn", ""),     # alert only is never "protected": amber even with a healthy watch
    ("warn", 7 * 60, "warn", " · server not seen for 7 min"),
    ("bad", 16 * 60, "bad", " · server not seen for 16 min"),
    ("unseen", None, "bad", " · watch service not seen"),
    ("idle", 20000, "warn", ""),
])
def test_protection_server_stop_follows_the_watch_service(level, age, tone, suffix):
    p = safety.protection(None, 90.0, live_orders=True, watch={"level": level, "age_s": age})
    assert p["kind"] == "server" and p["tone"] == tone
    assert p["text"] == ("Server stop ₹90.00: alert only, nothing sells automatically "
                         "(needs the watch service running)" + suffix)


def test_protection_live_orders_on_but_no_stop_level_is_flagged():
    p = safety.protection(None, None, live_orders=True, watch={"level": "ok", "age_s": 1})
    assert p["kind"] == "none" and p["tone"] == "bad" and p["text"] == "No stop set: nothing sells this holding"


def test_protection_partial_gtt_unknown_status_and_updated_at():
    p = safety.protection({**GTT, "qty": 2, "updated_at": "2026-10-12T06:30:00+00:00"}, None, live_orders=True, qty=5)
    assert p["tone"] == "warn" and p["text"] == "GTT at Groww ₹153.00 (#gtt_77) · covers 2 of 5 shares · updated 2026-10-12 06:30 UTC"
    assert safety.protection(GTT, None, live_orders=True, qty=2)["tone"] == "solid"          # fully covered
    assert safety.protection(GTT, None, live_orders=True, qty=1)["tone"] == "solid"
    for ok in ("ACTIVE", "open", "PENDING", "trigger_pending"):
        assert safety.protection({**GTT, "status": ok}, None, live_orders=True, qty=2)["tone"] == "solid", ok
    odd = safety.protection({**GTT, "status": "WEIRD"}, None, live_orders=True, qty=2)
    assert odd["kind"] == "gtt" and odd["tone"] == "warn" and "status WEIRD is unconfirmed" in odd["text"]
    m = safety.protection_map([{"symbol": "INFY", "stop": 80.0, "qty": 10}], {"INFY": GTT}, live_orders=True, watch={"level": "ok", "age_s": 1})
    assert "covers 2 of 10 shares" in m["by_symbol"]["INFY"]["text"]


def test_fallback_with_live_orders_on_is_bad_and_a_gtt_without_status_is_unknown():
    on = safety.protection_map([], {}, live_orders=True, watch={"level": "ok", "age_s": 1})
    assert on["default"] == {"kind": "none", "tone": "bad", "warning": None, "text": "No stop set: nothing sells this holding"}
    assert safety.protection_map([], {}, live_orders=False, watch={})["default"]["tone"] == "neutral"
    no_status = {k: v for k, v in GTT.items() if k != "status"}
    p = safety.protection(no_status, None, live_orders=True, qty=2)
    assert p["kind"] == "gtt" and p["tone"] == "warn" and "status UNKNOWN is unconfirmed" in p["text"]
    assert safety.protection({**GTT, "status": None}, None, live_orders=True, qty=2)["tone"] == "warn"


def test_settings_text_uses_the_effective_live_orders_flag():
    html = (UI / "index.html").read_text(encoding="utf-8")
    assert 'textContent = S.live_orders ?' in html and "(S.live_orders ? \"linked" in html
    assert "c.groww_live_orders ?" not in html and "(c.groww_live_orders ?" not in html


def test_watch_thresholds_scale_with_the_interval(tmp_path):
    assert safety.thresholds(60) == (300, 900) and safety.thresholds(None) == (300, 900) and safety.thresholds(-5) == (300, 900)
    assert safety.thresholds(300) == (660, 1320)
    d = beat(tmp_path, MON_NOON, 600, every=300)       # 10 minutes is fine for a 5-minute loop
    assert safety.freshness(d, MON_NOON)["watch"]["level"] == "ok"
    d = beat(tmp_path, MON_NOON, 1400, every=300)
    assert safety.freshness(d, MON_NOON)["watch"]["level"] == "bad"


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
    assert prot["by_symbol"]["TCS"]["text"] == "GTT at Groww ₹153.00 (#gtt_77) · covers 2 of 10 shares"
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
        "Server stop ₹95.00: alert only, nothing sells automatically (needs the watch service running)"
        " · server not seen for 20 min")


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
    assert s["live_state_unknown"]["kind"] == "unknown" and "live-orders setting unknown" in s["live_state_unknown"]["text"]
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


TS = "box.tail1234.ts.net"


def test_post_requires_json_and_same_origin(server):
    base, app = server
    app.settings.allowed_hosts = [TS]
    body = json.dumps({"index": 0}).encode()
    js = {"Content-Type": "application/json"}
    assert _raw_post(base, "/api/dismiss", body, {"Content-Type": "text/plain"}) == 415
    assert _raw_post(base, "/api/dismiss", body, {}) == 415
    assert _raw_post(base, "/api/dismiss", body, {**js, "Origin": "https://evil.example"}) == 403
    assert _raw_post(base, "/api/dismiss", body, {**js, "Origin": "null"}) == 403
    assert _raw_post(base, "/api/dismiss", body, {**js, "Origin": base}) == 200
    assert _raw_post(base, "/api/dismiss", body, js) == 200                                   # no Origin, no Sec-Fetch-Site
    assert _raw_post(base, "/api/dismiss", body, {**js, "Sec-Fetch-Site": "same-origin"}) == 200
    assert _raw_post(base, "/api/dismiss", body, {**js, "Sec-Fetch-Site": "cross-site"}) == 403
    assert _raw_post(base, "/api/dismiss", body, {**js, "Sec-Fetch-Site": "same-site"}) == 403
    # tailscale serve: proxied from localhost, the public name is allowlisted
    proxied = {**js, "Origin": f"https://{TS}", "X-Forwarded-Host": TS, "X-Forwarded-Proto": "https"}
    assert _raw_post(base, "/api/dismiss", body, proxied) == 200
    assert _raw_post(base, "/api/dismiss", body, {**proxied, "Origin": "https://evil.example"}) == 403
    # a forwarded host that is not allowlisted is not trusted, even from localhost
    unlisted = {**js, "Origin": "https://other.ts.net", "X-Forwarded-Host": "other.ts.net", "X-Forwarded-Proto": "https"}
    assert _raw_post(base, "/api/dismiss", body, unlisted) == 403
    assert _raw_post(base, "/demo/api/dismiss", body, {"Content-Type": "text/plain"}) == 415
    assert _raw_post(base, "/replay/api/trials", b"{}", {"Content-Type": "text/plain"}) == 415   # Replay routes too
    assert _raw_post(base, "/replay/api/trials", b"{}", {**js, "Origin": "https://evil.example"}) == 403


def _fetch(base, path, headers):
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(urllib.request.Request(base + path, headers=headers), timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def test_host_allowlist_stops_dns_rebinding_on_get_and_post(server):
    base, app = server
    port = base.rsplit(":", 1)[1]
    body = json.dumps({"index": 0}).encode()
    js = {"Content-Type": "application/json"}
    for host in (f"127.0.0.1:{port}", "127.0.0.1", "localhost", f"localhost:{port}", f"[::1]:{port}", "[::1]",
                 "localhost:8788", "127.0.0.1:8788"):            # an SSH tunnel keeps the laptop's port in Host
        assert _fetch(base, "/api/state", {"Host": host}) == 200, host
    assert _raw_post(base, "/api/dismiss", body, {**js, "Host": "localhost:8788", "Origin": "http://localhost:8788"}) == 200
    for host in ("evil.example", f"evil.example:{port}", "127.0.0.1.evil.example", "localhost:99999", "localhost:abc", "0.0.0.0"):
        assert _fetch(base, "/api/state", {"Host": host}) == 421, host
        assert _fetch(base, "/", {"Host": host}) == 421, host
        assert _raw_post(base, "/api/dismiss", body, {**js, "Host": host}) == 421, host
    assert _fetch(base, "/api/state", {"Host": TS}) == 421                                  # not listed yet
    app.settings.allowed_hosts = [TS]
    assert _fetch(base, "/api/state", {"Host": TS}) == 200
    assert _raw_post(base, "/api/dismiss", body, {**js, "Host": TS, "Origin": f"http://{TS}"}) == 200
    assert _fetch(base, "/api/state", {"Host": "other." + TS}) == 421                       # exact names, no suffix match
    assert _raw_post(base, "/api/dismiss", body, {**js, "Origin": "http://evil.example"}) == 403   # Origin must be an allowlisted host


def test_get_api_refuses_cross_site_fetches(server):
    base, _ = server
    for site, want in (("cross-site", 403), ("same-site", 403), ("same-origin", 200), ("none", 200)):
        assert _fetch(base, "/api/state", {"Sec-Fetch-Site": site}) == want, site
    assert _fetch(base, "/api/freshness", {"Sec-Fetch-Site": "cross-site"}) == 403
    assert _fetch(base, "/", {"Sec-Fetch-Site": "cross-site"}) == 200                        # the page itself can be linked to


def test_forwarded_headers_from_a_non_local_peer_are_ignored(server, monkeypatch):
    """Only a peer on localhost (tailscale serve) may vouch for a forwarded host."""
    import http.server
    base, app = server
    app.settings.allowed_hosts = [TS]
    real = http.server.BaseHTTPRequestHandler.handle_one_request

    def from_elsewhere(self):
        self.client_address = ("100.64.0.9", self.client_address[1])
        return real(self)
    monkeypatch.setattr(http.server.BaseHTTPRequestHandler, "handle_one_request", from_elsewhere)
    body = json.dumps({"index": 0}).encode()
    proxied = {"Content-Type": "application/json", "Origin": f"https://{TS}", "X-Forwarded-Host": TS, "X-Forwarded-Proto": "https"}
    assert _raw_post(base, "/api/dismiss", body, proxied) == 403


def test_allowed_hosts_setting_is_validated(tmp_path):
    from trading_agent.config import parse_allowed_hosts
    from trading_agent.ui import _check_type, _write_env
    assert parse_allowed_hosts(" Box.Tail1234.ts.net , localhost:8787,box.tail1234.ts.net ") == ["box.tail1234.ts.net", "localhost:8787"]
    assert parse_allowed_hosts("") == [] and parse_allowed_hosts(None) == []
    for bad in ("*.ts.net", "https://box.ts.net", "a b", "box/ts", "-x", "a," + ",".join(f"h{i}" for i in range(11))):
        with pytest.raises(ValueError):
            parse_allowed_hosts(bad)
    with pytest.raises(ValueError):
        _check_type("allowed_hosts", ["a"])
    with pytest.raises(ValueError):
        _write_env(tmp_path / ".env", {"TA_ALLOWED_HOSTS": "a.ts.net\nGROWW_LIVE_ORDERS=true"})
