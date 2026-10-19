"""Recent runs: the quiet checks fold into one line per stretch (TA.groupRuns in common.js, renderRuns in index.html). The grouping is
client side only; the server still records every run. A node run of the pure function plus text checks of the card."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parent.parent / "trading_agent" / "ui"

HARNESS = r"""
const fs = require("fs"), vm = require("vm"), path = require("path");
const ctx = {document: {documentElement: {dataset: {}}, body: {dataset: {}}, getElementById: () => null, querySelectorAll: () => [], addEventListener() {}},
  console, localStorage: {getItem: () => null, setItem() {}, removeItem() {}}, matchMedia: () => ({matches: false, addEventListener() {}}), Intl, Date, Math, JSON, Number, String, Object, Array, Set, Map, Promise,
  isNaN, isFinite, parseFloat, parseInt, encodeURIComponent, setTimeout, clearTimeout, addEventListener() {}};
ctx.window = ctx; vm.createContext(ctx);
vm.runInContext(fs.readFileSync(path.join(process.argv[1], "common.js"), "utf8"), ctx);
const G = ctx.TA.groupRuns;
const quiet = (t) => ({at: `2026-10-19T12:${String(t).padStart(2, "0")}:00+00:00`, new_trades: 0, skipped: true});
const real = (t, extra) => Object.assign({at: `2026-10-19T12:${String(t).padStart(2, "0")}:00+00:00`, new_trades: 1, recommendations: 1, orders: 0, model: "claude-opus-5-5", usage: {cost_usd: 0.075}}, extra || {});
const shape = (runs) => G(runs).map(x => x.type === "group" ? "G" + x.runs.length : "R");
const out = {};
out.empty = [shape([]), shape(null), shape(undefined)];
out.single = shape([quiet(5)]);
out.allQuiet = shape([quiet(9), quiet(8), quiet(7), quiet(6)]);
out.mixed = shape([quiet(30), quiet(29), real(28), quiet(27), quiet(26), quiet(25)]);
out.realFirst = shape([real(30), quiet(29), quiet(28)]);
out.errorInTheMiddle = shape([quiet(30), quiet(29), real(28, {error: "boom"}), quiet(27), quiet(26)]);
out.notQuiet = shape([quiet(9), Object.assign(quiet(8), {baseline: true}), Object.assign(quiet(7), {dry_run: true}), Object.assign(quiet(6), {refusal: "x"}),
  Object.assign(quiet(5), {stop: "step_limit"}), Object.assign(quiet(4), {fallback_used: true}), {at: "2026-10-19T12:03:00+00:00", new_trades: 0, skipped: false, recommendations: 0, orders: 0, model: "m"}, quiet(2), quiet(1)]);
const g = G([quiet(30), quiet(29), real(28), quiet(27), quiet(26)]);
out.order = g.map(x => x.type === "group" ? x.newest.at.slice(14, 16) + "-" + x.oldest.at.slice(14, 16) : x.run.at.slice(14, 16));
out.keysStable = [G([quiet(30), quiet(29)])[0].key, G([quiet(30), quiet(29), quiet(28)])[0].key, G([quiet(31), quiet(30), quiet(29)])[0].key];
out.groupRuns = g[0].runs.map(r => r.at.slice(14, 16));
out.junk = shape([null, undefined, quiet(3), null, quiet(2)]);
out.isQuiet = [ctx.TA.isQuietRun(quiet(1)), ctx.TA.isQuietRun(real(1)), ctx.TA.isQuietRun(null), ctx.TA.isQuietRun({skipped: true, new_trades: 0, usage: {}})];
const big = []; for (let i = 0; i < 22; i++) big.push(quiet(59 - i)); big.push(real(36)); big.push(quiet(35), quiet(34));
out.day = shape(big);
console.log(JSON.stringify(out));
"""


def _run():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    r = subprocess.run([node, "-e", HARNESS, str(UI)], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_group_runs_folds_quiet_stretches_and_keeps_everything_else():
    o = _run()
    assert o["empty"] == [[], [], []] and o["single"] == ["R"]                      # one quiet check is shown as it is
    assert o["allQuiet"] == ["G4"]
    assert o["mixed"] == ["G2", "R", "G3"]                                          # quiet / real / quiet, newest first
    assert o["realFirst"] == ["R", "G2"]
    assert o["errorInTheMiddle"] == ["G2", "R", "G2"]                               # a run with an error is never folded
    assert o["notQuiet"] == ["R", "R", "R", "R", "R", "R", "R", "G2"]               # baseline, dry run, refusal, stop, fallback, a Claude call: full entries
    assert o["order"] == ["30-29", "28", "27-26"] and o["groupRuns"] == ["30", "29"]
    assert o["keysStable"][0] == o["keysStable"][1] != o["keysStable"][2]            # the key is the newest run's time: older runs added behind it keep it
    assert o["junk"] == ["G2"]                                                       # empty entries are skipped, they do not break a stretch
    assert o["isQuiet"] == [True, False, False, False]
    assert o["day"] == ["G22", "R", "G2"]                                            # a quiet day is a handful of lines


def test_the_card_renders_a_real_button_with_aria_and_keeps_open_groups_across_refreshes():
    html = (UI / "index.html").read_text(encoding="utf-8")
    css = (UI / "nocturne.css").read_text(encoding="utf-8")
    assert 'class="run-fold" aria-expanded="${open}" aria-controls="${id}"' in html and "<button type=\"button\" class=\"run-fold\"" in html
    assert "const runsOpen = new Set()" in html and "runsOpen.has(x.key)" in html and "runsOpen.add(k)" in html and "runsOpen.delete(k)" in html
    assert 'nothing new</b>, ${x.runs.length} checks (${hm(x.oldest.at)} to ${hm(x.newest.at)})' in html
    assert "TA.groupRuns(runs)" in html and 'renderRuns(S.runs)' in html and "run-chev" in html
    assert ".runs .run-fold {" in css and "min-height: 44px" in css[css.index(".runs .run-fold {"):][:500]
    assert 'aria-expanded="true"] .run-chev' in css and "prefers-reduced-motion: reduce) { .runs .run-chev" in css
    block = css[css.index("/* Recent runs: a stretch"):]
    import re
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(", block)
    assert "groupRuns, isQuietRun" in (UI / "common.js").read_text(encoding="utf-8")


def test_the_server_still_records_every_run_and_sends_more_of_them():
    ui = (UI.parent / "ui.py").read_text(encoding="utf-8")
    assert '"runs": list(reversed(st.data["runs"][-60:]))' in ui
    state = (UI.parent / "state.py").read_text(encoding="utf-8")
    assert 'self.data["runs"] = (self.data["runs"] + [info])[-200:]' in state       # nothing about what is recorded changed
