"""Screener page: the browser logic through a node harness, the markup, navigation and palette command, the
read-only guarantees, the Settings switch for the TradingView columns, and the phone-width fix for long status pills."""
import json
import re
import shutil
import subprocess
import textwrap
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
                   'id="sc-status"', 'id="sc-notes"', 'id="sc-drawer"', 'aria-controls="sc-pop"',
                   'name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"', "<title>Screener"):
        assert needle in HTML, needle
    assert HTML.index("/static/common.js") < HTML.index("/static/palette.js") < HTML.index("/static/screener.js")


def test_every_page_links_to_the_screener_and_it_marks_itself_current():
    for html in (INDEX, REPLAY, HTML):
        assert '<a href="/screener" data-tab="screener"' in html
    assert 'data-tab="screener" aria-current="page"' in HTML
    for html in (INDEX, REPLAY):
        assert 'data-tab="screener" aria-current' not in html


def test_screener_page_has_the_live_header_and_applies_the_saved_theme():
    # the header is the Live page's (tests/test_header.py compares them); the theme switch lives in Settings > Appearance on the Live page
    assert 'id="btn-palette"' in HTML and "<span>Search</span>" in HTML and 'aria-keyshortcuts="Control+K Meta+K"' in HTML and 'id="btn-settings"' in HTML
    head = HTML.split("</head>")[0]
    assert "dataset.theme" in head and 'localStorage.getItem("theme")' in head and head.index("dataset.theme") < head.index("nocturne.css")
    assert "data-theme-choice" not in HTML
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
        text = re.sub(r'<meta name="theme-color" content="#[0-9a-fA-F]{6}">', "", text)   # the browser bar colour must be a literal
        assert not literal.findall(text), (name, literal.findall(text))


def test_local_storage_is_always_wrapped_in_try_catch():
    for m in re.finditer(r"localStorage\.(get|set)Item", JS):
        window = JS[max(0, m.start() - 80):m.end() + 60]
        assert "try {" in window, window
    assert "ta_screener_presets" in JS


def test_user_text_is_escaped_into_the_table_and_chips():
    # every place a server or user string reaches innerHTML goes through esc(...)
    for needle in ("esc(shortSym(r.symbol))", "esc(r.name)", "esc(describeFilter(fl))", "esc(v)", "esc(colLabel(c))", "esc(p.name)", "esc(data.error)"):
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


def test_add_filter_popup_is_not_closed_by_a_height_only_resize():
    # a phone's keyboard (raised when the popup focuses its field) resizes the height; only a width change may close the popup
    assert "wider = window.innerWidth !== lastW" in JS and "if(wider && popOpen()) closePop()" in JS


def test_the_page_groups_universes_by_market_and_switches_currency_and_hides_india_only_parts():
    assert 'id="sc-market"' in HTML
    for needle in ("UNIVERSE_GROUPS", "function switchMarket", "function applyMarket", "INDIA_ONLY.indexOf(", "numLocale = cur().loc", "view-only; prices delayed",
                   "Loading ${Math.max(0, d.total - d.pending)} of ${d.total} stocks", "r.code || r.symbol", "!(foreign() && p.india)", "cur().sym"):
        assert needle in JS, needle
    assert "/api/screener?universe=" in JS and "/api/order" not in JS


def test_screener_v3_market_tabs_title_all_chip_and_phone_cards():
    """Screener v3: India / US / UK / Japan tabs over the card (each with its exchanges and an open/closed line), the
    list's name as the title, an "All" chip before the presets, and on phones a card per stock with a Sort control."""
    assert '<div class="sc-markets" id="sc-markets" role="group" aria-label="Market"></div>' in HTML
    assert 'id="sc-sortbar"' in HTML and 'id="sc-cards"' in HTML
    for needle in ('function renderMarkets', 'function fillUniverse', 'function renderCards', 'data-preset="" aria-pressed=',
                   'if(!id){ st.preset = ""; st.filters = [];', '$("sc-title").textContent = titleOf(st.universe);',
                   '$("sc-cards").hidden = !on; $("sc-sortbar").hidden = !on;'):
        assert needle in JS, needle
    assert ".sc-main { padding-bottom: calc(96px + env(safe-area-inset-bottom, 0px)); }" in CSS   # room for the Ask bar


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_market_tabs_cover_every_list_and_nse_hours(tmp_path):
    f = tmp_path / "t.js"
    f.write_text(textwrap.dedent("""
        const L = require(process.argv[2]);
        const all = [].concat(...L.MARKET_TABS.map(m => m.universes)).sort();
        const at = (iso) => L.nseOpen(new Date(iso)).open;
        console.log(JSON.stringify({all, uni: L.UNIVERSES.slice().sort(), ids: L.MARKET_TABS.map(m => m.id),
          titles: [L.titleOf("NIFTY50"), L.titleOf("NIFTYMIDCAP150"), L.titleOf("SP500_US"), L.titleOf("HOLDINGS")],
          open: [at("2026-10-19T04:00:00Z"), at("2026-10-19T03:40:00Z"), at("2026-10-19T10:05:00Z"), at("2026-10-18T05:00:00Z")]}));
    """), encoding="utf-8")
    r = subprocess.run(["node", str(f), str(UI / "screener.js")], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    o = json.loads(r.stdout)
    assert o["all"] == o["uni"] and o["ids"] == ["IN", "US", "UK", "JP"]
    assert o["titles"] == ["Nifty 50", "Nifty Midcap 150", "S&P 500", "My holdings"]
    assert o["open"] == [True, False, False, False]   # Mon 09:30 IST open; 09:10 before; 15:35 after; Sunday closed


def test_the_loading_row_holds_only_the_loading_message_and_the_notes_stand_apart():
    """While the list loads, the progress block (spinner, message, timer) carries one short message; the filter and
    TradingView notes go to their own line, so a phone never squeezes them between the spinner and the timer."""
    body = JS[JS.index("function renderFresh"):]
    body = body[:body.index("// ---- presets")]
    assert 'TA.loader.progress($("sc-status"), bits.shift() || "Loading…"' in body
    assert 'TA.loader.clearProgress($("sc-status"), "")' in body and '$("sc-notes").textContent = bits.join(" ")' in body
    assert HTML.index('id="sc-status"') < HTML.index('id="sc-notes"') < HTML.index("Click a row for details")
