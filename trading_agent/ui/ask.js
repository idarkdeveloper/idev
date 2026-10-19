// Ask: a typed or spoken sentence -> one of a few READ-ONLY actions (open a stock, chart, size tool, a tab, the portfolio, data
// health) or "read today's news" aloud. Used by the Ctrl+K palette (palette.js) on every page. Needs common.js (window.TA).
//
// Safety: the only request this file makes besides reading the news is the sentence itself, to /api/ask, whose allowlist has no
// order intent. Nothing here can buy or sell or open an order form: stock actions go through the palette's own routing
// (look up, chart, size). The browser's speech recognition (Chrome sends the audio to its own speech service) and speech
// synthesis are used only when the browser has them.
(function(){
  "use strict";
  const TA = window.TA;
  if(!TA) return;
  const esc = TA.esc;
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  const hasTTS = () => !!(window.speechSynthesis && window.SpeechSynthesisUtterance);
  const WS = {research: "research", health: "health", tools: "tools", deals: "deals", "paper accounts": "paper"};
  const PAGES = {live: "/", replay: "/replay", demo: "/demo", screener: "/screener"};

  // ---- the result card ----
  let card = null, body = null, ctl = null, hideT = 0, reading = false;
  let gen = 0, queue = [], qi = 0, paused = false;   // reading aloud: one utterance per item (a long text is cut off by Chrome)
  function ensureCard(){
    if(card) return;
    card = document.createElement("section");
    card.id = "ask-card"; card.className = "ask-card"; card.hidden = true;
    card.setAttribute("role", "region"); card.setAttribute("aria-label", "Assistant"); card.setAttribute("aria-live", "polite");
    card.innerHTML = `<div class="ask-head"><b class="ask-title">Assistant</b><button type="button" class="small ask-x" aria-label="Close">×</button></div>
      <div class="ask-body"></div><div class="ask-ctl" hidden><button type="button" class="small" data-ask="pause">Pause</button><button type="button" class="small" data-ask="stop">Stop</button></div>`;
    document.body.appendChild(card);
    body = card.querySelector(".ask-body"); ctl = card.querySelector(".ask-ctl");
    card.querySelector(".ask-x").addEventListener("click", () => { stop(); hide(); });
    ctl.addEventListener("click", (e) => {
      const b = e.target.closest && e.target.closest("[data-ask]"); if(!b) return;
      if(b.dataset.ask === "stop"){ stop(); }
      else if(b.dataset.ask === "pause"){
        // speechSynthesis.pause() is unreliable on Android, so Pause stops after the item being read and Resume goes on with the next one
        if(paused){ paused = false; b.textContent = "Pause"; setState("Reading…"); if(!window.speechSynthesis.speaking) next(gen); }
        else { paused = true; b.textContent = "Resume"; setState("Paused after this item"); }
      }
    });
    card.addEventListener("keydown", (e) => { if(e.key === "Escape"){ stop(); hide(); } });
  }
  const hide = () => { clearTimeout(hideT); if(card) card.hidden = true; };
  function show(title, html, opts){
    ensureCard(); clearTimeout(hideT);
    card.querySelector(".ask-title").textContent = title;
    body.innerHTML = html; card.hidden = false;
    ctl.hidden = !(opts && opts.reading);
    if(opts && opts.autoHide) hideT = setTimeout(hide, opts.autoHide);
  }
  function setState(text){ const s = body && body.querySelector(".ask-state"); if(s) s.textContent = text; }

  // ---- reading aloud ----
  function stop(){
    gen++; queue = []; paused = false;
    if(hasTTS()){ try { window.speechSynthesis.cancel(); } catch(e){} }
    reading = false; if(ctl) ctl.hidden = true;
    const pb = ctl && ctl.querySelector('[data-ask="pause"]'); if(pb) pb.textContent = "Pause";
    const s = body && body.querySelector(".ask-state"); if(s) s.textContent = "";
  }
  function pickVoice(){
    try { const v = window.speechSynthesis.getVoices() || []; return v.find(x => /^en[-_]IN$/i.test(x.lang)) || v.find(x => /^en/i.test(x.lang)) || null; } catch(e){ return null; }
  }
  function finished(){ reading = false; paused = false; if(ctl) ctl.hidden = true; setState(""); }
  function next(g){
    if(g !== gen || paused) return;
    if(qi >= queue.length){ finished(); return; }
    const u = new window.SpeechSynthesisUtterance(String(queue[qi]));
    u.lang = "en-IN"; u.rate = 1.0;
    const v = pickVoice(); if(v){ u.voice = v; u.lang = v.lang; }
    let done = false;
    const after = (failed) => { if(done || g !== gen) return; done = true; if(failed){ finished(); return; } qi++; next(g); };
    u.onend = () => after(false);
    u.onerror = (e) => after(!(e && (e.error === "canceled" || e.error === "interrupted")) ? true : false);
    window.speechSynthesis.speak(u);
  }
  function speak(text){   // text: one string or an array of pieces, spoken one after the other
    const parts = (Array.isArray(text) ? text : [text]).map(x => String(x || "").trim()).filter(Boolean);
    if(!hasTTS() || !parts.length) return false;
    try {
      stop(); const g = gen;
      queue = parts; qi = 0; paused = false; reading = true;
      if(ctl) ctl.hidden = false;
      next(g);
      return true;
    } catch(e){ return false; }
  }

  // ---- listening (the microphone) ----
  const micSupported = () => !!SR;
  const MIC_ERRORS = {"not-allowed": "The microphone is blocked. Allow it for this site in the browser's settings, then try again.",
    "service-not-allowed": "The microphone is blocked. Allow it for this site in the browser's settings, then try again.",
    "network": "Speech recognition needs an internet connection (the browser sends the audio to its speech service).",
    "no-speech": "I did not hear anything. Tap the microphone and try again.", "audio-capture": "No microphone was found.",
    "language-not-supported": "This browser cannot recognise that language."};
  function listen(o){
    o = o || {};
    if(!SR){ if(o.onError) o.onError("Speech recognition is not available in this browser."); return null; }
    let rec, got = false, dead = false;
    try { rec = new SR(); } catch(e){ if(o.onError) o.onError("Speech recognition could not start."); return null; }
    rec.lang = "en-IN"; rec.interimResults = false; rec.continuous = false; rec.maxAlternatives = 1;
    rec.onstart = () => { if(!dead && o.onState) o.onState(true); };
    rec.onresult = (e) => {
      if(dead) return;
      const r = e.results && e.results[e.results.length - 1];
      if(r && r.isFinal !== false && r[0] && r[0].transcript){ got = true; if(o.onText) o.onText(String(r[0].transcript).trim()); }
    };
    rec.onerror = (e) => { if(!dead && o.onError) o.onError(MIC_ERRORS[e && e.error] || "I could not hear you (" + ((e && e.error) || "error") + ").");  };
    rec.onend = () => { if(o.onState) o.onState(false); };
    try { rec.start(); } catch(e){ if(o.onError) o.onError("Speech recognition could not start."); return null; }
    return {stop(){ try { rec.stop(); } catch(e){} },
            abort(){ dead = true; try { rec.abort(); } catch(e){ try { rec.stop(); } catch(e2){} } if(o.onState) o.onState(false); },   // late results are ignored
            get heard(){ return got; }};
  }

  // ---- acting on an answer (every action is a page open or a read) ----
  function stockAction(op, ticker, host, mode){
    const t = TA.palette && TA.palette.routeStockOp(op, ticker, mode, host);
    if(!t) return;
    if(t.via === "host") host[t.op](t.symbol); else location.assign(t.url);
  }
  function goTab(tab, host){
    if(PAGES[tab]){ if(location.pathname !== PAGES[tab]) location.assign(PAGES[tab]); return; }
    const id = WS[tab];
    if(id && typeof host.goWorkspace === "function") host.goWorkspace(id);
    else if(id) location.assign("/#" + id);
  }
  const SCOPE_LABEL = {portfolio: "your holdings", market: "the market", stock: ""};
  function newsHtml(d){
    const rows = (d.items || []).map(i => `<li><span class="ask-tag ${esc(i.tag)}">${esc(i.tag)}</span> <b>${esc(i.name || i.symbol)}</b> ${esc(i.title)} <span class="sub">${esc(i.source)}${i.time_ist ? " · " + esc(i.time_ist) + " IST" : ""}</span></li>`).join("");
    return (d.note ? `<p class="sub">${esc(d.note)}</p>` : "") + (rows ? `<ol class="ask-news">${rows}</ol>` : `<p>No news items found.</p>`) + `<p class="ask-state sub" role="status"></p>`;
  }
  // A request that gives up after ms with a plain message; runId lets a newer question drop an older answer.
  let runId = 0;
  async function timed(url, body, ms, msg){
    const ac = new AbortController(), t = setTimeout(() => ac.abort(), ms);
    try { return await TA.api(url, body, ac.signal); }
    catch(e){ if(e && e.name === "AbortError") throw new Error(msg); throw e; }
    finally { clearTimeout(t); }
  }
  async function readNews(args, host, id){
    const q = "/api/news/today?scope=" + encodeURIComponent(args.scope || "portfolio") + (args.ticker ? "&ticker=" + encodeURIComponent(args.ticker) : "");
    show("News " + (SCOPE_LABEL[args.scope] ? "on " + SCOPE_LABEL[args.scope] : args.name ? "on " + args.name : ""),
         `<div class="ask-loading" role="status">${TA.loader ? TA.loader.spinner() : ""} <span>Reading the news… fetching headlines</span></div>`);
    let d;
    try { d = await timed(q, null, 45000, "The news is taking too long. Try again in a minute."); }
    catch(e){ if(id !== runId) return; show("News", `<p>${esc(e.message || "The news could not be loaded.")}</p>`); return; }
    if(id !== runId) return;
    const ok = hasTTS() && (d.items || []).length;
    show("News", newsHtml(d) + (hasTTS() ? "" : `<p class="sub">This browser cannot read aloud.</p>`), {reading: ok});
    if(ok && speak(d.speech_parts && d.speech_parts.length ? d.speech_parts : d.speech)) setState("Reading…");
  }
  async function run(text, host){
    host = host || {};
    const mode = host.mode || "live";
    text = String(text || "").trim().slice(0, 300);
    if(!text) return;
    stop();
    const id = ++runId;
    show("Assistant", `<p class="sub">${TA.loader ? TA.loader.spinner() : ""} Thinking…</p>`);
    let r;
    try { r = await timed("/api/ask", {text}, 20000, "That took too long. Try again."); }
    catch(e){ if(id === runId) show("Assistant", `<p>${esc(e.message || "That did not work.")}</p>`, {autoHide: 8000}); return; }
    if(id !== runId) return;
    const a = r.args || {};
    if(r.intent === "open_stock" || r.intent === "open_chart" || r.intent === "open_size"){
      const op = r.intent === "open_chart" ? "chart" : r.intent === "open_size" ? "size" : "lookup";
      const alts = (a.alternatives || []).filter(x => x && x.symbol);
      show("Assistant", `<p>${esc(r.say)}</p>` + (alts.length ? `<p class="sub">Not it? ${alts.map(x => `<button type="button" class="small ask-alt" data-sym="${esc(x.symbol)}">${esc(x.name || x.symbol)}</button>`).join(" ")}</p>` : ""), {autoHide: alts.length ? 12000 : 3500});
      if(alts.length) body.querySelectorAll(".ask-alt").forEach(b => b.addEventListener("click", () => { hide(); stockAction(op, b.dataset.sym, host, mode); }));
      stockAction(op, a.ticker, host, mode);
    } else if(r.intent === "open_tab"){ show("Assistant", `<p>${esc(r.say)}</p>`, {autoHide: 2500}); goTab(a.tab, host); }
    else if(r.intent === "portfolio_summary"){
      show("Assistant", `<p>${esc(r.say)}</p>`, {autoHide: 2500});
      if(typeof host.goWorkspace === "function"){ host.goWorkspace("overview"); setTimeout(() => { const m = document.getElementById("mp-card"); if(m && m.scrollIntoView) m.scrollIntoView({behavior: "smooth", block: "start"}); }, 150); }
      else location.assign("/");
    }
    else if(r.intent === "show_health"){ show("Assistant", `<p>${esc(r.say)}</p>`, {autoHide: 2500}); goTab("health", host); }
    else if(r.intent === "news_today"){ await readNews(a, host, id); }
    else show("Assistant", `<p>${esc(r.say || "I did not understand that.")}</p>`, {autoHide: r.intent === "refused" ? 9000 : 12000});
  }

  TA.ask = {run, listen, speak, stop, micSupported, speechSupported: hasTTS};
})();
