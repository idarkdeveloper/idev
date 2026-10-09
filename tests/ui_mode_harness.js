// Runs trading_agent/ui/index.html's page script against a stub DOM and canned API answers, in the mode given
// on the command line ("live" or "demo"), and prints what that mode shows as JSON. Used by tests/test_ui.py.
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const mode = process.argv[2] || "live";
const sample = process.argv[3] === "sample";
const ui = path.join(__dirname, "..", "trading_agent", "ui");
const html = fs.readFileSync(path.join(ui, "index.html"), "utf8");
const common = fs.readFileSync(path.join(ui, "common.js"), "utf8");
const page = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]).join("\n");
const initiallyHidden = new Set([...html.matchAll(/<[^>]*\bid="([^"]+)"[^>]*\bhidden\b[^>]*>/g)].map(m => m[1]));

const els = {};
function el(id) {
  if (els[id]) return els[id];
  const t = {innerHTML: "", textContent: "", value: "", className: "", hidden: initiallyHidden.has(id), disabled: false,
             checked: false, open: false, clientWidth: 600, dataset: {}, style: {}, children: [], _id: id};
  const p = new Proxy(t, {
    get(o, k) {
      if (k in o) return o[k];
      if (k === "classList") return {toggle() {}, add() {}, remove() {}};
      if (k === "querySelectorAll") return () => [];
      return () => p;  // querySelector, closest, addEventListener, showModal, scrollIntoView, ...
    },
    set(o, k, v) { o[k] = v; return true; },
  });
  els[id] = p;
  return p;
}

const baseSettings = {market: "in", currency: "INR", watch_investor: "Ashish Kacholia", watch_source: "deals", data_source: "nse",
  broker: "local-paper", mode: "paper", auto_trade: false, claude_model: "claude-opus-5-5", notify_email_to: "",
  notify_webhook_url: "", paper_starting_cash: 100000, groww_gtt_stops: false, max_slippage_pct: 1, demo: sample};
const state = {
  now: "2026-10-10T10:00:00+00:00", page: mode, regime: null, watch: {on: false, every: 60, auto_exit: false}, orders: [],
  forward: null, backtest: null, screen: null, factor_backtest: null, signal_lab: null, equity_history: [], equity_stats: null,
  costs: {examples: {"25000": 40}}, settings: baseSettings, market_day: null,
  connections: {claude: true, groww: true, groww_credentials: true, groww_live_orders: false, groww_gtt_active: false, data: "nse", prices: "groww"},
  account: {cash: 99000, equity: 100000, currency: "INR"}, positions: [{symbol: "LAURUSLABS", qty: 10, avg_entry_price: 100,
    current_price: 100, market_value: 1000, unrealized_pl: 0, high_water: 100, stop: 90, gtt: null}],
  performance: {pnl: 0, pnl_pct: 0, fees_paid: 0, starting_cash: 100000, orders: 1}, broker_error: null, deals: [], deals_error: null,
  recommendations: [{index: 0, ticker: "SENCO", action: "buy", headline: "h", rationale: "r", confidence: "high",
    suggested_notional_usd: 5000, at: "2026-10-01T00:00:00+00:00", dismissed: false}],
  runs: [], seen_count: 0, busy: false, running: null, jobs: [],
};
const portfolio = {linked: true, at: "2026-10-10T10:00:00+00:00", holdings: [{symbol: "TCS", qty: 1, sellable_qty: 1, avg_price: 100,
  price: 110, invested: 100, value: 110, pl: 10, pl_pct: 0.1, kind: "equity"}], invested: 100, value: 110, pl: 10, pl_pct: 0.1, unpriced: []};

const answers = (url) => url.includes("/api/state") ? state : url.includes("/api/my-portfolio") ? portfolio
  : url.includes("/api/scorecard") ? {summary: {by_action: {}, horizons: [5], pending: 0, benchmark: "^NSEI"}, rows: []} : {};

const document = {
  body: {dataset: Object.assign({mode}, sample ? {sample: "1"} : {})},
  getElementById: el, querySelectorAll: () => [], addEventListener() {}, createElement: () => el("_new"),
};
const ctx = {document, console, setTimeout, clearTimeout, setInterval: () => 0, Intl, Date, Math, JSON, Number, String, Object, Array,
  Set, Map, Promise, URLSearchParams, encodeURIComponent, isNaN, isFinite, parseFloat, parseInt,
  fetch: async (url) => ({ok: true, status: 200, statusText: "OK", json: async () => JSON.parse(JSON.stringify(answers(String(url))))}),
  confirm: () => { throw new Error("window.confirm must not be used for the reset"); }};
ctx.window = ctx;
ctx.addEventListener = () => {};
vm.createContext(ctx);
try {
  vm.runInContext(common, ctx);
  vm.runInContext(page, ctx);
} catch (e) { console.error(e.stack); process.exit(1); }

setTimeout(() => {
  const banner = els["demo-banner"] && !els["demo-banner"].hidden
    ? els["demo-title"].textContent + " | " + els["btn-demo-reset"].textContent : "";
  console.log(JSON.stringify({
    tiles: el("tiles").innerHTML.replace(/<[^>]+>/g, " "),
    paper_buy_button: el("recs").innerHTML.includes("data-order"),
    dismiss_button: el("recs").innerHTML.includes("data-dismiss"),
    pp_card_hidden: el("pp-card").hidden,
    banner,
    check_hidden: el("check-split").hidden, settings_hidden: el("btn-settings").hidden, watch_hidden: el("btn-watch").hidden,
  }));
  process.exit(0);
}, 300);
