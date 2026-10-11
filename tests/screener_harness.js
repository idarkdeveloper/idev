// Drives the pure logic of trading_agent/ui/screener.js (columns, filters, presets, sort, saved presets, CSV, polling).
// No browser, no network. Prints {"n": checks run, "failures": [...]} for tests/test_screener_page.py.
const assert = require("assert");
const path = require("path");
const S = require(path.join(__dirname, "..", "trading_agent", "ui", "screener.js"));

const failures = []; let n = 0;
function check(name, fn) { n++; try { fn(); } catch (e) { failures.push(name + ": " + (e && e.message || e)); } }

const row = (o) => Object.assign({symbol: "X", name: "X Ltd", industry: "IT", price: 100, chg_1d: 0, market_cap_cr: 1000, rsi: 50, above_200: true,
  rel_volume: 1, div_yield: 1, pct_from_high: -10, mom_eligible: true, mom_score: 0, held: false, deal: false, band: "10%"}, o);
const rows = [
  row({symbol: "AAA", name: "Alpha Tech", industry: "IT", market_cap_cr: 8000, rsi: 28, chg_1d: -3, above_200: true, rel_volume: 2.5, div_yield: 4.2, pct_from_high: -2, mom_score: 1.5, deal: true}),
  row({symbol: "BBB", name: "Beta Bank", industry: "Financial Services", market_cap_cr: 3000, rsi: 55, chg_1d: 1.5, above_200: false, rel_volume: 0.8, div_yield: 0.5, pct_from_high: -30, mom_eligible: false, mom_score: -1, held: true}),
  row({symbol: "CCC", name: "Gamma Cement", industry: "Construction Materials", market_cap_cr: null, rsi: null, chg_1d: 2, above_200: null, rel_volume: null, div_yield: null, pct_from_high: -6, mom_score: 0.2, band: null}),
  row({symbol: "DDD", name: "Delta Drugs", industry: "Healthcare", market_cap_cr: 12000, rsi: 34, chg_1d: 0, above_200: true, rel_volume: 2.0, div_yield: 3.0, pct_from_high: -4.9, mom_score: 0.9}),
];
const syms = (rs) => rs.map(r => r.symbol).join(",");
const F = (col, op, value, value2) => ({col, op, value, value2});

check("columns are unique and well formed", () => {
  const keys = S.COLS.map(c => c.key);
  assert.strictEqual(new Set(keys).size, keys.length);
  for (const c of S.COLS) { assert.ok(["stock", "text", "num", "pct", "bool"].includes(c.type), c.key); assert.ok(c.label, c.key); }
  for (const want of ["symbol", "industry", "price", "chg_1d", "ret_1w", "ret_1m", "ret_6m", "ret_1y", "ret_12_1", "volume", "rel_volume", "market_cap_cr", "pe", "div_yield", "pct_from_high", "above_200", "rsi", "atr_pct", "band", "held", "deal"]) assert.ok(keys.includes(want), want);
});

check("TradingView columns are their own group, labelled, and off the default list unless on", () => {
  const tvc = S.COLS.filter(c => c.group === "tv");
  assert.ok(tvc.length >= 3);
  for (const c of tvc) { assert.ok(/TV\)$/.test(c.label), c.label); assert.ok(/TradingView, unofficial/.test(c.tip), c.key); }
  assert.ok(!S.defaultColumns(false, false).some(k => k.startsWith("tv_")));
  assert.ok(S.defaultColumns(false, true).some(k => k.startsWith("tv_")));
});

check("phones start with a compact set that always has the stock first", () => {
  const narrow = S.defaultColumns(true, false), wide = S.defaultColumns(false, false);
  assert.strictEqual(narrow[0], "symbol");
  assert.ok(narrow.length <= 4 && narrow.length < wide.length);
});

check("numeric operators", () => {
  assert.strictEqual(syms(S.applyFilters(rows, [F("market_cap_cr", ">", "5000")])), "AAA,DDD");
  assert.strictEqual(syms(S.applyFilters(rows, [F("market_cap_cr", ">=", 8000)])), "AAA,DDD");
  assert.strictEqual(syms(S.applyFilters(rows, [F("rsi", "<", 35)])), "AAA,DDD");
  assert.strictEqual(syms(S.applyFilters(rows, [F("rsi", "<=", 34)])), "AAA,DDD");
  assert.strictEqual(syms(S.applyFilters(rows, [F("rsi", "=", 55)])), "BBB");
  assert.strictEqual(syms(S.applyFilters(rows, [F("market_cap_cr", ">", "5,000")])), "AAA,DDD");   // a comma in the typed number
});

check("between is inclusive and order-free, and needs both ends", () => {
  assert.strictEqual(syms(S.applyFilters(rows, [F("chg_1d", "between", -2, 2)])), "BBB,CCC,DDD");
  assert.strictEqual(syms(S.applyFilters(rows, [F("chg_1d", "between", 2, -2)])), "BBB,CCC,DDD");
  assert.strictEqual(syms(S.applyFilters(rows, [F("chg_1d", "between", 0, 0)])), "DDD");
  assert.strictEqual(S.applyFilters(rows, [F("chg_1d", "between", 1, "")]).length, 0);
  assert.strictEqual(S.validFilter(F("chg_1d", "between", 1, "")), false);
  assert.strictEqual(S.validFilter(F("chg_1d", "between", 1, 2)), true);
});

check("an unknown figure never passes a filter on it", () => {
  assert.ok(!S.applyFilters(rows, [F("rsi", "<", 100)]).some(r => r.symbol === "CCC"));
  assert.ok(!S.applyFilters(rows, [F("market_cap_cr", ">", -1)]).some(r => r.symbol === "CCC"));
  assert.ok(!S.applyFilters(rows, [F("above_200", "=", true)]).some(r => r.symbol === "CCC"));
  assert.ok(!S.applyFilters(rows, [F("above_200", "=", false)]).some(r => r.symbol === "CCC"));
  assert.ok(!S.applyFilters(rows, [F("band", "contains", "")]).some(r => r.symbol === "CCC"));
});

check("yes/no and text filters", () => {
  assert.strictEqual(syms(S.applyFilters(rows, [F("above_200", "=", true)])), "AAA,DDD");
  assert.strictEqual(syms(S.applyFilters(rows, [F("above_200", "=", false)])), "BBB");
  assert.strictEqual(syms(S.applyFilters(rows, [F("above_200", "=", "yes")])), "AAA,DDD");
  assert.strictEqual(syms(S.applyFilters(rows, [F("held", "=", true)])), "BBB");
  assert.strictEqual(syms(S.applyFilters(rows, [F("deal", "=", true)])), "AAA");
  assert.strictEqual(syms(S.applyFilters(rows, [F("industry", "is", "it")])), "AAA");
  assert.strictEqual(syms(S.applyFilters(rows, [F("industry", "is", "Information")])), "");
  assert.strictEqual(syms(S.applyFilters(rows, [F("industry", "contains", "con")])), "CCC");
});

check("filters combine with AND, and the name box searches name and ticker", () => {
  assert.strictEqual(syms(S.applyFilters(rows, [F("market_cap_cr", ">", 5000), F("rsi", "<", 30)])), "AAA");
  assert.strictEqual(syms(S.applyFilters(rows, [], "gamma")), "CCC");
  assert.strictEqual(syms(S.applyFilters(rows, [], " bbb ")), "BBB");
  assert.strictEqual(syms(S.applyFilters(rows, [], "ta")), "BBB,DDD");        // Beta Bank, Delta Drugs (Alpha Tech has no "ta")
  assert.strictEqual(syms(S.applyFilters(rows, [F("above_200", "=", true)], "tech")), "AAA");
  assert.strictEqual(syms(S.applyFilters(rows, [])), "AAA,BBB,CCC,DDD");
});

check("a filter on a column that does not exist is ignored, not fatal", () => {
  assert.strictEqual(S.applyFilters(rows, [F("nope", ">", 1)]).length, 4);
  assert.strictEqual(S.validFilter(F("nope", ">", 1)), false);
  assert.strictEqual(S.validFilter(F("rsi", "contains", "x")), false);      // an operator the column does not offer
  assert.strictEqual(S.validFilter(F("rsi", ">", "abc")), false);
  assert.strictEqual(S.validFilter(F("industry", "is", "  ")), false);
  assert.strictEqual(S.validFilter(F("held", "=", true)), true);
});

check("describing a chip", () => {
  assert.strictEqual(S.describeFilter(F("market_cap_cr", ">", "5000")), "Market cap (₹ cr) > 5,000");
  assert.strictEqual(S.describeFilter(F("rsi", "<", 30)), "RSI (14) < 30");
  assert.strictEqual(S.describeFilter(F("above_200", "=", true)), "Above 200-day = yes");
  assert.strictEqual(S.describeFilter(F("industry", "is", "IT")), "Sector / industry is IT");
  assert.strictEqual(S.describeFilter(F("chg_1d", "between", -2, 2)), "Change 1D between -2% and 2%");
  assert.strictEqual(S.describeFilter(F("rsi", ">=", 70)), "RSI (14) ≥ 70");
});

check("sorting puts unknown figures last in both directions, text sorts by name, ties keep their order", () => {
  assert.strictEqual(syms(S.sortRows(rows, "market_cap_cr", "desc")), "DDD,AAA,BBB,CCC");
  assert.strictEqual(syms(S.sortRows(rows, "market_cap_cr", "asc")), "BBB,AAA,DDD,CCC");
  assert.strictEqual(syms(S.sortRows(rows, "symbol", "desc")), "DDD,CCC,BBB,AAA");
  assert.strictEqual(syms(S.sortRows(rows, "industry", "asc")), "CCC,BBB,DDD,AAA");   // Construction, Financial, Healthcare, IT
});

check("sorting text is alphabetical", () => {
  assert.deepStrictEqual(S.sortRows(rows, "industry", "asc").map(r => r.industry), ["Construction Materials", "Financial Services", "Healthcare", "IT"]);
  const flags = S.sortRows(rows, "held", "desc");
  assert.strictEqual(flags[0].symbol, "BBB");
  const eq = S.sortRows([row({symbol: "P", chg_1d: 1}), row({symbol: "Q", chg_1d: 1}), row({symbol: "R", chg_1d: 2})], "chg_1d", "desc");
  assert.strictEqual(syms(eq), "R,P,Q");
});

check("the built-in presets match the brief and use valid filters", () => {
  const ids = S.PRESETS.map(p => p.id);
  for (const want of ["momentum", "near-high", "oversold", "dividend", "volume", "followed"]) assert.ok(ids.includes(want), want);
  assert.deepStrictEqual(S.PRESETS.map(p => p.label), ["Momentum leaders", "Near 52-week high", "Oversold above 200-day", "High dividend", "Unusual volume", "Followed investors' buys"]);
  for (const p of S.PRESETS) {
    assert.ok(p.filters.length >= 1 && p.filters.every(S.validFilter), p.id);
    assert.ok(S.colByKey(p.sort.key), p.id);
    if (p.universe) assert.ok(S.UNIVERSES.includes(p.universe), p.id);
  }
});

check("presets pick the right stocks", () => {
  const run = (id) => { const p = S.presetById(id); return syms(S.sortRows(S.applyFilters(rows, p.filters), p.sort.key, p.sort.dir)); };
  assert.strictEqual(run("momentum"), "AAA,DDD,CCC");                                  // passes the factor screen, best score first
  assert.strictEqual(run("near-high"), "AAA,DDD");                                     // best (closest to the high) first
});

check("near 52-week high is within five percent", () => {
  const p = S.presetById("near-high");
  assert.strictEqual(syms(S.applyFilters(rows, p.filters)), "AAA,DDD");
});

check("oversold above 200-day is RSI under 35 and above the 200-day average", () => {
  const p = S.presetById("oversold");
  assert.strictEqual(syms(S.applyFilters(rows, p.filters)), "AAA,DDD");
  assert.strictEqual(syms(S.applyFilters([row({symbol: "E", rsi: 20, above_200: false}), row({symbol: "F", rsi: 35, above_200: true}), row({symbol: "G", rsi: 34.9, above_200: true})], p.filters)), "G");
});

check("high dividend and unusual volume", () => {
  assert.strictEqual(syms(S.applyFilters(rows, S.presetById("dividend").filters)), "AAA");
  assert.strictEqual(syms(S.applyFilters(rows, S.presetById("volume").filters)), "AAA");      // more than 2x: 2.5 yes, 2.0 no
  assert.strictEqual(S.presetById("followed").universe, "DEALS");
  assert.strictEqual(syms(S.applyFilters(rows, S.presetById("followed").filters)), "AAA");
});

check("saved presets are cleaned: bad entries dropped, names trimmed, at most 20", () => {
  const good = {name: "  My view  ", universe: "NIFTY100", filters: [F("rsi", "<", 30), F("bogus", ">", 1), F("rsi", "contains", "x")], sort: {key: "rsi", dir: "asc"}, cols: ["symbol", "nope", "rsi"]};
  const out = S.cleanSaved([good, null, 5, {name: ""}, {name: "No sort", filters: "x", sort: {key: "nope", dir: "up"}, universe: "MARS"}]);
  assert.strictEqual(out.length, 2);
  assert.deepStrictEqual(out[0], {name: "My view", universe: "NIFTY100", filters: [{col: "rsi", op: "<", value: 30, value2: undefined}], sort: {key: "rsi", dir: "asc"}, cols: ["symbol", "rsi"]});
  assert.deepStrictEqual(out[1], {name: "No sort", universe: null, filters: [], sort: null, cols: []});
  assert.deepStrictEqual(S.cleanSaved("junk"), []);
  assert.strictEqual(S.cleanSaved(Array.from({length: 30}, (_, i) => ({name: "p" + i}))).length, S.MAX_SAVED);
  assert.strictEqual(S.cleanSaved([{name: "x".repeat(100)}])[0].name.length, 40);
});

check("CSV has the shown columns, the name beside the stock, quotes escaped and unknowns blank", () => {
  const csv = S.toCsv([row({symbol: "AAA", name: 'Alpha "Tech", Ltd', rsi: 28.5, above_200: true, market_cap_cr: null})], ["symbol", "rsi", "above_200", "market_cap_cr"]);
  const lines = csv.trim().split("\r\n");
  assert.strictEqual(lines[0], "Stock,Name,RSI (14),Above 200-day,Market cap (₹ cr)");
  assert.strictEqual(lines[1], 'AAA,"Alpha ""Tech"", Ltd",28.5,yes,');
});

check("CSV never lets a text cell run as a spreadsheet formula, but numbers stay numbers", () => {
  const csv = S.toCsv([row({symbol: "=cmd|x", name: "@evil", industry: "+1+1", chg_1d: -3.5})], ["symbol", "industry", "chg_1d"]);
  const cells = csv.trim().split("\r\n")[1];
  assert.strictEqual(cells, "'=cmd|x,'@evil,'+1+1,-3.5");
});

check("polling continues while stocks are pending or the list is loading, and stops on an error or completion", () => {
  assert.strictEqual(S.needsPoll({pending: 5, loading: false}), true);
  assert.strictEqual(S.needsPoll({pending: 0, loading: true}), true);
  assert.strictEqual(S.needsPoll({pending: 0, loading: false}), false);
  assert.strictEqual(S.needsPoll({pending: 3, error: "NSE down"}), false);
  assert.strictEqual(S.needsPoll(null), false);
});

check("universe list is the six indices plus holdings and followed buys", () => {
  assert.deepStrictEqual(S.UNIVERSES.slice(0, 6), ["NIFTY50", "NIFTY100", "NIFTY200", "NIFTY500", "NIFTYMIDCAP150", "NIFTYSMALLCAP250"]);
  assert.deepStrictEqual(S.UNIVERSES.slice(6), ["HOLDINGS", "DEALS"]);
  assert.strictEqual(S.UNIVERSE_LABEL.HOLDINGS, "My holdings");
  assert.strictEqual(S.UNIVERSE_LABEL.DEALS, "Followed investors' recent buys");
});

console.log(JSON.stringify({n, failures}));
