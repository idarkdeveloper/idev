# Health tab: data health page and Groww login counter

Built from docs/design/health-brief.md (section C, its addition, and "Also in this job").

## Status
Done. One draft PR from branch `ccr-fe90f681-zvcmsm` (the cloud session's single branch), based on master a1a981b.

## What was built
- **Data health** (`trading_agent/health.py`, `GET /api/health`, read-only, the same Host/Origin guard as every API route).
  - Twelve sources:
    - NSE bulk/block deals (or QuiverQuant), BSE deals, insider filings (NSE PIT), NSE announcements
    - Yahoo prices, FII/DII flows, NIFTY 500 breadth, NSE price bands, news
    - Groww holdings (from the saved files only), the pre-market canary, the watch service and heartbeat
  - Each source shows:
    - what it feeds
    - its last success and age
    - rows in its last fetch
    - its schedule, and how late it is against it
    - back-off: the NSE breaker file, BSE's 15-minute pause, the Groww cool-down and 145 stop, the canary's buy pause
    - the last error, redacted: URL paths and queries, bearer tokens, key=value secrets, Telegram bot tokens, JWTs, long opaque strings and e-mail addresses
    - a status pill: ok / late / failing / not configured
  - Failing sources are listed first.
  - Schedules are measured from the latest NSE trading day using the cached holiday list, so a weekend or holiday is never late and the page makes no network call.
  - New record `state/source_runs.json` (`note_run` / `recorded`). It is written by the existing fetch code where nothing was recorded before:
    - the deals fetch in `runner.check`, the watch's alerts-only path, and the dashboard's `App.deals`
    - `Watcher.poll_announcements` and `Watcher.poll_news`
    - `BSEClient` (once per call: success or failure)
  - The other sources come from what already existed: `flows.json`/`breadth.json` attempts, `price_bands.json` and its tries file, Yahoo cache mtimes, `integration_check.json` + `canary_failed.json`, `watch_alive.json`, `nse_breaker.json`, and the Groww snapshot, token and block files. The token itself is never read out.
- **Groww login counter** (`trading_agent/groww_logins.py`).
  - Writes an append-only `state/groww_logins.jsonl`: `{at (IST), host: server|laptop, result: ok | refused-429 | error-<code> | error-no-token | network error}`. It never holds the token, key, secret, TOTP or reply text.
  - It is written by `cached_access_token` at the single place it sends a token request. A cached token or an active cool-down sends and logs nothing.
  - The day runs from 06:00 IST. Warn (amber) at 100, red at 140.
  - At 145 sent, the token code refuses to send more until 06:00, even with `force=True`.
  - The Health tab shows:
    - "N sent · ok · refused · of 150", with a meter
    - the token's age and expiry
    - the cool-down and its end
    - the last 10 requests
    - the note that only this machine's own API requests count
- **Live page**: a fifth tab "Health" (`#health`, plus `#groww-logins`).
  - Its badge shows the number of failing sources; it reloads every 2 minutes and when the tab opens.
  - The Ctrl+K "Go to Health" command comes from the WORKSPACES list.
  - Fits 360 px with no horizontal scroll; tokens only (nocturne.css block "Health tab").
- **Groww test button**: may run outside the Groww window as an explicit check, and says so. It never passes `force`, so it cannot bypass the cool-down after a 429. A token it needs is counted, and it is refused at 145.
- **"Refresh from Groww"** on the portfolio card:
  - `POST /api/groww/refresh`, Live page only, behind the same guard.
  - Calls `read_groww_portfolio(force=True)`. That bypasses the window but not the cool-down, and the endpoint refuses with 409 before anything is sent while a cool-down is on.
  - The button is disabled during a cool-down and shows when it ends; `cooldown_until` comes with every `/api/my-portfolio` answer.
- **Portfolio card wording**: for `source == "saved"` with `market_closed` it shows the plain note "Market closed — holdings saved at the <date> close · prices from Yahoo (delayed)", without error styling. Real failures keep the error styling.

## Tests
- `tests/test_health.py` (14):
  - redaction
  - the run record
  - intraday ok / late / failing, plus weekend and holiday
  - not configured
  - flows and breadth: done, late at 21:00, "still <date>" is not an error, error is failing
  - the NSE breaker
  - bands, Yahoo, canary and watch
  - the Groww row and panel (token never exposed, cool-down, 145 stop)
  - failing first and counts
  - the endpoint (GET only)
  - Refresh from Groww (forces the read, refused during a cool-down, Live page only)
  - the Groww test never forces
  - page markup and tokens-only CSS
- `tests/test_groww_logins.py` (7):
  - the 06:00 day start
  - results and host logged with no secret
  - cached token and cool-down log nothing
  - counting across the 06:00 reset and the levels
  - the 145 hard stop, even when forced
  - the host from TA_ROLE
  - trimming, and a write failure never raising
- `tests/test_livev2.py`: the WORKSPACES list now includes health.

## Concerns
- `source_runs.json` is written on every deals, news and announcement poll: one small atomic write per watch tick, by both the watch process and the dashboard. Last writer wins for a source, which is fine for a status page.
- The heartbeat itself (pings to HEARTBEAT_URL) keeps its state in the watch process only. The page uses `watch_alive.json`, which the same process writes, and says whether HEARTBEAT_URL is set.
- Yahoo has no persisted error record (its breaker is in memory). The page shows the newest cache refresh, so it can be late but never "failing".
