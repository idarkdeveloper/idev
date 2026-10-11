# Health tab (data health page) for the Live v2 page

# Three dashboard items, built one after another (each its own builder, after the chart upgrade merges)

Common rules for every builder: repo C:\projects\Share Trade, own worktree from current master, commit, do NOT push,
no subagents, fakes only, NO network, never call Groww/Telegram/Anthropic/Resend, never read .env. Nothing here may
place, modify or cancel an order. Commit trailer: Co-Authored-By naming the model you actually run.
Tests: single files with plain pytest (no -n 8); the full suite with `-n 8` exactly ONCE at the end, in the
background, time limit >= 10 min, never two at once; plus `ruff check trading_agent tests` and `mypy`.
Phone: everything fits 360 px with no horizontal page scroll; colours from nocturne.css tokens (resolve color-mix()
values through a probe element when a canvas needs them). Reply with ONLY: Status, worktree + branch, commits,
test summary, concerns, report path.


## C. Data health page
- New page (linked from the dashboard nav) listing every data source: NSE deals, BSE deals, announcements, insider
  filings, Yahoo prices, FII/DII flows, market breadth, price bands, news, Groww (holdings/session only: read the saved
  state, never log in or call Groww to check), the pre-market canary, the heartbeat.
- Per source: what it feeds (screen, morning email, evening email, agent, alerts), last success time and age, rows in
  the last fetch, delay vs its expected schedule, back-off/breaker state (the NSE breaker exists), last error text
  (redacted of anything secret-looking), and a status pill ok / late / failing / not configured. Sort failing first.
- Data comes from what the app already records (state dir files, the existing /api/freshness, canary result, breaker
  state); add small "last run" records where a source records nothing yet, written by the existing fetch code.
  GET /api/health returns it as JSON; read-only; same guard.
- Tests: each status rule with fake state files and a fixed clock (weekend/holiday = not late), redaction, endpoint.
- Report: .superpowers/health-report.md


## C (addition). Groww login counter
- Record every token request the agent SENDS to Groww and what came BACK, in a small append-only log in the state
  dir (time IST, host = server/laptop, result = ok / refused-429 / other error code / network error, never the token
  or any secret). Written by the existing token code at the single place it calls Groww for a token.
- Health shows: "Groww logins today (since 06:00 IST): 3 sent · 2 ok · 1 refused · of 150", the token's age and when
  it expires, the cool-down state and its end, and the last 10 attempts. A day counter resets at 06:00 IST.
- Note on the page: counts only this agent's requests on this machine (server and laptop each keep their own log; the
  server page reads its own). The 150 is the Groww Trade API token limit; signing in to the Groww app or website does not use it. Only other API scripts using the same API key would also count, and those are not seen.
- Warn (amber) at 100 sent, red at 140, and the token code refuses to send more once 145 are sent in the day.
- Tests: counting across the 06:00 reset, results recorded, no secret in the log, the 145 hard stop.

## Also in this job
- Put it on the Live v2 page as a fifth tab "Health" (#health), plus a Ctrl+K "Go to Health" command; the tab badge shows
  the number of failing sources.
- Settings' Groww test button: respect the Groww window (trading_agent/groww_hours.py), or count it as an explicit
  user call through the login counter; it must never bypass the cool-down after a 429.
- Portfolio card: a "Refresh from Groww" button that calls read_groww_portfolio(force=True) (new POST, same CSRF/Host
  guard; read-only), disabled during a cool-down with the time it ends.
- Portfolio card wording: for results with source == "saved" and market_closed == True show the plain note
  "Market closed — holdings saved at the <date> close" (no error styling); keep the error styling for real failures.
- Work on a new branch from current master and open a draft PR; full suite (pytest -n 8), ruff and mypy green.