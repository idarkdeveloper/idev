"""One header on every page: Live, Demo, Replay and Screener share the Live page's header markup (brand, subtitle, mode tabs, search,
gear). Text-level checks, no browser."""
from __future__ import annotations

import re
import urllib.request
from pathlib import Path

from .test_ui import server  # noqa: F401  (fixture)

UI = Path(__file__).resolve().parent.parent / "trading_agent" / "ui"


def _header(html: str) -> str:
    html = html.replace("\r\n", "\n")
    return html[html.index("<header>"):html.index("</header>") + len("</header>")]


def _normal(h: str) -> str:
    """The header without what may differ per page: the active tab and the words of the subtitle."""
    h = h.replace(' aria-current="page"', "")
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
        assert [m for m in re.findall(r'data-tab="(\w+)"', h)] == ["live", "replay", "demo", "screener"], n


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
