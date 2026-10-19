// Voice assistant bar: an "Ask about your market..." bar at the bottom of every page (and the same bar inside the Look up card,
// scoped to one stock) that grows into a voice card: listening -> thinking (real stages) -> speaking (words light up as they are
// read, with the figures) -> a short transcript with follow-up chips. It is only a face for Ask (ask.js): the sentence goes to
// /api/ask and the answer is a page action, the news or your own figures. READ-ONLY: nothing here can start a trade or fill in a
// trade form; the only page actions are the ones ask.js already allows (open a stock, a tab, the portfolio). Needs common.js and ask.js.
(function(){
  "use strict";

  // ---- pure helpers (also loaded by node for the tests) ----
  const MAX_TURNS = 6;                                   // the transcript keeps the last few turns, in memory only
  const TRANSITIONS = {
    idle:      {mic: "listening", submit: "thinking", close: "idle"},
    listening: {heard: "thinking", submit: "thinking", stop: "idle", fail: "error", close: "idle"},
    thinking:  {result: "speaking", fail: "error", cancel: "idle", close: "idle"},
    speaking:  {spoken: "done", stop: "done", submit: "thinking", mic: "listening", fail: "error", close: "idle"},
    done:      {mic: "listening", submit: "thinking", close: "idle", replay: "speaking"},
    error:     {mic: "listening", submit: "thinking", dismiss: "idle", close: "idle"},
  };
  // The next state for an event; an event that does not apply leaves the state as it is.
  const transition = (state, ev) => (TRANSITIONS[state] && TRANSITIONS[state][ev]) || state;
  const wordsOf = (text) => String(text || "").split(/\s+/).filter(Boolean);
  // The word (0-based) being spoken at character charIndex of text; the last word that starts at or before it.
  function wordIndexAt(text, charIndex){
    const re = /\S+/g; let m, idx = -1, i = 0;
    const c = Math.max(0, charIndex | 0);
    while((m = re.exec(String(text || ""))) !== null){ if(m.index <= c) idx = i; else break; i++; }
    return Math.max(0, idx);
  }
  // Pieces of speech -> {words, starts}: every word in order and the index of the first word of each piece.
  function layout(parts){
    const words = [], starts = [];
    (parts || []).forEach((p) => { starts.push(words.length); wordsOf(p).forEach(w => words.push(w)); });
    return {words, starts};
  }
  function capHistory(list, item, max){
    const out = (list || []).concat([item]);
    return out.slice(Math.max(0, out.length - (max || MAX_TURNS)));
  }
  // Follow-up chips from what was just answered: {label, text} asks again, {label, action: "replay"} reads the answer again.
  function chipsFor(res){
    const r = res || {}, out = [];
    if(r.intent === "news_today"){
      out.push({label: "Read it again", action: "replay"});
      const first = (r.facts || []).find(f => f && f.sym);
      const sym = r.ticker || (first && first.sym);
      if(sym) out.push({label: "Open " + sym, text: "open " + sym});
      if(r.scope === "stock") out.push({label: "Market news", text: "market news"});
    } else if(r.intent === "portfolio_summary"){
      if(r.topic !== "movers") out.push({label: "Top gainer today", text: "top gainer today"});
      if(r.topic !== "cash") out.push({label: "Cash", text: "how much cash"});
      out.push({label: "Open portfolio", text: "show my portfolio"});
    } else if(r.intent === "open_stock" || r.intent === "open_chart" || r.intent === "open_size"){
      const t = r.ticker || "";
      out.push({label: t ? "News on " + t : "Read the news", text: t ? "news on " + t : "read today's news"});
      if(t && r.intent !== "open_chart") out.push({label: "Show chart", text: "show " + t + " chart"});
    } else if(r.intent === "ideas"){
      out.push({label: "Read the news", text: "read today's news"}, {label: "Latest deals", text: "latest deals"}, {label: "Is the market open", text: "is the market open"});
    } else if(r.intent === "market_summary"){
      out.push({label: "Read the news", text: "read today's news"}, {label: "Is the market open", text: "is the market open"}, {label: "Profit and loss today", text: "profit and loss today"});
    } else if(r.intent === "market_status"){
      out.push({label: "Stock ideas", text: "stock ideas"}, {label: "Read the news", text: "read today's news"}, {label: "Latest deals", text: "latest deals"});
    } else if(r.intent === "followed_deals"){
      out.push({label: "Open deals", text: "go to deals"}, {label: "Stock ideas", text: "stock ideas"}, {label: "Read the news", text: "read today's news"});
    } else {
      out.push({label: "Read the news", text: "read today's news"}, {label: "Profit and loss today", text: "profit and loss today"}, {label: "How is my portfolio", text: "how is my portfolio"});
    }
    return out.slice(0, 3);
  }
  const core = {MAX_TURNS, TRANSITIONS, transition, wordsOf, wordIndexAt, layout, capHistory, chipsFor};
  if(typeof module !== "undefined" && module.exports) module.exports = core;
  if(typeof window === "undefined" || !window.TA || typeof document === "undefined") return;

  // ---- the page part ----
  const TA = window.TA;
  const MUTE_KEY = "ta.voice.muted";
  const SVG = {
    mic: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="3" width="6" height="11" rx="3"></rect><path d="M5 11a7 7 0 0 0 14 0M12 18v3"></path></svg>',
    micOff: '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="3" width="6" height="11" rx="3"></rect><path d="M5 11a7 7 0 0 0 14 0M12 18v3M3 3l18 18"></path></svg>',
    spk: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 9v6h4l5 4V5L8 9H4z"></path><path d="M16 9a4 4 0 0 1 0 6"></path></svg>',
    spkOff: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 9v6h4l5 4V5L8 9H4z"></path><path d="M17 9l5 6M22 9l-5 6"></path></svg>',
    stop: '<svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="2"></rect></svg>',
    close: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"></path></svg>',
    gear: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 6h10M18 6h2M4 12h2M10 12h10M4 18h12M20 18h0"></path><circle cx="16" cy="6" r="2"></circle><circle cx="8" cy="12" r="2"></circle><circle cx="18" cy="18" r="2"></circle></svg>',
  };
  const loadMuted = () => { try { return localStorage.getItem(MUTE_KEY) === "1"; } catch(e){ return false; } };
  const saveMuted = (m) => { try { localStorage.setItem(MUTE_KEY, m ? "1" : "0"); } catch(e){} };
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const nowHm = () => new Date().toLocaleTimeString("en-IN", {hour: "2-digit", minute: "2-digit", hour12: false});
  const host = () => (TA.palette && TA.palette.host) || {mode: (document.body && document.body.dataset.mode) || "live"};
  const el = (tag, cls, text) => { const e = document.createElement(tag); if(cls) e.className = cls; if(text != null) e.textContent = text; return e; };
  const icon = (svg) => { const s = el("span", "va-i"); s.innerHTML = svg; return s; };   // static markup only, never answer text
  const btn = (cls, label, svg, text) => { const b = el("button", cls); b.type = "button"; b.setAttribute("aria-label", label); b.title = label; if(svg) b.appendChild(icon(svg)); if(text) b.appendChild(document.createTextNode(text)); return b; };
  const MIC_BLOCKED = new Set(["not-allowed", "service-not-allowed"]);

  let active = null;   // only one bar listens or speaks at a time

  function create(o){
    const inline = !!o.inline;
    let ticker = o.ticker || null;
    let state = "idle", muted = loadMuted(), err = null, heard = "", micCtl = null, seq = 0, opener = null;
    let turns = [], cur = null, chain = Promise.resolve(), wordEls = [], lay = null, spoken = 0, fb = 0, gotBoundary = {};
    const canMic = TA.ask.micSupported();

    // -- markup --
    const root = el("div", "va" + (inline ? " va-inline" : ""));
    const bar = el("form", "va-bar"); bar.setAttribute("role", "search");
    const input = el("input", "va-input"); input.type = "text"; input.autocomplete = "off"; input.maxLength = 300; input.setAttribute("aria-label", "Ask a question");
    const kbd = el("kbd", "va-kbd", "Space");
    const micBtn = btn("va-mic", "Tap to talk", SVG.mic);
    bar.append(input, ...(inline ? [] : [kbd]), ...(canMic ? [micBtn] : []));
    const card = el("section", "va-card"); card.hidden = true; card.setAttribute("role", "dialog"); card.setAttribute("aria-label", "Voice assistant"); card.tabIndex = -1;
    const head = el("div", "va-head");
    const title = el("span", "va-title");
    const tools = el("div", "va-tools");
    const voiceBtn = btn("va-ic", "Voice", SVG.gear), muteBtn = btn("va-ic va-mute", "Read answers aloud", SVG.spk), closeBtn = btn("va-ic", "Close", SVG.close);
    voiceBtn.setAttribute("aria-expanded", "false");
    tools.append(voiceBtn, muteBtn, closeBtn); head.append(title, tools);
    const menu = el("div", "va-menu"); menu.hidden = true;
    const stage = el("div", "va-stage"); stage.setAttribute("role", "status"); stage.setAttribute("aria-live", "polite");
    const chips = el("div", "va-chips");
    const foot = el("form", "va-foot");
    const input2 = el("input", "va-input"); input2.type = "text"; input2.autocomplete = "off"; input2.maxLength = 300; input2.setAttribute("aria-label", "Type a question");
    const micBtn2 = btn("va-mic", "Tap to talk", SVG.mic);
    foot.append(input2, ...(canMic ? [micBtn2] : []));
    card.append(head, menu, stage, chips, ...(inline ? [] : [foot]));
    root.append(bar, card);

    // -- rendering --
    function setPlaceholder(){
      const ph = ticker ? "Ask about " + ticker + "…" : "Ask about your market…";
      input.placeholder = ph; input2.placeholder = ticker ? "Ask about " + ticker : "Type a question";
      title.textContent = ticker ? "Agent · " + ticker : "Agent · voice";
    }
    function orb(){ const d = el("div", "va-orb"); d.setAttribute("aria-hidden", "true"); for(let i = 0; i < 4; i++) d.appendChild(el("i")); return d; }
    function wave(live){ const w = el("span", "va-wave" + (live ? " live" : "")); w.setAttribute("aria-hidden", "true"); for(let i = 0; i < 5; i++) w.appendChild(el("i")); return w; }
    function factsEl(facts){
      const box = el("div", "va-facts");
      (facts || []).forEach((f) => {
        const row = el("div", "va-fact" + (f.long ? " va-long" : "")); row.setAttribute("role", "row");
        const k = el("span", "va-fk", f.label); k.setAttribute("role", "rowheader");
        const v = el("span", "va-fv" + (f.tone === "pos" ? " pl-profit" : f.tone === "neg" ? " pl-loss" : ""), f.value); v.setAttribute("role", "cell");
        row.append(k, v); box.appendChild(row);
      });
      if(facts && facts.length){ box.setAttribute("role", "table"); box.setAttribute("aria-label", "Figures"); }
      return facts && facts.length ? box : null;
    }
    function linkEl(link){
      if(!link || !/^\/[a-z0-9/_?=&.#-]*$/i.test(String(link.href))) return null;
      const a = el("a", "va-link", link.label || "Open"); a.href = link.href; return a;
    }
    function bubble(q){ return el("div", "va-q", q); }
    function renderAnswerWords(t){
      const box = el("div", "va-words"); wordEls = [];
      lay = layout(t.parts);
      lay.words.forEach((w) => { const s = el("span", "va-w", w); wordEls.push(s); box.append(s, document.createTextNode(" ")); });
      return box;
    }
    function paintSpoken(){ wordEls.forEach((s, i) => s.classList.toggle("on", i < spoken)); }
    function transcript(){
      const box = el("div", "va-hist");
      if(!turns.length){
        box.appendChild(el("p", "va-hint", ticker ? "Ask about " + ticker + ": news, the chart, or open it in full." : "Ask about your portfolio, the news or any stock. Tap the microphone or type."));
        return box;
      }
      box.appendChild(el("div", "va-day", "Today"));
      turns.forEach((t) => {
        const item = el("div", "va-turn");
        const qrow = el("div", "va-qrow"); qrow.append(el("span", "va-at", t.at), bubble(t.q));
        const arow = el("div", "va-arow");
        arow.appendChild(el("div", "va-a", t.say));
        const replay = btn("va-ic va-small", "Play again", SVG.spk); replay.addEventListener("click", () => speakTurn(t));
        arow.appendChild(replay);
        item.append(qrow, arow);
        const f = factsEl(t.facts); if(f) item.appendChild(f);
        const l = linkEl(t.link); if(l) item.appendChild(l);
        box.appendChild(item);
      });
      return box;
    }
    function renderChips(){
      chips.textContent = "";
      const t = turns[turns.length - 1];
      if(state !== "done" || !t) return;
      (t.chips || []).forEach((c) => {
        const b = el("button", "va-chip", c.label); b.type = "button";
        b.addEventListener("click", () => { if(c.action === "replay") speakTurn(t); else submit(c.text); });
        chips.appendChild(b);
      });
    }
    function render(){
      root.dataset.state = state;
      stage.textContent = "";
      const speechOff = !TA.ask.speechSupported();
      const info = TA.ask.voiceInfo ? TA.ask.voiceInfo() : {none: false};
      muteBtn.hidden = speechOff;
      muteBtn.setAttribute("aria-pressed", String(muted));
      const mlab = muted ? "Voice off: text only" : "Read answers aloud";
      muteBtn.title = mlab; muteBtn.setAttribute("aria-label", mlab);
      muteBtn.firstChild.innerHTML = muted ? SVG.spkOff : SVG.spk;
      const micState = state === "listening" ? "stop" : state === "thinking" ? "wait" : state === "speaking" ? "stop-speech" : "talk";
      [micBtn, micBtn2].forEach((b) => {
        b.dataset.mode = micState; b.disabled = micState === "wait";
        b.classList.toggle("on", state === "listening");
        const lab = micState === "stop" ? "Stop listening" : micState === "stop-speech" ? "Stop speaking" : micState === "wait" ? "Thinking" : "Tap to talk";
        b.title = lab; b.setAttribute("aria-label", lab);
        b.firstChild.innerHTML = micState === "stop" || micState === "stop-speech" ? SVG.stop : SVG.mic;
      });
      if(state === "listening"){
        stage.append(orb(), el("div", "va-label va-red", "Listening"));
        const h = el("div", "va-heard", heard); h.append(el("span", "va-caret")); stage.append(h, el("div", "va-sub", "Pause when you are done, or tap the mic to stop."));
      } else if(state === "thinking" && cur){
        stage.append(bubble(cur.q));
        const list = el("ol", "va-steps");
        (cur.steps || []).forEach((s) => {
          const li = el("li", "va-step " + s.status);
          li.append(el("span", "va-dot", s.status === "done" ? "✓" : ""), el("span", "va-sl", s.label));
          list.appendChild(li);
        });
        stage.appendChild(list);
      } else if(state === "speaking" && cur){
        stage.appendChild(bubble(cur.q));
        const who = el("div", "va-who"); who.append(el("b", null, "Agent"), wave(true), el("span", null, muted ? "Voice off · text only" : "Speaking"));
        stage.append(who, renderAnswerWords(cur)); paintSpoken();
        const f = factsEl(cur.facts); if(f) stage.appendChild(f);
        const l = linkEl(cur.link); if(l) stage.appendChild(l);
      } else if(state === "error" && err){
        const ic = el("div", "va-erric"); ic.innerHTML = err.kind === "mic" ? SVG.micOff : SVG.close;
        stage.append(ic, el("div", "va-errt", err.kind === "mic" ? "Microphone is blocked" : "That did not work"), el("div", "va-errm", err.message));
        const row = el("div", "va-erra");
        if(canMic && err.kind !== "net"){ const again = el("button", "va-btn primary", "Try again"); again.type = "button"; again.addEventListener("click", startListen); row.appendChild(again); }
        const typeIt = el("button", "va-btn", "Type instead"); typeIt.type = "button"; typeIt.addEventListener("click", typeInstead); row.appendChild(typeIt);
        stage.appendChild(row);
      } else {
        stage.appendChild(transcript());
      }
      if(info.none) stage.appendChild(el("p", "va-note", "No English voice installed: install Google Text-to-speech and an English (India) voice in Android settings. Answers stay text-only."));
      else if(speechOff) stage.appendChild(el("p", "va-note", "This browser cannot read aloud; answers are shown as text."));
      renderChips();
      const lastTurn = stage.querySelector(".va-turn:last-child");
      stage.scrollTop = state === "done" && lastTurn ? Math.max(0, lastTurn.offsetTop - 8) : state === "thinking" ? 0 : stage.scrollHeight;
    }
    const apply = (ev) => { state = transition(state, ev); };

    // -- the voice menu --
    function fillMenu(){
      menu.textContent = "";
      const info = TA.ask.voiceInfo();
      if(!info.voices.length){ menu.appendChild(el("p", "va-note", info.none ? "No English voice installed: install Google Text-to-speech and an English (India) voice in Android settings." : "No voices reported yet; the browser's default voice is used.")); }
      const lab = el("label", "va-sub", "Voice"); const sel = el("select", "va-select"); sel.setAttribute("aria-label", "Voice");
      const auto = el("option", null, info.auto); auto.value = ""; sel.appendChild(auto);
      info.voices.forEach((v) => { const op = el("option", null, v.name + " (" + v.lang + ")"); op.value = v.id; sel.appendChild(op); });
      sel.value = info.chosen || "";
      sel.addEventListener("change", () => { TA.ask.setVoice(sel.value); });
      const test = el("button", "va-btn", "Test voice"); test.type = "button";
      test.addEventListener("click", () => { TA.ask.stop(); if(!TA.ask.testVoice()) test.textContent = "No voice to test"; });
      lab.appendChild(sel); menu.append(lab, test);
    }
    voiceBtn.addEventListener("click", () => { menu.hidden = !menu.hidden; voiceBtn.setAttribute("aria-expanded", String(!menu.hidden)); if(!menu.hidden) fillMenu(); });
    try { window.speechSynthesis && window.speechSynthesis.addEventListener && window.speechSynthesis.addEventListener("voiceschanged", () => { if(!menu.hidden) fillMenu(); }); } catch(e){}

    // -- the flow --
    function haltAll(){
      seq++; clearTimeout(fb); chain = Promise.resolve();
      if(micCtl){ const c = micCtl; micCtl = null; try { c.abort(); } catch(e){} }
      try { TA.ask.stop(); } catch(e){}
    }
    function open(){
      if(active && active !== api) active.suspend();
      active = api;
      const fresh = card.hidden;
      if(fresh){ opener = document.activeElement && document.activeElement !== document.body ? document.activeElement : null; card.hidden = false; root.classList.add("open"); }
      render();
      if(fresh){ try { card.focus({preventScroll: true}); } catch(e){} }
    }
    function suspend(){ haltAll(); state = "idle"; card.hidden = true; root.classList.remove("open"); render(); if(active === api) active = null; }
    function close(){
      haltAll(); apply("close"); card.hidden = true; root.classList.remove("open"); heard = ""; render(); if(active === api) active = null;
      const f = opener && opener.isConnected ? opener : (inline ? input : micBtn);
      opener = null; try { if(f && f.focus) f.focus(); } catch(e){}
    }
    function fail(kind, message){ haltAll(); err = {kind, message}; state = "error"; open(); render(); const b = stage.querySelector("button"); if(b) b.focus(); }
    function typeInstead(){ haltAll(); err = null; state = turns.length ? "done" : "idle"; render(); (inline ? input : input2).focus(); }
    function startListen(){
      if(!canMic){ typeInstead(); return; }
      open(); haltAll(); const my = seq; heard = ""; err = null; state = "listening"; render();
      let gotText = false, failed = false;
      micCtl = TA.ask.listen({
        onState: (on) => { if(my !== seq || on) return; if(!gotText && !failed && state === "listening"){ micCtl = null; apply("stop"); if(turns.length) state = "done"; render(); } },
        onInterim: (t) => { if(my !== seq) return; heard = t; const h = stage.querySelector(".va-heard"); if(h){ h.textContent = t; h.appendChild(el("span", "va-caret")); } },
        onText: (t) => { if(my !== seq) return; gotText = true; micCtl = null; heard = t; apply("heard"); submit(t, true); },
        onError: (m, code) => { if(my !== seq) return; failed = true; fail(MIC_BLOCKED.has(code) || /blocked/i.test(m) ? "mic" : "listen", m); },
      });
      if(!micCtl && state === "listening" && !failed){ state = "idle"; render(); }
    }
    function stopMic(){ if(micCtl){ const c = micCtl; micCtl = null; seq++; try { c.stop(); } catch(e){} apply("stop"); if(turns.length) state = "done"; render(); } }
    function stageStep(key, label){   // a real stage from the request; each one stays visible at least 300 ms
      const my = seq;
      chain = chain.then(() => {
        if(my !== seq || !cur) return null;
        cur.steps.forEach(s => { if(s.status === "now") s.status = "done"; });
        cur.steps.push({key, label, status: "now"}); if(state === "thinking") render();
        return sleep(300);
      });
    }
    async function submit(text, fromVoice){
      text = String(text || "").trim().slice(0, 300);
      if(!text) return;
      open(); haltAll(); const my = seq;
      input.value = ""; input2.value = ""; err = null;
      cur = {q: text, at: nowHm(), steps: [], parts: [], facts: [], say: "", link: null, chips: []};
      state = "thinking"; render();
      try {
        const res = await TA.ask.answer(text, {host: host(), ticker, onStage: stageStep});
        await chain;
        if(my !== seq || !res) return;
        cur.steps.forEach(s => { s.status = "done"; });
        Object.assign(cur, {parts: res.parts, say: res.say, facts: res.facts, link: res.link, chips: chipsFor(res), intent: res.intent});
        turns = capHistory(turns, cur);
        state = "thinking"; apply("result");
        const speak = !muted && TA.ask.speechSupported() && !(TA.ask.voiceInfo && TA.ask.voiceInfo().none);
        if(!speak){ spoken = Infinity; render(); apply("spoken"); render(); return; }
        speakCur(my);
      } catch(e){
        if(my !== seq) return;
        const msg = e && e.message ? String(e.message) : "That did not work.";
        fail(/took too long|too long|network|failed to fetch/i.test(msg) ? "net" : "ask", msg);
      }
    }
    function speakCur(my){
      lay = layout(cur.parts); spoken = 0; gotBoundary = {}; state = "speaking"; render();
      const hooks = {
        onStart: (i) => { if(my !== seq) return; spoken = lay.starts[i] || 0; paintSpoken(); startFallback(my, i); },
        onBoundary: (i, ci) => { if(my !== seq) return; gotBoundary[i] = true; clearTimeout(fb); spoken = (lay.starts[i] || 0) + wordIndexAt(cur.parts[i], ci) + 1; paintSpoken(); },
        onEnd: () => { if(my !== seq) return; clearTimeout(fb); spoken = Infinity; paintSpoken(); apply("spoken"); render(); },
      };
      if(!TA.ask.speak(cur.parts, hooks)){ spoken = Infinity; apply("spoken"); render(); }
    }
    // Some engines never send word boundaries: after a short wait light the words up by an estimate of the reading time.
    function startFallback(my, i){
      clearTimeout(fb);
      const words = wordsOf(cur.parts[i]), base = lay.starts[i] || 0; let k = 0;
      const tick = () => {
        if(my !== seq || gotBoundary[i] || k >= words.length) return;
        spoken = base + k + 1; paintSpoken(); const d = 70 + 55 * words[k].length; k++;
        fb = setTimeout(tick, d);
      };
      fb = setTimeout(() => { if(my === seq && !gotBoundary[i]) tick(); }, 450);
    }
    function speakTurn(t){
      if(!TA.ask.speechSupported()) return;
      haltAll(); const my = seq; cur = t; open();
      if(muted){ state = "done"; render(); return; }
      state = "done"; apply("replay"); speakCur(my);
    }

    // -- events --
    bar.addEventListener("submit", (e) => { e.preventDefault(); submit(input.value); });
    foot.addEventListener("submit", (e) => { e.preventDefault(); submit(input2.value); });
    const micClick = () => { if(state === "listening") stopMic(); else if(state === "speaking"){ haltAll(); spoken = Infinity; apply("stop"); render(); } else if(state !== "thinking") startListen(); };
    micBtn.addEventListener("click", micClick); micBtn2.addEventListener("click", micClick);
    muteBtn.addEventListener("click", () => { muted = !muted; saveMuted(muted); if(muted && state === "speaking"){ haltAll(); spoken = Infinity; apply("stop"); } render(); });
    closeBtn.addEventListener("click", close);
    card.addEventListener("keydown", (e) => { if(e.key === "Escape"){ e.stopPropagation(); close(); } });

    const api = {root, open, close, suspend, startListen, stopMic, submit, setTicker(t){ ticker = t || null; setPlaceholder(); }, get state(){ return state; }, get turns(){ return turns; }, isOpen: () => !card.hidden};
    setPlaceholder(); render();
    return api;
  }

  // ---- mount ----
  let globalBar = null, scoped = null, scopedFor = null;
  function mount(){
    if(globalBar || !document.body) return;
    globalBar = create({inline: false});
    document.body.appendChild(globalBar.root);
    document.body.classList.add("va-on");
    document.addEventListener("keydown", (e) => {
      if(e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey) return;
      const t = e.target, tag = t && t.tagName ? t.tagName : "";
      const editable = /^(INPUT|TEXTAREA|SELECT|BUTTON|A|SUMMARY)$/.test(tag) || (t && t.isContentEditable);
      if((e.key === " " || e.code === "Space") && !editable && !e.repeat && !document.querySelector("dialog[open]") && TA.ask.micSupported()){
        e.preventDefault();
        if(globalBar.state === "listening") globalBar.stopMic(); else globalBar.startListen();
      } else if(e.key === "Escape" && globalBar.isOpen() && !document.querySelector("dialog[open]")){ globalBar.close(); }
    });
  }
  // The Look up card: the same bar and card inline, scoped to the stock that is shown there.
  function setScope(sym){
    const box = document.getElementById("lk-ask");
    if(!box || !sym) return;
    if(!scoped){ scoped = create({inline: true, ticker: sym}); box.appendChild(scoped.root); }
    else if(scopedFor !== sym){ scoped.close(); scoped.setTicker(sym); }
    scopedFor = sym;
  }
  TA.voice = Object.assign({mount, setScope}, core);
  if(document.readyState === "loading") document.addEventListener("DOMContentLoaded", mount); else mount();
})();
