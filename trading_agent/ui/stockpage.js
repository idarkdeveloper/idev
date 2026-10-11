// The stock page: the "Look up a stock" card laid out like a stock screen. One component, no page-level globals:
//   const page = TA.stockPage(host, "TCS", {mode, api, exchange, holding, context, onPractice, onLoaded, onOpen});
//   page.load("INFY"); page.setExchange("BSE"); page.destroy();
// It mounts in the dashboard's Look up card today, and can mount in a /inspect page, a side pane or a phone drawer.
//
// Safety: the Practice buy / Practice sell buttons never send an order. They call opts.onPractice(side, symbol) and the
// host page opens the existing practice order form pre-filled (the Live page goes to /demo?buy=SYM / ?sell=SYM). The
// only server reads here are GETs: /api/lookup, /api/stock, /api/candles, /api/news.
(function(){
  "use strict";

  // ---------- pure helpers (also exported for the node tests) ----------
  const isNum = (v) => typeof v === "number" && isFinite(v);
  const group = (n, d) => Number(n).toLocaleString("en-IN", {minimumFractionDigits: d, maximumFractionDigits: d});
  const rupees = (v, d) => isNum(v) ? (v < 0 ? "−" : "") + "₹" + group(Math.abs(v), d == null ? 2 : d) : "n/a";
  const signedRupees = (v, d) => !isNum(v) ? "n/a" : (v >= 0 ? "+" : "−") + "₹" + group(Math.abs(v), d == null ? 2 : d);
  const percent = (f, d) => isNum(f) ? (f * 100).toFixed(d == null ? 2 : d) + "%" : "n/a";            // 0.1234 -> "12.34%"
  const signedPercent = (f, d) => !isNum(f) ? "n/a" : (f >= 0 ? "+" : "−") + Math.abs(f * 100).toFixed(d == null ? 2 : d) + "%";
  const ratio = (v, d) => isNum(v) ? v.toFixed(d == null ? 2 : d) : "n/a";
  const crore = (cr) => !isNum(cr) ? "n/a" : Math.abs(cr) < 1 ? "₹" + cr.toFixed(2) + " Cr" : "₹" + group(Math.round(cr), 0) + " Cr";   // 1234567 -> ₹12,34,567 Cr
  const volume = (v) => isNum(v) ? group(v, 0) : "n/a";
  const tone = (v) => !isNum(v) || v === 0 ? "" : v > 0 ? "pl-profit" : "pl-loss";
  // Where a price sits between a low and a high: 0..1, or null when the range is missing or flat.
  const markerPos = (price, lo, hi) => !isNum(price) || !isNum(lo) || !isNum(hi) || !(hi > lo) ? null : Math.min(1, Math.max(0, (price - lo) / (hi - lo)));
  // Lower / upper circuit text from the payload's circuit block: values with the band, "No band", or n/a.
  function circuitText(c, which){
    if(!c || c.state === "unknown") return "n/a";
    if(c.state === "none") return "No band";
    return rupees(c[which]);
  }
  // Bar heights for the financial chart: revenue and profit share one scale so they compare. Negative profit hangs below zero.
  function barLayout(periods, height){
    const top = Math.max(1, ...periods.map(p => Math.max(Math.abs(p.revenue_cr || 0), Math.abs(p.profit_cr || 0))));
    const h = (v) => isNum(v) ? Math.max(1, Math.abs(v) / top * height) : 0;
    return periods.map(p => ({label: p.label, revenue: h(p.revenue_cr), profit: h(p.profit_cr), loss: isNum(p.profit_cr) && p.profit_cr < 0}));
  }
  // The period shown in the figures: the selected one (default the newest) and the one before it.
  function periodView(periods, idx){
    if(!periods || !periods.length) return null;
    const i = Math.min(Math.max(isNum(idx) ? idx : periods.length - 1, 0), periods.length - 1);
    return {cur: periods[i], prev: i > 0 ? periods[i - 1] : null, index: i};
  }
  const bandText = (b) => b == null ? "" : String(b);
  // Shares held: quantity, average price, value, P&L in rupees and percent at the current price.
  function holdingFigures(h, price){
    if(!h || !isNum(h.qty) || h.qty <= 0) return null;
    const avg = isNum(h.avg_entry_price) ? h.avg_entry_price : h.avg_price;
    const invested = isNum(avg) ? h.qty * avg : null;
    const value = isNum(price) ? h.qty * price : null;
    const pl = invested != null && value != null ? value - invested : null;
    return {qty: h.qty, avg, value, pl, plPct: pl != null && invested ? pl / invested : null, stop: isNum(h.stop) ? h.stop : null, stopLabel: h.stop_label || null,
            stopType: h.stop_type || null};
  }

  // ---------- the component ----------
  let uid = 0;
  const SENT = {positive: ["ok", "▲ Positive"], negative: ["bad", "▼ Negative"], neutral: ["", "● Neutral"]};
  const safeUrl = (u) => /^https?:\/\//i.test(String(u || "")) ? u : "";
  const TABS = [["overview", "Overview"], ["technicals", "Technicals"], ["news", "News"]];

  function stockPage(host, ticker, opts){
    const TA = window.TA, esc = TA.esc;
    opts = opts || {};
    const api = opts.api || ((p) => TA.api(p));
    const id = "sp" + (++uid);
    const phone = () => typeof matchMedia === "function" && matchMedia("(max-width: 700px)").matches;
    const st = {ticker: "", r: null, stock: null, stockError: null, news: null, tab: "overview", fin: "quarterly", finIdx: null, shIdx: 0,
                exchange: opts.exchange === "BSE" ? "BSE" : "NSE", open: {}};
    let seq = 0, stockSeq = 0, chartCtl = null, dead = false, root = null;

    const price = () => {
      const p = st.stock && st.stock.price;
      if(p && isNum(p.last)) return p.last;
      return st.exchange === "NSE" && st.r && isNum(st.r.price) ? st.r.price : null;   // the lookup's price is the NSE one
    };
    const isOpen = (sec, dflt) => Object.prototype.hasOwnProperty.call(st.open, sec) ? st.open[sec] : dflt;
    const sec = (key, title, body, dflt, hint) => `<details class="sp-sec" data-sec="${key}"${isOpen(key, dflt) ? " open" : ""}><summary><span>${title}</span>${hint ? `<span class="sub sp-hint">${hint}</span>` : ""}</summary><div class="sp-sec-body">${body}</div></details>`;
    const kv = (label, value, cls) => `<div class="sp-kvi"><div class="label">${esc(label)}</div><div class="sp-v ${cls || ""}">${esc(value)}</div></div>`;
    const loading = (what) => `<div class="sub">Loading ${esc(what)}…</div>`;

    // ---- header ----
    function headHtml(){
      const r = st.r, d = st.stock, p = d && d.price, last = price();
      const name = (r && r.name) || (d && d.name) || st.ticker;
      const chg = p && isNum(p.change) ? p.change : null, chgPct = p && isNum(p.change_pct) ? p.change_pct : null;
      const cls = tone(chg);
      const exch = ["NSE", "BSE"].map(x => `<button type="button" data-exch="${x}" aria-pressed="${x === st.exchange}" class="${x === st.exchange ? "on" : ""}">${x}</button>`).join("");
      return `<div class="sp-name">${esc(name)}</div>
        ${r && r.matched_from ? `<div class="sub" style="font-size:12px">Matched "${esc(r.matched_from)}" to ${esc(st.ticker)}</div>` : ""}
        <div class="sp-code row" style="gap:8px"><span class="sp-codetxt">${esc(st.ticker)} · ${st.exchange}</span>
          <span class="seg sp-exch" role="group" aria-label="Exchange">${exch}</span>
          ${r && r.band ? `<span class="pill ${r.band_skip ? "bad" : ""}" title="${esc(r.band_note || "NSE daily price band")}" style="font-size:11px">Band ${esc(bandText(r.band))}</span>` : ""}
          ${r && r.momentum ? `<span class="pill ${r.momentum.verdict === "strong" ? "ok" : r.momentum.verdict === "weak" ? "bad" : ""}">${esc(r.momentum.verdict || "n/a")} momentum</span>` : ""}</div>
        <div class="sp-price row" style="gap:10px;align-items:baseline"><span class="sp-px">${rupees(last)}</span>
          <span class="sp-chg ${cls}">${chg != null ? `${signedRupees(chg)} (${signedPercent(chgPct)})` : (st.stock || st.stockError ? "" : "…")}</span>
          ${chg != null ? `<span class="sub">1D</span>` : ""}</div>
        ${st.stockError && !(p && isNum(p.last)) ? `<div class="sub" style="font-size:12px">Price figures unavailable: ${esc(st.stockError)}</div>` : ""}
        <div class="sp-actions">${actionsHtml()}</div>`;
    }
    function actionsHtml(){
      return `<button type="button" class="small" data-practice="sell">Practice sell</button><button type="button" class="small primary" data-practice="buy">Practice buy</button>`;
    }
    function paintHead(){ const e = root.querySelector(".sp-head"); if(e) e.innerHTML = headHtml(); }

    // ---- holding ----
    function holdingHtml(){
      const r = st.r; if(!r) return "";
      const h = r.position || (opts.holding ? opts.holding(st.ticker) : null);
      const f = holdingFigures(h, price());
      if(!f) return "";
      return `<div class="sp-hold card-inset"><h3>Your holding</h3><div class="sp-kv">
        ${kv("Shares", String(f.qty))}${kv("Average price", rupees(f.avg))}${kv("Value", rupees(f.value))}
        ${kv("Profit / loss", signedRupees(f.pl), tone(f.pl))}${kv("Return", signedPercent(f.plPct), tone(f.plPct))}
        ${f.stop != null ? kv("Stop", rupees(f.stop) + (f.stopLabel ? " (" + f.stopLabel + ")" : "")) : (f.stopType === "none" ? kv("Stop", "none set") : "")}</div></div>`;
    }
    function paintHolding(){ const e = root.querySelector(".sp-holdbox"); if(e) e.innerHTML = holdingHtml(); }

    // ---- overview sections ----
    function insightsHtml(){
      const r = st.r; if(!r) return loading("insights");
      const ctx = opts.context ? opts.context() : undefined;
      const t = TA.lookupTakeaway(r, undefined, ctx || {});
      return t ? `<div class="callout"><b style="color:var(--color-accent)">What this means.</b> ${t}</div><div class="sub" style="font-size:12px;margin-top:6px">Rules-based reading, not advice.</div>` : `<div class="sub">No reading available.</div>`;
    }
    function rangeRow(left, right, lo, hi, last, aria){
      const pos = markerPos(last, lo, hi);
      return `<div class="sp-rng"><div class="sp-rl"><span class="sub">${esc(left)}</span><span class="sub">${esc(right)}</span></div>
        <div class="qtrack" role="img" aria-label="${esc(aria)}${pos != null ? ", price is " + Math.round(pos * 100) + "% of the way up" : ""}"><span class="qdot" style="left:${pos == null ? 50 : (pos * 100).toFixed(1)}%${pos == null ? ";display:none" : ""}"></span></div>
        <div class="sp-rl"><b>${esc(rupees(lo))}</b><b>${esc(rupees(hi))}</b></div></div>`;
    }
    function performanceHtml(){
      const d = st.stock; if(!d) return st.stockError ? `<div class="sub">Unavailable: ${esc(st.stockError)}</div>` : loading("figures");
      const p = d.price || {}, w = d.week52 || {}, last = price();
      return rangeRow("Today's low", "Today's high", p.day_low, p.day_high, last, "Today's range")
        + rangeRow("52 week low", "52 week high", w.low, w.high, last, "52-week range")
        + `<div class="sp-kv">${kv("Open", rupees(p.open))}${kv("Prev. close", rupees(p.prev_close))}${kv("Volume", volume(p.volume))}
          ${kv("Lower circuit", circuitText(d.circuit, "lower"))}${kv("Upper circuit", circuitText(d.circuit, "upper"))}</div>
          <div class="sub" style="font-size:12px">${d.circuit && d.circuit.state === "band" ? "Circuit limits: " + esc(d.circuit.band) + " NSE price band applied to the previous close." : d.circuit && d.circuit.state === "none" ? "This stock has no NSE price band." : "NSE price band not known for this stock."}</div>`;
    }
    function fundamentalsHtml(){
      const d = st.stock; if(!d) return st.stockError ? `<div class="sub">Unavailable: ${esc(st.stockError)}</div>` : loading("fundamentals");
      const f = d.fundamentals; if(!f) return `<div class="sub">Fundamentals unavailable.</div>`;
      const peers = d.industry_pe_n ? ` (median of ${d.industry_pe_n} peers)` : "";
      return `<div class="sp-kv sp-kv2">${kv("Mkt cap", f.market_cap_cr == null ? "n/a" : crore(f.market_cap_cr))}${kv("ROE", percent(f.roe))}
        ${kv("P/E (TTM)", ratio(f.pe))}${kv("EPS (TTM)", rupees(f.eps))}${kv("P/B", ratio(f.pb))}${kv("Div yield", percent(f.dividend_yield))}
        ${kv("Industry P/E", ratio(f.industry_pe))}${kv("Book value", rupees(f.book_value))}
        ${kv("Debt to equity", ratio(f.debt_to_equity))}${kv("Face value", rupees(f.face_value))}</div>
        <div class="sub" style="font-size:12px">Figures from ${esc(d.source || "Yahoo Finance")}${peers ? "; industry P/E is the median over same-industry peers we have saved" + esc(peers) : ""}.</div>`;
    }
    function financialHtml(){
      const d = st.stock; if(!d) return st.stockError ? `<div class="sub">Unavailable: ${esc(st.stockError)}</div>` : loading("financials");
      const fin = d.financials || {}, q = fin.quarterly || [], y = fin.yearly || [];
      let kind = st.fin; if(!(fin[kind] || []).length) kind = q.length ? "quarterly" : "yearly";
      const periods = fin[kind] || [];
      const chips = ["quarterly", "yearly"].map(k => `<button type="button" class="ck-chip" data-fin-kind="${k}" aria-pressed="${k === kind}">${k === "quarterly" ? "Quarterly" : "Yearly"}</button>`).join("");
      if(!periods.length) return `<div class="ck-chips" role="group" aria-label="Period">${chips}</div><div class="sub" style="margin-top:6px">Yahoo returned no income statements for this stock.</div>${growthHtml(fin.growth)}`;
      const v = periodView(periods, st.finKind === kind ? st.finIdx : null), bars = barLayout(periods, 84);
      const n = periods.length, gw = 300 / Math.max(n, 1), bw = Math.min(26, gw * 0.3), base = 96;
      const svg = bars.map((b, i) => {
        const x = i * gw + gw / 2, sel = i === v.index;
        return `<g class="sp-grp${sel ? " sel" : ""}" data-fin-idx="${i}" role="button" tabindex="0" aria-label="${esc(b.label)}: revenue ${esc(crore(periods[i].revenue_cr))}, profit ${esc(crore(periods[i].profit_cr))}" aria-pressed="${sel}">
          <rect class="sp-hit" x="${(x - gw / 2).toFixed(1)}" y="0" width="${gw.toFixed(1)}" height="132"></rect>
          <rect class="sp-bar-rev" x="${(x - bw - 1).toFixed(1)}" y="${(base - b.revenue).toFixed(1)}" width="${bw.toFixed(1)}" height="${b.revenue.toFixed(1)}" rx="2"></rect>
          <rect class="${b.loss ? "sp-bar-loss" : "sp-bar-profit"}" x="${(x + 1).toFixed(1)}" y="${(b.loss ? base : base - b.profit).toFixed(1)}" width="${bw.toFixed(1)}" height="${b.profit.toFixed(1)}" rx="2"></rect>
          <text class="sp-axis" x="${x.toFixed(1)}" y="126" text-anchor="middle">${esc(b.label)}</text></g>`;
      }).join("");
      const cur = v.cur, prev = v.prev;
      const line = (name, val, ch) => `<div class="sp-fin-i"><div class="label">${name}</div><div class="sp-v">${esc(crore(val))}</div><div class="${tone(ch)}" style="font-size:12px">${ch == null ? "" : esc(signedPercent(ch, 1)) + " vs " + esc(prev ? prev.label : "")}</div></div>`;
      return `<div class="ck-chips" role="group" aria-label="Period">${chips}</div>
        <svg class="sp-fin" viewBox="0 0 300 132" role="group" aria-label="Revenue and profit, last ${n} ${kind === "quarterly" ? "quarters" : "years"}">${svg}</svg>
        <div class="sp-legend sub"><span><i class="sp-sw sp-sw-rev"></i>Revenue</span><span><i class="sp-sw sp-sw-profit"></i>Profit</span><span>Tap a bar for that period</span></div>
        <div class="sp-fin-now"><div class="sub">${esc(cur.label)}</div><div class="sp-fin-row">${line("Revenue", cur.revenue_cr, cur.revenue_change)}${line("Profit", cur.profit_cr, cur.profit_change)}</div></div>
        ${growthHtml(fin.growth)}`;
    }
    function growthHtml(g){
      if(!g) return "";
      const cell = (v) => `<td class="num ${tone(v)}">${esc(signedPercent(v, 1))}</td>`;
      return `<table class="sp-growth"><caption class="sr-only">Growth</caption><thead><tr><th>Growth</th><th class="num">1Y (TTM)</th><th class="num">3Y CAGR</th></tr></thead>
        <tbody><tr><td>Revenue</td>${cell((g.revenue || {}).y1)}${cell((g.revenue || {}).cagr3)}</tr><tr><td>Profit</td>${cell((g.profit || {}).y1)}${cell((g.profit || {}).cagr3)}</tr></tbody></table>`;
    }
    function aboutHtml(){
      const d = st.stock; if(!d) return st.stockError ? `<div class="sub">Unavailable: ${esc(st.stockError)}</div>` : loading("company details");
      const a = d.about || {};
      if(!a.summary && !a.sector && !a.industry && !a.website) return `<div class="sub">No company profile from Yahoo.</div>`;
      return `${a.summary ? `<p class="sp-about">${esc(a.summary)}</p>` : ""}<div class="sp-kv">${kv("Sector", a.sector || "n/a")}${kv("Industry", a.industry || a.nse_industry || "n/a")}${kv("Website", a.website || "n/a")}</div>`;
    }
    function shareholdingHtml(){
      const d = st.stock; if(!d) return st.stockError ? `<div class="sub">Unavailable: ${esc(st.stockError)}</div>` : loading("shareholding");
      const s = d.shareholding || {};
      if(s.state !== "ok" || !(s.quarters || []).length) return `<div class="sub">${esc(s.message || "unavailable from NSE right now")}</div>`;
      const i = Math.min(st.shIdx, s.quarters.length - 1), q = s.quarters[i];
      const chips = s.quarters.map((x, k) => `<button type="button" class="ck-chip" data-sh-idx="${k}" aria-pressed="${k === i}">${esc(x.label)}</button>`).join("");
      const bar = (label, v) => `<div class="sp-sh"><div class="sp-shl"><span>${label}</span><b>${isNum(v) ? v.toFixed(2) + "%" : "n/a"}</b></div><div class="sp-shbar" role="img" aria-label="${label} ${isNum(v) ? v.toFixed(2) + "%" : "not available"}"><span style="width:${isNum(v) ? Math.min(100, Math.max(0, v)).toFixed(1) : 0}%"></span></div></div>`;
      return `<div class="ck-chips" role="group" aria-label="Quarter">${chips}</div><div style="margin-top:8px">${bar("Promoters", q.promoters)}${bar("DIIs", q.dii)}${bar("Public", q.public)}${bar("FIIs", q.fii)}</div>
        <div class="sub" style="font-size:12px">From NSE's shareholding filing for the quarter ended ${esc(q.date)}. A category the filing summary does not carry shows n/a.</div>`;
    }
    function similarHtml(){
      const d = st.stock; if(!d) return st.stockError ? `<div class="sub">Unavailable: ${esc(st.stockError)}</div>` : loading("similar stocks");
      const rows = d.similar || [];
      if(!rows.length) return `<div class="sub">No same-industry stocks found in the index lists.</div>`;
      return `<div class="sp-sim">${rows.map(x => `<button type="button" class="sp-simrow" data-open="${esc(x.symbol)}" aria-label="Open ${esc(x.symbol)}"><span class="sp-simname"><b>${esc(x.symbol)}</b><span class="sub">${esc(x.name || "")}</span></span><span class="sp-simpx">${esc(rupees(x.price))}</span><span class="sp-simchg ${tone(x.change_pct)}">${esc(signedPercent(x.change_pct))}</span></button>`).join("")}</div>`;
    }
    function overviewHtml(){
      const fund = !!(st.stock && st.stock.fund);
      const dflt = !phone();
      return sec("insights", "Insights", insightsHtml(), dflt)
        + sec("performance", "Performance", performanceHtml(), true)
        + (fund ? "" : sec("fundamentals", "Fundamentals", fundamentalsHtml(), true))
        + (fund ? "" : sec("financials", "Financial performance", financialHtml(), true))
        + sec("about", "About company", aboutHtml(), false)
        + (fund ? "" : sec("shareholding", "Shareholding pattern", shareholdingHtml(), true))
        + (fund ? "" : sec("similar", "Similar stocks", similarHtml(), true));
    }
    function paintOverview(){ const e = root.querySelector('[data-panel="overview"]'); if(e) e.innerHTML = overviewHtml(); }

    // ---- technicals ----
    function technicalsHtml(){
      const r = st.r; if(!r) return loading("technicals");
      const m = r.momentum || {}, d = st.stock, t = d && d.technicals;
      const cls = m.verdict === "strong" ? "ok" : m.verdict === "weak" ? "bad" : "";
      const row = (label, value, note) => `<tr><td>${esc(label)}</td><td class="num">${esc(value)}</td><td class="sub">${esc(note || "")}</td></tr>`;
      let table = "";
      if(!d) table = st.stockError ? `<div class="sub">Indicator values unavailable: ${esc(st.stockError)}</div>` : loading("indicator values");
      else if(!t || !t.available) table = `<div class="sub">Not enough daily price history for the indicators.</div>`;
      else table = `<div class="scroll"><table class="sp-tech"><thead><tr><th>Indicator</th><th class="num">Value</th><th>Reading</th></tr></thead><tbody>
          ${row("RSI (14)", ratio(t.rsi14, 1), t.rsi_zone ? "Daily RSI is " + t.rsi_zone : "")}
          ${row("MACD (12, 26)", ratio(t.macd), t.macd != null && t.macd_signal != null ? (t.macd >= t.macd_signal ? "Above its signal line" : "Below its signal line") : "")}
          ${row("MACD signal (9)", ratio(t.macd_signal))}${row("MACD histogram", ratio(t.macd_hist), t.macd_hist != null ? (t.macd_hist >= 0 ? "Positive" : "Negative") : "")}
          ${row("ADX (14)", ratio(t.adx, 1), t.adx_reading || "")}${row("+DI / −DI", ratio(t.plus_di, 1) + " / " + ratio(t.minus_di, 1))}
          ${row("50-day average", rupees(t.ma50), isNum(t.ma50) && isNum(t.close) ? (t.close >= t.ma50 ? "Price above it" : "Price below it") : "")}
          ${row("200-day average", rupees(t.ma200), isNum(t.ma200) && isNum(t.close) ? (t.close >= t.ma200 ? "Price above it" : "Price below it") : "")}
          </tbody></table></div><div class="sub" style="font-size:12px">Daily bars up to ${esc(t.as_of || "")}.</div>`;
      const mom = [["6 months", m.ret_6m], ["12 months (less the latest month)", m.ret_12_1], ["From 52-week high", m.pct_from_52w_high]].filter(x => x[1] != null)
        .map(x => `<div class="sp-kvi"><div class="label">${esc(x[0])}</div><div class="sp-v ${tone(x[1])}">${esc(signedPercent(x[1], 1))}</div></div>`).join("");
      return `<div class="row" style="gap:8px"><span class="pill ${cls}">${esc(m.verdict || "n/a")} momentum</span>${m.above_200dma === false ? `<span class="pill bad">below 200-day average</span>` : m.above_200dma ? `<span class="pill">above 200-day average</span>` : ""}</div>
        <p class="sp-about">${esc(r.momentum_summary || "")}</p>${mom ? `<div class="sp-kv">${mom}</div>` : ""}
        <h3 style="margin:12px 0 6px">Indicators</h3>${table}
        <div class="sub" style="font-size:12px;margin-top:6px">Switch the chart to Candles to overlay EMA, Bollinger bands, RSI, MACD and volume (daily ranges).</div>`;
    }
    function paintTechnicals(){ const e = root.querySelector('[data-panel="technicals"]'); if(e) e.innerHTML = technicalsHtml(); }

    // ---- news ----
    function newsHtml(n){
      if(!n) return `<div class="sub" style="margin-top:6px">Loading news…</div>`;
      if(n.demo) return `<div class="sub">News is not used in the offline sample.</div>`;
      const items = n.items || [];
      const istStamp = (iso) => { const d = new Date(iso); return isNaN(d) ? "" : d.toLocaleString("en-IN", {timeZone: "Asia/Kolkata", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hour12: false}) + " IST"; };
      const rows = items.map(i => { const s = SENT[i.sentiment], href = safeUrl(i.link);
        const chip = s ? `<span class="pill ${s[0]}" title="${esc((i.event || "").replace(/_/g, " "))}, ${esc(i.confidence || "")} confidence">${s[1]}</span> ` : "";
        return `<div class="ann"><div class="meta">${esc(istStamp(i.published))} · ${esc(i.source)}${i.event ? " · " + esc(i.event.replace(/_/g, " ")) : ""}</div><div>${chip}${href ? `<a href="${esc(href)}" target="_blank" rel="noopener noreferrer">${esc(i.title)}</a>` : esc(i.title)}</div></div>`; }).join("");
      const errs = (n.errors || []).length ? `<div class="sub" style="font-size:12px">Some sources failed: ${esc(n.errors.join("; "))}</div>` : "";
      const note = !items.length ? "" : n.tagger === "none" ? "Headlines are not labelled: start Ollama or set NEWS_TAGGER (see the README)."
        : TA.newsNeedsLabels(n) ? (n.tagger && n.tagger !== "pending" ? `Labelling with ${n.tagger}…` : "Labelling headlines…")
        : "Labels come from a language model and can be wrong.";
      return `<h3 style="margin-top:6px">News, last 7 days</h3>${rows || `<div class="sub">none found</div>`}${errs}${note ? `<div class="sub" style="font-size:12px">${esc(note)}</div>` : ""}`;
    }
    function newsPanelHtml(){
      const r = st.r; if(!r) return loading("news");
      const anns = r.announcements && r.announcements.length
        ? r.announcements.map(a => `<div class="ann"><div class="meta">${esc(a.at)} · ${esc(a.category)}</div><div>${a.file && safeUrl(a.file) ? `<a href="${esc(a.file)}" target="_blank" rel="noopener noreferrer">${esc(a.text || a.category)}</a>` : esc(a.text || a.category)}</div></div>`).join("")
        : `<div class="sub">${esc(r.announcements_error || "none found")}</div>`;
      return `<h3 style="margin-top:6px">NSE announcements</h3>${anns}<div class="sp-news">${newsHtml(st.news)}</div>`;
    }
    function paintNews(){ const e = root.querySelector('[data-panel="news"]'); if(e) e.innerHTML = newsPanelHtml(); }

    function pollNews(r, mySeq, startedAt){
      if(TA.newsPollNext(r.news, startedAt, Date.now(), mySeq === seq && !dead) !== "poll") return;
      setTimeout(async () => {
        if(mySeq !== seq || dead) return;
        try {
          const n = await api("/api/news?ticker=" + encodeURIComponent(r.ticker));
          if(mySeq !== seq || dead) return;
          r.news = n; st.news = n; paintNews(); paintOverview();
        } catch(e){ return; }
        pollNews(r, mySeq, startedAt);
      }, TA.NEWS_POLL_MS);
    }

    // ---- tabs, shell ----
    function tabsHtml(){
      return `<div class="sp-tabs" role="tablist" aria-label="Stock sections">${TABS.map(([k, l]) =>
        `<button type="button" role="tab" id="${id}-t-${k}" aria-controls="${id}-p-${k}" aria-selected="${k === st.tab}" tabindex="${k === st.tab ? 0 : -1}" data-tab="${k}">${l}</button>`).join("")}</div>`
        + TABS.map(([k]) => `<div class="sp-panel" role="tabpanel" id="${id}-p-${k}" aria-labelledby="${id}-t-${k}" data-panel="${k}"${k === st.tab ? "" : " hidden"}></div>`).join("");
    }
    function showTab(k){
      st.tab = k;
      root.querySelectorAll("[data-tab]").forEach(b => { const on = b.dataset.tab === k; b.setAttribute("aria-selected", on ? "true" : "false"); b.tabIndex = on ? 0 : -1; });
      root.querySelectorAll("[data-panel]").forEach(p => { p.hidden = p.dataset.panel !== k; });
    }
    function shell(){
      root.innerHTML = `<div class="sp-head"></div><div class="sp-chartbox"></div><div class="sp-holdbox"></div>${tabsHtml()}
        <div class="sp-bar" role="group" aria-label="Practice orders (practice account only)"><div class="sp-bar-note sub">Practice account only</div><div class="sp-bar-btns">${actionsHtml()}</div></div>`;
      paintHead(); paintHolding(); paintOverview(); paintTechnicals(); paintNews();
    }

    // ---- chart ----
    function mountChart(r, mySeq){
      const box = root.querySelector(".sp-chartbox"); if(!box) return;
      if(chartCtl){ chartCtl.destroy(); chartCtl = null; }
      box.innerHTML = `<div class="chart lk-chart" data-ck></div>`;
      const ck = box.firstChild, hist = r.history || [], pos = r.position, refs = [];
      if(pos){ refs.push({y: pos.avg_entry_price, label: "Your cost"}); if(pos.stop != null) refs.push({y: pos.stop, label: "Stop", color: TA.C.neg}); }
      const drawLine = (why) => {   // the plain SVG line chart: when the library is missing or the candle data fails
        if(mySeq !== seq || dead || !ck.isConnected) return;
        if(chartCtl){ chartCtl.destroy(); chartCtl = null; }
        ck.innerHTML = "";
        TA.lineChart(ck, {x: hist.map(p => p.d), height: 170, left: 52, endLabels: false, legend: true, refs,
          yFmt: v => TA.sym() + Math.round(v).toLocaleString("en-IN"), label: `${r.ticker} price, last year`,
          empty: r.history_error ? "Price history unavailable: " + r.history_error : "No price history.",
          series: [{name: "Price", color: TA.C.s1, values: hist.map(p => p.c)}, {name: "200-day average", color: TA.C.ctx, width: 1.5, values: hist.map(p => p.ma200)}]});
      };
      try {
        chartCtl = TA.stockChart(ck, {ticker: r.ticker, isCurrent: () => mySeq === seq && !dead, fallback: drawLine, exchange: st.exchange, exchangeChips: false,
          fetchCandles: (range, exch) => api("/api/candles?ticker=" + encodeURIComponent(st.ticker) + "&range=" + encodeURIComponent(range) + "&exchange=" + encodeURIComponent(exch || "NSE"))});
      } catch(e){ drawLine(e.message); }
    }

    // ---- stock figures (the second request) ----
    async function loadStock(mySeq){
      const my = ++stockSeq;
      st.stock = null; st.stockError = null; st.finIdx = null; paintHead(); paintOverview(); paintTechnicals();
      try {
        const d = await api("/api/stock?ticker=" + encodeURIComponent(st.ticker) + "&exchange=" + st.exchange);
        if(mySeq !== seq || my !== stockSeq || dead) return;
        st.stock = d; st.stockError = d.error || null;
        if(d.financials && !(d.financials[st.fin] || []).length) st.fin = (d.financials.quarterly || []).length ? "quarterly" : "yearly";
      } catch(e){
        if(mySeq !== seq || my !== stockSeq || dead) return;
        st.stockError = e.message || "request failed";
      }
      paintHead(); paintHolding(); paintOverview(); paintTechnicals();
    }

    // ---- public ----
    async function load(t){
      const my = ++seq; stockSeq++;
      if(chartCtl){ chartCtl.destroy(); chartCtl = null; }   // chart.remove() before the page is redrawn
      st.ticker = String(t || "").trim().toUpperCase(); st.r = null; st.stock = null; st.stockError = null; st.news = null; st.tab = "overview";
      st.exchange = "NSE";   // a new stock starts on NSE (a ".BO" code on BSE); the switch is per stock
      if(/\.BO$/i.test(st.ticker)){ st.exchange = "BSE"; st.ticker = st.ticker.replace(/\.BO$/i, ""); }
      root.innerHTML = `<div class="sub">Looking up ${esc(st.ticker)}…</div>`;
      let r;
      try { r = await api("/api/lookup?ticker=" + encodeURIComponent(t)); }
      catch(e){ if(my === seq && !dead) root.innerHTML = `<span class="sub">${esc(e.message)}</span>`; return; }
      if(my !== seq || dead) return;
      st.r = r; st.ticker = String(r.ticker || st.ticker).toUpperCase().replace(/\.BO$/i, "");
      if(/\.BO$/i.test(String(r.ticker || ""))) st.exchange = "BSE";
      shell(); mountChart(r, my);
      if(opts.onLoaded) { try { opts.onLoaded(r); } catch(e){} }
      api("/api/news?ticker=" + encodeURIComponent(st.ticker)).then(n => {
        if(my !== seq || dead) return;
        r.news = n; st.news = n; paintNews(); paintOverview(); paintTechnicals(); pollNews(r, my, Date.now());
      }).catch(e => { if(my === seq && !dead){ st.news = {items: [], errors: ["News unavailable: " + e.message], tagger: "none"}; paintNews(); } });
      await loadStock(my);
    }
    function setExchange(x){
      x = x === "BSE" ? "BSE" : "NSE";
      if(dead || !st.r || x === st.exchange) return;
      st.exchange = x;
      if(chartCtl) chartCtl.setExchange(x);
      loadStock(seq);
    }

    // ---- events (one set of listeners on the root; nothing global) ----
    function onClick(ev){
      const t = ev.target.closest && ev.target.closest("button, [data-fin-idx]"); if(!t || !root.contains(t)) return;
      if(t.dataset.exch){ setExchange(t.dataset.exch); return; }
      if(t.dataset.tab){ showTab(t.dataset.tab); return; }
      if(t.dataset.practice){ if(opts.onPractice) opts.onPractice(t.dataset.practice, st.ticker); return; }
      if(t.dataset.finKind){ st.fin = t.dataset.finKind; st.finIdx = null; paintOverview(); return; }
      if(t.dataset.finIdx !== undefined){ st.finIdx = Number(t.dataset.finIdx); st.finKind = st.fin; paintOverview(); return; }
      if(t.dataset.shIdx !== undefined){ st.shIdx = Number(t.dataset.shIdx); paintOverview(); return; }
      if(t.dataset.open){ if(opts.onOpen) opts.onOpen(t.dataset.open); else load(t.dataset.open); }
    }
    function onKey(ev){
      const t = ev.target;
      if(t && t.dataset && t.dataset.finIdx !== undefined && (ev.key === "Enter" || ev.key === " ")){ ev.preventDefault(); t.dispatchEvent(new MouseEvent("click", {bubbles: true})); return; }
      if(t && t.dataset && t.dataset.tab && (ev.key === "ArrowRight" || ev.key === "ArrowLeft")){
        const keys = TABS.map(x => x[0]), i = keys.indexOf(t.dataset.tab), n = keys[(i + (ev.key === "ArrowRight" ? 1 : keys.length - 1)) % keys.length];
        ev.preventDefault(); showTab(n); const b = root.querySelector(`[data-tab="${n}"]`); if(b) b.focus();
      }
    }
    function onToggle(ev){ const d = ev.target; if(d && d.matches && d.matches("details.sp-sec") && d.dataset.sec) st.open[d.dataset.sec] = d.open; }

    root = document.createElement("div");
    root.className = "sp";
    root.addEventListener("click", onClick);
    root.addEventListener("keydown", onKey);
    root.addEventListener("toggle", onToggle, true);
    host.innerHTML = "";
    host.appendChild(root);
    if(ticker) load(ticker);
    return {
      load, setExchange,
      destroy(){ dead = true; seq++; if(chartCtl){ chartCtl.destroy(); chartCtl = null; } root.removeEventListener("click", onClick); root.removeEventListener("keydown", onKey); root.removeEventListener("toggle", onToggle, true); if(root.parentNode) root.parentNode.removeChild(root); },
      get ticker(){ return st.ticker; }, get exchange(){ return st.exchange; }, get element(){ return root; }
    };
  }

  const exported = {isNum, rupees, signedRupees, percent, signedPercent, ratio, crore, volume, tone, markerPos, circuitText, barLayout, periodView, holdingFigures};
  stockPage.helpers = exported;
  if(typeof window !== "undefined" && window.TA) window.TA.stockPage = stockPage;
  if(typeof module !== "undefined" && module.exports) module.exports = stockPage;
})();
