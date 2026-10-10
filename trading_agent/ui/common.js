// Shared helpers for the Live, Demo and Replay pages (Nocturne design).
window.TA = (function(){
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
  let currencyFn = () => "INR";  // pages set this once their state says otherwise
  const currency = () => currencyFn();
  const sym = () => currency() === "INR" ? "₹" : "$";
  const money = (v, d=0) => v == null ? "n/a" : sym() + Number(v).toLocaleString(currency() === "INR" ? "en-IN" : "en-US", {maximumFractionDigits:d, minimumFractionDigits:d});
  const signed = (v, d=0) => v == null ? "n/a" : (v >= 0 ? "+" : "−") + money(Math.abs(v), d);
  const pct = (v, d=1) => v == null ? "n/a" : (v >= 0 ? "+" : "") + (v*100).toFixed(d) + "%";
  const when = (iso) => { if(!iso) return ""; const dt = new Date(iso); return dt.toLocaleString(undefined,{day:"2-digit",month:"short",hour:"2-digit",minute:"2-digit"}); };
  const cap = (s) => s ? s[0].toUpperCase()+s.slice(1) : "";
  const toast = (m) => { const t=$("toast"); t.textContent=m; t.style.display="block"; clearTimeout(t._t); t._t=setTimeout(()=>t.style.display="none",4500); };
  // The Demo page sets <body data-api="/demo">, so the same page talks to the practice-account app.
  const BASE = () => (document.body && document.body.dataset.api) || "";
  const api = async (path, body) => {
    const r = await fetch((path.startsWith("/api/") ? BASE() : "") + path, body ? {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)} : {});
    const j = await r.json().catch(()=>({}));
    if(!r.ok){ const err = new Error(j.error || r.statusText); err.data = j; err.status = r.status; throw err; }
    return j;
  };
  const tile = (l, v, s, cls) => `<div class="tile"><div class="label">${l}</div><div class="big${cls ? " " + cls : ""}">${v}</div><div class="sub">${s}</div></div>`;

  // ---- charts: plain SVG, one y-axis, hairline grid, crosshair tooltip ----
  // Chart colours are theme tokens from nocturne.css (--chart-*), handed to SVG/CSS as var(...) strings, so a chart
  // repaints the moment data-theme flips and nothing here holds a colour literal.
  const C = {s1:"var(--chart-s1)", s2:"var(--chart-s2)", s3:"var(--chart-s3)", ctx:"var(--chart-ctx)", grid:"var(--chart-grid)", base:"var(--chart-base)", pos:"var(--chart-pos)", neg:"var(--chart-neg)", ring:"var(--chart-ring)"};
  const NS = "http://www.w3.org/2000/svg";
  function niceTicks(min, max, n){
    n = n || 4; let span = max - min; if(!(span > 0)) span = Math.abs(max) || 1;
    const raw = span / n, mag = Math.pow(10, Math.floor(Math.log10(raw))), e = raw / mag;
    const step = (e >= 7.5 ? 10 : e >= 3.5 ? 5 : e >= 1.5 ? 2 : 1) * mag;
    const lo = Math.floor(min / step) * step, hi = Math.ceil(max / step) * step, out = [];
    for(let v = lo; v <= hi + step / 2; v += step) out.push(+v.toFixed(10));
    return out;
  }
  const shortDate = (d) => { const x = new Date(d + (d.length === 10 ? "T00:00:00" : "")); return isNaN(x) ? d : x.toLocaleDateString("en-IN", {day:"numeric", month:"short", year:"2-digit"}); };
  function lineChart(el, cfg){
    cfg = Object.assign({}, cfg, {refs: (cfg.refs || []).filter(r => r && r.y != null && isFinite(r.y))});   // no level, no line or legend entry
    const xs = cfg.x, n = xs.length;
    if(n < 2){ el.innerHTML = `<div class="chart-empty">${esc(cfg.empty || "Not enough data to draw yet.")}</div>`; return; }
    const W = Math.max(280, el.clientWidth || 560), H = cfg.height || 200;
    const m = {l: cfg.left || 60, r: cfg.endLabels ? 92 : 14, t: 10, b: 24}, pw = W - m.l - m.r, ph = H - m.t - m.b;
    const vals = []; cfg.series.forEach(s => s.values.forEach(v => { if(v != null && isFinite(v)) vals.push(v); }));
    (cfg.refs || []).forEach(r => vals.push(r.y));
    const ticks = niceTicks(Math.min(...vals), Math.max(...vals), 4), y0 = ticks[0], y1 = ticks[ticks.length - 1];
    const X = i => m.l + (n === 1 ? 0 : i / (n - 1) * pw), Y = v => m.t + ph - (v - y0) / ((y1 - y0) || 1) * ph;
    const yF = cfg.yFmt || (v => v.toLocaleString());
    let g = "";
    ticks.forEach(t => { g += `<line x1="${m.l}" x2="${W - m.r}" y1="${Y(t)}" y2="${Y(t)}" stroke="${t === y0 ? C.base : C.grid}" stroke-width="1"/><text x="${m.l - 8}" y="${Y(t) + 4}" text-anchor="end">${esc(yF(t))}</text>`; });
    const xt = Math.max(2, Math.min(5, n, Math.floor(pw / 95)));
    for(let k = 0; k < xt; k++){ const i = Math.round(k * (n - 1) / (xt - 1)); g += `<text x="${X(i)}" y="${H - 6}" text-anchor="${k === 0 ? "start" : k === xt - 1 ? "end" : "middle"}">${esc(shortDate(xs[i]))}</text>`; }
    (cfg.refs || []).forEach(r => { g += `<line x1="${m.l}" x2="${W - m.r}" y1="${Y(r.y)}" y2="${Y(r.y)}" stroke="${r.color || C.ctx}" stroke-width="1" stroke-dasharray="4 3"><title>${esc(r.label)} ${esc(yF(r.y))}</title></line>`; });
    const placed = [];
    cfg.series.forEach(s => {
      let d = "", pen = false;
      s.values.forEach((v, i) => { if(v == null || !isFinite(v)){ pen = false; return; } d += (pen ? "L" : "M") + X(i).toFixed(1) + " " + Y(v).toFixed(1); pen = true; });
      if(s.area && d){ const fi = s.values.findIndex(v => v != null), li = s.values.length - 1 - [...s.values].reverse().findIndex(v => v != null);
        g += `<path d="${d}L${X(li).toFixed(1)} ${Y(y0)}L${X(fi).toFixed(1)} ${Y(y0)}Z" fill="${s.color}" fill-opacity="0.08"/>`; }
      g += `<path d="${d}" fill="none" stroke="${s.color}" stroke-width="${s.width || 2}" stroke-linejoin="round" stroke-linecap="round"/>`;
      const li = s.values.length - 1 - [...s.values].reverse().findIndex(v => v != null);
      if(li >= 0 && li < n && s.values[li] != null){
        g += `<circle cx="${X(li)}" cy="${Y(s.values[li])}" r="4" fill="${s.color}" stroke="${C.ring}" stroke-width="2"/>`;
        const ly = Y(s.values[li]);
        if(cfg.endLabels && !placed.some(py => Math.abs(py - ly) < 13)){ placed.push(ly); g += `<text x="${X(li) + 8}" y="${ly + 4}" style="fill:var(--ink2)">${esc(s.label || s.name)}</text>`; }
      }
    });
    const refs = cfg.refs || [], showLegend = cfg.legend !== false && (cfg.series.length + refs.length) > 1;
    el.innerHTML = (showLegend ? `<div class="legend-row" style="margin-bottom:6px">${cfg.series.map(s => `<span><i style="background:${s.color}"></i>${esc(s.name)}</span>`).join("")}${refs.map(r => `<span><i style="background:repeating-linear-gradient(90deg, ${r.color || C.ctx} 0 4px, transparent 4px 7px)"></i>${esc(r.label)} ${esc(yF(r.y))}</span>`).join("")}</div>` : "")
      + `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(cfg.label || "chart")}">${g}<line class="xh" x1="0" x2="0" y1="${m.t}" y2="${m.t + ph}" stroke="${C.base}" stroke-width="1" visibility="hidden"/><g class="dots"></g><rect x="${m.l}" y="${m.t}" width="${pw}" height="${ph}" fill="transparent" class="hit"/></svg><div class="ctip"></div>`;
    const svg = el.querySelector("svg"), tip = el.querySelector(".ctip"), xh = svg.querySelector(".xh"), dots = svg.querySelector(".dots");
    const at = (ev) => { const r = svg.getBoundingClientRect(), sx = (ev.clientX - r.left) * W / r.width; return Math.max(0, Math.min(n - 1, Math.round((sx - m.l) / pw * (n - 1)))); };
    const hit = svg.querySelector(".hit");
    const show = (ev) => {
      const i = at(ev), x = X(i);
      xh.setAttribute("x1", x); xh.setAttribute("x2", x); xh.setAttribute("visibility", "visible");
      dots.innerHTML = cfg.series.map(s => s.values[i] == null ? "" : `<circle cx="${x}" cy="${Y(s.values[i])}" r="4" fill="${s.color}" stroke="${C.ring}" stroke-width="2"/>`).join("");
      tip.innerHTML = `<b>${esc(shortDate(xs[i]))}</b>` + cfg.series.map(s => s.values[i] == null ? "" : `<br><span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${s.color};margin-right:6px"></span>${esc(s.name)} ${esc(yF(s.values[i]))}`).join("");
      tip.style.display = "block";
      const r = svg.getBoundingClientRect(), px = x * r.width / W, tw = tip.offsetWidth;
      tip.style.left = Math.max(0, Math.min(r.width - tw, px + 12 + tw > r.width ? px - tw - 12 : px + 12)) + "px"; tip.style.top = "8px";
      return i;
    };
    const hide = () => { xh.setAttribute("visibility", "hidden"); dots.innerHTML = ""; tip.style.display = "none"; };
    // Mouse: follow the pointer. Touch: a tap (no movement) shows the nearest point, a tap elsewhere hides it; the hit
    // area is touch-action: pan-y so a vertical swipe always scrolls the page, and there is no drag-to-scrub.
    hit.addEventListener("mousemove", ev => { if(!touch.recent()) show(ev); });
    hit.addEventListener("mouseleave", () => { if(!touch.recent()) hide(); });
    let down = null, shown = -1;
    hit.addEventListener("pointerdown", ev => { down = ev.pointerType === "touch" ? {x: ev.clientX, y: ev.clientY} : null; });
    hit.addEventListener("pointerup", ev => {
      if(ev.pointerType !== "touch" || !down) return;
      const moved = Math.hypot(ev.clientX - down.x, ev.clientY - down.y); down = null;
      if(moved > 10) return;   // a swipe, not a tap
      touch.stamp();
      if(shown === at(ev) && tip.style.display === "block"){ hide(); shown = -1; touch.open = null; return; }
      if(touch.open) touch.open();
      shown = show(ev); touch.open = () => { hide(); shown = -1; };
    });
    hit.addEventListener("pointercancel", () => { down = null; });
  }
  // One document-level listener hides an open touch tooltip when the next tap lands anywhere but on a chart's hit area.
  const touch = {at: 0, open: null, stamp(){ this.at = Date.now(); }, recent(){ return Date.now() - this.at < 800; }};
  if(typeof document !== "undefined" && document.addEventListener){
    document.addEventListener("pointerup", ev => {
      if(ev.pointerType !== "touch" || !touch.open) return;
      const t = ev.target;
      if(t && t.classList && t.classList.contains && t.classList.contains("hit")) return;
      touch.open(); touch.open = null;
    });
  }
  function histogram(el, values, cfg){
    if(values.length < 2){ el.innerHTML = `<div class="chart-empty">${esc(cfg.empty || "Not enough data to draw yet.")}</div>`; return; }
    const W = Math.max(280, el.clientWidth || 560), H = cfg.height || 170, m = {l:40, r:14, t:10, b:26}, pw = W - m.l - m.r, ph = H - m.t - m.b;
    let lo = Math.min(...values, 0), hi = Math.max(...values, 0);
    const bw = niceTicks(lo, hi, 8); const step = bw[1] - bw[0];
    lo = bw[0]; hi = bw[bw.length - 1];
    const bins = []; for(let b = lo; b < hi - step / 2; b += step) bins.push({lo:b, hi:b + step, n:0});
    values.forEach(v => { const k = Math.min(bins.length - 1, Math.max(0, Math.floor((v - lo) / step))); bins[k].n++; });
    const maxN = Math.max(...bins.map(b => b.n)), yt = niceTicks(0, maxN, 3).filter(t => Number.isInteger(t));
    const X = v => m.l + (v - lo) / (hi - lo) * pw, Y = c => m.t + ph - c / (yt[yt.length - 1] || 1) * ph;
    let g = "";
    yt.forEach(t => { g += `<line x1="${m.l}" x2="${W - m.r}" y1="${Y(t)}" y2="${Y(t)}" stroke="${t === 0 ? C.base : C.grid}"/><text x="${m.l - 8}" y="${Y(t) + 4}" text-anchor="end">${t}</text>`; });
    bins.forEach((b, k) => { if(!b.n) return; const x0 = X(b.lo) + 1, x1 = X(b.hi) - 1, mid = (b.lo + b.hi) / 2;
      g += `<path d="M${x0} ${Y(0)}V${Y(b.n) + 3}q0 -3 3 -3H${x1 - 3}q3 0 3 3V${Y(0)}Z" fill="${mid < 0 ? C.neg : C.pos}"><title>${pct(b.lo,0)} to ${pct(b.hi,0)}: ${b.n} deal${b.n === 1 ? "" : "s"}</title></path>`; });
    g += `<line x1="${X(0)}" x2="${X(0)}" y1="${m.t}" y2="${m.t + ph}" stroke="var(--ink2)" stroke-width="1"/>`;
    [lo, 0, hi].forEach((v, k) => { g += `<text x="${X(v)}" y="${H - 6}" text-anchor="${k === 0 ? "start" : k === 2 ? "end" : "middle"}">${pct(v,0)}</text>`; });
    el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(cfg.label || "histogram")}">${g}</svg>`;
  }
  const rupeesShort = v => { const a = Math.abs(v); return (v < 0 ? "−" : "") + sym() + (a >= 1e7 ? (a / 1e7).toFixed(2) + " Cr" : a >= 1e5 ? (a / 1e5).toFixed(2) + " L" : Math.round(a).toLocaleString("en-IN")); };
  const inr = (v, d=0) => v == null ? "n/a" : "₹" + Number(v).toLocaleString("en-IN", {minimumFractionDigits:d, maximumFractionDigits:d});
  const sinr = (v, d=0) => v == null ? "n/a" : (v >= 0 ? "+" : "−") + inr(Math.abs(v), d);
  const spct = (v) => v == null ? "n/a" : (v >= 0 ? "+" : "−") + Math.abs(v * 100).toFixed(2) + "%";

  // ---- "What this means" for a looked-up stock; `now` is the replay date on the Replay page ----
  const daysAgo = (at, now) => { const d = new Date(String(at).replace(" ", "T")); return isNaN(d) ? null : Math.floor(((now ?? Date.now()) - d) / 864e5); };
  const shortDay = (at) => { const d = new Date(String(at).replace(" ", "T")); return isNaN(d) ? String(at).slice(0, 10) : d.toLocaleDateString("en-IN", {day: "numeric", month: "short"}); };
  const istDay = (at) => { const d = new Date(String(at)); return isNaN(d) ? String(at).slice(0, 10) : d.toLocaleDateString("en-IN", {timeZone: "Asia/Kolkata", day: "numeric", month: "short"}); };
  const clip = (t, n) => t.length <= n ? t : t.slice(0, t.lastIndexOf(" ", n) > n * 0.6 ? t.lastIndexOf(" ", n) : n).replace(/[,;:.]$/, "") + "…";
  function lookupTakeaway(r, now, ctx){
    const m = r.momentum || {}, out = [], name = r.name ? r.name.replace(/ Limited$/, "") : r.ticker;
    if(m.error) return "";
    const p6 = m.ret_6m, p12 = m.ret_12_1, hi = m.pct_from_52w_high;
    const trend = m.verdict === "strong" ? "a strong uptrend" : m.verdict === "weak" ? "a weak, falling trend" : "no clear trend";
    out.push(`${esc(name)} is in ${trend}: ${pct(p6)} over 6 months and ${pct(p12)} over the past year leaving out the latest month.`);
    if(m.above_200dma === false) out.push("It trades below its 200-day average, so the strategy's rules make no new buy here.");
    if(p6 != null && p6 > 0.5) out.push(`It has already risen ${pct(p6)} in six months: stocks this stretched often fall back sharply on any disappointment.`);
    if(hi != null && hi > -0.03) out.push("It is right at its 52-week high.");
    else if(hi != null && hi < -0.3) out.push(`It is ${Math.abs(hi * 100).toFixed(0)}% below its 52-week high.`);
    const anns = (r.announcements || []).filter(a => { const d = daysAgo(a.at, now); return d == null || d <= 60; });
    const find = (re) => anns.find(a => re.test((a.category || "") + " " + (a.text || "")));
    const tw = find(/trading window/i), res = find(/financial result|outcome of board meeting/i);
    if(res && daysAgo(res.at, now) != null && daysAgo(res.at, now) <= 14) out.push(`Results came out on ${shortDay(res.at)}: the price may still be reacting.`);
    else if(tw) out.push(`It closed its insider trading window on ${shortDay(tw.at)}, which usually means quarterly results are due within a few weeks: a big move either way is possible around them.`);
    const deal = find(/acquisition|amalgamation|merger|demerger|scheme of arrangement|takeover/i);
    if(deal) out.push(`Corporate action on ${shortDay(deal.at)} ("${esc(clip((deal.text || deal.category).replace(/^.*?(informed the Exchange (about|regarding) )/i, "").replace(/Hon ble/g, "Hon'ble"), 90))}"): read it before buying.`);
    const flags = [[/pledge/i, "a promoter pledge disclosure"], [/auditor/i, "a change of auditor"], [/credit rating/i, "a credit rating update"],
                   [/buy ?back/i, "a share buyback"], [/bonus|split|sub-division/i, "a bonus issue or share split"],
                   [/penalty|show cause|adjudicat|settlement order|sebi order|levied|fine imposed/i, "a regulatory order or penalty"]];
    const hits = flags.filter(([re]) => find(re)).map(([, txt]) => txt);
    if(hits.length) out.push(`Also in the last two months: ${hits.join(", ")}. Worth a look.`);
    const bad = ((r.news && r.news.items) || []).find(n => n.sentiment === "negative" && (n.confidence === "medium" || n.confidence === "high")
                                                            && (d => d != null && d <= 3)(daysAgo(n.published, now)));
    if(bad) out.push(`Negative news on ${istDay(bad.published)} (${esc(bad.source)}): "${esc(clip(bad.title, 110))}". Read it before buying.`);
    const pos = r.position;
    if(pos && r.price){
      const pl = r.price / pos.avg_entry_price - 1, room = pos.stop ? 1 - pos.stop / r.price : null;
      const kind = pos.stop_label || "trailing", stopTxt = pos.stop_type === "none" ? "; it has no stop-loss, so nothing sells it automatically"
        : room != null ? `; its ${kind} stop at ${money(pos.stop, 2)} is ${(room * 100).toFixed(1)}% below the price` : "";
      out.push(`You hold ${pos.qty} at ${money(pos.avg_entry_price, 2)} (${pl >= 0 ? "+" : ""}${(pl * 100).toFixed(1)}%)${stopTxt}.`);
    }
    // The market filter (live page only; Replay passes no ctx): risk-off, or the Nifty under its 200-day average, means no new buys.
    const rg = ctx && ctx.regime && !ctx.regime.error ? ctx.regime : null;
    const nifty = rg && rg.markets && rg.markets.nifty50;
    const marketWait = !!rg && (rg.regime === "risk_off" || (nifty && nifty.above_200dma === false));
    if(marketWait){
      const riskOff = rg.regime === "risk_off", below = !!(nifty && nifty.above_200dma === false);
      const why = riskOff && below ? "The market is risk-off and the Nifty is below its 200-day average"
                : riskOff ? "The market is risk-off" : "The Nifty is below its 200-day average";
      out.push(`${why}, so the agent's rules hold off new buys until it recovers; if you buy anyway, keep the position small.`);
    }
    const strong = m.verdict === "strong" && m.above_200dma !== false;
    const stockWait = m.verdict === "weak" || m.above_200dma === false;
    const risk = `${tw || res || deal ? ", but there is event risk ahead, so keep any position small" : ""}${p6 != null && p6 > 0.5 ? " and expect big swings" : ""}`;
    out.push(strong ? (marketWait ? `Bottom line: trend-wise it passes, but the market filter says wait${risk}.` : `Bottom line: trend-wise it is the kind of stock the screen buys${risk}.`)
                    : stockWait ? "Bottom line: the momentum rules say wait; a disclosed buy here would be a watch, not a buy."
                    : "Bottom line: nothing decisive either way; it needs a reason beyond the price trend.");
    if(ctx && (marketWait || stockWait)){
      const top = ctx.screen && ctx.screen.top ? ctx.screen.top.slice(0, 5).map(x => esc(x.symbol)) : null;
      const at = ctx.screen && ctx.screen.at ? String(ctx.screen.at).slice(0, 10) : null, today = new Date().toISOString().slice(0, 10);
      out.push(top && top.length ? `Stocks passing the screen ${at && at !== today ? "as of " + esc(at) : "today"}: ${top.join(", ")}.` : "Run Screen to see which stocks pass today.");
    }
    return out.join(" ");
  }

  // ---- company suggestions under ticker boxes: Up/Down move, Enter picks, Esc closes ----
  function attachSuggest(ids, onPick){
    const sug = $("suggest"), sugCache = new Map();
    let sugFor = null, sugItems = [], sugActive = -1, sugTimer = null, sugSeq = 0;
    const hl = (text, q) => {
      const i = text.toLowerCase().indexOf(q.toLowerCase());
      return i < 0 || !q ? esc(text) : esc(text.slice(0, i)) + "<mark>" + esc(text.slice(i, i + q.length)) + "</mark>" + esc(text.slice(i + q.length));
    };
    function placeSuggest(){
      if(!sugFor) return;
      const r = sugFor.getBoundingClientRect(), w = Math.min(Math.max(r.width, 340), window.innerWidth - 16);
      sug.style.width = w + "px";
      sug.style.left = Math.max(8, Math.min(r.left, window.innerWidth - w - 8)) + "px";
      const below = window.innerHeight - r.bottom;
      if(below < 220 && r.top > below){ sug.style.top = ""; sug.style.bottom = (window.innerHeight - r.top + 4) + "px"; }
      else { sug.style.bottom = ""; sug.style.top = (r.bottom + 4) + "px"; }
    }
    function closeSuggest(){
      sug.classList.remove("open"); sugItems = []; sugActive = -1;
      if(sugFor){ sugFor.setAttribute("aria-expanded", "false"); sugFor.removeAttribute("aria-activedescendant"); }
    }
    function renderSuggest(q){
      if(!sugFor) return;
      sug.innerHTML = sugItems.length
        ? sugItems.map((h, i) => `<div class="opt" role="option" id="sug-${i}" data-i="${i}" aria-selected="${i === sugActive}"><span class="sym">${hl(h.symbol, q)}</span><span class="nm">${hl(h.name, q)}</span></div>`).join("")
          + `<div class="hint">↑ ↓ to move · Enter to choose · Esc to close</div>`
        : `<div class="none">No matching companies on NSE</div>`;
      sug.classList.add("open"); placeSuggest();
      sugFor.setAttribute("aria-expanded", "true");
      if(sugActive >= 0){ sugFor.setAttribute("aria-activedescendant", "sug-" + sugActive); $("sug-" + sugActive).scrollIntoView({block: "nearest"}); }
      else sugFor.removeAttribute("aria-activedescendant");
    }
    function setActive(i){  // move the highlight without rebuilding the list
      sugActive = i;
      sug.querySelectorAll(".opt").forEach(o => o.setAttribute("aria-selected", String(Number(o.dataset.i) === i)));
      if(i >= 0){ sugFor.setAttribute("aria-activedescendant", "sug-" + i); $("sug-" + i).scrollIntoView({block: "nearest"}); }
    }
    function pickSuggest(i){
      const h = sugItems[i], input = sugFor;
      if(!h || !input) return;
      input.value = h.symbol; closeSuggest();
      onPick(input, h.symbol);
    }
    async function fetchSuggest(q){
      const seq = ++sugSeq;
      let hits = sugCache.get(q.toLowerCase());
      if(!hits){
        try { hits = await api("/api/search?q=" + encodeURIComponent(q)); sugCache.set(q.toLowerCase(), hits); }
        catch(e){ hits = []; }
      }
      if(seq !== sugSeq || !sugFor || sugFor.value.trim() !== q) return;  // a newer keystroke won
      sugItems = hits; sugActive = hits.length ? 0 : -1; renderSuggest(q);
    }
    ids.forEach(id => {
      const input = $(id);
      input.setAttribute("role", "combobox"); input.setAttribute("aria-autocomplete", "list");
      input.setAttribute("aria-controls", "suggest"); input.setAttribute("aria-expanded", "false");
      input.addEventListener("input", () => {
        sugFor = input; const q = input.value.trim();
        clearTimeout(sugTimer);
        if(q.length < 2){ closeSuggest(); return; }
        sugTimer = setTimeout(() => fetchSuggest(q), 150);
      });
      input.addEventListener("keydown", (ev) => {
        if(sugFor !== input || !sug.classList.contains("open")) return;
        if(ev.key === "ArrowDown" || ev.key === "ArrowUp"){
          ev.preventDefault(); if(!sugItems.length) return;
          setActive((sugActive + (ev.key === "ArrowDown" ? 1 : -1) + sugItems.length) % sugItems.length);
        } else if(ev.key === "Enter" && sugActive >= 0){ ev.preventDefault(); pickSuggest(sugActive); }
        else if(ev.key === "Escape"){ ev.preventDefault(); closeSuggest(); }
        else if(ev.key === "Tab"){ closeSuggest(); }
      });
      input.addEventListener("blur", () => setTimeout(() => { if(document.activeElement !== input) closeSuggest(); }, 120));
    });
    sug.addEventListener("mousedown", (ev) => {  // mousedown, so the input keeps focus until the pick
      const o = ev.target.closest(".opt"); if(!o) return;
      ev.preventDefault(); pickSuggest(Number(o.dataset.i));
    });
    sug.addEventListener("mousemove", (ev) => {
      const o = ev.target.closest(".opt"); if(!o || Number(o.dataset.i) === sugActive) return;
      setActive(Number(o.dataset.i));
    });
    window.addEventListener("resize", placeSuggest);
    window.addEventListener("scroll", () => { if(sug.classList.contains("open")) placeSuggest(); }, {passive: true});
  }

  // News labels arrive a little after the headlines (a background tagger). A headline list still needs labels when
  // some items are untagged while a tagger is working ("pending" or a named one; "none" means nobody will label them).
  const NEWS_POLL_MS = 5000, NEWS_POLL_MAX_MS = 120000;
  const newsNeedsLabels = (n) => !!n && !n.demo && n.tagger !== "none" && (n.items || []).some(i => !i.sentiment);
  // "poll" = ask again in NEWS_POLL_MS; "stop" = all tagged, no tagger, two minutes passed, or another stock was looked up.
  const newsPollNext = (n, startedAt, now, stillCurrent) =>
    (!stillCurrent || now - startedAt >= NEWS_POLL_MAX_MS || !newsNeedsLabels(n)) ? "stop" : "poll";

  // ---- theme: Dark / Light / Auto. The head script of each page has already set data-theme (no flash); this keeps it
  // live. The choice lives in localStorage "theme" ("dark" or "light"; absent means Auto) and every read or write is
  // guarded: storage may throw (private window, blocked site data) and the page must still work. ----
  const theme = (function(){
    const KEY = "theme", listeners = [];
    let choice = "auto", mq = null, bound = false;
    const root = () => (typeof document !== "undefined" && document.documentElement) || null;
    const readStored = () => { try { const v = localStorage.getItem(KEY); return v === "dark" || v === "light" ? v : "auto"; } catch(e){ return "auto"; } };
    const media = () => { try { return typeof matchMedia === "function" ? matchMedia("(prefers-color-scheme: light)") : null; } catch(e){ return null; } };
    // Auto follows the OS; when the OS gives no answer the dashboard stays dark, as it always was.
    const resolve = (c) => c === "dark" || c === "light" ? c : (mq && mq.matches ? "light" : "dark");
    function paint(){
      const r = resolve(choice), el = root();
      if(el && el.dataset){ el.dataset.theme = r; el.dataset.themeChoice = choice; }
      if(typeof document !== "undefined" && typeof document.querySelectorAll === "function"){
        document.querySelectorAll("[data-theme-choice]").forEach(b => {
          const on = b.dataset.themeChoice === choice;
          b.setAttribute("aria-pressed", on ? "true" : "false");
          if(b.classList) b.classList.toggle("on", on);
        });
      }
      listeners.slice().forEach(fn => { try { fn(r, choice); } catch(e){} });
      return r;
    }
    function set(c){
      choice = c === "dark" || c === "light" ? c : "auto";
      try { if(choice === "auto") localStorage.removeItem(KEY); else localStorage.setItem(KEY, choice); } catch(e){}
      return paint();
    }
    function init(){
      choice = readStored(); mq = media();
      if(!bound){
        bound = true;
        if(mq){ const on = () => { if(choice === "auto") paint(); }; if(mq.addEventListener) mq.addEventListener("change", on); else if(mq.addListener) mq.addListener(on); }
        if(typeof window !== "undefined" && window.addEventListener) window.addEventListener("storage", (e) => { if(!e || e.key === KEY || e.key == null){ choice = readStored(); paint(); } });
        if(typeof document !== "undefined" && typeof document.querySelectorAll === "function")
          document.querySelectorAll("[data-theme-choice]").forEach(b => b.addEventListener("click", () => set(b.dataset.themeChoice)));
      }
      return paint();
    }
    return {init, set, get: () => choice, resolved: () => resolve(choice), onChange: (fn) => { listeners.push(fn); }};
  })();

  // One horizon of a signal-lab result as sorted rows, for the table and the plain-English reading.
  function signalRows(r, h){
    const res = (r.results || {})[h] || {}, label = k => (r.signals[k] || {}).label || k;
    return Object.entries(res).filter(([, s]) => s.t_stat != null).map(([k, s]) => ({k, label: label(k), t: s.t_stat, verdict: s.verdict, model: k === "walk_forward_model", s}))
      .sort((a, b) => b.t - a.t);
  }
  // Plain-English reading of the portfolio backtest, built from the numbers shown.
  function factorTakeaway(r){
    const st = r.stats, sg = st.strategy, ew = st.equal_weight;
    const fund = st.index_fund, bench = fund || st.benchmark;
    const benchName = fund ? `the ${r.index_fund_symbol} index fund` : /^NIFTYBEES/i.test(r.benchmark_symbol || "") ? "NIFTY 50 with dividends" : "NIFTY 50";
    if(!sg || sg.total_return == null || !bench || bench.total_return == null) return "";
    const yrs = r.months / 12, yrsTxt = yrs >= 1.5 ? `${yrs.toFixed(1)} years` : `${r.months} months`;
    const rs = v => "₹" + Math.round(100 * (1 + v));
    const pp = v => (v >= 0 ? "+" : "−") + Math.abs(v * 100).toFixed(1) + " points";
    const out = [];
    const beat = sg.total_return > bench.total_return;
    out.push(`Over ${yrsTxt}, every ₹100 in the top-${r.top} ${esc(r.universe)} factor portfolio became ${rs(sg.total_return)} after ${money(r.costs_paid)} of charges, against ${rs(bench.total_return)} in ${benchName}. That is ${(sg.cagr * 100).toFixed(1)}% a year against ${(bench.cagr * 100).toFixed(1)}%.`);
    if(ew && ew.cagr != null && !fund){
      const screenAdds = sg.cagr - ew.cagr;
      out.push(screenAdds > 0
        ? `But just holding every ${esc(r.universe)} member equally, with no screen and no charges, made ${pct(ew.total_return)}. So most of the lead over NIFTY 50 came from ${esc(r.universe)} stocks as a group doing better than the 50 largest; the screen itself added about ${pp(screenAdds)} a year on top.`
        : `Just holding every ${esc(r.universe)} member equally made ${pct(ew.total_return)}, more than the screen: picking the top ${r.top} cost money against owning them all.`);
    }
    if(sg.volatility && bench.volatility) out.push(`It was a much bumpier ride: ${(sg.volatility / bench.volatility).toFixed(1)}× the swings, with a worst fall of ${pct(sg.max_drawdown)} against ${pct(bench.max_drawdown)}.`);
    const curve = r.strategy || [], bc = fund ? (r.index_fund || []) : (r.benchmark || []);
    if(curve.length > 13){
      let pk = 0; curve.forEach((v, i) => { if(v > curve[pk]) pk = i; });
      const fromPeak = curve[curve.length - 1] / curve[pk] - 1;
      const n = curve.length - 1, a = n - 12;
      const last12 = curve[n] / curve[a] - 1, b12 = bc[a] && bc[n] ? bc[n] / bc[a] - 1 : null;
      if(fromPeak < -0.1 && pk < n) out.push(`Most of the gain came early: it peaked in ${shortDate(r.dates[pk])} and is ${Math.abs(fromPeak * 100).toFixed(1)}% below that peak now.`);
      if(b12 != null) out.push(`Over the last 12 months it returned ${pct(last12)} against ${pct(b12)} for ${benchName}${last12 < b12 ? ", so it has been lagging lately" : ""}.`);
    }
    if(!r.point_in_time) out.push("It ranks today's index members only, which flatters it: stocks that fell out of the index are missing.");
    const vd = r.validation;
    if(vd && vd.verdict) out.push(vd.verdict === "likely skill" ? "The luck checks below say this looks like skill rather than chance."
      : vd.verdict === "no edge" ? "The luck checks below found no edge: treat the result above as luck."
      : "The luck checks below say this could easily be luck.");
    out.push(beat
      ? `Bottom line: it beat ${benchName} in this one window, but with far bigger drops${ew && sg.cagr - ew.cagr < 0.03 && !fund ? ", much of the edge was the universe rather than the picks," : ""} and past results like this often fade. Worth forward-testing in paper money, not betting big on.`
      : `Bottom line: after charges it did not beat ${benchName}; owning the index fund was simpler and better here.`);
    return out.join(" ");
  }
  // Plain-English reading of one horizon's results, built from the numbers shown.
  function signalTakeaway(rows, r, h){
    const per = h === "5" ? "week" : h === "20" ? "month" : h === "60" ? "quarter" : h + " trading days";
    const cost = r.round_trip_cost || 0, t2 = v => (v >= 0 ? "+" : "−") + Math.abs(v).toFixed(2);
    const gapOf = x => x.s.top_minus_bottom, fam = x => (r.signals[x.k] || {}).family || "";
    const names = xs => xs.map(x => x.label).join(", ");
    const by = v => rows.filter(x => x.verdict === v), signals = rows.filter(x => !x.model);
    const pred = by("predictive"), rev = by("reversed"), luck = by("could be luck"), small = by("too small to trade");
    const out = [];
    if(pred.length){
      out.push(`${names(pred)} predicted the next ${per} strongly enough to matter: t of 3 or more and a gap between the top and bottom fifth bigger than the ${(cost * 100).toFixed(2)}% charges. Treat it as a candidate, and check it holds on the other horizons too.`);
    } else {
      const best = rows[0];
      out.push(`None of the ${signals.length} signals reliably predicted the next ${per}.`);
      if(best) out.push(`The strongest, ${best.label} (t ${t2(best.t)}), ${best.t < 2 ? "is inside the range luck alone produces (under 2)" : "is borderline"}, and its top fifth beat the bottom fifth by ${pct(gapOf(best), 2)}, ${gapOf(best) != null && gapOf(best) < cost ? "less than" : "against"} the ${(cost * 100).toFixed(2)}% a round trip costs in charges.`);
    }
    if(luck.length || small.length) out.push(`${names(luck.concat(small))} looked promising but ${small.length && !luck.length ? "the gap is smaller than the charges" : "could still be luck (t between 2 and 3)"}.`);
    const up = signals.filter(x => x.t > 0).length, down = signals.filter(x => x.t < 0).length;
    if(!pred.length && Math.abs(up - down) <= 3) out.push(`${up} pointed the right way and ${down} the wrong way, about the split you'd get from coin tosses.`);
    const avg = f => { const xs = signals.filter(x => fam(x) === f).map(x => x.t); return xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null; };
    const trend = avg("trend"), mr = avg("mean reversion");
    if(trend != null && mr != null && trend < -0.5 && mr > 0.5) out.push(`Trend signals (recent winners, stocks near highs) leaned the wrong way and mean-reversion ones (recent losers) the right way: over a ${per}, winners in this index tended to give a little back. Too weak to trade on.`);
    else if(trend != null && mr != null && trend > 0.5 && mr < -0.5) out.push(`Trend signals leaned the right way and mean-reversion ones the wrong way: over a ${per}, winners tended to keep going, though not reliably.`);
    if(rev.length) out.push(`${names(rev)} came out reversed (t of −2 or below): doing the opposite would have worked, which may be real or chance.`);
    const model = rows.find(x => x.model);
    if(model && model.verdict !== "predictive") out.push(`The walk-forward model, which combines every signal and learns only from the past, did no better (t ${t2(model.t)}).`);
    const all = [];
    Object.entries(r.results || {}).forEach(([hh, res]) => Object.entries(res).forEach(([k, s]) => { if(s.deflated_sharpe != null) all.push({k, hh, s}); }));
    all.sort((a, b) => b.s.deflated_sharpe - a.s.deflated_sharpe);
    const odds = all[0];
    if(odds && r.trials) out.push(`After charges and allowing for the ${r.trials} signal-and-horizon combinations tried, the best odds of a real edge across all horizons belong to ${esc((r.signals[odds.k] || {}).label || odds.k)} (${odds.hh}d) at ${Math.round(odds.s.deflated_sharpe * 100)}%, and that is the best of ${r.trials} tries, so some of it is just picking the winner.`);
    out.push(pred.length ? "Bottom line: one possible edge worth watching, not yet a reason to trade."
                         : `Bottom line: don't buy or sell ${esc(r.universe)} stocks on these signals alone over a ${per}.`);
    return out.join(" ");
  }
  // Plain-English reading of a position-size result (Live and Replay).
  function sizeTakeaway(r, riskPct, maxPct){
    if(!r || !r.price || !r.equity) return "";
    const out = [], share = r.notional / r.equity;
    if(!r.qty){
      return `At ${money(r.price, 2)} a share, even one share would break the ${maxPct}% cap on your ${money(r.equity)} account, so the suggested size is zero.`;
    }
    out.push(`Buy ${r.qty} share${r.qty === 1 ? "" : "s"} for about ${money(r.notional)}: ${(share * 100).toFixed(1)}% of your ${money(r.equity)}.`);
    if(r.atr){
      const byRisk = Math.floor((r.equity * riskPct / 100) / (2 * r.atr));
      if(byRisk > r.qty) out.push(`The ${maxPct}% cap set this: the ${riskPct}% risk rule alone would allow about ${byRisk} shares (${money(byRisk * r.price)}), so the cap is protecting you from putting too much in one stock.`);
      else out.push(`The ${riskPct}% risk rule set this: a ${pct(2 * r.atr / r.price)} move (twice the usual daily range) would cost about ${riskPct}% of your equity.`);
    } else out.push("There was no volatility data, so it is sized at half the cap.");
    if(r.stop){
      const loss = r.qty * (r.price - r.stop);
      out.push(`If it falls to the stop at ${money(r.stop, 2)} (${pct(r.stop / r.price - 1)}), you would lose about ${money(loss)}, ${(loss / r.equity * 100).toFixed(2)}% of your equity.`);
    }
    const c = r.round_trip_cost;
    if(c) out.push(`Buying and selling costs about ${money(c.total)} (${(c.total_bps / 100).toFixed(2)}%), so it must rise ${(c.total_bps / 100).toFixed(2)}% just to break even${c.total_bps > 70 ? "; at this size the flat ₹20 charges weigh heavily, and a bigger, rarer trade costs less in percent" : ""}.`);
    return out.join(" ");
  }

  // ---- safety and freshness: mode strip, freshness chip, protection line, URL state (pure functions + small painters) ----
  const safety = (function(){
    const P = {
      lock: '<rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
      alert: '<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>',
      flask: '<path d="M9 2h6"/><path d="M10 2v6L4.5 18a2 2 0 0 0 1.8 3h11.4a2 2 0 0 0 1.8-3L14 8V2"/>',
      rewind: '<polygon points="11 19 2 12 11 5 11 19"/><polygon points="22 19 13 12 22 5 22 19"/>',
      shield: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><polyline points="9 12 11 14 15 10"/>',
      shieldoff: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><line x1="3" y1="3" x2="21" y2="21"/>',
    };
    const icon = (n) => `<svg class="ico" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${P[n] || ""}</svg>`;
    const MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
    const day = (iso, withYear) => { const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(iso || "")); return m ? `${Number(m[3])} ${MONTHS[Number(m[2]) - 1]}${withYear ? " " + m[1] : ""}` : ""; };

    // The strip under the header. mode: "live" | "demo" | "replay"; o.liveOrders: true | false | undefined (not loaded yet);
    // o.date: the replay clock date (ISO).
    function modeStrip(mode, o){
      o = o || {};
      if(mode === "replay") return {kind: "replay", icon: "rewind",
        text: o.date ? `REPLAY · past data up to ${day(o.date, true)} · practice money` : "REPLAY · past data only · practice money"};
      if(mode === "demo") return {kind: "practice", icon: "flask", text: "PRACTICE · practice money only · no real orders possible"};
      if(o.liveOrders === "unknown") return {kind: "unknown", icon: "alert", text: "LIVE · your real Groww account · live-orders setting unknown (could not read the server state)"};
      if(o.liveOrders === true) return {kind: "liveon", icon: "alert", text: "LIVE · real Groww account · live orders ON (agent/watch can trade)"};
      if(o.liveOrders === false) return {kind: "live", icon: "lock", text: "LIVE · your real Groww account · read-only on this page · live orders OFF"};
      return {kind: "live", icon: "lock", text: "LIVE · your real Groww account · read-only on this page · checking the live-orders setting"};
    }
    function paintStrip(res){
      try {
        const el = $("modestrip"); if(!el) return;
        el.className = "modestrip " + res.kind;
        $("modestrip-icon").innerHTML = icon(res.icon);
        $("modestrip-text").textContent = res.text;
        if(document.documentElement && document.documentElement.dataset) document.documentElement.dataset.strip = res.kind;
      } catch(e){}
    }

    const ageText = (s) => s >= 7200 ? Math.floor(s / 3600) + " h" : s >= 90 ? Math.round(s / 60) + " min" : Math.max(0, Math.round(s)) + " s";
    const closeText = (iso) => { const m = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})/.exec(String(iso || "")); return m ? `${day(m[1])} ${m[2]}` : ""; };
    // The freshness chip from the /api/freshness pieces: {level: "ok" | "warn" | "bad" | "idle", text, title}.
    function freshnessChip(f){
      if(!f) return {level: "idle", text: "Freshness unknown", title: ""};
      const w = f.watch || {}, p = f.prices || {}, d = f.deals || {};
      let level, text;
      if(!f.market_open){
        level = "idle"; text = p.last_close ? `Prices from ${closeText(p.last_close)} close` : "Market closed";
      } else if(!w.seen){
        level = f.live_orders ? "warn" : "idle"; text = "Watch service: not seen";
      } else if(w.level === "bad"){ level = "bad"; text = `Watch not seen for ${ageText(w.age_s)}`; }
      else if(w.level === "warn"){ level = "warn"; text = `Watch not seen for ${ageText(w.age_s)}`; }
      else { level = "ok"; text = `Data live · watch ${ageText(w.age_s)} ago`; }
      if(w.seen && w.last_error && f.market_open) text += " · last tick had an error";
      if(f.nse_degraded){ text += " · NSE degraded"; if(level === "ok" || level === "idle") level = "warn"; }
      const bits = [];
      bits.push(f.market_open ? "NSE open (09:15 to 15:30 IST)" : "NSE closed");
      bits.push(w.seen ? `watch service last seen ${ageText(w.age_s)} ago` + (w.every ? ` (ticks every ${w.every} s)` : "") : "watch service: not seen");
      if(w.last_error) bits.push("watch error: " + w.last_error);
      if(p.bar_at) bits.push("newest price bar " + p.bar_at);
      if(d.age_s != null) bits.push(`deals fetched ${ageText(d.age_s)} ago`);
      return {level, text, title: bits.join(" · ")};
    }
    function paintChip(res){
      try {
        const el = $("freshness"); if(!el) return;
        el.className = "pill fresh " + res.level; el.title = res.title || "";
        $("fresh-text").textContent = res.text;
      } catch(e){}
    }

    // Protection line for one holding (safety.protection from the server): icon + words + optional warning.
    function protectionHtml(p){
      if(!p) return "";
      const ic = p.kind === "gtt" ? "shield" : p.kind === "server" ? (p.tone === "neutral" ? "shield" : "alert") : (p.tone === "bad" ? "alert" : "shieldoff");
      return `<span class="prot ${esc(p.tone)}">${icon(ic)}<span>${esc(p.text)}</span></span>` + (p.warning ? `<span class="prot-warn">${icon("alert")}<span>${esc(p.warning)}</span></span>` : "");
    }

    // ---- URL state: investor filter, signal-lab horizon, open section ----
    const slug = (n) => String(n == null ? "" : n).toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
    const HORIZONS = [5, 20, 60];
    // known: {investors: [names], anchors: [names]}. Unknown values are ignored; "all" is an explicit empty selection.
    function parseUrl(search, hash, known){
      known = known || {};
      const out = {}, q = new URLSearchParams(String(search || "").replace(/^\?/, ""));
      const inv = q.get("investor");
      if(inv !== null){
        if(inv.toLowerCase() === "all") out.investor = [];
        else {
          const names = known.investors || [], picked = [];
          inv.split(",").map(slug).forEach(sg => { const n = names.find(x => slug(x) === sg); if(n && !picked.includes(n)) picked.push(n); });
          if(picked.length) out.investor = picked;
        }
      }
      const h = q.get("h");
      if(h !== null && /^\d+$/.test(h) && HORIZONS.includes(Number(h))) out.h = Number(h);
      const a = String(hash || "").replace(/^#/, "");
      if(a && (known.anchors || []).includes(a)) out.anchor = a;
      return out;
    }
    // st: {investor: [names] | undefined (leave out), h: number | undefined, anchor: string | undefined}
    function buildUrl(pathname, st){
      st = st || {};
      const q = [];
      if(st.investor !== undefined) q.push("investor=" + (st.investor.length ? st.investor.map(slug).join(",") : "all"));
      if(st.h && HORIZONS.includes(Number(st.h))) q.push("h=" + Number(st.h));
      return (pathname || "/") + (q.length ? "?" + q.join("&") : "") + (st.anchor ? "#" + st.anchor : "");
    }
    // The section to name in the URL: the one whose box holds the line `line` px below the viewport top ("" when none does).
    function pickSection(tops, line){
      let best = null;   // the section under the line; where two columns both are, the one that started last
      (tops || []).forEach(t => { if(t.top <= line && (t.bottom == null || t.bottom > line) && (!best || t.top > best.top)) best = t; });
      return best ? best.anchor : "";
    }
    return {modeStrip, paintStrip, freshnessChip, paintChip, ageText, protectionHtml, icon, slug, parseUrl, buildUrl, pickSection, day};
  })();
  // ---- stock chart: TradingView Lightweight Charts v5 (vendored at /static/lightweight-charts.js) for the lookup card.
  // Candles + volume + EMA/BB lines in the main pane, RSI and MACD in their own panes, a hover legend, range and
  // indicator chips (remembered in localStorage). Colours are the nocturne.css tokens, resolved to rgb() through a probe
  // element because the tokens use color-mix() which the library's canvas colour parser cannot read. ----
  const STOCK_RANGES = ["1M", "3M", "6M", "1Y", "2Y", "5Y"];
  const STOCK_TOGGLES = [["ema", "EMA"], ["bb", "BB"], ["rsi", "RSI"], ["macd", "MACD"], ["vol", "Volume"]];
  const STOCK_KEY = "lkChart";
  const stockDefaults = () => ({range: "1Y", on: {ema: true, bb: false, rsi: true, macd: false, vol: true}});
  function stockPrefs(){
    const p = stockDefaults();
    try {
      const v = JSON.parse(localStorage.getItem(STOCK_KEY) || "null");
      if(v && STOCK_RANGES.includes(v.range)) p.range = v.range;
      if(v && v.on) STOCK_TOGGLES.forEach(([k]) => { if(typeof v.on[k] === "boolean") p.on[k] = v.on[k]; });
    } catch(e){}
    return p;
  }
  function saveStockPrefs(p){ try { localStorage.setItem(STOCK_KEY, JSON.stringify(p)); } catch(e){} }

  // One token -> "rgb(r, g, b)" / "rgba(r, g, b, a)". Probe element first (resolves var() and color-mix()), then a 1px
  // canvas so even the `color(srgb ...)` form that browsers return for color-mix() becomes plain rgba.
  function resolveColor(token, fallback){
    let probe = null;
    try {
      probe = document.createElement("span");
      probe.style.cssText = "position:absolute;left:-9999px;top:0;visibility:hidden;color:" + fallback;
      probe.style.color = "var(" + token + ", " + fallback + ")";
      document.body.appendChild(probe);
      const css = getComputedStyle(probe).color;
      if(/^rgba?\(/.test(css)) return css;
      const cv = document.createElement("canvas"); cv.width = cv.height = 1;
      const cx = cv.getContext("2d");
      cx.clearRect(0, 0, 1, 1); cx.fillStyle = css; cx.fillRect(0, 0, 1, 1);
      const d = cx.getImageData(0, 0, 1, 1).data;
      return d[3] === 255 ? `rgb(${d[0]}, ${d[1]}, ${d[2]})` : `rgba(${d[0]}, ${d[1]}, ${d[2]}, ${+(d[3] / 255).toFixed(3)})`;
    } catch(e){ return fallback; }
    finally { if(probe && probe.parentNode) probe.parentNode.removeChild(probe); }
  }
  function withAlpha(rgb, a){
    const m = String(rgb).match(/[\d.]+/g);
    return m && m.length >= 3 ? `rgba(${m[0]}, ${m[1]}, ${m[2]}, ${a})` : rgb;
  }
  function stockColors(){
    const c = (t, f) => resolveColor(t, f);
    const x = {
      bg: c("--color-surface", "currentColor"), text: c("--color-muted", "currentColor"), grid: c("--color-rule", "currentColor"),
      border: c("--color-divider", "currentColor"), ink: c("--color-text", "currentColor"),
      up: c("--color-profit", "currentColor"), down: c("--color-loss", "currentColor"),
      accent: c("--color-accent", "currentColor"), amber: c("--color-replay", "currentColor"), teal: c("--color-demo", "currentColor"),
      accent2: c("--color-accent-2", "currentColor")
    };
    x.volUp = withAlpha(x.up, 0.35); x.volDown = withAlpha(x.down, 0.35);
    x.band = withAlpha(x.text, 0.8);
    return x;
  }

  const fmtVol = (v) => v == null ? "n/a" : v >= 1e7 ? (v / 1e7).toFixed(2) + " Cr" : v >= 1e5 ? (v / 1e5).toFixed(2) + " L"
    : Math.round(v).toLocaleString("en-IN");
  const timeKey = (t) => typeof t === "string" ? t
    : t && typeof t === "object" && t.year ? `${t.year}-${String(t.month).padStart(2, "0")}-${String(t.day).padStart(2, "0")}` : null;

  // stockChart(host, {ticker, fetchCandles(range) -> Promise<payload>, isCurrent() -> bool, fallback(message)})
  // Builds the controls, legend and chart inside ``host``; returns {destroy()}. Throws if the library is missing so the
  // caller can draw the old line chart instead; async failures call ``fallback``.
  function stockChart(host, cfg){
    const LW = window.LightweightCharts;
    if(!LW || typeof LW.createChart !== "function" || !LW.CandlestickSeries) throw new Error("chart library missing");
    const prefs = stockPrefs();
    let chart = null, data = null, dead = false, seq = 0, colors = null, ro = null, mo = null, mq = null;
    let series = {}, byDate = new Map(), legendFor = null, savedRange = null;
    host.innerHTML = `<div class="ck-controls"><div class="ck-chips" role="group" aria-label="Chart range">${STOCK_RANGES.map(r =>
        `<button type="button" class="ck-chip" data-range="${r}" aria-pressed="false">${r}</button>`).join("")}</div>
      <div class="ck-chips" role="group" aria-label="Indicators">${STOCK_TOGGLES.map(([k, l]) =>
        `<button type="button" class="ck-chip" data-tog="${k}" aria-pressed="false">${l}</button>`).join("")}</div></div>
      <div class="ck-legend" aria-live="off"></div><div class="ck-box"></div><div class="ck-note sub"></div>`;
    const box = host.querySelector(".ck-box"), legend = host.querySelector(".ck-legend"), note = host.querySelector(".ck-note");

    function paintChips(){
      host.querySelectorAll("[data-range]").forEach(b => b.setAttribute("aria-pressed", b.dataset.range === prefs.range ? "true" : "false"));
      host.querySelectorAll("[data-tog]").forEach(b => b.setAttribute("aria-pressed", prefs.on[b.dataset.tog] ? "true" : "false"));
    }
    const heights = () => {
      const narrow = (box.clientWidth || host.clientWidth || 360) < 560;
      return {main: narrow ? 260 : 360, rsi: narrow ? 70 : 100, macd: narrow ? 80 : 110};
    };
    const totalHeight = () => {
      const h = heights();
      return h.main + (prefs.on.rsi ? h.rsi + 1 : 0) + (prefs.on.macd ? h.macd + 1 : 0) + 26;
    };
    function layoutPanes(){
      if(!chart) return;
      const h = heights(), panes = chart.panes();
      chart.applyOptions({width: Math.max(box.clientWidth, 100), height: totalHeight()});   // size first, then each pane
      // Panes share the plot height by stretch factor, so the factors are the wanted heights (main 260 : RSI 70 : MACD 80).
      let i = 1;
      if(panes[0]) panes[0].setStretchFactor(h.main);
      if(prefs.on.rsi && panes[i]) panes[i++].setStretchFactor(h.rsi);
      if(prefs.on.macd && panes[i]) panes[i++].setStretchFactor(h.macd);
    }

    function applyColors(){
      if(!chart) return;
      colors = stockColors();
      const k = colors;
      chart.applyOptions({
        layout: {background: {type: LW.ColorType ? LW.ColorType.Solid : "solid", color: k.bg}, textColor: k.text,
                 panes: {separatorColor: k.border, separatorHoverColor: k.border, enableResize: false}},
        grid: {vertLines: {color: k.grid}, horzLines: {color: k.grid}},
        rightPriceScale: {borderColor: k.border}, timeScale: {borderColor: k.border},
        crosshair: {vertLine: {color: k.text, labelBackgroundColor: k.accent}, horzLine: {color: k.text, labelBackgroundColor: k.accent}}
      });
      const s = series;
      if(s.candle) s.candle.applyOptions({upColor: k.up, downColor: k.down, wickUpColor: k.up, wickDownColor: k.down});
      if(s.vol && data) s.vol.setData(data.bars.map(b => ({time: b.time, value: b.volume, color: b.close >= b.open ? k.volUp : k.volDown})));
      if(s.ema20) s.ema20.applyOptions({color: k.teal});
      if(s.ema50) s.ema50.applyOptions({color: k.amber});
      if(s.ma200) s.ma200.applyOptions({color: k.accent});
      ["bbU", "bbM", "bbL"].forEach(n => { if(s[n]) s[n].applyOptions({color: k.band}); });
      if(s.rsi) s.rsi.applyOptions({color: k.accent});
      if(s.guides) s.guides.forEach(g => g.applyOptions({color: k.text}));
      if(s.macd) s.macd.applyOptions({color: k.accent});
      if(s.sig) s.sig.applyOptions({color: k.amber});
      if(s.hist && data) s.hist.setData(data.macd_hist.map(p => ({time: p.time, value: p.value, color: p.value >= 0 ? k.volUp : k.volDown})));
      if(s.costLine) s.costLine.applyOptions({color: k.ink});
      if(s.stopLine) s.stopLine.applyOptions({color: k.down});
      updateLegend(legendFor);
    }

    function teardown(){
      if(chart){
        try { savedRange = chart.timeScale().getVisibleLogicalRange(); } catch(e){}
        try { chart.remove(); } catch(e){}
      }
      chart = null; series = {}; byDate = new Map();
    }

    function build(keepRange){
      teardown();
      if(!keepRange) savedRange = null;
      if(dead || !data || !data.bars || !data.bars.length) return;
      const k = colors = stockColors(), on = prefs.on;
      const fmt = (v) => money(v, 2);
      chart = LW.createChart(box, {
        width: Math.max(box.clientWidth, 100), height: totalHeight(),
        layout: {fontFamily: getComputedStyle(document.body).fontFamily, fontSize: 11, attributionLogo: true},
        handleScroll: {mouseWheel: false, vertTouchDrag: false}, handleScale: {mouseWheel: true, pinch: true, axisPressedMouseMove: true},
        timeScale: {timeVisible: false, rightOffset: 2},
        crosshair: {mode: 0}
      });
      const line = (opts, pane) => chart.addSeries(LW.LineSeries, Object.assign({lineWidth: 1, lastValueVisible: false,
        priceLineVisible: false, crosshairMarkerVisible: false}, opts), pane);
      // the price scale also covers your cost and stop, so those lines are never off-screen
      const keep = [data.position && data.position.cost, data.position && data.position.stop].filter(v => v != null);
      series.candle = chart.addSeries(LW.CandlestickSeries, {borderVisible: false, priceLineVisible: false,
        priceFormat: {type: "custom", formatter: fmt, minMove: 0.01},
        autoscaleInfoProvider: (base) => {
          const r = base();
          if(!r || !r.priceRange || !keep.length) return r;
          return Object.assign({}, r, {priceRange: {minValue: Math.min(r.priceRange.minValue, ...keep),
                                                    maxValue: Math.max(r.priceRange.maxValue, ...keep)}});
        }}, 0);
      series.candle.setData(data.bars.map(b => ({time: b.time, open: b.open, high: b.high, low: b.low, close: b.close})));
      if(on.vol){
        series.vol = chart.addSeries(LW.HistogramSeries, {priceFormat: {type: "volume"}, priceScaleId: "vol",
          lastValueVisible: false, priceLineVisible: false}, 0);
        chart.priceScale("vol").applyOptions({scaleMargins: {top: 0.8, bottom: 0}});
        series.candle.priceScale().applyOptions({scaleMargins: {top: 0.06, bottom: 0.22}});
      }
      const setLine = (name, pts, opts) => { series[name] = line(opts, 0); series[name].setData(pts || []); };
      if(on.ema){ setLine("ema20", data.ema20, {}); setLine("ema50", data.ema50, {}); setLine("ma200", data.ma200, {lineWidth: 2}); }
      if(on.bb){
        setLine("bbU", data.bb_upper, {}); setLine("bbM", data.bb_mid, {lineStyle: 2}); setLine("bbL", data.bb_lower, {});
      }
      const pos = data.position;
      if(pos && pos.cost != null) series.costLine = series.candle.createPriceLine({price: pos.cost, lineWidth: 1, lineStyle: 2,
        axisLabelVisible: true, title: "Your cost", color: k.ink});
      if(pos && pos.stop != null) series.stopLine = series.candle.createPriceLine({price: pos.stop, lineWidth: 1, lineStyle: 2,
        axisLabelVisible: true, title: "Stop", color: k.down});
      let pane = 1;
      if(on.rsi){
        series.rsi = line({lineWidth: 1, priceFormat: {type: "custom", formatter: (v) => v.toFixed(0)},
          autoscaleInfoProvider: () => ({priceRange: {minValue: 0, maxValue: 100}})}, pane++);
        series.rsi.setData(data.rsi14 || []);
        series.guides = [70, 30].map(p => series.rsi.createPriceLine({price: p, lineWidth: 1, lineStyle: 2, axisLabelVisible: false, title: "", color: k.text}));
      }
      if(on.macd){
        const pf = {priceFormat: {type: "custom", formatter: (v) => v.toFixed(2)}};
        series.hist = chart.addSeries(LW.HistogramSeries, Object.assign({lastValueVisible: false, priceLineVisible: false}, pf), pane);
        series.macd = line(pf, pane); series.sig = line(pf, pane);
        series.macd.setData(data.macd || []); series.sig.setData(data.macd_signal || []);
        pane++;
      }
      data.bars.forEach((b, i) => byDate.set(b.time, i));
      layoutPanes();
      if(keepRange && savedRange) { try { chart.timeScale().setVisibleLogicalRange(savedRange); } catch(e){ chart.timeScale().fitContent(); } }
      else chart.timeScale().fitContent();
      chart.subscribeCrosshairMove((param) => updateLegend(param && param.time ? timeKey(param.time) : null));
      applyColors();
    }

    // Legend: the hovered bar's date, O H L C, change vs previous close, volume and every visible indicator; the
    // latest bar when nothing is hovered.
    function valueAt(arr, key){
      if(!arr) return null;
      let lo = 0, hi = arr.length - 1;
      while(lo <= hi){ const m = (lo + hi) >> 1; if(arr[m].time === key) return arr[m].value; if(arr[m].time < key) lo = m + 1; else hi = m - 1; }
      return null;
    }
    function updateLegend(key){
      legendFor = key;
      if(!data || !data.bars || !data.bars.length){ legend.textContent = ""; return; }
      let i = key != null && byDate.has(key) ? byDate.get(key) : data.bars.length - 1;
      const b = data.bars[i], prev = i > 0 ? data.bars[i - 1].close : null, on = prefs.on;
      const chg = prev ? (b.close - prev) / prev : null, cls = chg == null ? "" : chg >= 0 ? "ck-up" : "ck-down";
      const p = [`<b>${esc(b.time)}</b>`, `O ${esc(money(b.open, 2))}`, `H ${esc(money(b.high, 2))}`, `L ${esc(money(b.low, 2))}`,
        `C ${esc(money(b.close, 2))}`];
      if(chg != null) p.push(`<span class="${cls}">${chg >= 0 ? "+" : "−"}${Math.abs(chg * 100).toFixed(2)}%</span>`);
      if(on.vol) p.push(`Vol ${esc(fmtVol(b.volume))}`);
      const add = (label, arr, d, color) => { const v = valueAt(arr, b.time); if(v != null) p.push(`<span style="color:${color || "inherit"}">${label} ${esc(Number(v).toFixed(d))}</span>`); };
      const k = colors || {};
      if(on.ema){ add("EMA20", data.ema20, 2, k.teal); add("EMA50", data.ema50, 2, k.amber); add("200d", data.ma200, 2, k.accent); }
      if(on.bb){ add("BB↑", data.bb_upper, 2); add("BB mid", data.bb_mid, 2); add("BB↓", data.bb_lower, 2); }
      if(on.rsi) add("RSI", data.rsi14, 1, k.accent);
      if(on.macd){ add("MACD", data.macd, 2, k.accent); add("Signal", data.macd_signal, 2, k.amber); add("Hist", data.macd_hist, 2); }
      legend.innerHTML = p.join(" · ");
    }

    async function load(){
      const mine = ++seq;
      note.textContent = "Loading chart…";
      let payload;
      try { payload = await cfg.fetchCandles(prefs.range); }
      catch(e){ if(mine === seq && !dead && cfg.isCurrent()) cfg.fallback(e.message || "chart data unavailable"); return; }
      if(mine !== seq || dead || !cfg.isCurrent()) return;   // a newer range or another stock was asked for meanwhile
      if(!payload || payload.error || !payload.bars || !payload.bars.length){ cfg.fallback((payload && payload.error) || "no price history"); return; }
      data = payload; note.textContent = "";
      try { build(false); } catch(e){ cfg.fallback(e.message || "chart failed"); }
    }

    host.addEventListener("click", (ev) => {
      const t = ev.target.closest && ev.target.closest("button[data-range], button[data-tog]");
      if(!t || dead) return;
      ev.preventDefault();
      if(t.dataset.range){ if(t.dataset.range === prefs.range) return; prefs.range = t.dataset.range; paintChips(); saveStockPrefs(prefs); load(); return; }
      prefs.on[t.dataset.tog] = !prefs.on[t.dataset.tog]; paintChips(); saveStockPrefs(prefs);
      try { build(true); } catch(e){ cfg.fallback(e.message || "chart failed"); }
    });

    // Repaint on a change of the theme attribute on <html> (the Dark / Light / Auto switch) and on a change of the
    // system colour scheme (Auto follows it). Always re-applied: harmless when a fixed theme is chosen.
    try { mo = new MutationObserver(() => applyColors()); mo.observe(document.documentElement, {attributes: true, attributeFilter: ["data-theme"]}); } catch(e){}
    const onScheme = () => applyColors();
    try {
      mq = typeof matchMedia === "function" ? matchMedia("(prefers-color-scheme: dark)") : null;
      if(mq){ if(mq.addEventListener) mq.addEventListener("change", onScheme); else if(mq.addListener) mq.addListener(onScheme); }
    } catch(e){ mq = null; }
    try { if(typeof ResizeObserver === "function"){ ro = new ResizeObserver(() => { if(chart && !dead) layoutPanes(); }); ro.observe(box); } } catch(e){}

    paintChips(); load();
    return {
      destroy(){
        dead = true; seq++;
        if(mo) mo.disconnect();
        if(ro) ro.disconnect();
        if(mq){ if(mq.removeEventListener) mq.removeEventListener("change", onScheme); else if(mq.removeListener) mq.removeListener(onScheme); }
        teardown();
      },
      get chart(){ return chart; }
    };
  }

  return {$, esc, setCurrency: (fn) => { currencyFn = fn; }, currency, sym, money, signed, pct, when, cap, toast, api, tile,
          C, NS, niceTicks, shortDate, lineChart, histogram, rupeesShort, inr, sinr, spct,
          daysAgo, shortDay, clip, lookupTakeaway, factorTakeaway, signalTakeaway, signalRows, sizeTakeaway, attachSuggest, newsNeedsLabels, newsPollNext, NEWS_POLL_MS, theme, safety, stockChart};
})();
window.TA.theme.init();
