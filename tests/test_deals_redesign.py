"""Disclosed deals card: one scrolling chip row, deals grouped by day, a 3-line link card per deal (index.html, nocturne.css, TA.dayLabel)."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parent.parent / "trading_agent" / "ui"
HTML = (UI / "index.html").read_text(encoding="utf-8")
CSS = (UI / "nocturne.css").read_text(encoding="utf-8")

HARNESS = r"""
const fs = require("fs"), vm = require("vm"), path = require("path");
const ctx = {document: {documentElement: {dataset: {}}, body: {dataset: {}}, getElementById: () => null, querySelectorAll: () => [], addEventListener() {}},
  console, localStorage: {getItem: () => null, setItem() {}, removeItem() {}}, matchMedia: () => ({matches: false, addEventListener() {}}), Intl, Date, Math, JSON, Number, String, Object, Array, Set, Map, Promise,
  isNaN, isFinite, parseFloat, parseInt, encodeURIComponent, setTimeout, clearTimeout, addEventListener() {}};
ctx.window = ctx; vm.createContext(ctx);
vm.runInContext(fs.readFileSync(path.join(process.argv[1], "common.js"), "utf8"), ctx);
const L = ctx.TA.dayLabel;
console.log(JSON.stringify([L("2026-10-19", "2026-10-19"), L("2026-10-18", "2026-10-19"), L("2026-10-16", "2026-10-19"), L("2026-10-01", "2026-10-19"), L("2026-12-31", "2027-01-01"),
  L("", "2026-10-19"), L("soon", "2026-10-19"), L("2026-10-16", ""), L(null, null), L("2026-03-01", "2026-03-02")]));
"""


def test_day_labels():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    r = subprocess.run([node, "-e", HARNESS, str(UI)], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == ["Today · 19 Oct", "Yesterday · 18 Oct", "Fri · 16 Oct", "Thu · 1 Oct", "Yesterday · 31 Dec", "Earlier", "soon", "Fri · 16 Oct", "Earlier", "Yesterday · 1 Mar"]


def test_the_card_markup_keeps_every_hook_and_gains_the_new_ones():
    for needle in ('id="newcount"', 'id="dealsrc"', 'id="deal-filter"', 'id="deal-sep"', 'id="deal-chips"', 'id="deal-add"', 'tbody id="deals"', 'id="deal-count"', 'id="deals-more"', 'id="deals-more-wrap"',
                   'class="chips-row dl-chips"', '<table class="dt dl-table">'):
        assert needle in HTML, needle
    assert "<thead><tr><th>Date</th>" not in HTML.split('data-anchor="deals"')[1].split('data-anchor="insider"')[0]      # no table header: cards
    assert 'data-deal-filter="" aria-pressed="' in HTML and "data-deal-filter=\"${esc(n)}\" aria-pressed=" in HTML        # the investor chips keep their hooks
    assert '<td colspan="5" class="dl-cell"><a class="dl-deal" href="/?stock=${encodeURIComponent(d.ticker)}" data-lookup="${esc(d.ticker)}"' in HTML     # the row is a real link
    assert 'data-open-stock="${esc(d.ticker)}"' in HTML                                                                  # the row click still opens the stock view


def test_a_deal_is_three_lines_with_words_and_arrows_not_colour_alone():
    body = HTML[HTML.index("function dealRow(d)"):HTML.index("let dealsLimit")]
    assert '"▼ SELL" : "▲ BUY"' in body and "dl-val" in body and "dl-l2" in body and "dl-l3" in body
    assert "You hold none" in body and "Held · " in body and "dl-new\">NEW" in body                                       # chips only when relevant, "New" is a word
    assert "bigInr(z.qty * z.price)" in HTML and "dealKind(d)" in body
    assert 'MODE === "demo"' in HTML[HTML.index("function heldQty"):HTML.index("function dealRow")]                      # practice positions on Demo, Groww holdings on Live


def test_deals_are_grouped_by_day_and_shown_eight_then_ten_more():
    assert "let dealsLimit = 8" in HTML and "dealsLimit += 10" in HTML and 'TA.dayLabel(day, today)' in HTML
    assert 'class="dl-day"' in HTML and "deal${n === 1 ? \"\" : \"s\"}" in HTML
    assert "`Show ${left} more`" in HTML and "if(dk !== dealsKey){ dealsKey = dk; dealsLimit = 8; }" in HTML                # a new filter starts again from 8
    assert "No deals in the last 30 days" in HTML and "New ones appear here as they are disclosed." in HTML                # the empty state


def test_css_one_chip_row_44px_targets_sticky_day_and_tokens_only():
    block = CSS[CSS.index("/* Disclosed deals: one scrolling chip row"):]
    assert "flex-wrap: nowrap; overflow-x: auto" in block and "mask-image: linear-gradient(to right, black" in block
    assert re.search(r"button\.chip, \.chips-row\.dl-chips \.chip-add[^{]*\{[^}]*min-height: 44px", block)
    assert 'aria-pressed="true"]::before { content: "✓"' in block                                                           # selected = filled and a tick
    assert "tr.dl-day td { position: sticky" in block and "min-height: 76px" in block and "font-variant-numeric: tabular-nums" in block
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(", block)
    assert ".va" not in block                                                                                              # the voice button stays where it is
    assert "#deals td" not in CSS and "#deals tr" not in CSS                                                               # the old phone card rules for deals are gone
