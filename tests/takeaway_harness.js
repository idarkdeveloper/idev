// Loads trading_agent/ui/common.js under node and prints TA.lookupTakeaway for each case in the JSON file given as the
// first argument: [{r, now, ctx}] -> [text]. Used by tests/test_takeaway.py (the Python port must say the same).
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const common = fs.readFileSync(path.join(__dirname, "..", "trading_agent", "ui", "common.js"), "utf8");
const el = (id) => ({id, innerHTML: "", textContent: "", className: "", title: "", dataset: {}});
const document = {documentElement: {dataset: {}}, getElementById: el, addEventListener() {}, body: {dataset: {}}};
const ctx = {document, console, setTimeout, clearTimeout, Intl, Date, Math, JSON, Number, String, Object, Array, Set, Map, Promise,
  URLSearchParams, encodeURIComponent, isNaN, isFinite, parseFloat, parseInt, localStorage: {getItem: () => null, setItem() {}}};
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(common, ctx);
const cases = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
process.stdout.write(JSON.stringify(cases.map(c => ctx.TA.lookupTakeaway(c.r, c.now, c.ctx))));
