// Loads trading_agent/ui/common.js under node and prints what TA.safety returns for the mode strip, the freshness chip,
// the protection line and the URL state helpers, as JSON. Used by tests/test_safety.py.
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const common = fs.readFileSync(path.join(__dirname, "..", "trading_agent", "ui", "common.js"), "utf8");
const els = {};
const el = (id) => els[id] || (els[id] = {id, innerHTML: "", textContent: "", className: "", title: "", dataset: {}});
const document = {documentElement: {dataset: {}}, getElementById: el, addEventListener() {}, body: {dataset: {}}};
const ctx = {document, console, setTimeout, clearTimeout, Intl, Date, Math, JSON, Number, String, Object, Array, Set, Map, Promise,
  URLSearchParams, encodeURIComponent, isNaN, isFinite, parseFloat, parseInt, localStorage: {getItem: () => null, setItem() {}}};
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(common, ctx);
const S = ctx.TA.safety;

const watch = (level, age, extra) => Object.assign({seen: true, at: "x", age_s: age, every: 60, last_error: null, level}, extra || {});
const fresh = (over) => Object.assign({market_open: true, live_orders: false, watch: watch("ok", 40),
  prices: {last_close: "2026-10-09T15:30:00+05:30", bar_at: "2026-10-09"}, deals: {age_s: 120}}, over || {});
const chip = (f) => { const c = S.freshnessChip(f); return {level: c.level, text: c.text}; };
const names = ["ASHISH KACHOLIA", "VIJAY KEDIA", "Dolly Khanna"];
const anchors = ["portfolio", "deals", "signal-lab"];

S.paintStrip(S.modeStrip("live", {liveOrders: true}));
const painted = {text: els["modestrip-text"].textContent, cls: els.modestrip.className, icon: els["modestrip-icon"].innerHTML,
  theme: document.documentElement.dataset.strip};
S.paintChip(S.freshnessChip(fresh({market_open: true, watch: watch("bad", 1000)})));
const paintedChip = {text: els["fresh-text"].textContent, cls: els.freshness.className, title: els.freshness.title};

console.log(JSON.stringify({
  strip: {
    live_off: S.modeStrip("live", {liveOrders: false}), live_on: S.modeStrip("live", {liveOrders: true}),
    live_unknown: S.modeStrip("live", {}), live_state_unknown: S.modeStrip("live", {liveOrders: "unknown"}), demo: S.modeStrip("demo", {liveOrders: true}),
    replay: S.modeStrip("replay", {date: "2021-03-09"}), replay_home: S.modeStrip("replay", {}),
  },
  painted,
  chip: {
    ok: chip(fresh()), warn: chip(fresh({watch: watch("warn", 7 * 60)})), bad: chip(fresh({watch: watch("bad", 16 * 60)})),
    hours: chip(fresh({watch: watch("bad", 3 * 3600 + 5)})),
    unseen: chip(fresh({watch: {seen: false, level: "unseen", age_s: null}})),
    unseen_live: chip(fresh({live_orders: true, watch: {seen: false, level: "unseen", age_s: null}})),
    closed: chip(fresh({market_open: false, watch: watch("idle", 90000)})),
    error: chip(fresh({watch: watch("ok", 30, {last_error: "boom"})})),
    none: chip(null),
    title: S.freshnessChip(fresh()).title,
  },
  painted_chip: paintedChip,
  prot: {
    gtt: S.protectionHtml({kind: "gtt", tone: "solid", text: "GTT at Groww ₹99.00 (#g1)", warning: null}),
    escaped: S.protectionHtml({kind: "none", tone: "neutral", text: "<b>x</b>", warning: null}),
    warn: S.protectionHtml({kind: "server", tone: "warn", text: "Server stop", warning: "GTT problem: nope"}),
  },
  url: {
    slug: S.slug("Dolly  Khanna!"),
    parse_one: S.parseUrl("?investor=vijay-kedia&h=20", "#deals", {investors: names, anchors}),
    parse_two: S.parseUrl("?investor=vijay-kedia,dolly-khanna,vijay-kedia", "", {investors: names, anchors}),
    parse_all: S.parseUrl("?investor=all", "", {investors: names, anchors}),
    parse_unknown: S.parseUrl("?investor=nobody&h=7&x=1", "#nowhere", {investors: names, anchors}),
    parse_bad_h: S.parseUrl("?h=20abc", "", {investors: names, anchors}),
    parse_empty: S.parseUrl("", "", {investors: names, anchors}),
    parse_mixed: S.parseUrl("?investor=nobody,ashish-kacholia&h=60", "#signal-lab", {investors: names, anchors}),
    build_all: S.buildUrl("/", {investor: []}),
    build_full: S.buildUrl("/demo", {investor: ["VIJAY KEDIA", "Dolly Khanna"], h: 60, anchor: "deals"}),
    build_none: S.buildUrl("/", {}),
    build_bad_h: S.buildUrl("/", {h: 7}),
    pick: S.pickSection([{anchor: "portfolio", top: -400, bottom: -20}, {anchor: "deals", top: -10, bottom: 600}, {anchor: "lookup", top: 80, bottom: 900}], 120),
    pick_none: S.pickSection([{anchor: "deals", top: 300, bottom: 900}], 120),
    pick_two_columns: S.pickSection([{anchor: "deals", top: 10, bottom: 800}, {anchor: "lookup", top: 100, bottom: 500}], 120),
  },
}));
