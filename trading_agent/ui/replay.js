(function(){
  const {$, esc, pct, toast, api, tile, C, lineChart, rupeesShort, inr, sinr, spct, lookupTakeaway, attachSuggest, shortDate, factorTakeaway, signalTakeaway, signalRows} = TA;
  let T = null, slug = null, side = "buy";
  const WHO = {you: "You", agent: "Agent (rules)", nifty: "Nifty"};
  const tone = v => v == null ? "" : v > 0 ? "pl-profit" : v < 0 ? "pl-loss" : "";
  const fmtDay = d => new Date(d + "T00:00:00").toLocaleDateString("en-IN", {weekday: "short", day: "numeric", month: "short", year: "numeric"});
  const fall = v => v != null && v < 0 ? "worst fall " + spct(v) : "no fall yet";

  async function waitJob(id, statusEl){
    let failures = 0;
    for(;;){
      let j;
      try{
        j = await api(`/replay/api/job/${id}`);
        failures = 0;
      }catch(e){
        if(++failures >= 5){
          toast("Lost contact with the dashboard; reload the page");
          return {ok: false, finished_at: "lost", message: "Lost contact with the dashboard; reload the page"};
        }
        await new Promise(r => setTimeout(r, 2000));
        continue;
      }
      if(statusEl) statusEl.textContent = j.message || "Working…";
      if(j.finished_at) return j;
      await new Promise(r => setTimeout(r, 1000));
    }
  }

  // ---- home ----
  async function loadHome(){
    const meta = await api("/replay/api/meta");
    $("n-start").min = meta.earliest; $("n-start").max = meta.today; $("n-earliest").textContent = fmtDay(meta.earliest);
    $("n-universe").innerHTML = meta.universes.map(u => `<option value="${esc(u)}" ${u === "NIFTYMIDCAP150" ? "selected" : ""}>${esc(u)}</option>`).join("");
    const rows = await api("/replay/api/trials");
    $("trials").innerHTML = rows.length ? rows.map(t => `<tr>
        <td><a href="#${esc(t.slug)}" data-open="${esc(t.slug)}" style="font-weight:600;text-decoration:none">${esc(t.name)}</a>${t.ended ? ' <span class="pill">ended</span>' : ""}<div class="sub" style="font-size:12px">${esc(t.universe)}</div></td>
        <td>${esc(shortDate(t.start))}</td><td>${esc(shortDate(t.clock))}</td>
        <td class="num ${tone(t.you)}">${t.you == null ? "n/a" : spct(t.you)}</td><td class="num ${tone(t.agent)}">${t.agent == null ? "n/a" : spct(t.agent)}</td><td class="num ${tone(t.nifty)}">${t.nifty == null ? "n/a" : spct(t.nifty)}</td>
        <td><button type="button" class="small" data-open="${esc(t.slug)}">Open</button></td></tr>`).join("")
      : `<tr><td colspan="7" class="empty">No replays yet. Start one below.</td></tr>`;
  }
  function show(which){
    $("home").hidden = which !== "home"; $("trial").hidden = which !== "trial"; $("banner").hidden = which !== "trial";
  }
  $("new-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const body = Object.fromEntries(new FormData(ev.target).entries());
    $("n-submit").disabled = true; $("n-status").textContent = "Starting…";
    try {
      const j = await api("/replay/api/trials", body);
      if(j.ok === false){ toast(j.message); return; }
      const done = await waitJob(j.id, $("n-status"));
      if(!done.ok){ toast(done.message); return; }
      location.hash = done.result.slug;
    } catch(e){ toast(e.message); }
    finally { $("n-submit").disabled = false; }
  });

  // ---- one replay ----
  async function open(s){
    slug = s; show("trial");
    let r;
    try { r = await fetch(`/replay/api/trial/${encodeURIComponent(s)}`); }
    catch(e){ toast(e.message); return; }
    const body = await r.json().catch(() => ({}));
    if(r.status === 404){ toast(body.error || "No such replay"); location.hash = ""; return; }
    if(!r.ok){ toast(body.error || "Could not load this replay"); if(!T) $("step-status").textContent = body.error || ""; return; }
    T = body; render(); loadTools();
  }
  function render(){
    const t = T.trial, ended = !!t.ended;
    $("banner").innerHTML = `Replay · ${esc(fmtDay(t.clock))}${ended ? " · ended" : ""} <span class="sub">${esc(t.name)} · ${esc(t.universe)} vs ${esc(t.benchmark)} · started ${esc(fmtDay(t.start))} · dividends ${t.dividends === "cash" ? "taken as cash" : "reinvested"}</span>`;
    document.querySelectorAll("[data-step], #b-end, #ask-btn, #ro-form button, #ro-form input").forEach(b => b.disabled = ended);
    $("step-status").textContent = `${T.race.dates.length - 1} trading days since the start`;
    $("ro-date").textContent = fmtDay(t.clock); $("ro-autostop").checked = !!t.auto_stop;
    $("race-tiles").innerHTML = ["you", "agent", "nifty"].map(w => tile(WHO[w] + (w === "nifty" ? ` (${esc(t.benchmark)})` : ""),
      inr(T.tiles[w].value), `<span class="${tone(T.tiles[w].return)}">${spct(T.tiles[w].return)}</span> · ${fall(T.tiles[w].worst_fall)}`)).join("");
    $("race-sub").textContent = `${inr(t.cash)} each on ${shortDate(t.start)}`;
    lineChart($("race-chart"), {x: T.race.dates, height: 240, left: 64, legend: true, endLabels: false, yFmt: rupeesShort,
      label: "Replay race: you, the agent and the Nifty fund", empty: "The race chart starts after the first step.",
      refs: [{y: t.cash, label: "Start"}],
      series: [{name: "You", color: C.s1, values: T.race.you}, {name: "Agent", color: C.s2, values: T.race.agent},
               {name: t.benchmark, color: C.s3, values: T.race.nifty}]});
    renderYou(); renderPicks(); renderClaude(); renderScore();
  }
  function renderYou(){
    const ps = T.you.positions;
    const inv = ps.reduce((a, p) => a + p.qty * p.avg_entry_price, 0), val = ps.reduce((a, p) => a + (p.market_value || 0), 0);
    const pl = val - inv, plp = inv ? pl / inv : null;
    const stat = (k, v, sub, cls) => `<div class="mkt"><span class="k">${k}</span><span class="v ${cls || ""}">${v}</span><span class="sub" style="font-size:12px">${sub}</span></div>`;
    $("yp-stats").innerHTML = stat("Invested", inr(inv), ps.length ? `in ${ps.length} stock${ps.length === 1 ? "" : "s"}` : "nothing bought yet")
      + stat("Value now", inr(val), "at the replay day's close") + stat("Profit / loss", sinr(pl), plp == null ? "on open positions" : spct(plp) + " on open positions", tone(pl))
      + stat("Account total", inr(T.you.equity), `cash ${inr(T.you.cash)}`, tone(T.tiles.you.return));
    $("yp-sub").textContent = `as of ${shortDate(T.trial.clock)}`;
    $("yp-rows").innerHTML = ps.length ? ps.map(p => { const i = p.qty * p.avg_entry_price, pc = i ? p.unrealized_pl / i : null; return `<tr>
        <td><a href="#" data-lookup="${esc(p.symbol)}" style="font-weight:600;text-decoration:none">${esc(p.symbol)}</a>${p.suspended ? ` <span class="pill warn" style="font-size:10px;padding:1px 6px" title="last traded ${esc(p.last_trade)}">suspended</span>` : ""}</td>
        <td class="num">${p.qty}</td><td class="num">${inr(p.avg_entry_price, 2)}</td><td class="num">${inr(p.current_price, 2)}</td>
        <td class="num">${inr(i)}</td><td class="num">${inr(p.market_value)}</td><td class="num ${tone(p.unrealized_pl)}">${sinr(p.unrealized_pl)}</td>
        <td class="num ${tone(pc)}">${pc == null ? "n/a" : spct(pc)}</td>
        <td class="num ${p.stop != null && p.current_price <= p.stop ? "pl-loss" : ""}">${p.stop != null ? inr(p.stop, 2) : "n/a"}</td>
        <td>${T.trial.ended ? "" : `<button type="button" class="small" data-sell="${esc(p.symbol)}" data-qty="${p.qty}">Sell</button>`}</td></tr>`; }).join("")
      : `<tr><td colspan="10" class="empty">No stocks yet. Look one up, copy one of the agent's picks, or type a ticker below.</td></tr>`;
    $("yp-foot").innerHTML = ps.length ? `<tr><td>Total</td><td></td><td></td><td></td><td class="num">${inr(inv)}</td><td class="num">${inr(val)}</td><td class="num ${tone(pl)}">${sinr(pl)}</td><td class="num ${tone(plp)}">${plp == null ? "n/a" : spct(plp)}</td><td></td><td></td></tr>` : "";
  }
  function renderPicks(){
    const p = T.picks, a = T.agent;
    if(!p){ $("picks").innerHTML = `<div class="sub">No picks yet.</div>`; return; }
    $("pk-sub").textContent = `top ${T.trial.top} by momentum on ${shortDate(p.date)} · next rebalance ${a.next_rebalance}`;
    $("picks").innerHTML = `<div class="scroll"><table><thead><tr><th>Stock</th><th class="num">12-1 mom.</th><th class="num">6 m</th><th>Agent</th><th><span class="sr-only">Copy</span></th></tr></thead><tbody>
      ${p.rows.slice(0, T.trial.top).map(r => `<tr><td><a href="#" data-lookup="${esc(r.symbol)}" style="font-weight:600;text-decoration:none">${esc(r.symbol)}</a>${r.name ? `<div class="sub" style="font-size:12px">${esc(r.name)}</div>` : ""}</td>
        <td class="num">${pct(r.ret_12_1)}</td><td class="num">${pct(r.ret_6m)}</td>
        <td>${p.will_buy.includes(r.symbol) ? '<span class="pill ok">buys next rebalance</span>' : '<span class="pill">holds</span>'}</td>
        <td>${T.trial.ended ? "" : `<button type="button" class="small" data-copy="${esc(r.symbol)}">Copy</button>`}</td></tr>`).join("")}</tbody></table></div>
      ${p.will_sell.length ? `<div class="sub">At the next rebalance the agent sells: ${p.will_sell.map(esc).join(", ")}.</div>` : ""}
      <div class="sub">Agent holds ${a.holdings.length} stock${a.holdings.length === 1 ? "" : "s"}${a.holdings.length ? ": " + a.holdings.map(h => esc(h.symbol)).join(", ") : ""}.</div>`;
  }
  function renderClaude(){
    $("ask-note").textContent = T.claude_ready ? `${T.claude_presses} ask${T.claude_presses === 1 ? "" : "s"} in this replay` : "Needs ANTHROPIC_API_KEY in .env";
    $("ask-btn").disabled = !T.claude_ready || !!T.trial.ended;
    $("ask-result").innerHTML = T.claude.slice().reverse().map(c => `<div class="ann"><div class="meta">${esc(fmtDay(c.date))} · may include hindsight</div><div>${esc(c.summary)}</div>
      ${(c.recommendations || []).map(r => `<div class="row" style="gap:6px;margin-top:4px"><span class="act ${esc(r.action)}">${esc(r.action.toUpperCase())}</span><b>${esc(r.ticker)}</b><span class="pill">${esc(r.confidence)}</span></div><div class="sub">${esc(r.headline)}. ${esc(r.rationale)}</div>`).join("")}</div>`).join("");
  }
  function renderScore(){
    const s = T.scorecard, n = T.next;
    const days = (new Date(T.trial.ended) - new Date(T.trial.start)) / 86400000;
    if(!s){ $("scorecard").innerHTML = ""; return; }
    const col = w => { const x = s[w]; return `<div class="mkt"><span class="k">${WHO[w]}</span><span class="v ${tone(x.return)}">${spct(x.return)}</span>
      <span class="sub" style="font-size:12px">${inr(x.final)} · ${days >= 365 && x.cagr != null ? spct(x.cagr) + " a year · " : ""}${fall(x.max_drawdown)}<br>${x.trades} trade${x.trades === 1 ? "" : "s"} · charges ${inr(x.charges)}${x.dividends ? ` · dividends ${inr(x.dividends)}` : ""}
      ${x.best && w !== "nifty" ? (x.best.symbol !== x.worst.symbol ? `<br>best ${esc(x.best.symbol)} ${sinr(x.best.pnl)} · worst ${esc(x.worst.symbol)} ${sinr(x.worst.pnl)}` : `<br>only ${esc(x.best.symbol)} ${sinr(x.best.pnl)}`) : ""}${x.hit_rate != null && w !== "nifty" ? `<br>${Math.round(x.hit_rate * 100)}% of stocks made money` : ""}</span></div>`; };
    const graded = (s.claude || []).filter(g => g.return != null && (g.action === "buy" || g.action === "sell"));
    $("scorecard").innerHTML = `<div class="card"><div class="cardhead"><h2>Scorecard</h2><span class="sub">${esc(shortDate(T.trial.start))} to ${esc(shortDate(T.trial.ended))}${T.trial.dividends === "cash" ? " · dividends credited before tax" : ""}</span></div>
      <div class="cardbody"><div class="stats">${["you", "agent", "nifty"].map(col).join("")}</div>
      ${graded.length ? `<div class="sub">Claude's ${graded.length} buy/sell call${graded.length === 1 ? "" : "s"}: ${graded.filter(g => (g.action === "buy") === (g.return > 0)).length} went the way it said, by the end date (may include hindsight).</div>` : ""}
      <h3 style="margin-top:8px">What happened next</h3>${n.error ? `<div class="sub">${esc(n.error)}</div>` : `<div class="chart" id="next-chart"></div>
      <div class="sub">Each portfolio held unchanged from the end of the replay to today.</div>`}</div></div>`;
    if(n.error) return;
    lineChart($("next-chart"), {x: n.dates, height: 200, left: 64, legend: true, endLabels: false, yFmt: rupeesShort, label: "After the replay",
      series: [{name: "You", color: C.s1, values: n.you}, {name: "Agent", color: C.s2, values: n.agent}, {name: T.trial.benchmark, color: C.s3, values: n.nifty}]});
  }

  // ---- actions ----
  document.querySelectorAll("[data-step]").forEach(b => b.addEventListener("click", async () => {
    document.querySelectorAll("[data-step]").forEach(x => x.disabled = true);
    try {
      const j = await api(`/replay/api/trial/${slug}/step`, {by: b.dataset.step});
      if(j.ok === false){ toast(j.message); return; }
      const done = await waitJob(j.id, $("step-status"));
      toast(done.message);
    } catch(e){ toast(e.message); }
    await open(slug);
  }));
  $("b-end").addEventListener("click", async () => {
    if(!confirm("End this replay? It becomes read-only and the scorecard and what happened next are shown.")) return;
    try { T = await api(`/replay/api/trial/${slug}/end`, {}); render(); } catch(e){ toast(e.message); }
  });
  $("b-home").addEventListener("click", () => { location.hash = ""; });
  document.querySelectorAll("#ro-form [data-side]").forEach(b => b.addEventListener("click", () => {
    side = b.dataset.side; document.querySelectorAll("#ro-form [data-side]").forEach(x => { x.classList.toggle("on", x === b); x.setAttribute("aria-pressed", x === b); });
  }));
  $("ro-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const body = {symbol: $("ro-symbol").value.trim().toUpperCase(), side};
    if($("ro-qty").value) body.qty = Number($("ro-qty").value); else if($("ro-amount").value) body.notional = Number($("ro-amount").value);
    else { toast("Enter an amount or a number of shares"); return; }
    try { const j = await api(`/replay/api/trial/${slug}/order`, body); toast(`${side === "buy" ? "Bought" : "Sold"} ${j.order.qty} ${j.order.symbol} @ ${inr(j.order.filled_avg_price, 2)}`); $("ro-amount").value = $("ro-qty").value = ""; await open(slug); }
    catch(e){ toast(e.message); }
  });
  $("ro-autostop").addEventListener("change", async (ev) => {
    try { await api(`/replay/api/trial/${slug}/auto-stop`, {on: ev.target.checked}); } catch(e){ toast(e.message); }
  });
  $("ask-btn").addEventListener("click", async () => {
    $("ask-btn").disabled = true; $("ask-note").textContent = "Asking Claude…";
    try { await api(`/replay/api/trial/${slug}/ask`, {ticker: $("rl-ticker").value.trim().toUpperCase() || null}); await open(slug); }
    catch(e){ toast(e.message); renderClaude(); }
  });
  async function lookup(t){
    $("rl-ticker").value = t; $("rl-result").innerHTML = `<span class="sub">Looking up ${esc(t)} as of ${esc(fmtDay(T.trial.clock))}…</span>`;
    try {
      const r = await api(`/replay/api/trial/${slug}/lookup?ticker=${encodeURIComponent(t)}&news=0`);
      const now = new Date(r.today + "T23:59:59").getTime(), note = lookupTakeaway(r, now);
      const v = (r.momentum || {}).verdict || "n/a";
      $("rl-result").innerHTML = `<div class="row" style="gap:8px"><span style="font-size:17px;font-weight:700">${esc(r.ticker)}</span>${r.price != null ? `<span class="mono">${inr(r.price, 2)}</span>` : ""}<span class="pill ${v === "strong" ? "ok" : v === "weak" ? "bad" : ""}">${esc(v)} momentum</span></div>
        <div class="sub">${esc(r.momentum_summary || "")}</div>
        <div id="rl-note">${note ? `<div class="callout" style="margin-top:6px"><b style="color:var(--color-accent)">What this means.</b> ${note}</div>` : ""}</div>
        <div class="chart" id="rl-chart" style="margin-top:6px"></div>
        <h3 style="margin-top:6px">NSE announcements, last 60 days</h3><div id="rl-news" aria-live="polite">${r.announcements_pending ? `<div class="sub">Loading NSE announcements… the first look-up of a company takes up to half a minute.</div>` : newsHtml(r)}</div>`;
      const h = r.history || [];
      lineChart($("rl-chart"), {x: h.map(p => p.d), height: 170, left: 52, endLabels: false, legend: true, label: `${r.ticker} price, year before the replay date`,
        yFmt: v => "₹" + Math.round(v).toLocaleString("en-IN"), empty: "No price history before this date.",
        refs: r.position ? [{y: r.position.avg_entry_price, label: "Your cost"}].concat(r.position.stop != null ? [{y: r.position.stop, label: "Stop", color: C.neg}] : []) : [],
        series: [{name: "Price", color: C.s1, values: h.map(p => p.c)}, {name: "200-day average", color: C.ctx, width: 1.5, values: h.map(p => p.ma200)}]});
      if(r.announcements_pending){
        const want = r.ticker;
        try {
          const n = await api(`/replay/api/trial/${slug}/news?ticker=${encodeURIComponent(want)}`);
          if($("rl-ticker").value.trim().toUpperCase() !== want || !$("rl-news")) return;  // a newer look-up won
          Object.assign(r, {announcements: n.announcements, announcements_error: n.announcements_error, announcements_pending: false});
          $("rl-news").innerHTML = newsHtml(r);
          const note2 = lookupTakeaway(r, now);
          $("rl-note").innerHTML = note2 ? `<div class="callout" style="margin-top:6px"><b style="color:var(--color-accent)">What this means.</b> ${note2}</div>` : "";
        } catch(e){ if($("rl-news")) $("rl-news").innerHTML = `<div class="sub">${esc(e.message)}</div>`; }
      }
    } catch(e){ $("rl-result").innerHTML = `<span class="sub">${esc(e.message)}</span>`; }
  }
  function newsHtml(r){
    return r.announcements.length ? r.announcements.map(a => `<div class="ann"><div class="meta">${esc(a.at)} · ${esc(a.category)}</div><div>${a.file && /^https?:\/\//.test(a.file) ? `<a href="${esc(a.file)}" target="_blank" rel="noopener">${esc(a.text || a.category)}</a>` : esc(a.text || a.category)}</div></div>`).join("")
      : `<div class="sub">${esc(r.announcements_error || "none in the 60 days before the replay date")}</div>`;
  }
  $("rl-form").addEventListener("submit", (ev) => { ev.preventDefault(); const t = $("rl-ticker").value.trim().toUpperCase(); if(t) lookup(t); });
  attachSuggest(["rl-ticker", "ro-symbol"], (input, s) => { if(input.id === "rl-ticker") lookup(s); else $("ro-amount").focus(); });
  document.querySelectorAll("[data-tool]").forEach(b => b.addEventListener("click", async () => {
    try {
      const j = await api(`/replay/api/trial/${slug}/tool`, {kind: b.dataset.tool, years: Number($("tl-years").value) || 3});
      if(j.ok === false){ toast(j.message); return; }
      $("tools").innerHTML = `<div class="sub">Running… this loads up to five years of prices before the replay date.</div>`;
      const done = await waitJob(j.id, null); toast(done.message); loadTools();
    } catch(e){ toast(e.message); }
  }));
  async function loadTools(){
    let r;
    try { r = await api(`/replay/api/trial/${slug}/tools`); } catch(e){ toast(e.message); return; }
    const ks = Object.keys(r);
    $("tools").innerHTML = ks.length ? ks.map(k => `<h3 style="margin-top:8px">${k === "signal_lab" ? "Signal lab" : "Factor backtest"} as of ${esc(shortDate(r[k].date))}, ${r[k].years} years</h3>${toolMeaning(k, r[k].result)}<pre class="mono" style="white-space:pre-wrap;font-size:12.5px">${esc(r[k].text)}</pre>`).join("") : `<div class="sub">Run a tool to see how the strategies looked with the data available on the replay date.</div>`;
  }
  // The same "What this means" reading the Live page gives, from the result as it stood on the replay date.
  function toolMeaning(kind, res){
    if(!res) return "";
    const box = (title, body) => body ? `<div class="callout" style="margin:6px 0"><b style="color:var(--color-accent)">${title}</b> ${body}</div>` : "";
    try {
      if(kind === "factor_backtest") return box("What this means.", factorTakeaway(res));
      const hs = (res.horizons || []).map(String).filter(h => res.results && res.results[h]);
      return hs.map(h => box(`What this means (next ${h === "5" ? "week" : h === "20" ? "month" : h === "60" ? "quarter" : h + " trading days"}).`,
                             signalTakeaway(signalRows(res, h), res, h))).join("");
    } catch(e){ return ""; }  // a reading that can't be built never hides the numbers below
  }
  document.addEventListener("click", async (ev) => {
    const o = ev.target.closest("[data-open]"); if(o){ ev.preventDefault(); location.hash = o.dataset.open; return; }
    const l = ev.target.closest("a[data-lookup]"); if(l){ ev.preventDefault(); lookup(l.dataset.lookup); $("rl-form").scrollIntoView({behavior: "smooth", block: "center"}); return; }
    const c = ev.target.closest("[data-copy]"); if(c){ $("ro-symbol").value = c.dataset.copy; document.querySelector('#ro-form [data-side="buy"]').click(); $("ro-amount").focus(); return; }
    const s = ev.target.closest("[data-sell]");
    if(s){
      if(!confirm(`Sell all ${s.dataset.qty} ${s.dataset.sell} at the replay day's close?`)) return;
      try { await api(`/replay/api/trial/${slug}/order`, {symbol: s.dataset.sell, side: "sell", qty: Number(s.dataset.qty)}); await open(slug); } catch(e){ toast(e.message); }
    }
  });
  let rsT; window.addEventListener("resize", () => { clearTimeout(rsT); rsT = setTimeout(() => { if(T && !$("trial").hidden) render(); }, 200); });

  function route(){ const h = decodeURIComponent(location.hash.slice(1)); if(h) open(h); else { show("home"); loadHome().catch(e => toast(e.message)); } }
  window.addEventListener("hashchange", route);
  route();
})();
