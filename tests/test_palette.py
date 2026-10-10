"""Ctrl+K command palette (trading_agent/ui/palette.js): the logic through a node harness (no browser, no network),
the page markup and accessibility attributes, and the rule that the palette can never place an order, least of all a
live one."""
import json
import re
import shutil
import subprocess
import threading
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "trading_agent" / "ui"
PALETTE = (UI / "palette.js").read_text(encoding="utf-8")
INDEX = (UI / "index.html").read_text(encoding="utf-8")
REPLAY = (UI / "replay.html").read_text(encoding="utf-8")


def _code(js: str) -> str:
    """The script without comments, so a sentence in a comment cannot hide or fake something in the code."""
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$|\s//\s.*$", "", js)


def test_palette_logic_in_node_harness():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    r = subprocess.run([node, str(Path(__file__).parent / "palette_harness.js")], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr + r.stdout
    out = json.loads(r.stdout)
    assert out["failures"] == [], out["failures"]
    assert out["n"] >= 15   # the harness really ran its checks


def test_palette_script_parses():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    r = subprocess.run([node, "--check", str(UI / "palette.js")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# ---------- the palette never places an order, and never offers a live one ----------
def test_palette_code_has_no_way_to_send_an_order():
    code = _code(PALETTE)
    for needle in ("/api/order", "/api/close", "/api/practice", "/api/stop", "method:", "POST", "fetch(", "XMLHttpRequest",
                   "sendBeacon", "groww", "Groww", "GROWW", "live_orders", "liveOrders", "paper_order", "confirm("):
        assert needle not in code, needle
    # the only server call it makes is the stock search, through the shared GET helper
    assert re.findall(r"""["'`](/api/[^"'`?]*)""", code) == ["/api/search"]
    assert "TA.api(u)" in code


def test_palette_offers_one_buy_and_it_is_the_practice_one():
    assert 'buy: "Practice buy"' in PALETTE
    assert "Live" not in re.findall(r"OP_LABEL = \{[^}]*\}", PALETTE)[0]
    assert re.search(r'OP_HINT = \{[^}]*buy: "practice account only', PALETTE)
    # the Demo page's own handler only fills the existing form; it does not submit it
    handler = INDEX[INDEX.index("palHost.buy = (s) =>"):]
    handler = handler[:handler.index("};")]
    assert 'api("/api/order"' not in handler and "requestSubmit" not in handler and ".submit(" not in handler
    assert '$("tk-symbol").value = s' in handler and "Place paper order" in handler
    # and the page code around the palette never posts either
    block = INDEX[INDEX.index("// ---- Ctrl+K command palette"):INDEX.index("refresh().then(() => { if(S && S.busy)")]
    assert "api(" not in block.replace("loadMyPortfolio", "").replace("loadFreshness", "").replace("loadIntegration", "").replace("Promise.all([refresh()", "")
    assert "/api/order" not in block and "/api/close" not in block


def test_practice_buy_on_live_goes_to_the_demo_page_not_to_an_order():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    js = ("const P=require(process.argv[1]);"
          "const o={};for(const mode of ['live','replay','demo']){for(const live of [true,false]){"
          "const host={liveOrders:live,GROWW_LIVE_ORDERS:String(live),lookup(){},size(){},chart(){}};"
          "o[mode+live]=P.routeStockOp('buy','SBIN',mode,host);}}console.log(JSON.stringify(o));")
    r = subprocess.run([node, "-e", js, str(UI / "palette.js")], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout)
    assert len(got) == 6
    assert all(v == {"via": "navigate", "op": "buy", "symbol": "SBIN", "url": "/demo?buy=SBIN"} for v in got.values())


# ---------- markup and accessibility ----------
@pytest.mark.parametrize("html", [INDEX, REPLAY], ids=["index", "replay"])
def test_pages_have_the_search_button_and_load_the_palette(html):
    assert 'id="btn-palette"' in html and 'aria-haspopup="dialog"' in html and 'aria-keyshortcuts="Control+K Meta+K"' in html
    assert "<span>Search</span>" in html     # a visible label, so a phone user can find it
    assert html.index("/static/common.js") < html.index("/static/palette.js")
    assert "TA.palette.install(" in html


def test_dialog_roles_and_combobox_attributes():
    for needle in ('role", "dialog"', 'aria-modal", "true"', 'role="combobox"', 'role="listbox"', 'aria-controls="pal-list"',
                   'role="option"', "aria-activedescendant", 'aria-selected="${on}"', 'aria-live="polite"', "ArrowDown", "ArrowUp",
                   '"Enter"', "Backspace", "e.key !== \"Tab\"", "showModal"):
        assert needle in PALETTE, needle
    # Ctrl and Cmd both open it
    assert "e.ctrlKey || e.metaKey" in PALETTE and '"k"' in PALETTE
    # focus returns to where it was
    assert "opener" in PALETTE and "o.focus()" in PALETTE


def test_palette_css_uses_tokens_and_fits_a_phone():
    css = (UI / "nocturne.css").read_text(encoding="utf-8")
    part = css[css.index("Ctrl+K command palette"):]
    assert "dialog.pal" in part and "calc(100vw - 24px)" in part and "(max-width: 700px)" in part
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(", part)


# ---------- the server side that is touched ----------
def test_palette_script_is_served_and_pages_still_open_with_the_landing_addresses(settings):
    from trading_agent.ui import App, make_server
    srv = make_server(App(settings, dotenv=None), port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        with urllib.request.urlopen(base + "/static/palette.js") as r:
            assert r.headers["Content-Type"].startswith("text/javascript")
            assert b"createModel" in r.read()
        for path in ("/?lookup=TCS", "/?chart=TCS", "/?size=TCS", "/demo?buy=TCS", "/replay"):
            page = urllib.request.urlopen(base + path).read().decode()
            assert 'id="btn-palette"' in page and "/static/palette.js" in page, path
        # the stock search the palette uses stays a plain GET answered with a list
        hits = json.loads(urllib.request.urlopen(base + "/api/search?q=tc").read().decode())
        assert isinstance(hits, list)
    finally:
        srv.shutdown()
