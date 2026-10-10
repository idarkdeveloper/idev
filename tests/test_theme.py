"""Light/dark theme: token parity, WCAG contrast of the token pairs the pages actually use, no colour literals in
page code, and (node) the head script and the Dark / Light / Auto switch."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "trading_agent" / "ui"
CSS = (UI / "nocturne.css").read_text(encoding="utf-8")


def _span(text: str, selector: str) -> tuple[int, int]:
    """Start and end of the body of `selector { ... }` (first match, braces balanced)."""
    start = text.index(selector + " {") + len(selector) + 2
    depth, i = 1, start
    while depth:
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        i += 1
    return start, i - 1


def _block(selector: str) -> dict[str, str]:
    a, b = _span(CSS, selector)
    out: dict[str, str] = {}
    for m in re.finditer(r"(--[\w-]+)\s*:\s*((?:[^;()]|\([^)]*(?:\([^)]*\)[^)]*)*\))+)\s*;", CSS[a:b]):
        out[m.group(1)] = " ".join(m.group(2).split())
    return out


DARK = _block(":root")
LIGHT = _block(':root[data-theme="light"]')


# ---------- WCAG helpers (pure functions) ----------
def _lin(c: float) -> float:
    c /= 255
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def luminance(rgb) -> float:
    r, g, b = rgb
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def contrast(a, b) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _parse(tokens, value):
    """Resolve a token value to (r, g, b, alpha). Handles hex, rgba(), var(), color-mix(in srgb, X p%, transparent)."""
    value = value.strip()
    m = re.fullmatch(r"var\((--[\w-]+)\)", value)
    if m:
        return _parse(tokens, tokens[m.group(1)])
    m = re.fullmatch(r"#([0-9a-fA-F]{6})", value)
    if m:
        h = m.group(1)
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 1.0)
    m = re.fullmatch(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+)\s*)?\)", value)
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)), float(m.group(4) or 1))
    m = re.fullmatch(r"color-mix\(in srgb,\s*(.+?)\s+(\d+)%,\s*transparent\)", value)
    if m:
        r, g, b, a = _parse(tokens, m.group(1))
        return (r, g, b, a * int(m.group(2)) / 100)
    raise ValueError(f"cannot resolve {value!r}")


def _flat(tokens, name_or_value, over):
    """Colour as it lands on `over` (an opaque rgb tuple)."""
    r, g, b, a = _parse(tokens, tokens.get(name_or_value, name_or_value))
    return tuple(round(c * a + o * (1 - a)) for c, o in zip((r, g, b), over))


def ratio(tokens, fg: str, bg: str) -> float:
    """fg token over bg token (a translucent bg lands on the page background first)."""
    under = _flat(tokens, bg, _flat(tokens, "--color-bg", (0, 0, 0)))
    return contrast(_flat(tokens, fg, under), under)


def banner_ratio(tokens, accent: str) -> float:
    page = _flat(tokens, "--color-bg", (0, 0, 0))
    under = _flat(tokens, f"color-mix(in srgb, var({accent}) 12%, transparent)", page)
    return contrast(_flat(tokens, accent, under), under)


TEXT = 4.5
NONTEXT = 3.0
# (label, foreground token, background token, minimum ratio)
PAIRS = [
    ("body text on page", "--color-text", "--color-bg", TEXT),
    ("body text on card", "--color-text", "--color-surface", TEXT),
    ("secondary text (muted) on card", "--color-muted", "--color-surface", TEXT),
    ("secondary text (muted) on page", "--color-muted", "--color-bg", TEXT),
    ("table header on card", "--color-th", "--color-surface", TEXT),
    ("form label on card", "--color-label", "--color-surface", TEXT),
    ("form label on page", "--color-label", "--color-bg", TEXT),
    ("rationale / .down (neutral-400) on card", "--color-neutral-400", "--color-surface", TEXT),
    ("link / primary button on card", "--color-accent", "--color-surface", TEXT),
    ("link / primary button on page", "--color-accent", "--color-bg", TEXT),
    ("link hover on card", "--color-accent-600", "--color-surface", TEXT),
    ("accent-2 on card", "--color-accent-2", "--color-surface", TEXT),
    ("danger / .up text on card", "--color-accent-300", "--color-surface", TEXT),
    ("profit on card", "--color-profit", "--color-surface", TEXT),
    ("loss on card", "--color-loss", "--color-surface", TEXT),
    ("profit on page", "--color-profit", "--color-bg", TEXT),
    ("loss on page", "--color-loss", "--color-bg", TEXT),
    ("pill default", "--color-neutral-100", "--color-neutral-800", TEXT),
    ("pill ok", "--color-accent-100", "--color-accent-800", TEXT),
    ("pill bad", "--color-bg", "--color-accent-300", TEXT),
    ("pill warn on card", "--color-accent", "--color-surface", TEXT),
    ("tooltip / toast / current tab", "--color-text", "--color-neutral-900", TEXT),
    ("callout secondary", "--color-muted", "--color-neutral-900", TEXT),
    ("chart series 1", "--chart-s1", "--color-surface", NONTEXT),
    ("chart series 2", "--chart-s2", "--color-surface", NONTEXT),
    ("chart series 3", "--chart-s3", "--color-surface", NONTEXT),
    ("chart context line", "--chart-ctx", "--color-surface", NONTEXT),
    ("chart negative bars", "--chart-neg", "--color-surface", NONTEXT),
    ("chart positive bars", "--chart-pos", "--color-surface", NONTEXT),
    ("focus ring on page", "--color-accent", "--color-bg", NONTEXT),
    ("focus ring on card", "--color-accent", "--color-surface", NONTEXT),
    ("input border on card", "--color-input-border", "--color-surface", NONTEXT),
]
# Dark mode must keep its existing look, and these dark values were already under the bar before the light theme.
DARK_BASELINE_EXCEPTIONS = {"input border on card", "chart negative bars", "chart context line", "link hover on card"}


def table(tokens, skip=()):
    rows = [(label, ratio(tokens, fg, bg), need) for label, fg, bg, need in PAIRS if label not in skip]
    for name, acc in (("Replay banner", "--color-replay"), ("Demo banner", "--color-demo")):
        rows.append((name, banner_ratio(tokens, acc), TEXT))
    return rows


def test_light_contrast():
    bad = [(l, round(r, 2), n) for l, r, n in table(LIGHT) if r < n]
    assert not bad, bad


def test_dark_contrast():
    bad = [(l, round(r, 2), n) for l, r, n in table(DARK, DARK_BASELINE_EXCEPTIONS) if r < n]
    assert not bad, bad


def test_contrast_function_is_wcag():
    assert round(contrast((0, 0, 0), (255, 255, 255)), 2) == 21.0
    assert round(contrast((119, 119, 119), (255, 255, 255)), 2) == 4.48   # the well-known #777 on white


def test_light_and_dark_define_the_same_tokens():
    assert set(DARK) == set(LIGHT), sorted(set(DARK) ^ set(LIGHT))
    assert len(DARK) > 60


def test_light_tokens_actually_differ():
    for name in ("--color-bg", "--color-surface", "--color-text", "--color-accent", "--color-profit", "--color-loss"):
        assert DARK[name] != LIGHT[name], name
    assert "color-scheme: light" in CSS and "color-scheme: dark" in CSS


# ---------- no colour literals in page code ----------
LITERAL = re.compile(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(")
LITERAL_ALLOWLIST: dict[str, set[str]] = {}   # file -> literal strings allowed (none today)


@pytest.mark.parametrize("name", ["index.html", "replay.html", "common.js", "replay.js"])
def test_no_colour_literals_in_page_code(name):
    text = (UI / name).read_text(encoding="utf-8")
    found = [m.group(0) for m in LITERAL.finditer(text) if m.group(0) not in LITERAL_ALLOWLIST.get(name, set())]
    assert not found, found


def test_css_literals_live_only_in_token_blocks():
    rest = CSS
    for sel in (':root[data-theme="light"]', ":root"):
        a, b = _span(rest, sel)
        rest = rest[:a] + rest[b:]
    rest = re.sub(r"/\*.*?\*/", "", rest, flags=re.S)
    rest = re.sub(r"unicode-range:[^;]*;", "", rest)       # U+0100 ranges are not colours
    assert not LITERAL.findall(rest), LITERAL.findall(rest)


def test_every_page_has_head_script_and_switch():
    for name in ("index.html", "replay.html"):
        html = (UI / name).read_text(encoding="utf-8")
        head = html.split("</head>")[0]
        assert "dataset.theme" in head and 'localStorage.getItem("theme")' in head, name
        assert head.index("dataset.theme") < head.index("nocturne.css"), name   # before CSS paints
        for choice in ("dark", "light", "auto"):
            assert f'data-theme-choice="{choice}"' in html, (name, choice)
        assert ">Theme<" in html and 'aria-pressed="' in html


# ---------- node harness ----------
def test_theme_switch_in_node_harness():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    r = subprocess.run([node, str(Path(__file__).parent / "theme_harness.js")], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr + r.stdout
    out = json.loads(r.stdout)
    for page in ("index.html", "replay.html"):
        o = out[page]
        assert o["head"] == {"stored_light": "light", "stored_dark": "dark", "auto_os_light": "light", "auto_os_dark": "dark",
                             "storage_throws": "dark", "storage_throws_os_light": "light", "no_matchmedia": "dark",
                             "junk_value": "dark", "junk_value_os_light": "light"}, page
        assert o["init"] == {"theme": "dark", "choice": "auto", "pressed": {"dark": "false", "light": "false", "auto": "true"}}
        assert o["light_click"] == {"theme": "light", "stored": "light", "pressed": {"dark": "false", "light": "true", "auto": "false"}}
        assert o["dark_click"]["theme"] == "dark" and o["dark_click"]["stored"] == "dark"
        assert o["auto_follows_os"] == ["light", "dark", "light"]
        assert o["manual_ignores_os"] == "dark"
        assert o["auto_click"]["stored"] is None and o["auto_click"]["pressed"]["auto"] == "true"
        assert o["throwing_storage"] == {"theme": "light", "choice": "light"}
        assert o["listener_calls"] >= 3
        assert o["other_tab"] == "dark"
        assert o["switch_markup"] is True
    assert out["chart_colours"] == {"s1": "var(--chart-s1)", "grid": "var(--chart-grid)", "ring": "var(--chart-ring)"}
