// Stock screener page (screener.html). Needs common.js (window.TA) and palette.js first.
//
// Read-only: the page only reads /api/screener. A row click opens a details drawer whose link goes to the dashboard's
// Look up (/?lookup=SYMBOL); nothing here sends an order or posts anything. Filtering and sorting happen here, in the
// browser, on the rows the server returns. The first half of the file is pure logic (columns, filters, presets, sort,
// CSV, sparkline paths) that tests/screener_harness.js runs under node; the second half touches the page.
(function(){
  "use strict";

  // ---- columns ----
  // type: stock | text | num | pct | bool. pct values arrive as plain percent numbers (1.5 = 1.5%). phone: shown by default
  // on a narrow screen. group "tv": from TradingView (unofficial), shown only when that setting is on and working.
  const COLS = [
    {key: "symbol", label: "Stock", type: "stock", def: true, phone: true, always: true},
    {key: "industry", label: "Sector / industry", type: "text", hide: true},
    {key: "price", label: "Price (₹)", short: "Price ₹", type: "num", dec: 2, def: true, phone: true},
    {key: "chg_1d", label: "Change 1D", short: "1D", type: "pct", def: true, phone: true, tone: true},
    {key: "ret_1w", label: "1W %", short: "1W", type: "pct", x: true, tone: true, tip: "One week return, dividends included"},
    {key: "ret_1m", label: "1M %", short: "1M", type: "pct", def: true, tone: true, tip: "One month return, dividends included"},
    {key: "ret_6m", label: "6M %", short: "6M", type: "pct", x: true, tone: true, tip: "Six month return, dividends included"},
    {key: "ret_1y", label: "1Y %", short: "1Y", type: "pct", def: true, tone: true, tip: "One year return, dividends included"},
    {key: "ret_12_1", label: "12-1 momentum %", short: "12-1 mom.", type: "pct", def: true, tone: true, tip: "Return over the past year leaving out the latest month (the factor screen's momentum)"},
    {key: "volume", label: "Volume", type: "num", dec: 0, x: true},
    {key: "rel_volume", label: "Relative volume", short: "Rel. vol", type: "num", dec: 2, def: true, tip: "Latest day's volume divided by the average of the 20 days before"},
    {key: "market_cap_cr", label: "Market cap (₹ cr)", short: "Mkt cap ₹cr", type: "num", dec: 0, def: true},
    {key: "pe", label: "P/E", type: "num", dec: 1, def: true},
    {key: "div_yield", label: "Dividend yield %", short: "Div. yield", type: "pct", dec: 2, x: true, plain: true},
    {key: "pct_from_high", label: "From 52-week high %", short: "vs 52w high", type: "pct", def: true, tone: true, tip: "How far the price is below its 52-week high"},
    {key: "rsi", label: "RSI (14)", short: "RSI", type: "num", dec: 1, def: true},
    {key: "atr_pct", label: "ATR %", short: "ATR", type: "pct", dec: 2, x: true, plain: true, tip: "14-day average true range as a share of the price (close to close)"},
    {key: "band", label: "Price band", short: "Band", type: "text", x: true, tip: "NSE price band: the most the price may move in a day"},
    {key: "above_200", label: "Above 200-day", short: "> 200d", type: "bool", x: true, tip: "Price above its 200-day average"},
    {key: "held", label: "Held", type: "bool", hide: true, tip: "You own it (saved holdings and the practice account)"},
    {key: "deal", label: "Deal", type: "bool", hide: true, tip: "A followed investor bought it in a disclosed deal in the last 30 days"},
    {key: "avg_turnover_cr", label: "Avg turnover (₹ cr/day)", short: "Turnover ₹cr", type: "num", dec: 2, x: true, tip: "Average daily traded value over 60 days"},
    {key: "mom_score", label: "Momentum score", short: "Mom. score", type: "num", dec: 2, x: true, tip: "The factor screen's composite score across this list"},
    {key: "mom_eligible", label: "Passes factor screen", short: "Factor screen", type: "bool", x: true, tip: "Above its 200-day average and liquid enough, as the factor screen requires"},
    {key: "tv_summary_label", label: "Summary (TV)", type: "text", group: "tv", def: true, tip: "from TradingView, unofficial"},
    {key: "tv_sector", label: "Sector (TV)", type: "text", group: "tv", x: true, tip: "from TradingView, unofficial"},
    {key: "tv_industry", label: "Industry (TV)", type: "text", group: "tv", x: true, tip: "from TradingView, unofficial"},
    {key: "tv_eps_growth", label: "EPS growth % (TV)", type: "pct", group: "tv", def: true, tone: true, tip: "from TradingView, unofficial"},
  ];
  const colByKey = (key) => COLS.find(c => c.key === key) || null;
  const UNIVERSES = ["NIFTY50", "NIFTY100", "NIFTY200", "NIFTY500", "NIFTYMIDCAP150", "NIFTYSMALLCAP250", "HOLDINGS", "DEALS"];
  const UNIVERSE_LABEL = {HOLDINGS: "My holdings", DEALS: "Followed investors' recent buys"};
  const OPS = {num: [">", ">=", "<", "<=", "=", "between"], pct: [">", ">=", "<", "<=", "=", "between"], text: ["is", "contains"], bool: ["="], stock: ["contains"]};
  const OP_WORD = {">": ">", ">=": "≥", "<": "<", "<=": "≤", "=": "=", between: "between", is: "is", contains: "contains"};

  const defaultColumns = (narrow, tvOn) => COLS.filter(c => (c.group !== "tv" || tvOn) && (narrow ? c.phone : c.def)).map(c => c.key);
  // "All columns": every table column (filter-only fields such as Held, Deal and the sector stay out of the grid).
  const allColumns = (tvOn) => COLS.filter(c => !c.hide && (c.group !== "tv" || tvOn)).map(c => c.key);

  // ---- filters ----
  // A filter is {col, op, value, value2?}. A stock whose figure is unknown ("n/a") never passes a filter on it.
  const num = (x) => { const n = typeof x === "number" ? x : parseFloat(String(x == null ? "" : x).replace(/[,\s₹%]/g, "")); return Number.isFinite(n) ? n : null; };
  function passes(row, f){
    const col = colByKey(f.col); if(!col) return true;
    const v = row[f.col];
    if(v === null || v === undefined || v === "") return false;
    if(col.type === "bool") return (v === true) === (f.value === true || f.value === "yes" || f.value === "true");
    if(col.type === "text" || col.type === "stock"){
      const a = String(v).toLowerCase(), b = String(f.value == null ? "" : f.value).toLowerCase();
      return f.op === "is" ? a === b : a.indexOf(b) >= 0;
    }
    const x = num(v), a = num(f.value), b = num(f.value2);
    if(x === null || a === null) return false;
    switch(f.op){
      case ">": return x > a; case ">=": return x >= a; case "<": return x < a; case "<=": return x <= a;
      case "=": return x === a;
      case "between": { if(b === null) return false; const lo = Math.min(a, b), hi = Math.max(a, b); return x >= lo && x <= hi; }
    }
    return false;
  }
  function matchesQuery(row, q){
    const t = String(q || "").trim().toLowerCase();
    if(!t) return true;
    return String(row.symbol || "").toLowerCase().indexOf(t) >= 0 || String(row.name || "").toLowerCase().indexOf(t) >= 0;
  }
  const applyFilters = (rows, filters, query) => rows.filter(r => matchesQuery(r, query) && (filters || []).every(f => passes(r, f)));
  // A filter that can be added: known column, an operator that column allows, and a number (two for "between") or text.
  function validFilter(f){
    const col = f && colByKey(f.col); if(!col) return false;
    if(OPS[col.type].indexOf(f.op) < 0) return false;
    if(col.type === "bool") return f.value === true || f.value === false;
    if(col.type === "text" || col.type === "stock") return String(f.value == null ? "" : f.value).trim() !== "";
    if(num(f.value) === null) return false;
    return f.op !== "between" || num(f.value2) !== null;
  }
  const groupNum = (n, dec) => Number(n).toLocaleString("en-IN", {minimumFractionDigits: dec || 0, maximumFractionDigits: dec || 0});
  function describeFilter(f){
    const col = colByKey(f.col); if(!col) return "";
    if(col.type === "bool") return col.label + " = " + (f.value === true || f.value === "yes" ? "yes" : "no");
    const unit = col.type === "pct" ? "%" : "";
    const val = (x) => col.type === "text" || col.type === "stock" ? String(x) : groupNum(num(x), Math.min(2, String(x).split(".")[1] ? String(x).split(".")[1].length : 0)) + unit;
    const label = col.label.replace(/ %$/, "");
    if(f.op === "between") return label + " between " + val(f.value) + " and " + val(f.value2);
    return label + " " + OP_WORD[f.op] + " " + val(f.value);
  }

  // ---- sort: unknown figures always last ----
  function sortRows(rows, key, dir){
    const col = colByKey(key), sign = dir === "asc" ? 1 : -1;
    const val = (r) => { const v = r[key]; return v === undefined || v === "" ? null : v; };
    return rows.map((r, i) => [r, i]).sort((A, B) => {
      const a = val(A[0]), b = val(B[0]);
      if(a === null && b === null) return A[1] - B[1];
      if(a === null) return 1;
      if(b === null) return -1;
      let c;
      if(col && (col.type === "text" || col.type === "stock")) c = String(a).localeCompare(String(b), "en", {sensitivity: "base"});
      else if(typeof a === "boolean") c = (a ? 1 : 0) - (b ? 1 : 0);
      else c = a < b ? -1 : a > b ? 1 : 0;
      return c ? c * sign : A[1] - B[1];
    }).map(x => x[0]);
  }

  // ---- presets ----
  const f = (col, op, value, value2) => ({col, op, value, value2});
  const PRESETS = [
    {id: "momentum", label: "Momentum leaders", note: "Passes the factor screen (above its 200-day average, liquid), best composite score first",
     filters: [f("mom_eligible", "=", true)], sort: {key: "mom_score", dir: "desc"}},
    {id: "near-high", label: "Near 52-week high", note: "Within 5% of its 52-week high",
     filters: [f("pct_from_high", ">=", -5)], sort: {key: "pct_from_high", dir: "desc"}},
    {id: "oversold", label: "Oversold above 200-day", note: "RSI below 35 while above its 200-day average",
     filters: [f("rsi", "<", 35), f("above_200", "=", true)], sort: {key: "rsi", dir: "asc"}},
    {id: "dividend", label: "High dividend", note: "Dividend yield above 3%",
     filters: [f("div_yield", ">", 3)], sort: {key: "div_yield", dir: "desc"}},
    {id: "volume", label: "Unusual volume", note: "Today's volume more than twice its 20-day average",
     filters: [f("rel_volume", ">", 2)], sort: {key: "rel_volume", dir: "desc"}},
    {id: "followed", label: "Followed investors' buys", note: "Bought by a followed investor in a disclosed deal in the last 30 days",
     universe: "DEALS", filters: [f("deal", "=", true)], sort: {key: "chg_1d", dir: "desc"}},
  ];
  const presetById = (id) => PRESETS.find(p => p.id === id) || null;

  // User presets live in localStorage as {name, universe, filters, sort, cols}; anything malformed is dropped.
  const MAX_SAVED = 20;
  function cleanSaved(list){
    const out = [];
    for(const p of Array.isArray(list) ? list : []){
      if(!p || typeof p.name !== "string" || !p.name.trim() || out.length >= MAX_SAVED) continue;
      const filters = (Array.isArray(p.filters) ? p.filters : []).filter(validFilter).map(x => ({col: x.col, op: x.op, value: x.value, value2: x.value2}));
      const sortOk = p.sort && colByKey(p.sort.key) && (p.sort.dir === "asc" || p.sort.dir === "desc");
      out.push({name: p.name.trim().slice(0, 40), universe: UNIVERSES.indexOf(p.universe) >= 0 ? p.universe : null, filters,
                sort: sortOk ? {key: p.sort.key, dir: p.sort.dir} : null,
                cols: (Array.isArray(p.cols) ? p.cols : []).filter(k => colByKey(k))});
    }
    return out;
  }

  // ---- CSV of the current view ----
  // Text cells that start with = + - @ get a leading apostrophe so a spreadsheet never runs them as a formula.
  function csvCell(v, col){
    if(v === null || v === undefined) return "";
    let s = typeof v === "boolean" ? (v ? "yes" : "no") : String(v);
    if((col.type === "text" || col.type === "stock") && /^[=+\-@\t\r]/.test(s)) s = "'" + s;
    return /[",\n\r]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  }
  function toCsv(rows, colKeys){
    const cols = colKeys.map(colByKey).filter(Boolean);
    const head = [];
    for(const c of cols){ head.push(csvCell(c.label, {type: "num"})); if(c.key === "symbol") head.push("Name"); }
    const lines = [head.join(",")];
    for(const r of rows){
      const cells = [];
      for(const c of cols){ cells.push(csvCell(r[c.key], c)); if(c.key === "symbol") cells.push(csvCell(r.name, {type: "text"})); }
      lines.push(cells.join(","));
    }
    return lines.join("\r\n") + "\r\n";
  }

  // ---- sparkline / line chart path (pure): closes → SVG "M x y L x y ..." in a w×h box with pad on every side ----
  function sparkPath(arr, w, h, pad){
    const pts = (Array.isArray(arr) ? arr : []).filter(x => typeof x === "number" && Number.isFinite(x));
    if(pts.length < 2) return "";
    const lo = Math.min.apply(null, pts), hi = Math.max.apply(null, pts);
    const sx = (w - pad * 2) / (pts.length - 1), sy = (h - pad * 2) / ((hi - lo) || 1);
    return pts.map((p, i) => (i ? "L" : "M") + (pad + i * sx).toFixed(1) + " " + (h - pad - (p - lo) * sy).toFixed(1)).join(" ");
  }

  // ---- polling: keep asking while stocks are still being fetched; ignore an answer for a list no longer shown ----
  const needsPoll = (data) => !!data && !data.error && (data.loading === true || (data.pending || 0) > 0);

  const logic = {COLS, colByKey, UNIVERSES, UNIVERSE_LABEL, OPS, OP_WORD, defaultColumns, allColumns, sparkPath, num, passes, matchesQuery, applyFilters,
                 validFilter, describeFilter, sortRows, PRESETS, presetById, cleanSaved, MAX_SAVED, csvCell, toCsv, needsPoll, groupNum};
  if(typeof module !== "undefined" && module.exports) module.exports = logic;
  if(typeof window === "undefined" || typeof document === "undefined" || !window.TA) return;
  window.TA.screener = logic;


  const {$, esc, toast, api} = window.TA;
  const store = {
    get(k){ try { return JSON.parse(localStorage.getItem(k) || "null"); } catch(e){ return null; } },
    set(k, v){ try { localStorage.setItem(k, JSON.stringify(v)); } catch(e){} },
  };
  const narrow = () => { try { return matchMedia("(max-width: 700px)").matches; } catch(e){ return false; } };
  const KEY_STATE = "ta_screener_state", KEY_SAVED = "ta_screener_presets";
  const st = {universe: "NIFTY50", filters: [], sort: {key: "market_cap_cr", dir: "desc"}, query: "", data: null, seq: 0, timer: null,
              preset: "", density: "comfortable", allCols: false, sel: null, pop: {col: "market_cap_cr", op: ">", bool: true}};
  let saved = cleanSaved(store.get(KEY_SAVED));
  let lastFocus = null;

  function loadState(){
    const s = store.get(KEY_STATE); if(!s) return;
    if(UNIVERSES.indexOf(s.universe) >= 0) st.universe = s.universe;
    st.filters = (Array.isArray(s.filters) ? s.filters : []).filter(validFilter);
    if(s.sort && colByKey(s.sort.key) && (s.sort.dir === "asc" || s.sort.dir === "desc")) st.sort = {key: s.sort.key, dir: s.sort.dir};
    if(s.density === "compact") st.density = "compact";
    st.allCols = s.allCols === true;
  }
  const saveState = () => store.set(KEY_STATE, {universe: st.universe, filters: st.filters, sort: st.sort, density: st.density, allCols: st.allCols});
  const tvOn = () => !!(st.data && st.data.tv && st.data.tv.state === "on");
  // The stock column is drawn on its own (sticky, with badges); these are the figure columns after it. Whatever a filter or
  // the sort uses is shown too, so a preset's own figures are always visible.
  const visibleCols = () => {
    const base = (st.allCols ? allColumns(tvOn()) : defaultColumns(narrow(), tvOn())).concat(st.filters.map(x => x.col), [st.sort.key]);
    return COLS.filter(c => c.key !== "symbol" && !c.hide && base.indexOf(c.key) >= 0 && (c.group !== "tv" || tvOn()));
  };
  const baseRows = () => st.data && st.data.rows ? st.data.rows : [];
  const currentRows = () => sortRows(applyFilters(baseRows(), st.filters, st.query), st.sort.key, st.sort.dir);

  // ---- formatting ----
  const toneCls = (v) => v == null ? "" : v > 0 ? "pl-profit" : v < 0 ? "pl-loss" : "";
  const missing = (v) => v === null || v === undefined || v === "";
  function fmt(c, v){
    if(missing(v)) return "n/a";
    if(c.type === "bool") return v ? "Yes" : "No";
    if(c.type === "text") return String(v);
    if(c.type === "pct") return (c.plain ? "" : (v > 0 ? "+" : "")) + groupNum(v, c.dec == null ? 2 : c.dec) + "%";
    return groupNum(v, c.dec);
  }
  const badges = (r) => (r.held ? '<span class="sc-badge sc-held">Held</span>' : "")
    + (r.deal ? `<span class="sc-badge sc-bought" title="${esc((r.deal_who || []).join(", "))}">Bought</span>` : "");
  function spark(r){
    const pts = Array.isArray(r.spark) ? r.spark.filter(Number.isFinite).slice(-30) : [];
    const h = st.density === "compact" ? 18 : 26, d = sparkPath(pts, 72, h, 2);
    if(!d) return '<span class="sub">n/a</span>';
    const up = pts[pts.length - 1] >= pts[0];
    return `<svg class="sc-spark ${up ? "up" : "down"}" width="72" height="${h}" viewBox="0 0 72 ${h}" aria-hidden="true"><path d="${d}"></path></svg>`;
  }
  function cell(c, r){
    const v = r[c.key];
    if(missing(v)) return `<td class="${c.type === "text" ? "" : "num"}"><span class="sub">n/a</span></td>`;
    if(c.type === "text") return `<td class="sc-text" title="${esc(v)}">${esc(v)}</td>`;
    if(c.type === "bool") return `<td>${esc(fmt(c, v))}</td>`;
    return `<td class="num ${c.tone ? toneCls(v) : ""}">${esc(fmt(c, v))}</td>`;
  }

  // ---- top row, presets, chips ----
  function renderTop(rows){
    const all = baseRows().length;
    $("sc-count").innerHTML = st.data && all ? `<b>${rows.length}</b> of ${all} stocks match` : "";
    for(const b of $("sc-density").querySelectorAll("[data-density]")) b.setAttribute("aria-pressed", String(b.dataset.density === st.density));
    $("sc-allcols").textContent = st.allCols ? "Fewer columns" : "All columns";
    $("sc-allcols").setAttribute("aria-pressed", String(st.allCols));
    $("sc-export").disabled = !rows.length;
    $("sc-card").classList.toggle("sc-compact", st.density === "compact");
  }
  function renderPresets(){
    const rows = baseRows(), have = rows.length > 0;
    const count = (filters) => have ? applyFilters(rows, filters, "").length : null;
    const pill = (id, label, n, title) => `<button type="button" class="sc-pill" data-preset="${esc(id)}" aria-pressed="${st.preset === id}"${title ? ` title="${esc(title)}"` : ""}>`
      + `<span>${label}</span>${n == null ? "" : `<span class="sc-pillcount">${n}</span>`}</button>`;
    // a preset that switches list (followed investors) can only be counted once its own list is showing
    const built = PRESETS.map(p => pill("b:" + p.id, esc(p.label), p.universe && p.universe !== st.universe ? null : count(p.filters), p.note));
    const mine = saved.map((p, i) => pill("s:" + i, esc(p.name), p.universe && p.universe !== st.universe ? null : count(p.filters), "Your saved view")
      + (st.preset === "s:" + i ? `<button type="button" class="sc-link sc-delsaved" data-del="${i}" aria-label="Delete saved view ${esc(p.name)}">Delete</button>` : ""));
    $("sc-presets").innerHTML = built.concat(mine).join("");
    const sel = /^b:/.test(st.preset) ? presetById(st.preset.slice(2)) : null;
    $("sc-presetnote").textContent = sel ? sel.note : "";
    $("sc-presetnote").hidden = !sel;
  }
  function renderChips(){
    $("sc-chips").innerHTML = st.filters.map((fl, i) =>
      `<span class="sc-chip"><span>${esc(describeFilter(fl))}</span><button type="button" data-rm="${i}" aria-label="Remove filter: ${esc(describeFilter(fl))}">×</button></span>`).join("");
    $("sc-clear").hidden = !st.filters.length && !st.query;
  }

  // ---- table ----
  function renderTable(){
    const cols = visibleCols(), rows = currentRows(), all = baseRows().length, data = st.data, span = cols.length + 2;
    const th = (key, label, cls, tip) => {
      const on = st.sort.key === key, dir = on ? (st.sort.dir === "asc" ? "ascending" : "descending") : "none";
      return `<th class="${cls}${on ? " on" : ""}" aria-sort="${dir}"${tip ? ` title="${esc(tip)}"` : ""}><button type="button" class="sc-sort" data-sort="${esc(key)}">${label}`
        + `${on ? `<span aria-hidden="true">${st.sort.dir === "asc" ? "▲" : "▼"}</span>` : ""}</button></th>`;
    };
    $("sc-head").innerHTML = "<tr>" + th("symbol", "Stock", "sc-stock", "") + `<th class="sc-sparkcol">30 days</th>`
      + cols.map(c => th(c.key, esc(c.short || c.label), c.type === "text" || c.type === "bool" ? "" : "num", c.tip ? c.label + ": " + c.tip : c.label)).join("") + "</tr>";
    let body;
    if(data && data.error && !all) body = `<tr><td colspan="${span}" class="empty">${esc(data.error)}</td></tr>`;
    else if(!all) body = `<tr><td colspan="${span}" class="empty">${data && data.loading ? "Loading the stock list…" : data ? "No stocks in this list." : "Loading…"}</td></tr>`;
    else if(!rows.length) body = `<tr><td colspan="${span}" class="empty">No stock matches these filters.</td></tr>`;
    else body = rows.map(r => {
      const sub = [r.name, r.industry].filter(Boolean).join(" · ");
      return `<tr data-sym="${esc(r.symbol)}" tabindex="0" aria-selected="${st.sel === r.symbol}">`
        + `<td class="sc-stock"><div class="sc-symrow"><span class="sc-sym">${esc(r.symbol)}</span>${badges(r)}</div>`
        + (sub ? `<div class="sc-name" title="${esc(sub)}">${esc(r.name)}${r.name && r.industry ? " · " : ""}${esc(r.industry)}</div>` : "") + `</td>`
        + `<td class="sc-sparkcol">${spark(r)}</td>` + cols.map(c => cell(c, r)).join("") + `</tr>`;
    }).join("");
    $("sc-rows").innerHTML = body;
    renderTop(rows); renderPresets(); renderChips(); renderFresh();
    if(st.sel) renderDrawer();
  }

  function renderFresh(){
    const d = st.data;
    let t = "";
    if(d && d.source){
      t = d.source + (d.as_of ? "; latest price bar " + d.as_of : "");
      if(d.generated_at){ try { t += "; updated " + new Date(d.generated_at).toLocaleTimeString("en-IN", {hour: "2-digit", minute: "2-digit", hour12: false}); } catch(e){} }
    }
    $("sc-fresh").textContent = t;
    const bits = [];
    if(d && (d.pending > 0 || d.loading)) bits.push(d.loading && !(d.rows || []).length ? "Loading the stock list…" : `${d.pending} stock${d.pending === 1 ? "" : "s"} still being fetched; the table fills in as they arrive.`);
    if(d && d.error && d.rows && d.rows.length) bits.push(d.error);
    if(d && d.tv && d.tv.state === "refused_server") bits.push("TradingView columns are never used on the server.");
    if(d && d.tv && d.tv.state === "backoff") bits.push("TradingView columns paused until " + new Date(d.tv.until).toLocaleString("en-IN", {day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hour12: false}) + " (TradingView said no or did not answer).");
    $("sc-status").textContent = bits.join(" ");
  }

  // ---- presets ----
  function applyPreset(id){
    if(st.preset === id){ st.preset = ""; st.filters = []; saveState(); renderTable(); return; }   // the active pill clears it
    const p = /^b:/.test(id) ? presetById(id.slice(2)) : /^s:/.test(id) ? saved[Number(id.slice(2))] : null;
    if(!p) return;
    st.preset = id;
    st.filters = (p.filters || []).map(x => ({col: x.col, op: x.op, value: x.value, value2: x.value2}));
    if(p.sort) st.sort = {key: p.sort.key, dir: p.sort.dir};
    const u = p.universe && UNIVERSES.indexOf(p.universe) >= 0 ? p.universe : null;
    saveState();
    if(u && u !== st.universe){ st.universe = u; $("sc-universe").value = u; load(true); }
    else renderTable();
  }
  const filtersChanged = () => { st.preset = ""; saveState(); renderTable(); };

  // ---- add-filter popover ----
  const popOpen = () => !$("sc-pop").hidden;
  function renderPop(){
    const col = colByKey(st.pop.col) || colByKey("market_cap_cr");
    if(OPS[col.type].indexOf(st.pop.op) < 0) st.pop.op = OPS[col.type][0];
    $("af-col").value = col.key;
    $("af-num").hidden = col.type === "bool";
    $("af-bool").hidden = col.type !== "bool";
    $("af-ops").innerHTML = OPS[col.type].filter(o => o !== "=").map(o =>
      `<button type="button" data-op="${esc(o)}" aria-pressed="${st.pop.op === o}">${esc(OP_WORD[o])}</button>`).join("");
    for(const b of $("af-bool").querySelectorAll("[data-bool]")) b.setAttribute("aria-pressed", String((b.dataset.bool === "yes") === st.pop.bool));
    const between = st.pop.op === "between", text = col.type === "text" || col.type === "stock";
    $("af-and").hidden = $("af-val2").hidden = !between;
    $("af-unit").textContent = col.type === "pct" ? "%" : "";
    $("af-val").setAttribute("inputmode", text ? "text" : "decimal");
    $("af-val").placeholder = text ? (col.key === "industry" ? "e.g. Information Technology" : "text") : "number";
    if(col.key === "industry"){
      $("af-val").setAttribute("list", "af-sectors");
      const set = Array.from(new Set(baseRows().map(r => r.industry).filter(Boolean))).sort();
      $("af-sectors").innerHTML = set.map(s => `<option value="${esc(s)}">`).join("");
    } else $("af-val").removeAttribute("list");
  }
  function openPop(){
    lastFocus = document.activeElement;
    const pop = $("sc-pop"), r = $("sc-addbtn").getBoundingClientRect();
    $("af-val").value = ""; $("af-val2").value = "";
    renderPop();
    pop.hidden = false; $("sc-pop-backdrop").hidden = false;
    $("sc-addbtn").setAttribute("aria-expanded", "true");
    const w = pop.offsetWidth, left = Math.max(8, Math.min(r.left, window.innerWidth - w - 8));
    pop.style.left = left + "px";
    pop.style.top = Math.min(r.bottom + 6, Math.max(8, window.innerHeight - pop.offsetHeight - 8)) + "px";
    (colByKey(st.pop.col).type === "bool" ? $("af-bool").querySelector("[aria-pressed=true]") : $("af-val")).focus();
  }
  function closePop(){
    if(!popOpen()) return;
    $("sc-pop").hidden = true; $("sc-pop-backdrop").hidden = true;
    $("sc-addbtn").setAttribute("aria-expanded", "false");
    if(lastFocus && lastFocus.focus) lastFocus.focus();
  }
  function readFilter(){
    const col = colByKey(st.pop.col), fl = {col: col.key, op: col.type === "bool" ? "=" : st.pop.op};
    if(col.type === "bool") fl.value = st.pop.bool;
    else { fl.value = $("af-val").value.trim(); if(fl.op === "between") fl.value2 = $("af-val2").value.trim(); }
    return fl;
  }

  // ---- drawer: one stock's details, prev / next through the rows as filtered and sorted ----
  const drawerOpen = () => !$("sc-drawer").hidden;
  function renderDrawer(){
    const rows = currentRows(), i = rows.findIndex(r => r.symbol === st.sel);
    if(i < 0){ closeDrawer(); return; }
    const r = rows[i], C = (k) => colByKey(k);
    const pts = Array.isArray(r.spark) ? r.spark.filter(Number.isFinite) : [];
    const line = sparkPath(pts, 360, 140, 4), up = pts.length > 1 && pts[pts.length - 1] >= pts[0];
    const chart = line ? `<svg class="sc-drchart ${up ? "up" : "down"}" viewBox="0 0 360 140" preserveAspectRatio="none" aria-hidden="true">`
      + `<path class="area" d="${line} L356 136 L4 136 Z"></path><path class="line" d="${line}"></path></svg>`
      + `<div class="sc-drrange sub"><span>${pts.length} days · low ₹${esc(groupNum(Math.min.apply(null, pts), 2))}</span><span>high ₹${esc(groupNum(Math.max.apply(null, pts), 2))}</span></div>`
      : `<div class="empty">No price history yet.</div>`;
    const ret = (k, lab) => { const v = r[k]; return `<div class="sc-ret"><div class="sc-eyebrow">${lab}</div><div class="${toneCls(v)}">${esc(missing(v) ? "n/a" : fmt(C(k), v))}</div></div>`; };
    const stat = (k, lab) => `<div class="sc-stat"><span>${esc(lab)}</span><span>${esc(fmt(C(k), r[k]))}</span></div>`;
    const sub = [r.name, r.industry].filter(Boolean);
    $("sc-drawer").innerHTML =
      `<div class="sc-drhead"><span class="sub">${i + 1} of ${rows.length}</span><span class="sc-drnav">`
      + `<button type="button" class="sc-icon" data-dr="prev" aria-label="Previous stock" ${i ? "" : "disabled"}>‹</button>`
      + `<button type="button" class="sc-icon" data-dr="next" aria-label="Next stock" ${i < rows.length - 1 ? "" : "disabled"}>›</button>`
      + `<button type="button" class="sc-icon" data-dr="close" aria-label="Close details">×</button></span></div>`
      + `<div class="sc-drbody"><div class="sc-symrow"><h3 id="dr-symbol">${esc(r.symbol)}</h3>${badges(r)}</div>`
      + (sub.length ? `<div class="sub">${esc(r.name)}${sub.length > 1 ? " · " : ""}${esc(r.industry)}</div>` : "")
      + `<div class="sc-drprice"><span class="sc-big">${missing(r.price) ? "n/a" : "₹" + esc(groupNum(r.price, 2))}</span>`
      + `<span class="${toneCls(r.chg_1d)}">${esc(fmt(C("chg_1d"), r.chg_1d))}</span><span class="sub">today</span></div>`
      + chart
      + `<div class="sc-rets">${ret("ret_1w", "1W")}${ret("ret_1m", "1M")}${ret("ret_6m", "6M")}${ret("ret_1y", "1Y")}${ret("ret_12_1", "12-1")}</div>`
      + `<div class="sc-stats">${stat("market_cap_cr", "Market cap (₹ cr)")}${stat("pe", "P/E")}${stat("div_yield", "Dividend yield")}${stat("volume", "Volume")}`
      + `${stat("rel_volume", "Relative volume")}${stat("pct_from_high", "From 52-week high")}${stat("rsi", "RSI (14)")}${stat("atr_pct", "ATR")}`
      + `${stat("band", "Price band")}${stat("above_200", "Above 200-day")}${stat("mom_score", "Momentum score")}${stat("mom_eligible", "Passes factor screen")}</div>`
      + (r.deal ? `<div class="sc-deal">Bought in a disclosed deal in the last 30 days by <b>${esc((r.deal_who || []).join(", ") || "a followed investor")}</b></div>` : "")
      + `<a class="sc-btn sc-outline sc-drlink" href="${esc(TA.palette.landingUrl("lookup", r.symbol))}">Open in Look up</a></div>`;
    for(const tr of $("sc-rows").querySelectorAll("tr[data-sym]")) tr.setAttribute("aria-selected", String(tr.dataset.sym === st.sel));
  }
  function openDrawer(sym){
    if(!drawerOpen()) lastFocus = document.activeElement;
    st.sel = sym;
    $("sc-drawer").hidden = false; $("sc-scrim").hidden = false;
    renderDrawer();
    const close = $("sc-drawer").querySelector("[data-dr=close]"); if(close) close.focus();
  }
  function closeDrawer(){
    const was = st.sel; st.sel = null;
    $("sc-drawer").hidden = true; $("sc-scrim").hidden = true;
    for(const tr of $("sc-rows").querySelectorAll("tr[aria-selected=true]")) tr.setAttribute("aria-selected", "false");
    const back = was && $("sc-rows").querySelector(`tr[data-sym="${CSS.escape(was)}"]`);
    if(back) back.focus(); else if(lastFocus && lastFocus.focus) lastFocus.focus();
  }
  function stepDrawer(delta){
    const rows = currentRows(), i = rows.findIndex(r => r.symbol === st.sel), j = i + delta;
    if(i < 0 || j < 0 || j >= rows.length) return;
    st.sel = rows[j].symbol; renderDrawer();
    const tr = $("sc-rows").querySelector(`tr[data-sym="${CSS.escape(st.sel)}"]`); if(tr) tr.scrollIntoView({block: "nearest"});
    const b = $("sc-drawer").querySelector(`[data-dr=${delta < 0 ? "prev" : "next"}]`);
    (b && !b.disabled ? b : $("sc-drawer").querySelector("[data-dr=close]")).focus();
  }

  // ---- data: one request now, then poll while stocks are still pending ----
  async function load(reset){
    const my = ++st.seq;
    clearTimeout(st.timer);
    if(reset){ st.data = null; if(drawerOpen()) closeDrawer(); renderTable(); }
    let data = null;
    try { data = await api("/api/screener?universe=" + encodeURIComponent(st.universe)); }
    catch(e){
      if(my !== st.seq) return;
      if(!st.data){ st.data = {rows: [], error: "Couldn't load the screener: " + e.message, pending: 0, loading: false, tv: {state: "off"}}; renderTable(); }
      else toast("Couldn't refresh the screener: " + e.message);
      st.timer = setTimeout(() => { if(my === st.seq && !document.hidden) load(false); }, 10000);
      return;
    }
    if(my !== st.seq) return;   // the list changed while this was in flight
    st.data = data;
    renderTable();
    if(popOpen()) renderPop();
    if(needsPoll(data)) st.timer = setTimeout(() => { if(my === st.seq) load(false); }, document.hidden ? 15000 : 3000);
  }

  function download(name, text){
    const url = URL.createObjectURL(new Blob([text], {type: "text/csv;charset=utf-8"}));
    const a = document.createElement("a"); a.href = url; a.download = name; document.body.appendChild(a); a.click();
    setTimeout(() => { URL.revokeObjectURL(url); a.remove(); }, 0);
  }

  function saveView(){
    const name = ($("sc-savename").value || "").trim();
    if(!name){ toast("Give the view a name first"); $("sc-savename").focus(); return; }
    const entry = cleanSaved([{name, universe: st.universe, filters: st.filters, sort: st.sort, cols: []}])[0];
    const i = saved.findIndex(p => p.name.toLowerCase() === entry.name.toLowerCase());
    if(i >= 0) saved[i] = entry; else if(saved.length >= MAX_SAVED){ toast("You can keep up to " + MAX_SAVED + " views; delete one first"); return; } else saved.push(entry);
    store.set(KEY_SAVED, saved);
    st.preset = "s:" + saved.findIndex(p => p.name === entry.name);
    $("sc-savename").value = ""; $("sc-savewrap").hidden = true; $("sc-save").hidden = false;
    renderPresets(); toast("View saved");
  }

  function init(){
    loadState();
    $("sc-universe").innerHTML = UNIVERSES.map(u => `<option value="${esc(u)}">${esc(UNIVERSE_LABEL[u] || u)}</option>`).join("");
    $("sc-universe").value = st.universe;
    $("af-col").innerHTML = COLS.filter(c => c.type !== "stock").map(c => `<option value="${esc(c.key)}">${esc(c.label)}</option>`).join("");
    renderTable();

    $("sc-universe").addEventListener("change", (e) => { st.universe = e.target.value; st.preset = ""; saveState(); load(true); });
    $("sc-q").addEventListener("input", (e) => { st.query = e.target.value; renderTable(); });
    $("sc-density").addEventListener("click", (e) => {
      const b = e.target.closest && e.target.closest("[data-density]"); if(!b) return;
      st.density = b.dataset.density === "compact" ? "compact" : "comfortable"; saveState(); renderTable();
    });
    $("sc-allcols").addEventListener("click", () => { st.allCols = !st.allCols; saveState(); renderTable(); });
    $("sc-export").addEventListener("click", () => {
      const rows = currentRows(); if(!rows.length) return;
      download(`screener-${st.universe.toLowerCase()}-${new Date().toISOString().slice(0, 10)}.csv`, toCsv(rows, ["symbol", "industry"].concat(visibleCols().map(c => c.key).filter(k => k !== "industry"))));
    });
    $("sc-presets").addEventListener("click", (e) => {
      const del = e.target.closest && e.target.closest("[data-del]");
      if(del){ saved.splice(Number(del.dataset.del), 1); store.set(KEY_SAVED, saved); st.preset = ""; renderPresets(); toast("View deleted"); return; }
      const b = e.target.closest && e.target.closest("[data-preset]"); if(b) applyPreset(b.dataset.preset);
    });
    $("sc-chips").addEventListener("click", (e) => {
      const rm = e.target.closest && e.target.closest("[data-rm]"); if(!rm) return;
      st.filters.splice(Number(rm.dataset.rm), 1); filtersChanged();
      $("sc-addbtn").focus();
    });
    $("sc-clear").addEventListener("click", () => { st.filters = []; st.query = ""; $("sc-q").value = ""; filtersChanged(); $("sc-q").focus(); });
    $("sc-save").addEventListener("click", () => { $("sc-save").hidden = true; $("sc-savewrap").hidden = false; $("sc-savename").focus(); });
    $("sc-saveok").addEventListener("click", saveView);
    $("sc-savename").addEventListener("keydown", (e) => {
      if(e.key === "Enter"){ e.preventDefault(); saveView(); }
      else if(e.key === "Escape"){ e.stopPropagation(); $("sc-savewrap").hidden = true; $("sc-save").hidden = false; $("sc-save").focus(); }
    });
    $("sc-head").addEventListener("click", (e) => {
      const b = e.target.closest && e.target.closest("[data-sort]"); if(!b) return;
      const key = b.dataset.sort, col = colByKey(key), textish = col.type === "text" || col.type === "stock";
      st.sort = st.sort.key === key ? {key, dir: st.sort.dir === "asc" ? "desc" : "asc"} : {key, dir: textish ? "asc" : "desc"};
      saveState(); renderTable();
    });
    // a row click toggles its details; Enter or Space on a focused row does the same
    const rowAct = (tr) => { if(st.sel === tr.dataset.sym) closeDrawer(); else openDrawer(tr.dataset.sym); };
    $("sc-rows").addEventListener("click", (e) => { const tr = e.target.closest && e.target.closest("tr[data-sym]"); if(tr) rowAct(tr); });
    $("sc-rows").addEventListener("keydown", (e) => {
      if(e.key !== "Enter" && e.key !== " ") return;
      const tr = e.target.closest && e.target.closest("tr[data-sym]"); if(!tr || e.target !== tr) return;
      e.preventDefault(); rowAct(tr);
    });

    $("sc-addbtn").addEventListener("click", () => popOpen() ? closePop() : openPop());
    $("sc-pop-backdrop").addEventListener("click", closePop);
    $("af-cancel").addEventListener("click", closePop);
    $("af-col").addEventListener("change", (e) => { st.pop.col = e.target.value; renderPop(); });
    $("af-ops").addEventListener("click", (e) => {
      const b = e.target.closest && e.target.closest("[data-op]"); if(!b) return;
      st.pop.op = b.dataset.op; renderPop(); $("af-val").focus();
    });
    $("af-bool").addEventListener("click", (e) => {
      const b = e.target.closest && e.target.closest("[data-bool]"); if(!b) return;
      st.pop.bool = b.dataset.bool === "yes"; renderPop();
    });
    $("sc-pop").addEventListener("submit", (e) => {
      e.preventDefault();
      const fl = readFilter();
      if(!validFilter(fl)){ toast("Enter a number" + (fl.op === "between" ? " in both boxes" : "") + " (or text) for this filter"); $("af-val").focus(); return; }
      st.filters.push(fl); closePop(); filtersChanged();
    });

    $("sc-scrim").addEventListener("click", closeDrawer);
    $("sc-drawer").addEventListener("click", (e) => {
      const b = e.target.closest && e.target.closest("[data-dr]"); if(!b) return;
      if(b.dataset.dr === "close") closeDrawer(); else stepDrawer(b.dataset.dr === "prev" ? -1 : 1);
    });
    $("sc-drawer").addEventListener("keydown", (e) => {
      if(e.key !== "Tab") return;   // keep focus inside the open dialog
      const f = Array.from($("sc-drawer").querySelectorAll("button:not([disabled]), a[href]"));
      if(!f.length) return;
      if(e.shiftKey && document.activeElement === f[0]){ e.preventDefault(); f[f.length - 1].focus(); }
      else if(!e.shiftKey && document.activeElement === f[f.length - 1]){ e.preventDefault(); f[0].focus(); }
    });
    document.addEventListener("keydown", (e) => {
      if(e.key !== "Escape") return;
      if(popOpen()){ e.preventDefault(); closePop(); }
      else if(drawerOpen()){ e.preventDefault(); closeDrawer(); }
    });
    document.addEventListener("visibilitychange", () => { if(!document.hidden && needsPoll(st.data)) load(false); });
    let rz = 0;
    window.addEventListener("resize", () => { clearTimeout(rz); rz = setTimeout(() => { if(!st.allCols) renderTable(); if(popOpen()) closePop(); }, 150); });
    load(true);
  }
  init();
})();
