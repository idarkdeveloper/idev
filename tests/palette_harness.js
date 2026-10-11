// Drives the pure parts of trading_agent/ui/palette.js (helpers, state machine, debounced searcher) with fake timers
// and fake fetches. No browser, no network. Prints {"n": checks run, "failures": [...]} for tests/test_palette.py.
const assert = require("assert");
const path = require("path");
const P = require(path.join(__dirname, "..", "trading_agent", "ui", "palette.js"));

const failures = []; let n = 0;
function check(name, fn) { n++; try { fn(); } catch (e) { failures.push(name + ": " + (e && e.message || e)); } }
async function checkAsync(name, fn) { n++; try { await fn(); } catch (e) { failures.push(name + ": " + (e && e.message || e)); } }

const cmds = (mode, extra) => P.buildCommands(Object.assign({mode, sections: [{id: "lookup", label: "Look up a stock"}, {id: "size", label: "Position size"}], refresh: true}, extra));
const model = (mode) => P.createModel({commands: cmds(mode || "live")});
const labels = (m) => m.items().map(r => r.label);

check("cleanSymbol", () => {
  assert.strictEqual(P.cleanSymbol(" tcs "), "TCS");
  assert.strictEqual(P.cleanSymbol("M&M"), "M&M");
  assert.strictEqual(P.cleanSymbol("BAJAJ-AUTO"), "BAJAJ-AUTO");
  for (const bad of ["", "  ", null, undefined, "a b", "<script>", "x/../y", "A".repeat(21), "-X", "TCS;DROP"]) assert.strictEqual(P.cleanSymbol(bad), null, String(bad));
});

check("landing addresses", () => {
  assert.deepStrictEqual(P.landing("?lookup=tcs"), {op: "lookup", symbol: "TCS"});
  assert.deepStrictEqual(P.landing("?size=INFY"), {op: "size", symbol: "INFY"});
  assert.deepStrictEqual(P.landing("?chart=M%26M"), {op: "chart", symbol: "M&M"});
  assert.deepStrictEqual(P.landing("?buy=RELIANCE"), {op: "buy", symbol: "RELIANCE"});
  assert.strictEqual(P.landing(""), null);
  assert.strictEqual(P.landing("?lookup=%3Cscript%3E"), null);
  assert.strictEqual(P.landing("?other=1"), null);
  assert.strictEqual(P.landingUrl("buy", "M&M"), "/demo?buy=M%26M");
  assert.strictEqual(P.landingUrl("lookup", "TCS"), "/?lookup=TCS");
});

check("practice buy never leaves the practice account, whatever the live-orders setting", () => {
  for (const mode of ["live", "demo", "replay", "weird"]) {
    for (const flags of [{}, {liveOrders: true}, {liveOrders: false}, {GROWW_LIVE_ORDERS: "true", live_orders: true}]) {
      for (const hasBuy of [true, false]) {
        const host = Object.assign({}, flags, hasBuy ? {buy() {}} : {});
        const t = P.routeStockOp("buy", "tcs", mode, host);
        assert.ok(t && t.op === "buy" && t.symbol === "TCS", JSON.stringify(t));
        if (t.via === "host") assert.ok(mode === "demo" && hasBuy, "host prefill only on the Demo page: " + mode);
        else assert.strictEqual(t.url, "/demo?buy=TCS", mode);
      }
    }
  }
  assert.strictEqual(P.routeStockOp("buy", "bad symbol", "demo", {buy() {}}), null);
  assert.strictEqual(P.routeStockOp("order", "TCS", "demo", {order() {}}), null);
  assert.deepStrictEqual(P.STOCK_OPS, ["lookup", "size", "chart", "buy"]);
});

check("practice sell is the same: fill the practice form on the Demo page, else open /demo?sell=SYM, never read a live setting", () => {
  for (const mode of ["live", "demo", "replay", "weird"]) {
    for (const flags of [{}, {liveOrders: true}, {GROWW_LIVE_ORDERS: "true", live_orders: true}]) {
      for (const hasSell of [true, false]) {
        const host = Object.assign({}, flags, hasSell ? {sell() {}} : {});
        const t = P.routeStockOp("sell", "tcs", mode, host);
        assert.ok(t && t.op === "sell" && t.symbol === "TCS", JSON.stringify(t));
        if (t.via === "host") assert.ok(mode === "demo" && hasSell, "host prefill only on the Demo page: " + mode);
        else assert.strictEqual(t.url, "/demo?sell=TCS", mode);
      }
    }
  }
  assert.deepStrictEqual(P.landing("?sell=TCS"), {op: "sell", symbol: "TCS"});
  assert.strictEqual(P.landingUrl("sell", "M&M"), "/demo?sell=M%26M");
  assert.strictEqual(P.routeStockOp("sell", "bad symbol", "demo", {sell() {}}), null);
  assert.deepStrictEqual(P.STOCK_OPS, ["lookup", "size", "chart", "buy"]);   // the menu is unchanged: Practice sell lives on the stock page
});

check("other stock actions use the page's handler, else open the page that has it", () => {
  assert.deepStrictEqual(P.routeStockOp("lookup", "tcs", "live", {lookup() {}}), {via: "host", op: "lookup", symbol: "TCS"});
  assert.deepStrictEqual(P.routeStockOp("size", "tcs", "replay", {}), {via: "navigate", op: "size", symbol: "TCS", url: "/?size=TCS"});
  assert.deepStrictEqual(P.routeStockOp("chart", "tcs", "replay", {}), {via: "navigate", op: "chart", symbol: "TCS", url: "/?chart=TCS"});
});

check("commands: pages exclude the current one, sections, themes, refresh only when offered", () => {
  const ids = (mode, extra) => cmds(mode, extra).map(c => c.id);
  assert.ok(!ids("live").includes("page-live") && ids("live").includes("page-demo") && ids("live").includes("page-replay"));
  assert.ok(!ids("demo").includes("page-demo") && ids("demo").includes("page-live"));
  assert.ok(ids("live").includes("sec-lookup") && ids("live").includes("sec-size"));
  assert.deepStrictEqual(ids("live").filter(i => i.startsWith("theme-")), ["theme-dark", "theme-light", "theme-auto"]);
  assert.ok(ids("live").includes("refresh") && !ids("live", {refresh: false}).includes("refresh"));
  assert.deepStrictEqual(cmds("live").find(c => c.id === "theme-light").run, {kind: "theme", choice: "light"});
  for (const c of cmds("live")) assert.ok(["page", "section", "theme", "refresh"].includes(c.run.kind), c.id);   // no command is an order
});

check("filtering and ranking", () => {
  const c = cmds("live");
  assert.strictEqual(P.filterCommands(c, "")[0].cmd.id, "page-replay");
  assert.strictEqual(P.filterCommands(c, "dark")[0].cmd.id, "theme-dark");
  assert.strictEqual(P.filterCommands(c, "DARK")[0].cmd.id, "theme-dark");
  assert.strictEqual(P.filterCommands(c, "zzzz").length, 0);
  assert.strictEqual(P.filterCommands(c, "pos size")[0].cmd.id, "sec-size");
  assert.strictEqual(P.filterCommands(c, "refresh")[0].cmd.id, "refresh");
  assert.ok(P.filterCommands(c, "page").map(x => x.cmd.id).includes("page-demo"));
});

check("typing shows commands, stocks from the search, and a typed ticker fallback", () => {
  const m = model();
  assert.ok(m.items().length >= 5 && m.items().every(r => r.type === "cmd"));
  m.setQuery("tata");
  m.setHits("tata", [{symbol: "TATASTEEL", name: "Tata Steel Ltd"}, {symbol: "TATAMOTORS", name: "Tata Motors"}, {symbol: "TATASTEEL", name: "dup"}]);
  const stocks = m.items().filter(r => r.type === "stock");
  assert.deepStrictEqual(stocks.map(r => r.symbol), ["TATASTEEL", "TATAMOTORS", "TATA"]);   // dup dropped, typed ticker last
  assert.strictEqual(stocks[0].detail, "Tata Steel Ltd");
  m.setQuery("xy z");
  m.setHits("xy z", []);
  assert.deepStrictEqual(labels(m), ["No match"]);
  assert.strictEqual(m.state.active, -1);
  assert.strictEqual(m.activate(), null);
});

check("a command that matches strongly comes before stocks, a weak match after", () => {
  const m = model();
  m.setQuery("size");
  m.setHits("size", [{symbol: "SIZEL", name: "Sizel Ltd"}]);
  const order = m.items().map(r => r.type + ":" + r.label);
  assert.strictEqual(order[0], "cmd:Position size");
  assert.ok(order.indexOf("stock:SIZEL") > 0);
});

check("choosing a stock opens its four actions; Backspace-on-empty goes back", () => {
  const m = model("live");
  m.setQuery("tcs"); m.setHits("tcs", [{symbol: "TCS", name: "Tata Consultancy Services"}]);
  const idx = m.items().findIndex(r => r.type === "stock");
  assert.strictEqual(m.activate(idx), null);
  assert.deepStrictEqual(m.state.stage, {symbol: "TCS", name: "Tata Consultancy Services"});
  assert.deepStrictEqual(m.items().map(r => r.label), ["Look up", "Size", "Chart", "Practice buy"]);
  assert.ok(m.items().every(r => r.type === "op" && r.symbol === "TCS"));
  m.setQuery("buy");
  assert.deepStrictEqual(m.items().map(r => r.op), ["buy"]);
  assert.deepStrictEqual(m.activate(), {kind: "stock", op: "buy", symbol: "TCS"});
  assert.ok(m.back());
  assert.strictEqual(m.state.stage, null);
  assert.ok(!m.back());
});

check("arrow keys wrap, skip note rows, Home and End", () => {
  const m = model();
  const n = m.items().length;
  assert.strictEqual(m.state.active, 0);
  m.move(-1); assert.strictEqual(m.state.active, n - 1);
  m.move(1); assert.strictEqual(m.state.active, 0);
  m.end(); assert.strictEqual(m.state.active, n - 1);
  m.home(); assert.strictEqual(m.state.active, 0);
  m.setQuery("tcs");     // searching: a note row may follow the commands
  m.setSearching(true);
  m.move(1); m.move(1); m.move(1);
  assert.ok(m.items()[m.state.active].type !== "note");
});

check("a late answer for an older query is dropped", () => {
  const m = model();
  m.setQuery("ta");
  m.setQuery("tata");
  assert.strictEqual(m.setHits("ta", [{symbol: "OLD", name: "old"}]), false);
  assert.ok(!m.items().some(r => r.symbol === "OLD"));
  assert.strictEqual(m.setHits("tata", [{symbol: "NEW", name: "new"}]), true);
  assert.ok(m.items().some(r => r.symbol === "NEW"));
});

check("hostile search results cannot become symbols", () => {
  const m = model();
  m.setQuery("zz"); m.setHits("zz", [{symbol: "<img src=x onerror=1>", name: "x"}, {symbol: "OK1", name: "<b>n</b>"}, null, {}]);
  const s = m.items().filter(r => r.type === "stock").map(r => r.symbol);
  assert.deepStrictEqual(s.filter(x => x !== "ZZ"), ["OK1"]);
});

check("search failure shows a note, commands stay", () => {
  const m = model();
  m.setQuery("tcs"); m.setHits("tcs", null, true);
  assert.ok(labels(m).includes("Stock search is unavailable right now"));
  assert.ok(m.items().some(r => r.type === "stock" && r.symbol === "TCS"));   // typed ticker still usable
});

// ---- searcher with a fake clock ----
function fakeClock() {
  let now = 0, id = 0; const timers = new Map();
  return {
    setTimeout(fn, ms) { const i = ++id; timers.set(i, {at: now + ms, fn}); return i; },
    clearTimeout(i) { timers.delete(i); },
    async tick(ms) {
      const end = now + ms;
      for (;;) {
        const next = [...timers.entries()].filter(([, t]) => t.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
        if (!next) break;
        now = next[1].at; timers.delete(next[0]); next[1].fn();
      }
      now = end;
    },
    pending: () => timers.size,
  };
}
const defer = () => { let res, rej; const p = new Promise((a, b) => { res = a; rej = b; }); return {p, res, rej}; };
const flush = () => new Promise(r => setImmediate(r));

(async () => {
  await checkAsync("debounce: rapid typing makes one request after 150 ms", async () => {
    const clk = fakeClock(), calls = [], got = [];
    const s = P.createSearcher({fetchJson: async (u) => { calls.push(u); return [{symbol: "TCS", name: "x"}]; }, setTimeout: clk.setTimeout, clearTimeout: clk.clearTimeout,
      onResult: (q, h, e) => got.push([q, h.length, e])});
    s.query("t"); s.query("tc"); await clk.tick(100); s.query("tcs"); await clk.tick(149);
    assert.strictEqual(calls.length, 0);
    await clk.tick(1); await flush();
    assert.deepStrictEqual(calls, ["/api/search?q=tcs"]);
    assert.deepStrictEqual(got, [["tcs", 1, false]]);
  });
  await checkAsync("stale responses are ignored even when they arrive last", async () => {
    const clk = fakeClock(), got = [], d = {a: defer(), b: defer()}; let k = 0;
    const s = P.createSearcher({fetchJson: () => (k++ === 0 ? d.a.p : d.b.p), setTimeout: clk.setTimeout, clearTimeout: clk.clearTimeout, onResult: (q, h) => got.push([q, h[0].symbol])});
    s.query("tata"); await clk.tick(150);          // request A is in flight
    s.query("tatas"); await clk.tick(150);         // request B
    d.b.res([{symbol: "B", name: ""}]); await flush();
    d.a.res([{symbol: "A", name: ""}]); await flush();
    assert.deepStrictEqual(got, [["tatas", "B"]]);
  });
  await checkAsync("clearing the box while a request is out drops its answer", async () => {
    const clk = fakeClock(), got = [], idle = [], d = defer();
    const s = P.createSearcher({fetchJson: () => d.p, setTimeout: clk.setTimeout, clearTimeout: clk.clearTimeout, onResult: () => got.push(1), onIdle: (t) => idle.push(t)});
    s.query("tcs"); await clk.tick(150); s.query(""); d.res([{symbol: "TCS", name: ""}]); await flush();
    assert.deepStrictEqual(got, []); assert.deepStrictEqual(idle, [""]);
  });
  await checkAsync("a failed request reports an error, one letter searches nothing", async () => {
    const clk = fakeClock(), got = []; let calls = 0;
    const s = P.createSearcher({fetchJson: async () => { calls++; throw new Error("down"); }, setTimeout: clk.setTimeout, clearTimeout: clk.clearTimeout, onResult: (q, h, e) => got.push([q, e])});
    s.query("t"); await clk.tick(500); assert.strictEqual(calls, 0); assert.strictEqual(clk.pending(), 0);
    s.query("tcs"); await clk.tick(150); await flush();
    assert.deepStrictEqual(got, [["tcs", true]]);
  });
  await checkAsync("cancel stops a pending request", async () => {
    const clk = fakeClock(); let calls = 0;
    const s = P.createSearcher({fetchJson: async () => { calls++; return []; }, setTimeout: clk.setTimeout, clearTimeout: clk.clearTimeout, onResult: () => {}});
    s.query("tcs"); s.cancel(); await clk.tick(1000); assert.strictEqual(calls, 0);
  });
  console.log(JSON.stringify({n, failures}));
})();
