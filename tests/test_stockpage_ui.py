"""Stock page front end (trading_agent/ui/stockpage.js): pure helpers through node, the practice-only rule for the Buy / Sell
buttons, the reusable-component shape, and the markup / CSS the layout depends on. No browser, no network."""
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
JS = (UI / "stockpage.js").read_text(encoding="utf-8")
INDEX = (UI / "index.html").read_text(encoding="utf-8")
CSS = (UI / "nocturne.css").read_text(encoding="utf-8")
COMMON = (UI / "common.js").read_text(encoding="utf-8")


def _code(js: str) -> str:
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$|\s//\s.*$", "", js)


def _node(expr: str):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    r = subprocess.run([node, "-e", f"const P=require(process.argv[1]).helpers;console.log(JSON.stringify({expr}))", str(UI / "stockpage.js")],
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_scripts_parse():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    for name in ("stockpage.js", "common.js", "palette.js"):
        r = subprocess.run([node, "--check", str(UI / name)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr


def test_formatting_helpers():
    out = _node("""{cr: [P.crore(1234567.4), P.crore(0.5), P.crore(null)], rs: [P.rupees(1234.5), P.rupees(-3.2), P.rupees(null)],
        pct: [P.percent(0.1234), P.percent(null)], sp: [P.signedPercent(0.1), P.signedPercent(-0.025), P.signedPercent(null)],
        vol: P.volume(1234567), ratio: [P.ratio(0.9), P.ratio(undefined)], tone: [P.tone(1), P.tone(-1), P.tone(0), P.tone(null)]}""")
    assert out["cr"] == ["₹12,34,567 Cr", "₹0.50 Cr", "n/a"] and out["rs"] == ["₹1,234.50", "−₹3.20", "n/a"]
    assert out["pct"] == ["12.34%", "n/a"] and out["sp"] == ["+10.00%", "−2.50%", "n/a"]
    assert out["vol"] == "12,34,567" and out["ratio"] == ["0.90", "n/a"] and out["tone"] == ["pl-profit", "pl-loss", "", ""]


def test_marker_position_and_circuit_text():
    out = _node("""{m: [P.markerPos(15, 10, 20), P.markerPos(5, 10, 20), P.markerPos(25, 10, 20), P.markerPos(15, 10, 10), P.markerPos(null, 1, 2)],
        c: [P.circuitText({state: "band", lower: 90, upper: 110}, "lower"), P.circuitText({state: "none"}, "upper"), P.circuitText({state: "unknown"}, "upper"), P.circuitText(null, "lower")]}""")
    assert out["m"] == [0.5, 0, 1, None, None]
    assert out["c"] == ["₹90.00", "No band", "n/a", "n/a"]


def test_bars_period_view_and_holding_figures():
    out = _node("""{b: P.barLayout([{label: "a", revenue_cr: 100, profit_cr: 25}, {label: "b", revenue_cr: 50, profit_cr: -10}], 80),
        v: [P.periodView([{label: "x"}, {label: "y"}], null).cur.label, P.periodView([{label: "x"}, {label: "y"}], 0).prev, P.periodView([], 0)],
        h: P.holdingFigures({qty: 10, avg_entry_price: 100, stop: 90, stop_label: "trailing"}, 120), none: P.holdingFigures(null, 1), zero: P.holdingFigures({qty: 0}, 1),
        g: P.holdingFigures({qty: 2, avg_price: 50}, null)}""")
    assert out["b"][0] == {"label": "a", "revenue": 80, "profit": 20, "loss": False}      # revenue and profit share one scale
    assert out["b"][1]["loss"] is True and out["b"][1]["profit"] == 8
    assert out["v"] == ["y", None, None]
    h = out["h"]
    assert (h["qty"], h["value"], h["pl"], h["plPct"], h["stop"]) == (10, 1200, 200, 0.2, 90)
    assert out["none"] is None and out["zero"] is None
    assert out["g"]["avg"] == 50 and out["g"]["value"] is None and out["g"]["pl"] is None   # a Groww row (avg_price), no price yet


# ---------- practice only ----------
def test_stock_page_code_has_no_way_to_send_an_order():
    code = _code(JS)
    for needle in ("/api/order", "/api/close", "/api/practice", "/api/stop", "method:", "POST", "fetch(", "XMLHttpRequest", "sendBeacon",
                   "groww", "Groww", "GROWW", "live_orders", "liveOrders", "paper_order", "confirm(", "submit("):
        assert needle not in code, needle
    # the only server calls are GET reads through the page's api helper
    assert sorted(set(re.findall(r"""["'`](/api/[^"'`?]*)""", code))) == ["/api/candles", "/api/lookup", "/api/news", "/api/stock"]


def test_buttons_are_labelled_practice_and_only_call_the_host():
    assert JS.count(">Practice sell</button>") == 1 and JS.count(">Practice buy</button>") == 1     # one shared template
    assert not re.search(r">\s*(Buy|Sell)\s*</button>", JS)
    assert "opts.onPractice(t.dataset.practice, st.ticker)" in JS
    # the dashboard routes them like the Ctrl+K palette: fill the practice form (Demo) or open /demo?buy= / ?sell=
    block = INDEX[INDEX.index("function practiceOrder("):INDEX.index("const stockOpts")]
    assert "TA.palette.routeStockOp(op, symbol, MODE, palHost)" in block
    assert "api(" not in block and "/api/order" not in block and "fetch(" not in block and "liveOrders" not in block and "live_orders" not in block
    assert "palHost[t.op](t.symbol)" in block and "location.assign(t.url)" in block
    sell = INDEX[INDEX.index("palHost.sell ="):INDEX.index("TA.palette.install(palHost)")]
    assert 'api("/api/order"' not in sell and "requestSubmit" not in sell and ".submit(" not in sell
    assert '$("tk-symbol").value = s' in sell and "Place paper order" in sell


def test_practice_sell_goes_to_the_demo_page_from_live_in_every_setting():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    js = ("const P=require(process.argv[1]);const o={};for(const mode of ['live','replay','demo']){for(const live of [true,false]){"
          "const host={liveOrders:live,GROWW_LIVE_ORDERS:String(live),lookup(){}};"
          "o[mode+live]=P.routeStockOp('sell','SBIN',mode,host);}}console.log(JSON.stringify(o));")
    r = subprocess.run([node, "-e", js, str(UI / "palette.js")], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout)
    assert len(got) == 6 and all(v == {"via": "navigate", "op": "sell", "symbol": "SBIN", "url": "/demo?sell=SBIN"} for v in got.values())


# ---------- reusable component ----------
def test_stock_page_is_one_component_without_page_globals():
    assert "function stockPage(host, ticker, opts)" in JS and "window.TA.stockPage = stockPage" in JS
    for api in ("load,", "setExchange", "destroy()"):
        assert api in JS
    code = _code(JS)
    assert "document.getElementById" not in code and "getElementById" not in code   # nothing by page id: it only touches its own root
    assert "document.querySelector" not in code and "document.addEventListener" not in code and "window.addEventListener" not in code
    assert "uid" in JS and 'id="${id}-t-${k}"' in JS                                 # ids are per instance, so two can coexist
    # index.html mounts it twice: in the Look up card and in the phone drawer
    assert 'TA.stockPage($("lk-result")' in INDEX and 'TA.stockPage($("sp-drawer-body")' in INDEX


def test_exchange_switch_drives_chart_price_and_quote():
    assert "chartCtl.setExchange(x)" in JS and "loadStock(seq)" in JS
    assert 'exchangeChips: false' in JS and "&exchange=" in JS
    assert "setExchange(x){" in COMMON and "exchangeChips !== false" in COMMON


# ---------- layout, tabs, sections ----------
def test_tabs_and_sections_follow_the_brief():
    assert [t for t in re.findall(r'\["(overview|technicals|news)", "([A-Za-z]+)"\]', JS)] == [("overview", "Overview"), ("technicals", "Technicals"), ("news", "News")]
    assert 'role="tablist"' in JS and 'role="tab"' in JS and 'role="tabpanel"' in JS and "aria-selected" in JS
    for title in ("Insights", "Performance", "Fundamentals", "Financial performance", "About company", "Shareholding pattern", "Similar stocks"):
        assert f'"{title}"' in JS, title
    assert "Top mutual funds" not in JS and "F&amp;O" not in JS and "F&O" not in JS
    for label in ("Mkt cap", "ROE", "P/E (TTM)", "EPS (TTM)", "P/B", "Div yield", "Industry P/E", "Book value", "Debt to equity", "Face value",
                  "Lower circuit", "Upper circuit", "Prev. close", "1Y (TTM)", "3Y CAGR", "Promoters", "DIIs", "Public", "FIIs"):
        assert label in JS, label
    assert "unavailable from NSE right now" in (ROOT / "trading_agent" / "stockpage.py").read_text(encoding="utf-8")
    assert "dflt = !phone()" in JS                      # Insights collapsed by default on phones


def test_chart_ranges_line_default_and_indicators_only_in_candles():
    assert 'STOCK_RANGES = ["1D", "1W", "1M", "3M", "6M", "1Y", "5Y", "ALL"]' in COMMON
    assert 'mode: "line"' in COMMON                                                      # line is the default
    assert "ind = c && !(data && data.intraday)" in COMMON                               # no indicators in line mode or on intraday bars
    assert "inds.hidden = prefs.mode !== \"candle\"" in COMMON
    assert 'title: "Prev close"' in COMMON and "data.intraday && data.prev_close" in COMMON


def test_layout_markup_and_css():
    assert 'id="sp-drawer"' in INDEX and 'class="sp-drawer"' in INDEX and "showModal" in INDEX and 'addEventListener("popstate"' in INDEX
    assert "stock=" in COMMON and "out.stock" in COMMON                                  # ?stock=SYM is read and written
    assert INDEX.index("/static/common.js") < INDEX.index("/static/stockpage.js") < INDEX.index("/static/lightweight-charts.js")
    part = CSS[CSS.index("Stock page (stockpage.js)"):]
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(", part)
    assert "position: sticky; bottom: 0" in part and "@media (max-width: 700px)" in part and "dialog.sp-drawer" in part
    assert "min-height: 44px" in part                                                    # phone tap targets


def test_stockpage_script_is_served(settings):
    from trading_agent.ui import App, make_server
    srv = make_server(App(settings, dotenv=None), port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        with urllib.request.urlopen(base + "/static/stockpage.js") as r:
            assert r.headers["Content-Type"].startswith("text/javascript")
            assert b"stockPage" in r.read()
        page = urllib.request.urlopen(base + "/?stock=TCS").read().decode()
        assert "/static/stockpage.js" in page and 'id="lk-result"' in page
    finally:
        srv.shutdown()
        srv.server_close()
