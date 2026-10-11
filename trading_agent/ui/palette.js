// Ctrl+K (Cmd+K) command palette, shared by the Live, Demo, Replay and Screener pages. Needs common.js (window.TA) first.
//
// Safety: the palette never sends an order and has no way to ask for a live (Groww) one. "Practice buy" only
// opens the practice account's own order form pre-filled (on the Demo page) or takes you to the Demo page with
// the ticker in the address (?buy=SYMBOL); you still press "Place paper order" there. Nothing in this file makes
// a POST, reads GROWW_LIVE_ORDERS, or names an order endpoint; tests/test_palette.py checks that.
//
// The file has three layers so the logic can be tested without a browser: pure helpers (symbols, ranking, routing),
// a state machine (createModel) and a debounced searcher (createSearcher); install() is the only part that
// touches the DOM.
(function(){
  "use strict";
  const SYMBOL = /^[A-Z0-9][A-Z0-9&._-]{0,19}$/;
  const cleanSymbol = (s) => { const t = String(s == null ? "" : s).trim().toUpperCase(); return SYMBOL.test(t) ? t : null; };
  const STOCK_OPS = ["lookup", "size", "chart", "buy"];   // what the menu offers
  const LANDING_OPS = STOCK_OPS.concat(["sell"]);          // what a page can be opened with; Practice sell is offered by the stock page, not the menu
  const OP_LABEL = {lookup: "Look up", size: "Size", chart: "Chart", buy: "Practice buy"};
  const OP_HINT = {lookup: "momentum, price and announcements", size: "how many shares to buy",
                   chart: "Look up, scrolled to the price chart", buy: "practice account only; opens the order form, you confirm there"};
  const PAGES = {live: ["Live", "/"], replay: ["Replay", "/replay"], demo: ["Demo", "/demo"], screener: ["Screener", "/screener"]};

  // Where the destination page expects each stock action to arrive: /?lookup=SYM, /?size=SYM, /?chart=SYM, /demo?buy=SYM.
  const landingUrl = (op, sym) => (op === "buy" || op === "sell" ? "/demo" : "/") + "?" + op + "=" + encodeURIComponent(sym);
  // The page that receives such an address reads it back with this; anything unknown or malformed is ignored.
  function landing(search){
    const out = [];
    try {
      const p = new URLSearchParams(String(search || ""));
      for(const op of LANDING_OPS){ const s = cleanSymbol(p.get(op)); if(s) out.push({op, symbol: s}); }
    } catch(e){}
    return out[0] || null;
  }
  // Practice buy has exactly two outcomes and neither reads any live-orders setting: pre-fill the practice form on
  // this page (Demo), or go to the Demo page with the ticker.
  // Practice sell is the same: it fills the practice order form on the sell side, or opens /demo?sell=SYM.
  function practiceOrderTarget(op, sym, mode, hostCanPrefill){
    const s = cleanSymbol(sym);
    if(!s || (op !== "buy" && op !== "sell")) return null;
    if(mode === "demo" && hostCanPrefill) return {via: "host", op, symbol: s};
    return {via: "navigate", op, symbol: s, url: landingUrl(op, s)};
  }
  const practiceBuyTarget = (sym, mode, hostCanPrefill) => practiceOrderTarget("buy", sym, mode, hostCanPrefill);
  // How a stock action is carried out: by this page's own handler when it has one, else by opening the page that has it.
  function routeStockOp(op, sym, mode, host){
    const s = cleanSymbol(sym);
    if(!s || LANDING_OPS.indexOf(op) < 0) return null;
    if(op === "buy" || op === "sell") return practiceOrderTarget(op, s, mode, !!(host && typeof host[op] === "function"));
    if(host && typeof host[op] === "function") return {via: "host", op, symbol: s};
    return {via: "navigate", op, symbol: s, url: landingUrl(op, s)};
  }

  // ---- commands ----
  // opts: {mode, workspaces: [{id, label}], sections: [{id, label}], holdings: [{symbol, name}], refresh: bool}.
  // Each command carries a plain descriptor, not a function.
  function buildCommands(opts){
    opts = opts || {};
    const out = [];
    for(const k of Object.keys(PAGES)){
      if(k === opts.mode) continue;
      out.push({id: "page-" + k, group: "Go to", label: PAGES[k][0] + " page", keywords: "page tab open " + k + (k === "screener" ? " stocks filter sort table scan" : ""), run: {kind: "page", url: PAGES[k][1]}});
    }
    for(const w of (opts.workspaces || [])){   // the page's tabs (workspaces): Overview, Deals & orders, Research, Tools
      out.push({id: "ws-" + w.id, group: "Go to", label: "Go to " + w.label, keywords: "tab workspace open switch " + w.id, run: {kind: "workspace", id: w.id}});
    }
    for(const s of (opts.sections || [])){
      out.push({id: "sec-" + s.id, group: "Go to", label: s.label, keywords: "section card jump scroll " + s.id, run: {kind: "section", id: s.id}});
    }
    for(const c of ["dark", "light", "auto"]){
      out.push({id: "theme-" + c, group: "Theme", label: "Theme: " + c[0].toUpperCase() + c.slice(1), keywords: "colour color appearance mode switch " + c, run: {kind: "theme", choice: c}});
    }
    const seenH = {};
    for(const h of (opts.holdings || [])){   // "Open stock" for each holding: the read-only stock view, never an order
      const s = cleanSymbol(h && h.symbol);
      if(!s || seenH[s]) continue;
      seenH[s] = 1;
      out.push({id: "hold-" + s, group: "Your holdings", label: "Open stock " + s, keywords: "holding stock view look up " + String((h && h.name) || ""), run: {kind: "stock", op: "lookup", symbol: s}});
    }
    if(opts.refresh) out.push({id: "refresh", group: "Data", label: "Refresh data", keywords: "reload update fetch latest", run: {kind: "refresh"}});
    return out;
  }
  // Score one command against the query: 0 = label (or a word of it) starts with every token, 1 = every token appears
  // in the label or keywords, null = no match. An empty query matches everything with score 0.
  function scoreCommand(cmd, query){
    const toks = String(query || "").toLowerCase().split(/\s+/).filter(Boolean);
    if(!toks.length) return 0;
    const label = cmd.label.toLowerCase(), words = label.split(/[^a-z0-9]+/).filter(Boolean), hay = label + " " + String(cmd.keywords || "").toLowerCase();
    let best = 0;
    for(const t of toks){
      if(hay.indexOf(t) < 0) return null;
      if(!(label.indexOf(t) === 0 || words.some(w => w.indexOf(t) === 0))) best = 1;
    }
    return best;
  }
  function filterCommands(cmds, query){
    const out = [];
    cmds.forEach((c, i) => { const s = scoreCommand(c, query); if(s !== null) out.push({c, s, i}); });
    out.sort((a, b) => a.s - b.s || a.i - b.i);
    return out.map(x => ({cmd: x.c, score: x.s}));
  }

  // ---- state machine ----
  // Items are {id, type: "stock"|"cmd"|"op"|"note", label, detail, group, run?, symbol?, op?}. "note" rows cannot be chosen.
  function createModel(opts){
    opts = opts || {};
    const commands = opts.commands || [];
    const st = {query: "", stage: null, hits: null, searching: false, error: false, active: 0};
    const stockItems = (withTyped) => {
      const rows = [], seen = {};
      for(const h of (st.hits || [])){
        const s = cleanSymbol(h && h.symbol);
        if(!s || seen[s]) continue;
        seen[s] = 1;
        rows.push({type: "stock", symbol: s, name: String(h.name || ""), label: s, detail: String(h.name || ""), group: "Stock"});
      }
      const typed = cleanSymbol(st.query);   // offline or unlisted: let a typed ticker through, the look-up validates it
      if(withTyped && typed && !seen[typed] && st.query.trim().length >= 2 && !/\s/.test(st.query.trim()))
        rows.push({type: "stock", symbol: typed, name: "", label: typed, detail: "use as typed", group: "Stock"});
      return rows;
    };
    function items(){
      let rows;
      if(st.stage){
        const q = st.query.trim().toLowerCase();
        rows = STOCK_OPS.filter(op => !q || OP_LABEL[op].toLowerCase().indexOf(q) >= 0)
          .map(op => ({type: "op", op, symbol: st.stage.symbol, label: OP_LABEL[op], detail: OP_HINT[op], group: st.stage.symbol}));
      } else {
        const m = filterCommands(commands, st.query), cmdRow = (x) => ({type: "cmd", id: x.cmd.id, label: x.cmd.label, detail: "", group: x.cmd.group, run: x.cmd.run});
        const strong = m.filter(x => x.score === 0).map(cmdRow), weak = m.filter(x => x.score !== 0).map(cmdRow);
        const q = st.query.trim();
        // a typed ticker is offered only when no command answers the same word ("size" is the Position size command)
        const stocks = q.length >= 2 ? stockItems(!strong.length) : [];
        rows = strong.concat(stocks, weak);
        if(q.length >= 2 && st.searching && !stocks.length) rows.push({type: "note", label: "Searching stocks…", detail: "", group: ""});
        else if(q.length >= 2 && st.error) rows.push({type: "note", label: "Stock search is unavailable right now", detail: "", group: ""});
        else if(q.length >= 2 && !rows.length) rows.push({type: "note", label: "No match", detail: "", group: ""});
        else if(q.length < 2 && !rows.length) rows.push({type: "note", label: "No match", detail: "", group: ""});
      }
      rows.forEach((r, i) => { r.index = i; });
      return rows;
    }
    const firstChoosable = (rows) => { const i = rows.findIndex(r => r.type !== "note"); return i < 0 ? -1 : i; };
    function clamp(){
      const rows = items(), ok = rows.filter(r => r.type !== "note").length;
      if(!ok){ st.active = -1; return; }
      if(st.active < 0 || st.active >= rows.length || rows[st.active].type === "note") st.active = firstChoosable(rows);
    }
    return {
      state: st, items,
      setQuery(q){ st.query = String(q == null ? "" : q); st.active = 0; if(st.query.trim().length < 2){ st.hits = null; st.error = false; st.searching = false; } clamp(); },
      setSearching(on){ st.searching = !!on; if(on) st.error = false; clamp(); },
      // Results are kept only if they answer what is in the box now (a late answer to an older query is dropped).
      setHits(forQuery, hits, error){
        if(String(forQuery).trim() !== st.query.trim()) return false;
        st.searching = false; st.error = !!error; st.hits = error ? null : (hits || []);
        clamp(); return true;
      },
      move(d){
        const rows = items(), n = rows.length; if(!n || firstChoosable(rows) < 0){ st.active = -1; return; }
        let i = st.active;
        for(let k = 0; k < n; k++){ i = (i + d + n) % n; if(rows[i].type !== "note") break; }
        st.active = i;
      },
      moveTo(i){ const rows = items(); if(rows[i] && rows[i].type !== "note") st.active = i; },
      home(){ st.active = firstChoosable(items()); },
      end(){ const rows = items(); let i = rows.length - 1; while(i >= 0 && rows[i].type === "note") i--; st.active = i; },
      // Choosing a stock moves to its action list (returns null); choosing a command or action returns what to do.
      activate(index){
        const rows = items(), r = rows[index == null ? st.active : index];
        if(!r || r.type === "note") return null;
        if(r.type === "stock"){ st.stage = {symbol: r.symbol, name: r.name}; st.query = ""; st.hits = null; st.error = false; st.searching = false; st.active = 0; clamp(); return null; }
        if(r.type === "op") return {kind: "stock", op: r.op, symbol: r.symbol};
        return r.run;
      },
      back(){ if(!st.stage) return false; st.stage = null; st.query = ""; st.active = 0; clamp(); return true; },
      reset(){ st.query = ""; st.stage = null; st.hits = null; st.searching = false; st.error = false; st.active = 0; clamp(); },
    };
  }

  // ---- debounced search with stale answers dropped ----
  // fetchJson(url) -> Promise of an array. onResult(query, hits, error) is called only for the newest query.
  function createSearcher(o){
    const delay = o.delay == null ? 150 : o.delay, st = o.setTimeout || setTimeout, ct = o.clearTimeout || clearTimeout;
    let timer = null, seq = 0;
    return {
      query(q){
        const my = ++seq, text = String(q == null ? "" : q).trim();
        if(timer !== null){ ct(timer); timer = null; }
        if(text.length < 2){ if(o.onIdle) o.onIdle(text); return; }
        if(o.onPending) o.onPending(text);
        timer = st(async () => {
          timer = null;
          let hits = null, error = false;
          try { hits = await o.fetchJson("/api/search?q=" + encodeURIComponent(text)); if(!Array.isArray(hits)) hits = []; }
          catch(e){ error = true; }
          if(my !== seq) return;   // the box changed while this was in flight
          o.onResult(text, hits, error);
        }, delay);
      },
      cancel(){ seq++; if(timer !== null){ ct(timer); timer = null; } },
    };
  }

  // ---- DOM: only install() below touches the page ----
  const isMac = () => { try { return /Mac|iPhone|iPad/i.test((navigator.userAgentData && navigator.userAgentData.platform) || navigator.platform || ""); } catch(e){ return false; } };

  // host: {mode, workspaces(): [{id, label}], goWorkspace(id), holdings(): [{symbol, name}], sections(): [{id, label}], lookup(sym), size(sym), chart(sym), buy(sym), refresh(), goSection(id)}
  // Every key is optional; a missing stock handler sends you to the page that has one.
  function install(host){
    host = host || {};
    const TA = window.TA, esc = TA.esc, mode = host.mode || "live";
    let dlg = null, input, list, status, titleEl, model, searcher, opener = null, built = false;

    function commands(){
      return buildCommands({mode, workspaces: typeof host.workspaces === "function" ? host.workspaces() : [],
        sections: typeof host.sections === "function" ? host.sections() : [],
        holdings: typeof host.holdings === "function" ? host.holdings() : [], refresh: typeof host.refresh === "function"});
    }
    function build(){
      if(built) return;
      built = true;
      dlg = document.createElement("dialog");
      dlg.className = "pal"; dlg.id = "palette"; dlg.setAttribute("role", "dialog"); dlg.setAttribute("aria-modal", "true");
      dlg.setAttribute("aria-labelledby", "pal-title");
      dlg.innerHTML = `<div class="pal-head"><h2 id="pal-title" class="pal-title">Search stocks and commands</h2>
        <button type="button" class="small" id="pal-close" aria-label="Close search">Esc</button></div>
        <div class="pal-field"><input id="pal-input" type="text" role="combobox" aria-autocomplete="list" aria-expanded="true" aria-controls="pal-list" autocomplete="off" spellcheck="false" autocapitalize="off" placeholder="Type a company or ticker, or a command" aria-label="Search stocks and commands"></div>
        <div id="pal-list" class="pal-list" role="listbox" aria-label="Results"></div>
        <div class="pal-foot sub" aria-hidden="true">&uarr; &darr; move &middot; Enter choose &middot; Esc close &middot; practice buy only ever opens a form you confirm</div>
        <div id="pal-status" class="sr-only" role="status" aria-live="polite"></div>`;
      document.body.appendChild(dlg);
      input = dlg.querySelector("#pal-input"); list = dlg.querySelector("#pal-list"); status = dlg.querySelector("#pal-status"); titleEl = dlg.querySelector("#pal-title");
      searcher = createSearcher({
        fetchJson: (u) => TA.api(u),
        onIdle: () => { model.setSearching(false); paint(); },
        onPending: () => { model.setSearching(true); paint(); },
        onResult: (q, hits, error) => { if(model.setHits(q, hits, error)) paint(); },
      });
      input.addEventListener("input", () => { model.setQuery(input.value); paint(); searcher.query(model.state.stage ? "" : input.value); });
      input.addEventListener("keydown", onKey);
      dlg.addEventListener("keydown", onTab);
      dlg.querySelector("#pal-close").addEventListener("click", () => close());
      list.addEventListener("mousemove", (e) => { const o = e.target.closest && e.target.closest("[data-i]"); if(o && Number(o.dataset.i) !== model.state.active){ model.moveTo(Number(o.dataset.i)); markActive(false); } });
      list.addEventListener("mousedown", (e) => { e.preventDefault(); });   // a click must not pull focus out of the box
      list.addEventListener("click", (e) => { const o = e.target.closest && e.target.closest("[data-i]"); if(o) choose(Number(o.dataset.i)); });
      dlg.addEventListener("click", (e) => { if(e.target === dlg) close(); });   // the backdrop
      dlg.addEventListener("close", onClosed);
      dlg.addEventListener("cancel", () => { /* Esc: the browser closes it; onClosed returns focus */ });
    }
    function paint(){
      const rows = model.items(), st = model.state;
      titleEl.textContent = st.stage ? `${st.stage.symbol}: choose an action` : "Search stocks and commands";
      input.placeholder = st.stage ? `Action for ${st.stage.symbol}${st.stage.name ? " (" + st.stage.name + ")" : ""}` : "Type a company or ticker, or a command";
      let lastGroup = null, html = "";
      rows.forEach((r) => {
        if(r.group && r.group !== lastGroup){ html += `<div class="pal-group" role="presentation">${esc(r.group)}</div>`; lastGroup = r.group; }
        if(r.type === "note"){ html += `<div class="pal-note" role="presentation">${esc(r.label)}</div>`; return; }
        const on = r.index === st.active;
        html += `<div class="pal-opt" role="option" id="pal-o-${r.index}" data-i="${r.index}" aria-selected="${on}"><span class="pal-label">${esc(r.label)}</span>${r.detail ? `<span class="pal-detail">${esc(r.detail)}</span>` : ""}</div>`;
      });
      list.innerHTML = html;
      markActive(true);
      const n = rows.filter(r => r.type !== "note").length;
      status.textContent = st.searching ? "Searching" : `${n} result${n === 1 ? "" : "s"}`;
    }
    function markActive(scroll){
      const st = model.state, id = st.active >= 0 ? "pal-o-" + st.active : "";
      list.querySelectorAll("[role=option]").forEach(o => o.setAttribute("aria-selected", o.id === id ? "true" : "false"));
      if(id){ input.setAttribute("aria-activedescendant", id); list.setAttribute("aria-activedescendant", id); const el = list.querySelector("#" + id); if(el && scroll && el.scrollIntoView) el.scrollIntoView({block: "nearest"}); }
      else { input.removeAttribute("aria-activedescendant"); list.removeAttribute("aria-activedescendant"); }
    }
    function onKey(e){
      const k = e.key;
      if(k === "ArrowDown"){ e.preventDefault(); model.move(1); markActive(true); }
      else if(k === "ArrowUp"){ e.preventDefault(); model.move(-1); markActive(true); }
      else if(k === "Home" && !input.value){ e.preventDefault(); model.home(); markActive(true); }
      else if(k === "End" && !input.value){ e.preventDefault(); model.end(); markActive(true); }
      else if(k === "Enter"){ e.preventDefault(); if(!e.isComposing) choose(); }
      else if(k === "Backspace" && !input.value && model.state.stage){ e.preventDefault(); model.back(); searcher.cancel(); paint(); }
    }
    function onTab(e){   // keep Tab inside the dialog: the box and the close button
      if(e.key !== "Tab") return;
      const f = Array.from(dlg.querySelectorAll("input, button")).filter(x => !x.disabled);
      if(!f.length) return;
      const first = f[0], last = f[f.length - 1], a = document.activeElement;
      if(e.shiftKey && (a === first || !dlg.contains(a))){ e.preventDefault(); last.focus(); }
      else if(!e.shiftKey && (a === last || !dlg.contains(a))){ e.preventDefault(); first.focus(); }
    }
    function choose(i){
      const run = model.activate(i);
      if(run === null){ input.value = ""; searcher.cancel(); paint(); input.focus(); return; }   // a stock: now pick its action
      close();
      setTimeout(() => perform(run), 0);   // after focus has gone back, so a handler may move focus on
    }
    function perform(run){
      try {
        if(run.kind === "page"){ location.assign(run.url); }
        else if(run.kind === "workspace"){ if(typeof host.goWorkspace === "function") host.goWorkspace(run.id); }
        else if(run.kind === "section"){ if(typeof host.goSection === "function") host.goSection(run.id); }
        else if(run.kind === "theme"){ TA.theme.set(run.choice); TA.toast("Theme: " + run.choice); }
        else if(run.kind === "refresh"){ if(typeof host.refresh === "function") Promise.resolve(host.refresh()).then(() => TA.toast("Refreshed"), (e) => TA.toast(e && e.message ? e.message : "Refresh failed")); }
        else if(run.kind === "stock"){
          const t = routeStockOp(run.op, run.symbol, mode, host);
          if(!t) return;
          if(t.via === "host") host[t.op](t.symbol);
          else location.assign(t.url);
        }
      } catch(e){ TA.toast(e && e.message ? e.message : "That did not work"); }
    }
    function open(){
      build();
      if(dlg.open) { input.focus(); return; }
      opener = document.activeElement && document.activeElement !== document.body ? document.activeElement : document.getElementById("btn-palette");
      model = createModel({commands: commands()});
      input.value = ""; searcher.cancel(); paint();
      if(typeof dlg.showModal === "function") dlg.showModal(); else dlg.setAttribute("open", "");
      input.focus();
    }
    function close(){
      if(!dlg || !dlg.open) return;
      searcher.cancel();
      if(typeof dlg.close === "function") dlg.close(); else { dlg.removeAttribute("open"); onClosed(); }
    }
    function onClosed(){
      const o = opener; opener = null;
      if(o && o.isConnected !== false && typeof o.focus === "function"){ try { o.focus(); } catch(e){} }
    }
    // model must exist before the first paint, so open() builds it; guard the public handle meanwhile.
    model = createModel({commands: []});

    document.addEventListener("keydown", (e) => {
      if((e.ctrlKey || e.metaKey) && !e.altKey && !e.shiftKey && String(e.key).toLowerCase() === "k"){
        e.preventDefault();
        if(dlg && dlg.open) close(); else open();
      }
    });
    const btn = document.getElementById("btn-palette");
    if(btn){
      btn.addEventListener("click", () => open());
      const kbd = btn.querySelector(".pal-kbd"); if(kbd) kbd.textContent = isMac() ? "⌘K" : "Ctrl K";
    }
    return {open, close, isOpen: () => !!(dlg && dlg.open)};
  }

  const palette = {install, createModel, createSearcher, buildCommands, filterCommands, scoreCommand, cleanSymbol,
                   landing, landingUrl, routeStockOp, practiceBuyTarget, practiceOrderTarget, STOCK_OPS, OP_LABEL};
  if(typeof window !== "undefined" && window.TA) window.TA.palette = palette;
  if(typeof module !== "undefined" && module.exports) module.exports = palette;
})();
