"""Screener page: the browser logic through a node harness, the markup, navigation and palette command, the
read-only guarantees, the Settings switch for the TradingView columns, and the phone-width fix for long status pills."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from .test_ui import _get, _post, server  # noqa: F401  (server is a fixture)

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "trading_agent" / "ui"
HTML = (UI / "screener.html").read_text(encoding="utf-8")
JS = (UI / "screener.js").read_text(encoding="utf-8")
CSS = (UI / "nocturne.css").read_text(encoding="utf-8")
INDEX = (UI / "index.html").read_text(encoding="utf-8")
REPLAY = (UI / "replay.html").read_text(encoding="utf-8")
PALETTE = (UI / "palette.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _code(js: str) -> str:
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$|\s//\s.*$", "", js)


def test_screener_logic_in_node_harness():
    if not NODE:
        pytest.skip("node not installed")
    r = subprocess.run([NODE, str(Path(__file__).parent / "screener_harness.js")], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr + r.stdout
    out = json.loads(r.stdout)
    assert out["failures"] == [], out["failures"]
    assert out["n"] >= 20


def test_script_parses():
    if not NODE:
        pytest.skip("node not installed")
    r = subprocess.run([NODE, "--check", str(UI / "screener.js")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# ---------- markup ----------
def test_page_has_the_pickers_filters_table_and_export():
    for needle in ('id="sc-universe"', 'id="sc-presets"', 'id="sc-q"', 'id="sc-chips"', 'id="sc-addbtn"', 'id="sc-pop"', 'id="sc-density"',
                   'id="sc-allcols"', 'id="sc-export"', 'id="sc-save"', 'id="sc-head"', 'id="sc-rows"', 'id="sc-count"', 'id="sc-fresh"',
                   'id="sc-status"', 'id="sc-drawer"', 'aria-controls="sc-pop"',
                   'name="viewport" content="width=device-width, initial-scale=1"', "<title>Screener"):
        assert needle in HTML, needle
    assert HTML.index("/static/common.js") < HTML.index("/static/palette.js") < HTML.index("/static/screener.js")


def test_every_page_links_to_the_screener_and_it_marks_itself_current():
    for html in (INDEX, REPLAY, HTML):
        assert '<a href="/screener" data-tab="screener"' in html
    assert 'data-tab="screener" aria-current="page"' in HTML
    for html in (INDEX, REPLAY):
        assert 'data-tab="screener" aria-current' not in html


def test_screener_page_has_the_search_button_and_theme_switch():
    assert 'id="btn-palette"' in HTML and "<span>Search</span>" in HTML and 'aria-keyshortcuts="Control+K Meta+K"' in HTML
    head = HTML.split("</head>")[0]
    assert "dataset.theme" in head and 'localStorage.getItem("theme")' in head and head.index("dataset.theme") < head.index("nocturne.css")
    for choice in ("dark", "light", "auto"):
        assert f'data-theme-choice="{choice}"' in HTML
    assert 'class="toast"' in HTML


def test_palette_has_a_screener_command_everywhere_but_on_the_screener():
    assert 'screener: ["Screener", "/screener"]' in PALETTE
    if not NODE:
        pytest.skip("node not installed")
    js = ("const P=require(process.argv[1]);const o={};for(const m of ['live','replay','demo','screener']){"
          "o[m]=P.buildCommands({mode:m}).filter(c=>c.id.startsWith('page-')).map(c=>c.run.url);}"
          "const hit=P.filterCommands(P.buildCommands({mode:'live'}),'screener').map(x=>x.cmd.label);"
          "console.log(JSON.stringify({o,hit}));")
    r = subprocess.run([NODE, "-e", js, str(UI / "palette.js")], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout)
    assert "/screener" in got["o"]["live"] and "/screener" in got["o"]["replay"] and "/screener" in got["o"]["demo"]
    assert "/screener" not in got["o"]["screener"] and "/" in got["o"]["screener"]
    assert got["hit"][0] == "Screener page"


def test_stock_actions_from_the_screener_page_navigate_to_the_page_that_has_them():
    if not NODE:
        pytest.skip("node not installed")
    js = ("const P=require(process.argv[1]);const o={};for(const op of ['lookup','size','chart','buy']){"
          "o[op]=P.routeStockOp(op,'TCS','screener',{});}console.log(JSON.stringify(o));")
    r = subprocess.run([NODE, "-e", js, str(UI / "palette.js")], capture_output=True, text=True, encoding="utf-8")
    got = json.loads(r.stdout)
    assert got["lookup"]["url"] == "/?lookup=TCS" and got["buy"]["url"] == "/demo?buy=TCS" and all(v["via"] == "navigate" for v in got.values())


# ---------- read-only, no orders, no colour literals ----------
def test_script_only_reads_the_screener_endpoint_and_cannot_send_an_order():
    code = _code(JS)
    for needle in ("/api/order", "/api/close", "/api/practice", "/api/stop", "/api/settings", "method:", "POST", "fetch(", "XMLHttpRequest",
                   "sendBeacon", "groww", "Groww", "GROWW", "live_orders", "liveOrders", "paper_order", "confirm("):
        assert needle not in code, needle
    assert re.findall(r"""["'`](/api/[^"'`?]*)""", code) == ["/api/screener"]
    assert "api(" in code and "TA.palette.landingUrl(" in code     # the details drawer links to Look up through the shared landing address


def test_no_colour_literals_in_the_screener_page_code():
    literal = re.compile(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(")
    for name, text in (("screener.html", HTML), ("screener.js", JS)):
        assert not literal.findall(text), (name, literal.findall(text))


def test_local_storage_is_always_wrapped_in_try_catch():
    for m in re.finditer(r"localStorage\.(get|set)Item", JS):
        window = JS[max(0, m.start() - 80):m.end() + 60]
        assert "try {" in window, window
    assert "ta_screener_presets" in JS


def test_user_text_is_escaped_into_the_table_and_chips():
    # every place a server or user string reaches innerHTML goes through esc(...)
    for needle in ("esc(r.symbol)", "esc(r.name)", "esc(describeFilter(fl))", "esc(v)", "esc(c.label)", "esc(p.name)", "esc(data.error)"):
        assert needle in JS, needle
    assert "innerHTML = d." not in JS and "innerHTML = data." not in JS


# ---------- Settings switch ----------
def test_tradingview_switch_is_in_settings_and_says_plainly_what_it_is():
    assert 'id="f-tv"' in INDEX
    note = INDEX[INDEX.index('id="f-tv-note"'):][:1200]
    for phrase in ("Off by default", "unofficial", "terms restrict automated access", "never called from the server", "No login, no cookies", "24 hours"):
        assert phrase in note, phrase
    assert "body.tradingview_screener = $(\"f-tv\").checked" in INDEX
    assert "tradingview_allowed" in INDEX


def test_tradingview_switch_persists_and_defaults_off(server):
    base, app = server
    _, st = _get(base + "/api/state")
    assert st["settings"]["tradingview_screener"] is False and st["settings"]["tradingview_allowed"] is True
    status, j = _post(base + "/api/settings", {"tradingview_screener": True})
    assert status == 200 and j["applied"] == {"TRADINGVIEW_SCREENER": "true"}
    assert "TRADINGVIEW_SCREENER=true" in (app.settings.state_dir / ".env").read_text()
    assert app.settings.tradingview_screener is True
    assert _get(base + "/api/state")[1]["settings"]["tradingview_screener"] is True
    status, j = _post(base + "/api/settings", {"tradingview_screener": "maybe"})
    assert status == 400
    assert _post(base + "/api/settings", {"tradingview_screener": False})[0] == 200 and app.settings.tradingview_screener is False


def test_the_server_role_is_reported_to_the_page_and_never_editable_from_it(server):
    base, app = server
    app.settings.ta_role = "server"
    assert _get(base + "/api/state")[1]["settings"]["tradingview_allowed"] is False
    status, j = _post(base + "/api/settings", {"ta_role": "laptop"})
    assert status == 200 and j["applied"] == {} and app.settings.ta_role == "server"     # unknown keys are ignored, so the role stays


# ---------- phone: long status pills wrap instead of widening the page ----------
def test_status_pills_may_wrap_on_a_phone():
    assert re.search(r"\.pill \{ max-width: 100%; \}", CSS)
    m = re.search(r"@media \(max-width: 700px\) \{ \.pill \{ white-space: normal; \}", CSS)
    assert m, "a long integration or freshness text must wrap inside its pill at phone width"


def test_screener_css_uses_tokens_and_sticks_the_stock_column():
    block = CSS[CSS.index("/* Screener page"):]
    assert not re.findall(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(", block)
    assert re.search(r"\.sc-table th\.sc-stock, \.sc-table td\.sc-stock \{ position: sticky; left: 0;", block)
    assert ".sc-scroll { max-height: 72vh; overflow: auto; }" in block
