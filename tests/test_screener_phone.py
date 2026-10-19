"""The phone Screener, tidied: no pointless columns button, one header row, one preset row that starts at the chosen preset, one sort row, a 3-line
labelled card; and the market status line that says when the NSE opens next."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parent.parent / "trading_agent" / "ui"
JS = (UI / "screener.js").read_text(encoding="utf-8")
CSS = (UI / "nocturne.css").read_text(encoding="utf-8")
BLOCK = CSS[CSS.index("/* Screener on a phone, tidier"):]


def test_next_open_in_the_market_status():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    code = """
      const L = require(%s);
      const at = (iso) => { const r = L.nseOpen(new Date(iso)); return [r.open, r.next]; };
      console.log(JSON.stringify([at("2026-10-19T12:00:00+05:30"), at("2026-10-19T17:00:00+05:30"), at("2026-10-16T17:00:00+05:30"), at("2026-10-17T11:00:00+05:30"),
        at("2026-10-18T11:00:00+05:30"), at("2026-10-19T07:00:00+05:30"), at("2026-10-19T09:14:00+05:30"), at("2026-10-19T09:15:00+05:30"), at("2026-10-19T15:30:00+05:30")]));""" % json.dumps(str(UI / "screener.js"))
    r = subprocess.run([node, "-e", code], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == [[True, ""], [False, "Tue 9:15"], [False, "Mon 9:15"], [False, "Mon 9:15"], [False, "Mon 9:15"], [False, "today 9:15"], [False, "today 9:15"], [True, ""], [False, "Tue 9:15"]]
    assert 'opens " + n.next' in JS and "opens ${n.next}" not in JS


def test_the_columns_button_is_hidden_on_phones_and_stays_on_tablet_and_desktop():
    assert "#sc-allcols { display: none; }" in BLOCK
    assert BLOCK.index("@media (max-width: 700px)") < BLOCK.index("#sc-allcols")
    assert 'id="sc-allcols"' in (UI / "screener.html").read_text(encoding="utf-8") and 'st.allCols ? "Fewer columns" : "All columns"' in JS


def test_one_header_row_with_the_list_as_the_title_and_the_filters_button():
    assert ".sc-top, .sc-filterbar { display: contents; }" in BLOCK
    assert re.search(r"\.sc-filterbar #sc-universe \{ grid-column: 1; grid-row: 1;[^}]*min-height: 44px[^}]*font-size: 20px", BLOCK)
    assert ".sc-filtersbtn { grid-column: 2; grid-row: 1; }" in BLOCK or ".sc-filtersbtn { margin-right: 0; }" in BLOCK
    assert ".sc-top h2 { position: absolute" in BLOCK                                           # the heading stays for screen readers (aria-labelledby)
    assert 'aria-labelledby="sc-title"' in (UI / "screener.html").read_text(encoding="utf-8")
    assert ".sc-top #sc-count { grid-column: 1 / -1; grid-row: 2;" in BLOCK


def test_presets_start_at_the_chosen_one_once_per_choice_with_a_fade():
    assert "scrolledTo !== st.preset" in JS and "$(\"sc-presets\").scrollLeft = Math.max(0, on.offsetLeft - 12 - 96)" in JS and "let scrolledTo = null" in JS
    assert "mask-image: linear-gradient(to right, black calc(100% - 36px), transparent)" in BLOCK and ".sc-pill" in CSS


def test_sort_is_one_row_with_an_icon_direction_button_that_has_a_label():
    assert 'aria-label", asc ? "Sorted low to high. Tap to reverse" : "Sorted high to low. Tap to reverse"' in JS
    assert "$(\"sc-sortdir\").innerHTML = `<svg" in JS
    assert re.search(r"\.sc-sortbar #sc-sortdir \{[^}]*width: 44px;[^}]*min-height: 44px", BLOCK)


def test_the_card_has_a_signed_change_with_an_arrow_and_a_labelled_metric():
    assert '${r.chg_1d > 0 ? "▲" : r.chg_1d < 0 ? "▼" : ""}' in JS and 'class="sc-metric"' in JS
    assert 'grid-template-areas: "sym px" "name chg" "spark sort"' in BLOCK and "min-height: 72px" in BLOCK
    assert ".sc-cr-spark svg { width: 88px; height: 28px;" in BLOCK
    for hook in ("sc-cr-sym", "sc-cr-spark", "sc-cr-px", "sc-cr-name", "sc-cr-sort", "sc-cr-chg"):
        assert hook in JS, hook                                                                  # the existing hooks stay


def test_the_phone_screener_has_room_for_the_voice_button_and_tokens_only():
    assert 'body[data-mode="screener"].va-on { padding-bottom: calc(104px + env(safe-area-inset-bottom, 0px)); }' in BLOCK
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(", BLOCK)
    assert ".va" not in BLOCK.replace(".va-on", "")                                              # nothing here moves the voice button
