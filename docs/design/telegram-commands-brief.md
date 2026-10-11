# Telegram commands, step 1 (practice only)

## Telegram commands (planned, 10 Oct 2026)
Step 1 (practice): the server long-polls the bot (getUpdates, outbound only) and obeys only TELEGRAM_CHAT_ID; every
other chat ignored and logged. Commands: /lookup SYMBOL (price, momentum 1m/3m/6m, 200-day avg, stop, recent news,
the Look up "What this means" text), /size SYMBOL [risk% max%] (shares, cost, stop level, round-trip charges from the
existing sizing and cost code), /portfolio, /brief, /stops, /buy SYMBOL QTY|₹AMT and /sell SYMBOL QTY in the PRACTICE
account only, each with an inline Confirm/Cancel button (expires after 2 min). Names resolved like the dashboard
search; all replies HTML-escaped; rate-limited.
Step 2 (live, later): only with GROWW_LIVE_ORDERS=true and the live-readiness checks done; confirmation needs a TOTP
code from the user's authenticator; limit orders within 0.5%, size cap, canary/bands/BSE-only/free-shares rules all
apply; /halt = kill switch. Sells never blocked.

## This job = Step 1 only (Step 2 stays planned)
- Long-poll getUpdates from the watch service (or its own small thread/service if cleaner), outbound only; store the
  update offset in state so restarts don't replay commands; obey ONLY settings.telegram_chat_id (string compare),
  ignore + log (without message text) every other chat. Never echo the bot token; never log it.
- Commands as in the spec, plus /help. Reuse the dashboard's functions (search/resolve, lookup, size, costs, portfolio,
  brief, stops) — no duplicated logic. Replies HTML-escaped, Telegram limits respected (4096 chars), rate-limited
  (e.g. 20 commands/min per chat).
- /buy and /sell: PRACTICE account only, through the same practice order path the dashboard uses (Indian filters,
  collar time rule, day brake, HALT all apply). Inline Confirm/Cancel buttons; callback must match chat + a one-time
  nonce; expire after 2 min; a second tap does nothing. No code path to a live order exists in this feature, whatever
  GROWW_LIVE_ORDERS says — test it.
- Under HALT: every order command refuses; read commands still work. Add /status (mode strip text, HALT, day brake).
- Groww: read commands use the saved/market-hours rules (groww_hours); never force a Groww login from Telegram.
- Settings: TELEGRAM_COMMANDS=false by default (opt-in), in .env.example + README. Health tab row for the poller.
- Tests with a fake Telegram API: unknown chat ignored, each command's reply, confirm/expire/double-tap, nonce
  mismatch, HALT refusals, no live order path, offset persistence, rate limit, escaping of hostile names.
- New branch from current master, draft PR; pytest -n 8, ruff, mypy green; fakes only; never call real
  Telegram/Groww/Anthropic. Message share-trade-05 (session_01QzRcAu9Jqt3nyEcF2Rvb4B) with the PR number.