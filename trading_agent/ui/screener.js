// Stock screener page (screener.html). Needs common.js (window.TA) and palette.js first.
//
// Read-only: the page only reads /api/screener. Row clicks go to the dashboard's Look up (/?lookup=SYMBOL); nothing
// here sends an order or posts anything. Filtering and sorting happen here, in the browser, on the rows the server
// returns. The first half of the file is pure logic (columns, filters, presets, sort, CSV) that tests/screener_harness.js
// runs under node; the second half touches the page.
(function(){
  "use strict";

  // ---- columns ----
  // type: stock | text | num | pct | bool. pct values arrive as plain percent numbers (1.5 = 1.5%). phone: shown by default
  // on a narrow screen. group "tv": from TradingView (unofficial), shown only when that setting is on and working.
  const COLS = [
    {key: "symbol", label: "Stock", type: "stock", def: true, phone: true, always: true},
    {key: "industry", label: "Sector / industry", type: "text", def: true},
    {key: "price", label: "Price (₹)", type: "num", dec: 2, def: true, phone: true},
    {key: "chg_1d", label: "Change 1D", type: "pct", def: true, phone: true, tone: true},
    {key: "ret_1w", label: "1W %", type: "pct", def: true, tone: true, tip: "One week return, dividends included"},
    {key: "ret_1m", label: "1M %", type: "pct", def: true, tone: true, tip: "One month return, dividends included"},
    {key: "ret_6m", label: "6M %", type: "pct", def: true, tone: true, tip: "Six month return, dividends included"},
    {key: "ret_1y", label: "1Y %", type: "pct", def: true, tone: true, tip: "One year return, dividends included"},
    {key: "ret_12_1", label: "12-1 momentum %", type: "pct", def: true, tone: true, tip: "Return over the past year leaving out the latest month (the factor screen's momentum)"},
    {key: "volume", label: "Volume", type: "num", dec: 0, def: true},
    {key: "rel_volume", label: "Relative volume", type: "num", dec: 2, def: true, tip: "Latest day's volume divided by the average of the 20 days before"},
    {key: "market_cap_cr", label: "Market cap (₹ cr)", type: "num", dec: 0, def: true},
    {key: "pe", label: "P/E", type: "num", dec: 1, def: true},
    {key: "div_yield", label: "Dividend yield %", type: "pct", dec: 2, def: true, plain: true},
    {key: "pct_from_high", label: "From 52-week high %", type: "pct", def: true, tone: true, tip: "How far the price is below its 52-week high"},
    {key: "above_200", label: "Above 200-day", type: "bool", def: true, tip: "Price above its 200-day average"},
    {key: "rsi", label: "RSI (14)", type: "num", dec: 1, def: true},
    {key: "atr_pct", label: "ATR %", type: "pct", dec: 2, def: true, plain: true, tip: "14-day average true range as a share of the price (close to close)"},
    {key: "band", label: "Price band", type: "text", def: true, tip: "NSE price band: the most the price may move in a day"},
    {key: "held", label: "Held", type: "bool", def: true, tip: "You own it (saved holdings and the practice account)"},
    {key: "deal", label: "Deal", type: "bool", def: true, tip: "A followed investor bought it in a disclosed deal in the last 30 days"},
    {key: "avg_turnover_cr", label: "Avg turnover (₹ cr/day)", type: "num", dec: 2, def: false, tip: "Average daily traded value over 60 days"},
    {key: "mom_score", label: "Momentum score", type: "num", dec: 2, def: false, tip: "The factor screen's composite score across this list"},
    {key: "mom_eligible", label: "Passes factor screen", type: "bool", def: false, tip: "Above its 200-day average and liquid enough, as the factor screen requires"},
    {key: "tv_summary_label", label: "Summary (TV)", type: "text", group: "tv", def: true, tip: "from TradingView, unofficial"},
    {key: "tv_sector", label: "Sector (TV)", type: "text", group: "tv", def: false, tip: "from TradingView, unofficial"},
    {key: "tv_industry", label: "Industry (TV)", type: "text", group: "tv", def: false, tip: "from TradingView, unofficial"},
    {key: "tv_eps_growth", label: "EPS growth % (TV)", type: "pct", group: "tv", def: true, tone: true, tip: "from TradingView, unofficial"},
  ];
  const colByKey = (key) => COLS.find(c => c.key === key) || null;
  const UNIVERSES = ["NIFTY50", "NIFTY100", "NIFTY200", "NIFTY500", "NIFTYMIDCAP150", "NIFTYSMALLCAP250", "HOLDINGS", "DEALS"];
  const UNIVERSE_LABEL = {HOLDINGS: "My holdings", DEALS: "Followed investors' recent buys"};
  const OPS = {num: [">", ">=", "<", "<=", "=", "between"], pct: [">", ">=", "<", "<=", "=", "between"], text: ["is", "contains"], bool: ["="], stock: ["contains"]};
  const OP_WORD = {">": ">", ">=": "≥", "<": "<", "<=": "≤", "=": "=", between: "between", is: "is", contains: "contains"};

  const defaultColumns = (narrow, tvOn) => COLS.filter(c => (c.group !== "tv" || tvOn) && (narrow ? c.phone : c.def)).map(c => c.key);

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

  // ---- polling: keep asking while stocks are still being fetched; ignore an answer for a list no longer shown ----
  const needsPoll = (data) => !!data && !data.error && (data.loading === true || (data.pending || 0) > 0);

  const logic = {COLS, colByKey, UNIVERSES, UNIVERSE_LABEL, OPS, OP_WORD, defaultColumns, num, passes, matchesQuery, applyFilters,
                 validFilter, describeFilter, sortRows, PRESETS, presetById, cleanSaved, MAX_SAVED, csvCell, toCsv, needsPoll, groupNum};
  if(typeof module !== "undefined" && module.exports) module.exports = logic;
  if(typeof window === "undefined" || typeof document === "undefined" || !window.TA) return;
  window.TA.screener = logic;

  // =========================== the page ===========================
  const {$, esc, toast, api} = window.TA;
  const store = {
    get(k){ try { return JSON.parse(localStorage.getItem(k) || "null"); } catch(e){ return null; } },
    set(k, v){ try { localStorage.setItem(k, JSON.stringify(v)); } catch(e){} },
  };
  const narrow = () => { try { return matchMedia("(max-width: 700px)").matches; } catch(e){ return false; } };
  const KEY_STATE = "ta_screener_state", KEY_SAVED = "ta_screener_presets";
  const st = {universe: "NIFTY50", filters: [], sort: {key: "market_cap_cr", dir: "desc"}, cols: null, query: "", data: null,
              seq: 0, timer: null, preset: ""};
  let saved = cleanSaved(store.get(KEY_SAVED));
  let colsTouched = false;

  function loadState(){
    const s = store.get(KEY_STATE); if(!s) return;
    if(UNIVERSES.indexOf(s.universe) >= 0) st.universe = s.universe;
    st.filters = (Array.isArray(s.filters) ? s.filters : []).filter(validFilter);
    if(s.sort && colByKey(s.sort.key) && (s.sort.dir === "asc" || s.sort.dir === "desc")) st.sort = {key: s.sort.key, dir: s.sort.dir};
    if(Array.isArray(s.cols) && s.cols.length){ st.cols = s.cols.filter(k => colByKey(k)); colsTouched = true; }
  }
  const saveState = () => store.set(KEY_STATE, {universe: st.universe, filters: st.filters, sort: st.sort, cols: colsTouched ? st.cols : null});
  const tvOn = () => !!(st.data && st.data.tv && st.data.tv.state === "on");
  const visibleCols = () => {
    let base = colsTouched && st.cols ? st.cols : defaultColumns(narrow(), tvOn());
    // untouched columns: whatever a filter or the sort uses is shown too, so a preset's own figures are always visible
    if(!(colsTouched && st.cols)) base = base.concat(st.filters.map(x => x.col), [st.sort.key]);
    return COLS.filter(c => base.indexOf(c.key) >= 0 && (c.group !== "tv" || tvOn()));
  };

  // ---- cell rendering ----
  const toneCls = (v) => v == null ? "" : v > 0 ? "pl-profit" : v < 0 ? "pl-loss" : "";
  function cell(c, r){
    const v = r[c.key];
    if(c.type === "stock"){
      const href = "/?lookup=" + encodeURIComponent(r.symbol);
      return `<td class="sc-stock"><a href="${esc(href)}" data-sym="${esc(r.symbol)}" class="sc-sym">${esc(r.symbol)}</a>${r.name ? `<div class="sub sc-name" title="${esc(r.name)}">${esc(r.name)}</div>` : ""}</td>`;
    }
    if(c.key === "held") return `<td>${v ? '<span class="pill ok">Held</span>' : '<span class="sub">–</span>'}</td>`;
    if(c.key === "deal") return `<td>${v ? `<span class="pill warn" title="${esc((r.deal_who || []).join(", "))}">Bought</span>` : '<span class="sub">–</span>'}</td>`;
    if(v === null || v === undefined || v === "") return `<td class="${c.type === "text" ? "" : "num"}"><span class="sub">n/a</span></td>`;
    if(c.type === "bool") return `<td>${v ? "Yes" : "No"}</td>`;
    if(c.type === "text") return `<td class="sc-text" title="${esc(v)}">${esc(v)}</td>`;
    if(c.type === "pct"){
      const s = (c.plain ? "" : (v > 0 ? "+" : "")) + groupNum(v, c.dec == null ? 2 : c.dec) + "%";
      return `<td class="num ${c.tone ? toneCls(v) : ""}">${esc(s)}</td>`;
    }
    return `<td class="num">${esc(groupNum(v, c.dec))}</td>`;
  }

  function currentRows(){
    if(!st.data || !st.data.rows) return [];
    return sortRows(applyFilters(st.data.rows, st.filters, st.query), st.sort.key, st.sort.dir);
  }

  function renderTable(){
    const cols = visibleCols(), rows = currentRows(), all = st.data && st.data.rows ? st.data.rows.length : 0;
    $("sc-head").innerHTML = "<tr>" + cols.map(c => {
      const on = st.sort.key === c.key, dir = on ? (st.sort.dir === "asc" ? "ascending" : "descending") : "none";
      const tip = c.tip || (c.group === "tv" ? "from TradingView, unofficial" : "");
      return `<th class="${c.type === "stock" || c.type === "text" || c.type === "bool" ? "" : "num"}${c.type === "stock" ? " sc-stock" : ""}" aria-sort="${dir}"${tip ? ` title="${esc(tip)}"` : ""}><button type="button" class="sc-sort" data-sort="${esc(c.key)}">${esc(c.label)}${on ? `<span aria-hidden="true"> ${st.sort.dir === "asc" ? "▲" : "▼"}</span>` : ""}</button></th>`;
    }).join("") + "</tr>";
    const data = st.data;
    let body;
    if(data && data.error && !all) body = `<tr><td colspan="${cols.length}" class="empty">${esc(data.error)}</td></tr>`;
    else if(!all) body = `<tr><td colspan="${cols.length}" class="empty">${data && data.loading ? "Loading the stock list…" : data ? "No stocks in this list." : "Loading…"}</td></tr>`;
    else if(!rows.length) body = `<tr><td colspan="${cols.length}" class="empty">No stock matches these filters.</td></tr>`;
    else body = rows.map(r => `<tr data-sym="${esc(r.symbol)}">${cols.map(c => cell(c, r)).join("")}</tr>`).join("");
    $("sc-rows").innerHTML = body;
    $("sc-count").textContent = data && all ? `${rows.length} of ${all} stocks match` : "";
    renderFresh();
    $("sc-export").disabled = !rows.length;
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
    if(d && (d.pending > 0 || d.loading)) bits.push(d.loading && !d.rows.length ? "Loading the stock list…" : `${d.pending} stock${d.pending === 1 ? "" : "s"} still being fetched; the table fills in as they arrive.`);
    if(d && d.error && d.rows && d.rows.length) bits.push(d.error);
    if(d && d.tv && d.tv.state === "refused_server") bits.push("TradingView columns are never used on the server.");
    if(d && d.tv && d.tv.state === "backoff") bits.push("TradingView columns paused until " + new Date(d.tv.until).toLocaleString("en-IN", {day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hour12: false}) + " (TradingView said no or did not answer).");
    $("sc-status").textContent = bits.join(" ");
  }

  // ---- chips ----
  function renderChips(){
    $("sc-chips").innerHTML = st.filters.length ? st.filters.map((fl, i) =>
      `<span class="sc-chip"><span>${esc(describeFilter(fl))}</span><button type="button" class="small" data-rm="${i}" aria-label="Remove filter: ${esc(describeFilter(fl))}">×</button></span>`).join("")
      + `<button type="button" class="small" id="sc-clear">Clear filters</button>` : `<span class="sub">No filters. Add one, or pick a preset.</span>`;
  }

  // ---- add-filter form ----
  function renderFilterForm(){
    const colSel = $("af-col");
    if(!colSel.options.length){
      colSel.innerHTML = COLS.filter(c => c.type !== "stock").map(c => `<option value="${esc(c.key)}">${esc(c.label)}</option>`).join("");
      colSel.value = "market_cap_cr";
    }
    const col = colByKey(colSel.value);
    const opSel = $("af-op"), prev = opSel.value;
    opSel.innerHTML = OPS[col.type].map(o => `<option value="${esc(o)}">${esc(OP_WORD[o])}</option>`).join("");
    if(OPS[col.type].indexOf(prev) >= 0) opSel.value = prev;
    const op = opSel.value;
    $("af-val-wrap").hidden = col.type === "bool";
    $("af-bool-wrap").hidden = col.type !== "bool";
    $("af-val2-wrap").hidden = op !== "between";
    $("af-val").setAttribute("inputmode", col.type === "text" ? "text" : "decimal");
    $("af-val").setAttribute("list", col.key === "industry" ? "af-sectors" : "");
    $("af-val").placeholder = col.type === "text" ? (col.key === "industry" ? "e.g. Information Technology" : "text") : (col.type === "pct" ? "number, in %" : "number");
    if(col.key === "industry" && st.data && st.data.rows){
      const set = Array.from(new Set(st.data.rows.map(r => r.industry).filter(Boolean))).sort();
      $("af-sectors").innerHTML = set.map(s => `<option value="${esc(s)}">`).join("");
    }
  }
  function readFilter(){
    const col = colByKey($("af-col").value), op = $("af-op").value;
    const fl = {col: col.key, op};
    if(col.type === "bool") fl.value = $("af-bool").value === "yes";
    else { fl.value = $("af-val").value.trim(); if(op === "between") fl.value2 = $("af-val2").value.trim(); }
    return fl;
  }

  // ---- presets ----
  function renderPresets(){
    const opts = [`<option value="">Presets…</option>`].concat(PRESETS.map(p => `<option value="b:${esc(p.id)}">${esc(p.label)}</option>`));
    if(saved.length) opts.push(`<optgroup label="Mine">` + saved.map((p, i) => `<option value="s:${i}">${esc(p.name)}</option>`).join("") + `</optgroup>`);
    $("sc-preset").innerHTML = opts.join("");
    $("sc-preset").value = st.preset;
    $("sc-delpreset").hidden = !/^s:/.test(st.preset);
    const sel = PRESETS.find(p => "b:" + p.id === st.preset);
    $("sc-presetnote").textContent = sel ? sel.note : "";
  }
  function applyPreset(value){
    st.preset = value;
    let p = null;
    if(/^b:/.test(value)) p = presetById(value.slice(2));
    else if(/^s:/.test(value)) p = saved[Number(value.slice(2))] || null;
    if(!p){ renderPresets(); return; }
    st.filters = (p.filters || []).map(x => ({col: x.col, op: x.op, value: x.value, value2: x.value2}));
    if(p.sort) st.sort = {key: p.sort.key, dir: p.sort.dir};
    if(p.cols && p.cols.length){ st.cols = p.cols.slice(); colsTouched = true; }
    const u = p.universe && UNIVERSES.indexOf(p.universe) >= 0 ? p.universe : null;
    renderPresets(); renderChips(); saveState();
    if(u && u !== st.universe){ st.universe = u; $("sc-universe").value = u; load(true); }
    else renderTable();
  }

  // ---- column chooser ----
  function renderColumnChooser(){
    const on = visibleCols().map(c => c.key);
    $("sc-cols").innerHTML = COLS.filter(c => c.group !== "tv" || tvOn()).map(c =>
      `<label class="sc-colopt"><input type="checkbox" data-col="${esc(c.key)}" ${on.indexOf(c.key) >= 0 ? "checked" : ""} ${c.always ? "disabled" : ""}> ${esc(c.label)}</label>`).join("");
  }

  // ---- data: one request now, then poll while stocks are still pending ----
  async function load(reset){
    const my = ++st.seq;
    clearTimeout(st.timer);
    if(reset){ st.data = null; renderTable(); renderChips(); }
    let data = null;
    try { data = await api("/api/screener?universe=" + encodeURIComponent(st.universe)); }
    catch(e){
      if(my !== st.seq) return;
      if(!st.data) { st.data = {rows: [], error: "Couldn't load the screener: " + e.message, pending: 0, loading: false, tv: {state: "off"}}; renderTable(); }
      else toast("Couldn't refresh the screener: " + e.message);
      st.timer = setTimeout(() => { if(my === st.seq && !document.hidden) load(false); }, 10000);
      return;
    }
    if(my !== st.seq) return;   // the universe changed while this was in flight
    st.data = data;
    renderTable(); renderChips(); renderColumnChooser(); renderFilterForm();
    if(needsPoll(data)) st.timer = setTimeout(() => { if(my === st.seq) load(false); }, document.hidden ? 15000 : 3000);
  }

  function download(name, text){
    const url = URL.createObjectURL(new Blob([text], {type: "text/csv;charset=utf-8"}));
    const a = document.createElement("a"); a.href = url; a.download = name; document.body.appendChild(a); a.click();
    setTimeout(() => { URL.revokeObjectURL(url); a.remove(); }, 0);
  }

  function init(){
    loadState();
    $("sc-universe").innerHTML = UNIVERSES.map(u => `<option value="${esc(u)}">${esc(UNIVERSE_LABEL[u] || u)}</option>`).join("");
    $("sc-universe").value = st.universe;
    renderPresets(); renderChips(); renderTable(); renderColumnChooser(); renderFilterForm();

    $("sc-universe").addEventListener("change", (e) => { st.universe = e.target.value; st.preset = ""; renderPresets(); saveState(); load(true); });
    $("sc-preset").addEventListener("change", (e) => applyPreset(e.target.value));
    $("sc-q").addEventListener("input", (e) => { st.query = e.target.value; renderTable(); });
    $("sc-head").addEventListener("click", (e) => {
      const b = e.target.closest && e.target.closest("[data-sort]"); if(!b) return;
      const key = b.dataset.sort;
      st.sort = st.sort.key === key ? {key, dir: st.sort.dir === "asc" ? "desc" : "asc"} : {key, dir: (colByKey(key).type === "text" || colByKey(key).type === "stock") ? "asc" : "desc"};
      saveState(); renderTable();
    });
    $("sc-rows").addEventListener("click", (e) => {
      if(e.target.closest && e.target.closest("a")) return;   // the link itself opens Look up
      const tr = e.target.closest && e.target.closest("tr[data-sym]"); if(!tr) return;
      location.assign(TA.palette.landingUrl("lookup", tr.dataset.sym));
    });
    $("sc-chips").addEventListener("click", (e) => {
      const rm = e.target.closest && e.target.closest("[data-rm]");
      if(rm){ st.filters.splice(Number(rm.dataset.rm), 1); st.preset = ""; renderPresets(); renderChips(); saveState(); renderTable(); return; }
      if(e.target.id === "sc-clear"){ st.filters = []; st.preset = ""; renderPresets(); renderChips(); saveState(); renderTable(); }
    });
    $("af-col").addEventListener("change", renderFilterForm);
    $("af-op").addEventListener("change", renderFilterForm);
    $("af-form").addEventListener("submit", (e) => {
      e.preventDefault();
      const fl = readFilter();
      if(!validFilter(fl)){ toast("Enter a number" + (fl.op === "between" ? " in both boxes" : "") + " (or text) for this filter"); return; }
      st.filters.push(fl); st.preset = ""; $("af-val").value = ""; $("af-val2").value = "";
      renderPresets(); renderChips(); saveState(); renderTable();
    });
    $("sc-cols").addEventListener("change", (e) => {
      const cb = e.target.closest && e.target.closest("[data-col]"); if(!cb) return;
      const now = visibleCols().map(c => c.key), key = cb.dataset.col;
      st.cols = cb.checked ? (now.indexOf(key) >= 0 ? now : now.concat([key])) : now.filter(k => k !== key);
      colsTouched = true; saveState(); renderTable();
    });
    $("sc-colreset").addEventListener("click", () => { colsTouched = false; st.cols = null; saveState(); renderColumnChooser(); renderTable(); });
    $("sc-export").addEventListener("click", () => {
      const rows = currentRows(); if(!rows.length) return;
      download(`screener-${st.universe.toLowerCase()}-${new Date().toISOString().slice(0, 10)}.csv`, toCsv(rows, visibleCols().map(c => c.key)));
    });
    $("sc-save").addEventListener("click", () => {
      const name = ($("sc-savename").value || "").trim();
      if(!name){ toast("Give the preset a name first"); $("sc-savename").focus(); return; }
      const entry = cleanSaved([{name, universe: st.universe, filters: st.filters, sort: st.sort, cols: visibleCols().map(c => c.key)}])[0];
      const i = saved.findIndex(p => p.name.toLowerCase() === entry.name.toLowerCase());
      if(i >= 0) saved[i] = entry; else if(saved.length >= MAX_SAVED){ toast("You can keep up to " + MAX_SAVED + " presets; delete one first"); return; } else saved.push(entry);
      store.set(KEY_SAVED, saved);
      st.preset = "s:" + saved.findIndex(p => p.name === entry.name); $("sc-savename").value = "";
      renderPresets(); toast("Preset saved");
    });
    $("sc-delpreset").addEventListener("click", () => {
      const m = /^s:(\d+)$/.exec(st.preset); if(!m) return;
      saved.splice(Number(m[1]), 1); store.set(KEY_SAVED, saved); st.preset = ""; renderPresets(); toast("Preset deleted");
    });
    $("sc-reset").addEventListener("click", () => { st.filters = []; st.query = ""; $("sc-q").value = ""; st.preset = ""; st.sort = {key: "market_cap_cr", dir: "desc"}; colsTouched = false; st.cols = null; renderPresets(); renderChips(); renderColumnChooser(); saveState(); renderTable(); });
    document.addEventListener("visibilitychange", () => { if(!document.hidden && needsPoll(st.data)) load(false); });
    window.addEventListener("resize", () => { if(!colsTouched){ renderTable(); renderColumnChooser(); } });
    load(true);
  }
  init();
})();
