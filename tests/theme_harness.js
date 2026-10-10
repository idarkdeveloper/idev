// Drives the theme code of each page (the inline head script and common.js's Dark / Light / Auto switch) against stub
// browser objects and prints what it did as JSON. Used by tests/test_theme.py. No network, no real browser.
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const ui = path.join(__dirname, "..", "trading_agent", "ui");
const common = fs.readFileSync(path.join(ui, "common.js"), "utf8");

function makeMedia(initialLight) {
  const mq = {matches: initialLight, listeners: [],
    addEventListener(t, fn) { if (t === "change") this.listeners.push(fn); },
    set(light) { this.matches = light; this.listeners.forEach(fn => fn({matches: light})); }};
  return mq;
}
function makeStorage(initial, throws) {
  const data = Object.assign({}, initial);
  return {data, getItem(k) { if (throws) throw new Error("blocked"); return k in data ? data[k] : null; },
    setItem(k, v) { if (throws) throw new Error("blocked"); data[k] = String(v); },
    removeItem(k) { if (throws) throw new Error("blocked"); delete data[k]; }};
}
function makeButton(choice) {
  const attrs = {"aria-pressed": "false"}, cls = new Set(), handlers = [];
  return {dataset: {themeChoice: choice}, attrs, handlers,
    setAttribute(k, v) { attrs[k] = v; }, getAttribute(k) { return attrs[k]; },
    classList: {toggle(c, on) { if (on) cls.add(c); else cls.delete(c); }, add: c => cls.add(c), remove: c => cls.delete(c), contains: c => cls.has(c)},
    addEventListener(t, fn) { if (t === "click") handlers.push(fn); }, click() { handlers.forEach(fn => fn({})); }};
}

function headScript(html) {
  const head = html.split("</head>")[0];
  const m = head.match(/<script>([\s\S]*?)<\/script>/);
  if (!m) throw new Error("no inline head script");
  return m[1];
}
// What the head script resolves for a given storage / matchMedia environment.
function runHead(html, {stored, throws, light, noMedia}) {
  const document = {documentElement: {dataset: {}}};
  const ctx = {document, localStorage: makeStorage(stored || {}, throws)};
  if (!noMedia) ctx.matchMedia = () => makeMedia(!!light);
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(headScript(html), ctx);
  return document.documentElement.dataset.theme;
}

function page(html, {stored, throws, light} = {}) {
  const buttons = [...html.matchAll(/data-theme-choice="(dark|light|auto)"/g)].map(m => makeButton(m[1]));
  const root = {dataset: {}}, media = makeMedia(!!light), storage = makeStorage(stored || {}, throws), winListeners = {};
  const document = {documentElement: root, body: {dataset: {}}, getElementById: () => null,
    querySelectorAll: sel => sel === "[data-theme-choice]" ? buttons : [], addEventListener() {}};
  const ctx = {document, console, localStorage: storage, matchMedia: () => media, Intl, Date, Math, JSON, Number, String, Object, Array, Set, Map, Promise,
    isNaN, isFinite, parseFloat, parseInt, encodeURIComponent, setTimeout, clearTimeout,
    addEventListener: (t, fn) => { (winListeners[t] = winListeners[t] || []).push(fn); }};
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(common, ctx);
  const btn = c => buttons.find(b => b.dataset.themeChoice === c);
  const pressed = () => Object.fromEntries(buttons.map(b => [b.dataset.themeChoice, b.attrs["aria-pressed"]]));
  return {ctx, root, media, storage, buttons, btn, pressed, winListeners, TA: ctx.TA};
}

const out = {};
for (const file of ["index.html", "replay.html"]) {
  const html = fs.readFileSync(path.join(ui, file), "utf8");
  const o = {};
  o.head = {
    stored_light: runHead(html, {stored: {theme: "light"}, light: false}),
    stored_dark: runHead(html, {stored: {theme: "dark"}, light: true}),
    auto_os_light: runHead(html, {light: true}),
    auto_os_dark: runHead(html, {light: false}),
    storage_throws: runHead(html, {throws: true, light: false}),
    storage_throws_os_light: runHead(html, {throws: true, light: true}),
    no_matchmedia: runHead(html, {noMedia: true}),
    junk_value: runHead(html, {stored: {theme: "neon"}, light: false}),
    junk_value_os_light: runHead(html, {stored: {theme: "neon"}, light: true}),
  };
  o.switch_markup = ["dark", "light", "auto"].every(c => html.includes(`data-theme-choice="${c}"`)) && html.includes(">Theme<");

  let p = page(html);                                    // fresh visit, OS dark
  let calls = 0; p.TA.theme.onChange(() => { calls++; });
  p.TA.theme.init();
  o.init = {theme: p.root.dataset.theme, choice: p.root.dataset.themeChoice, pressed: p.pressed()};
  p.btn("light").click();
  o.light_click = {theme: p.root.dataset.theme, stored: p.storage.data.theme, pressed: p.pressed()};
  p.btn("dark").click();
  o.dark_click = {theme: p.root.dataset.theme, stored: p.storage.data.theme, pressed: p.pressed()};
  o.manual_ignores_os = (p.media.set(true), p.root.dataset.theme);      // a manual choice is not moved by the OS
  p.btn("auto").click();
  o.auto_click = {theme: p.root.dataset.theme, stored: p.storage.data.theme === undefined ? null : p.storage.data.theme, pressed: p.pressed()};
  const seq = [p.root.dataset.theme]; p.media.set(false); seq.push(p.root.dataset.theme); p.media.set(true); seq.push(p.root.dataset.theme);
  o.auto_follows_os = seq;                                // OS is light now, then dark, then light again
  p.btn("dark").click();
  o.listener_calls = calls;
  (p.winListeners.storage || []).forEach(fn => fn({key: "theme"}));      // another tab wrote the same value
  p.storage.data.theme = "dark"; p.btn("light").click(); p.storage.data.theme = "dark";
  (p.winListeners.storage || []).forEach(fn => fn({key: "theme"}));      // another tab chose dark
  o.other_tab = p.root.dataset.theme;

  const t = page(html, {throws: true, light: false});                    // storage throws everywhere
  t.TA.theme.init();
  t.btn("light").click();
  o.throwing_storage = {theme: t.root.dataset.theme, choice: t.TA.theme.get()};
  out[file] = o;
}
const probe = page(fs.readFileSync(path.join(ui, "index.html"), "utf8"));
out.chart_colours = {s1: probe.TA.C.s1, grid: probe.TA.C.grid, ring: probe.TA.C.ring};
console.log(JSON.stringify(out));
