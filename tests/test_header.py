"""One header on every page: Portfolio (Live and Practice), Replay and Screener share the Live page's header markup (brand, subtitle, mode tabs,
search, gear). Text-level checks, no browser."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import urllib.request
from pathlib import Path

import pytest

from trading_agent import ask

from .test_ui import server  # noqa: F401  (fixture)

UI = Path(__file__).resolve().parent.parent / "trading_agent" / "ui"


def _header(html: str) -> str:
    html = html.replace("\r\n", "\n")
    return html[html.index("<header>"):html.index("</header>") + len("</header>")]


def _normal(h: str) -> str:
    """The header without what may differ per page: the active tab and the words of the subtitle."""
    h = h.replace(' aria-current="page"', "")
    h = re.sub(r'\s*<div class="pf-row">.*?</div></div>', "", h, flags=re.S)   # only the Portfolio page has the Live / Practice switch in the bar
    return re.sub(r'(<div class="sub brand-sub" id="subtitle">).*?(</div></div>\n    </div>)', r"\1SUBTITLE\2", h, flags=re.S)


def test_the_four_pages_have_the_same_header():
    pages = {n: _header((UI / f"{n}.html").read_text(encoding="utf-8")) for n in ("index", "replay", "screener")}
    live = _normal(pages["index"])
    for n in ("replay", "screener"):
        assert _normal(pages[n]) == live, n
    for n, h in pages.items():                                                    # the Live page's own pieces, on every page
        for needle in ('class="wrap row hdr"', '<div class="brand-name">Trading Agent</div>', 'id="subtitle"', 'class="sub brand-sub"', 'id="btn-settings"',
                       'class="set-btn"', 'id="btn-palette"', 'id="lastcheck"', 'aria-label="Pages"'):
            assert needle in h, (n, needle)
        for old in ("theme-switch", "data-theme-choice", "sc-wrap"):
            assert old not in h, (n, old)
        assert [m for m in re.findall(r'data-tab="(\w+)"', h)] == ["portfolio", "replay", "screener"], n


def test_each_page_marks_its_own_tab_and_has_its_own_subtitle():
    for name, tab, words in (("replay", "replay", "Replay · practice money"), ("screener", "screener", "Screener ·")):
        h = _header((UI / f"{name}.html").read_text(encoding="utf-8"))
        assert f'data-tab="{tab}" aria-current="page"' in h and h.count('aria-current="page"') == 1, name
        assert words in h, name
    js = (UI / "replay.js").read_text(encoding="utf-8")
    assert 'Replay · practice money · data up to ${fmtDay(t.clock)}' in js          # the open replay's date is in the subtitle
    # the mode strips under the header keep their own text and colour
    assert 'class="modestrip replay"' in (UI / "replay.html").read_text(encoding="utf-8")
    assert 'class="modestrip live"' in (UI / "index.html").read_text(encoding="utf-8")


def test_the_served_demo_page_has_the_live_header(server):  # noqa: F811
    base, app = server
    got = {}
    for path in ("/", "/demo", "/replay", "/screener"):
        with urllib.request.urlopen(base + path, timeout=30) as r:
            got[path] = r.read().decode()
    live = _normal(_header(got["/"]))
    assert _normal(_header(got["/demo"])) == live and 'data-mode="demo"' in got["/demo"]
    assert _normal(_header(got["/replay"])) == live and _normal(_header(got["/screener"])) == live


def test_the_gear_on_replay_and_screener_opens_the_live_settings_and_the_page_opens_them():
    common = (UI / "common.js").read_text(encoding="utf-8")
    assert 'closest("#btn-settings")' in common and 'location.assign("/?settings=appearance")' in common
    assert '(m === "replay" || m === "screener")' in common                       # Live and Demo keep their own dialog
    index = (UI / "index.html").read_text(encoding="utf-8")
    assert 'q.get("settings") === "appearance"' in index and 'q.delete("settings")' in index and "history.replaceState" in index
    assert '$("btn-settings").click()' in index and 'id="set-appearance"' in index
    assert index.index('q.get("settings") === "appearance"') < index.index('$("btn-settings").addEventListener("click"')


def test_the_theme_key_is_shared_by_every_page():
    common = (UI / "common.js").read_text(encoding="utf-8")
    assert 'const KEY = "theme"' in common
    for name in ("index", "replay", "screener"):
        head = (UI / f"{name}.html").read_text(encoding="utf-8").split("</head>")[0]
        assert 'localStorage.getItem("theme")' in head and "dataset.theme" in head, name
    index = (UI / "index.html").read_text(encoding="utf-8")
    assert 'data-theme-choice="dark"' in index and 'data-theme-choice="auto"' in index   # Settings > Appearance, the one place to choose
    css = (UI / "nocturne.css").read_text(encoding="utf-8")
    assert 'body[data-mode="screener"] header .brand .sub' not in css                  # the Screener keeps the subtitle on a phone, as Live does


# ---- Portfolio: one screen, a Live / Practice switch under the tabs ----
def test_the_top_tabs_are_portfolio_replay_screener_and_the_switch_is_only_on_the_portfolio_page():
    index = (UI / "index.html").read_text(encoding="utf-8")
    for name in ("index", "replay", "screener"):
        h = _header((UI / f"{name}.html").read_text(encoding="utf-8"))
        assert ">Portfolio</a>" in h and ">Replay</a>" in h and ">Screener</a>" in h and ">Demo<" not in h and ">Live</a>" not in h, name
    assert "pf-switch" not in (UI / "replay.html").read_text(encoding="utf-8") and "pf-switch" not in (UI / "screener.html").read_text(encoding="utf-8")
    assert index.index('<div class="row controls">') < index.index('id="pf-switch"') < index.index("</header>") < index.index('id="modestrip"')   # in the top bar, banner below
    sw = index[index.index('<div class="pf-row">'):index.index('id="modestrip"')]
    assert 'role="radiogroup"' in sw and sw.count('role="radio"') == 2 and 'data-pf="live"' in sw and 'data-pf="practice"' in sw
    assert 'aria-checked="true"' in sw and 'aria-checked="false"' in sw and "Practice<span" in sw and "Demo" not in sw
    assert 'if(a.dataset.tab === "portfolio")' in index and 'setAttribute("href", "/demo")' in index   # Portfolio is the active tab on / and /demo


def test_the_switch_is_served_on_slash_and_demo_and_the_banners_keep_their_words(server):  # noqa: F811
    base, app = server
    for path, mode in (("/", None), ("/demo", "demo")):
        with urllib.request.urlopen(base + path, timeout=30) as r:
            html = r.read().decode()
        assert 'id="pf-switch"' in html and 'role="radiogroup"' in html and ">Portfolio</a>" in html, path
        if mode:
            assert f'data-mode="{mode}"' in html, path
    js = (UI / "common.js").read_text(encoding="utf-8")
    assert "PRACTICE · practice money only" in js and "LIVE · your real Groww account" in js


def test_the_switch_css_is_a_segmented_control_with_44px_targets_in_the_strip_colours():
    css = (UI / "nocturne.css").read_text(encoding="utf-8")
    block = css[css.index(".pf-switch {"):]
    assert "min-height: 44px" in block and "var(--strip-live-bg)" in block and "var(--strip-practice-bg)" in block and "border-radius: 999px" in block
    assert not re.search(r"#[0-9a-fA-F]{3,8}|rgba?\(", block)


def _node(code: str):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    r = subprocess.run([node, "-e", code], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_the_remembered_account_is_opened_only_at_exactly_slash():
    out = _node("""
      const P = require(%s);
      const at = (pathname, search, hash, remembered) => P.portfolioStart({pathname, search, hash}, remembered);
      console.log(JSON.stringify({
        plain: at("/", "", "", "practice"), live: at("/", "", "", "live"), none: at("/", "", "", null), junk: at("/", "", "", "x"),
        path: at("/replay", "", "", "practice"), demo: at("/demo", "", "", "practice"), query: at("/", "?stock=TCS", "", "practice"),
        hash: at("/", "", "#research", "practice"), lookup: at("/", "?lookup=TCS", "", "practice"), settings: at("/", "?settings=appearance", "", "practice"),
        nothing: P.portfolioStart(null, "practice"),
        toPractice: P.switchTarget("practice", ""), toLive: P.switchTarget("live", "#research"), keep: P.switchTarget("practice", "#tools"),
        badHash: P.switchTarget("practice", "#<script>"), badMode: P.switchTarget("x", "")}));""" % json.dumps(str(UI / "palette.js")))
    assert out == {"plain": "/demo", "live": None, "none": None, "junk": None, "path": None, "demo": None, "query": None, "hash": None, "lookup": None,
                   "settings": None, "nothing": None, "toPractice": "/demo", "toLive": "/#research", "keep": "/demo#tools", "badHash": "/demo", "badMode": "/"}
    index = (UI / "index.html").read_text(encoding="utf-8")
    assert 'KEY = "ta.portfolioMode"' in index and "set(b.dataset.pf)" in index                   # remembered when the user chooses, nowhere else
    assert index.count("set(") >= 1 and 'P.portfolioStart(location, get())' in index and 'MODE === "live"' in index
    assert "ArrowLeft" in index and "ArrowRight" in index and "tabIndex = on ? 0 : -1" in index    # keyboard: arrows move and choose, one tab stop


def test_ask_open_tab_accepts_portfolio_and_practice_as_aliases_and_nothing_else():
    for tab, want in (("portfolio", "live"), ("practice", "demo"), ("live", "live"), ("demo", "demo"), ("screener", "screener"), ("replay", "replay")):
        out = ask._finish("open_tab", {"tab": tab}, lambda *a, **k: [], "ollama")
        assert out["intent"] == "open_tab" and out["args"] == {"tab": want}, tab
    for bad in ("settings", "orders", "../x", "buy", "", "Portfolio page"):
        assert ask._finish("open_tab", {"tab": bad}, lambda *a, **k: [], "ollama")["intent"] == "unknown", bad
    assert ask.parse_rules("go to screener")["args"] == {"tab": "screener"} and ask.parse_rules("go to demo")["args"] == {"tab": "demo"}
    assert ask.parse_rules("open live")["args"] == {"tab": "live"}
    ui = (UI / "ask.js").read_text(encoding="utf-8")
    assert 'practice: "/demo"' in ui and 'portfolio: "/"' in ui
    assert 'demo: ["Practice", "/demo"]' in (UI / "palette.js").read_text(encoding="utf-8")
