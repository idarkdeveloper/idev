"""Live v2 (the Live page redesign): workspace tabs and their addresses, the agent's checklist, the palette's workspace and
holding commands, and the safety rule that none of the new page code can send an order. Node only, no browser, no network."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "trading_agent" / "ui"
INDEX = (UI / "index.html").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _code(js: str) -> str:
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$|\s//\s.*$", "", js)


def _between(a: str, b: str) -> str:
    i = INDEX.index(a)
    return INDEX[i:INDEX.index(b, i)]


# The page code this redesign added: workspaces and the URL, since-your-last-visit, deal filters and rows, recommendations,
# the sparkline / today's move loader, the Look up card and the full-view modal, and the two tools.
NEW_BLOCKS = [
    ("// ---- Workspaces (the tabs)", "// ---- stop protection per real holding"),
    ('// ---- "Since your last visit"', "function render(keepPositions)"),
    ("// ---- 30-day line and today's move", "let btCostSet"),
    ("const MPCOLS = ", '$("mp-refresh").addEventListener'),
    ("// ---- Look up card", "async function loadScorecard"),
    ('$("sz-form").addEventListener', "const download ="),
]


def _api_calls(code: str):
    """Every api(...) call in the code, as its full argument text (parentheses matched)."""
    out = []
    for m in re.finditer(r"\bapi\(", code):
        depth, i = 1, m.end()
        while depth and i < len(code):
            depth += {"(": 1, ")": -1}.get(code[i], 0)
            i += 1
        out.append(code[m.end():i - 1])
    return out


def _top_level_comma(args: str) -> bool:
    depth = 0
    quote = None
    for ch in args:
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in "\"'`":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            return True
    return False


def test_new_page_code_has_no_order_post():
    for a, b in NEW_BLOCKS:
        code = _code(_between(a, b))
        for needle in ("/api/order", "/api/close", "/api/practice", "/api/stop", "/api/check", "/api/settings", "/api/watch",
                       "fetch(", "XMLHttpRequest", "sendBeacon", "method:", "POST", "live_orders =", "confirm("):
            assert needle not in code, (a, needle)
        for args in _api_calls(code):
            assert not _top_level_comma(args), (a, args)   # api(path, body) is a POST; every call here is a one-argument GET
            assert re.match(r"""\s*[`"']/api/(candles|lookup|costs|size)""", args), (a, args)


def test_tabs_are_addressable_and_old_anchors_map_into_them():
    ws = re.search(r"const WORKSPACES = (\[.*?\]);", INDEX).group(1)
    assert [x[0] for x in json.loads(ws)] == ["overview", "deals", "research", "tools"]   # a data health tab joins later
    block = _between("const WS_OF = {", "};")
    for anchor, tab in (("portfolio", "overview"), ("practice", "overview"), ("forward", "overview"), ("recs", "overview"),
                        ("deals", "deals"), ("orders", "deals"), ("runs", "deals"),
                        ("lookup", "research"), ("screen", "research"), ("signal-lab", "research"), ("backtest", "research"),
                        ("track-record", "research"), ("size", "tools"), ("costs", "tools")):
        assert re.search(rf'"?{re.escape(anchor)}"?: "{tab}"', block), anchor
        assert f'data-anchor="{anchor}"' in INDEX, anchor
    for tab in ("overview", "deals", "research", "tools"):
        assert f'{tab}: "{tab}"' in block
    # the chosen tab is remembered (only on a choice, never at load) and the hash names it
    assert 'store.set("liveTab", ws)' in INDEX and 'WS_OF[store.get("liveTab")]' in INDEX
    assert 'window.addEventListener("hashchange"' in INDEX and "curAnchor = id; syncUrl();" in INDEX
    # layout and table rows are settings, read early by the head script and stored with try/catch
    head = INDEX.split("</head>")[0]
    assert 'localStorage.getItem("liveDensity")' in head and 'localStorage.getItem("liveLayout")' in head
    assert "store.set(\"liveLayout\", layout)" in INDEX and "store.set(\"liveDensity\", density)" in INDEX


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_palette_offers_each_workspace_and_open_stock_per_holding():
    js = """
const P=require(process.argv[1]);
const c=P.buildCommands({mode:'live',workspaces:[{id:'overview',label:'Overview'},{id:'deals',label:'Deals & orders'}],
  holdings:[{symbol:'TCS',name:'Tata Consultancy'},{symbol:'tcs'},{symbol:'bad sym!'}],refresh:true});
console.log(JSON.stringify({ws:c.filter(x=>x.id.startsWith('ws-')).map(x=>[x.label,x.run]),
  hold:c.filter(x=>x.id.startsWith('hold-')).map(x=>[x.label,x.run]),
  top:P.filterCommands(c,'deals')[0].cmd.id, name:P.filterCommands(c,'tata')[0].cmd.id,
  kinds:[...new Set(c.map(x=>x.run.kind))].sort()}));
"""
    r = subprocess.run([NODE, "-e", js, str(UI / "palette.js")], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    o = json.loads(r.stdout)
    assert o["ws"] == [["Go to Overview", {"kind": "workspace", "id": "overview"}], ["Go to Deals & orders", {"kind": "workspace", "id": "deals"}]]
    assert o["hold"] == [["Open stock TCS", {"kind": "stock", "op": "lookup", "symbol": "TCS"}]]   # one per symbol, malformed dropped
    assert o["top"] == "ws-deals" and o["name"] == "hold-TCS"
    assert o["kinds"] == ["page", "refresh", "stock", "theme", "workspace"]   # "stock" here is only ever the read-only look-up
    palette = (UI / "palette.js").read_text(encoding="utf-8")
    assert 'run.kind === "workspace"){ if(typeof host.goWorkspace === "function") host.goWorkspace(run.id); }' in palette
    host = _between("const palHost = {", "};")
    assert "workspaces: () => WORKSPACES" in host and "holdings: () =>" in host and "goWorkspace:" in host


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_agent_checklist_from_existing_data():
    js = r"""
const P=require(process.argv[1]).helpers, now=Date.parse('2026-10-11T06:00:00Z');
const strong={verdict:'strong',ret_6m:0.2,ret_12_1:0.35,above_200dma:true,avg_turnover_60d:5e8};
const on={regime:'risk_on',markets:{nifty50:{above_200dma:true}}}, off={regime:'risk_off',markets:{nifty50:{above_200dma:false}}};
const mk=(m,o)=>Object.assign({ticker:'X',momentum:m,announcements:[],band:null,band_skip:false},o||{});
const pick=a=>({call:a.call,tone:a.tone,passN:a.passN,n:a.n,st:Object.fromEntries(a.checks.map(c=>[c.key,c.state])),d:Object.fromEntries(a.checks.map(c=>[c.key,c.detail]))});
console.log(JSON.stringify({
 clean:pick(P.agentChecks(mk(strong),{regime:on},now)),
 market:pick(P.agentChecks(mk(strong),{regime:off},now)),
 events:pick(P.agentChecks(mk(strong,{announcements:[{at:'2026-10-01 18:00:00',category:'Trading Window',text:'Closure of trading window'}]}),{regime:on},now)),
 weak:pick(P.agentChecks(mk({verdict:'weak',ret_6m:-0.2,ret_12_1:-0.3,above_200dma:false,avg_turnover_60d:2e6}),{regime:on},now)),
 band:pick(P.agentChecks(mk(strong,{band:'5%',band_skip:true,band_note:'5% band: skipped for buys'}),{regime:on},now)),
 none:pick(P.agentChecks(mk({error:'no price history'}),undefined,now)), nothing:P.agentChecks(null)}));
"""
    r = subprocess.run([NODE, "-e", js, str(UI / "stockpage.js")], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    o = json.loads(r.stdout)
    keys = ["trend", "ma200", "market", "band", "events", "liquidity"]
    assert list(o["clean"]["st"]) == keys and o["clean"]["n"] == 6
    assert o["clean"]["call"] == "Passes the rules" and o["clean"]["passN"] == 6 and o["clean"]["tone"] == "pass"
    assert "₹50 Cr a day" in o["clean"]["d"]["liquidity"] and "Risk-on" in o["clean"]["d"]["market"]
    assert o["market"]["st"]["market"] == "fail" and o["market"]["call"] == "Market says wait"
    assert o["events"]["st"]["events"] == "warn" and o["events"]["call"] == "Passes, keep it small" and "results due within weeks" in o["events"]["d"]["events"]
    assert o["weak"]["call"] == "Wait" and o["weak"]["tone"] == "fail" and o["weak"]["st"]["liquidity"] == "fail" and o["weak"]["st"]["ma200"] == "fail"
    assert o["band"]["st"]["band"] == "fail" and o["band"]["call"] != "Passes the rules"
    assert o["none"]["st"]["trend"] == "warn" and o["none"]["st"]["market"] == "warn" and o["nothing"] is None
    js_src = (UI / "stockpage.js").read_text(encoding="utf-8")
    assert 'sec("checklist", "Agent\'s checklist"' in js_src


@pytest.mark.skipif(NODE is None, reason="node not installed")
@pytest.mark.parametrize("mode", ["live", "demo"])
def test_live_page_runs_in_the_harness_with_the_new_layout(mode):
    r = subprocess.run([NODE, str(Path(__file__).parent / "ui_mode_harness.js"), mode], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr + r.stdout
    out = json.loads(r.stdout)
    if mode == "live":
        for label in ("Holdings value", "Profit / loss", "Today", "Holdings"):
            assert label in out["tiles"]
        rows = out["mp_rows_html"]
        assert 'data-open-stock="TCS"' in rows and 'class="stop-state' in rows and "Not protected" in rows   # the Stop column
        assert rows.index("LAURUSLABS") < rows.index("TCS")   # biggest holding first
    # nothing was remembered at load: the tab, layout, table rows and the visit marker are written only on a choice
    assert not {"liveTab", "liveLayout", "liveDensity", "liveSeen"} & set(out["stored"])
