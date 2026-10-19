"""The loaders from the "Loader" design (common.js TA.loader, nocturne.css .ld-*): the drawn sparkline and step list
for a page's first load, the card skeleton, and the inline busy/done state on buttons. Node runs the helpers on a
tiny fake DOM; the page wiring and the CSS are checked as text. No network."""
import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parents[1] / "trading_agent" / "ui"
INDEX = (UI / "index.html").read_text(encoding="utf-8")
CSS = (UI / "nocturne.css").read_text(encoding="utf-8")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_skeleton_busy_button_and_page_loader(tmp_path):
    script = textwrap.dedent("""
        const window = {addEventListener(){}, matchMedia: () => ({matches: false, addEventListener(){}})};
        const document = {getElementById: () => null, addEventListener(){}, body: null, documentElement: {setAttribute(){}, dataset: {}}};
        eval(require('fs').readFileSync(process.argv[2], 'utf8') + '; globalThis.TA = window.TA;');
        const L = TA.loader, out = {};
        const rows = L.skeletonRows(4, 9);
        out.rows = (rows.match(/<tr class="ld-skrow"/g) || []).length;
        out.cells = (rows.split('</tr>')[0].match(/<td/g) || []).length;
        out.hidden = rows.includes('aria-hidden="true"');
        out.noDom = Object.keys(L.page({steps: ["a"]}));          // no body: a do-nothing controller, never an error
        // a fake button
        const cls = new Set(), attrs = {};
        const btn = {innerHTML: "Refresh", get textContent(){ return this.innerHTML.replace(/<[^>]+>/g, ""); }, disabled: false, style: {minWidth: ""},
          getBoundingClientRect: () => ({width: 80}), setAttribute: (k, v) => attrs[k] = v, getAttribute: (k) => attrs[k],
          removeAttribute: (k) => delete attrs[k], classList: {add: (...c) => c.forEach(x => cls.add(x)), remove: (...c) => c.forEach(x => cls.delete(x))}};
        let release; const job = new Promise(r => release = r);
        const p = L.busy(btn, () => job, {busy: "Refreshing…", done: (r) => r.ok ? "Updated" : null, doneMs: 10});
        out.during = {html: btn.innerHTML, disabled: btn.disabled, busy: attrs["aria-busy"], min: btn.style.minWidth, isBusy: L.isBusy(btn)};
        let ranTwice = false; L.busy(btn, () => { ranTwice = true; }); out.second = !ranTwice;   // a second click does nothing
        release({ok: true});
        p.then(async (r) => {
          out.result = r; out.done = {html: btn.innerHTML, disabled: btn.disabled, isBusy: L.isBusy(btn)};
          await new Promise(r => setTimeout(r, 30));
          out.after = {html: btn.innerHTML, disabled: btn.disabled, isBusy: L.isBusy(btn), min: btn.style.minWidth};
          const err = await L.busy(btn, async () => { throw new Error("nope"); }).catch(e => e.message);
          out.err = {msg: err, html: btn.innerHTML, disabled: btn.disabled, isBusy: L.isBusy(btn)};
          console.log(JSON.stringify(out));
        });
    """)
    f = tmp_path / "t.js"
    f.write_text(script, encoding="utf-8")
    r = subprocess.run(["node", str(f), str(UI / "common.js")], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    o = json.loads(r.stdout)
    assert o["rows"] == 4 and o["cells"] == 9 and o["hidden"] and sorted(o["noDom"]) == ["done", "fail", "step"]
    d = o["during"]
    assert 'class="ld-ring"' in d["html"] and "Refreshing…" in d["html"] and d["disabled"] and d["busy"] == "true"
    assert d["min"] == "80px" and d["isBusy"] and o["second"]
    assert o["result"] == {"ok": True} and "✓" in o["done"]["html"] and "Updated" in o["done"]["html"] and o["done"]["disabled"]
    assert o["after"] == {"html": "Refresh", "disabled": False, "isBusy": False, "min": ""}
    assert o["err"] == {"msg": "nope", "html": "Refresh", "disabled": False, "isBusy": False}


def test_the_page_uses_the_loaders():
    assert "TA.loader.page({once: \"ta.pageLoaded\", title: \"Loading today's prices\"" in INDEX   # first load of a tab session only
    assert 'steps: ["Deals, jobs and settings", "Portfolio from Groww"]' in INDEX
    assert "pageLoad.step(0," in INDEX and "pageLoad.step(1," in INDEX
    assert '$("mp-rows").innerHTML = TA.loader.skeletonRows(4, MPCOLS)' in INDEX
    for btn in ('$("mp-refresh")', '$("mp-groww-refresh")', '$("btn-regime")', '$("btn-dg-" + kind)'):
        assert f"TA.loader.busy({btn}" in INDEX, btn
    assert 'if($("mp-rows").querySelector(".ld-skrow")) $("mp-rows").innerHTML = ""' in INDEX   # a failed load clears it


def test_loader_css_is_tokenised_still_under_reduced_motion_and_before_the_phone_tail():
    start = CSS.index("/* Loaders (design")
    block = CSS[start:CSS.index("button.ld-done .ld-tick")]
    assert "#" not in block.split("*/", 1)[1]                    # colours only from the theme tokens
    assert "@media (prefers-reduced-motion: reduce)" in CSS[start:start + 6000]
    assert start < CSS.index("\n@media (max-width: 700px) {")    # the laptop session owns the phone tail
