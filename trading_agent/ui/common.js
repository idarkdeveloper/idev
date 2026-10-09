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
  // The Demo page sets <body data-api="/demo">, so the same page talks to its own sample-data app.
  const BASE = () => (document.body && document.body.dataset.api) || "";
  const api = async (path, body) => {
    const r = await fetch((path.startsWith("/api/") ? BASE() : "") + path, body ? {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)} : {});
    const j = await r.json().catch(()=>({}));
    if(!r.ok) throw new Error(j.error || r.statusText);
    return j;
  };
  const tile = (l, v, s) => `<div class="tile"><div class="label">${l}</div><div class="big">${v}</div><div class="sub">${s}</div></div>`;

  // ---- charts: plain SVG, one y-axis, hairline grid, crosshair tooltip ----
  // Nocturne chart palette: the accent leads, the text color and a neutral support it.
  const C = {s1:"#9184d9", s2:"#e9e9ed", s3:"#9397ab", ctx:"#75798c", grid:"rgba(233,233,237,.08)", base:"#595d6c", pos:"#9184d9", neg:"#75798c", ring:"#232532"};
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
    svg.querySelector(".hit").addEventListener("mousemove", ev => {
      const i = at(ev), x = X(i);
      xh.setAttribute("x1", x); xh.setAttribute("x2", x); xh.setAttribute("visibility", "visible");
      dots.innerHTML = cfg.series.map(s => s.values[i] == null ? "" : `<circle cx="${x}" cy="${Y(s.values[i])}" r="4" fill="${s.color}" stroke="${C.ring}" stroke-width="2"/>`).join("");
      tip.innerHTML = `<b>${esc(shortDate(xs[i]))}</b>` + cfg.series.map(s => s.values[i] == null ? "" : `<br><span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${s.color};margin-right:6px"></span>${esc(s.name)} ${esc(yF(s.values[i]))}`).join("");
      tip.style.display = "block";
      const r = svg.getBoundingClientRect(), px = x * r.width / W, tw = tip.offsetWidth;
      tip.style.left = Math.max(0, Math.min(r.width - tw, px + 12 + tw > r.width ? px - tw - 12 : px + 12)) + "px"; tip.style.top = "8px";
    });
    svg.querySelector(".hit").addEventListener("mouseleave", () => { xh.setAttribute("visibility", "hidden"); dots.innerHTML = ""; tip.style.display = "none"; });
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
  const clip = (t, n) => t.length <= n ? t : t.slice(0, t.lastIndexOf(" ", n) > n * 0.6 ? t.lastIndexOf(" ", n) : n).replace(/[,;:.]$/, "") + "…";
  function lookupTakeaway(r, now){
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
    const pos = r.position;
    if(pos && r.price){
      const pl = r.price / pos.avg_entry_price - 1, room = pos.stop ? r.price / pos.stop - 1 : null;
      out.push(`You hold ${pos.qty} at ${money(pos.avg_entry_price, 2)} (${pl >= 0 ? "+" : ""}${(pl * 100).toFixed(1)}%)${room != null ? `; its trailing stop at ${money(pos.stop, 2)} is ${(room * 100).toFixed(1)}% below the price` : ""}.`);
    }
    const strong = m.verdict === "strong" && m.above_200dma !== false;
    out.push(strong ? `Bottom line: trend-wise it is the kind of stock the screen buys${tw || res || deal ? ", but there is event risk ahead, so keep any position small" : ""}${p6 != null && p6 > 0.5 ? " and expect big swings" : ""}.`
                    : m.verdict === "weak" || m.above_200dma === false ? "Bottom line: the momentum rules say wait; a disclosed buy here would be a watch, not a buy."
                    : "Bottom line: nothing decisive either way; it needs a reason beyond the price trend.");
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

  return {$, esc, setCurrency: (fn) => { currencyFn = fn; }, currency, sym, money, signed, pct, when, cap, toast, api, tile,
          C, NS, niceTicks, shortDate, lineChart, histogram, rupeesShort, inr, sinr, spct,
          daysAgo, shortDay, clip, lookupTakeaway, attachSuggest};
})();
