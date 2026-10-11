# Live trading readiness: the four missing safeguards (build before GROWW_LIVE_ORDERS=true)

Work on a new branch from current master and open ONE draft PR (or one per safeguard, stacked, merged in order).
Fakes only in tests: a fake Groww API that records calls. NEVER call the real Groww API, never place, modify or cancel a
real order, never read .env. pytest -n 8, ruff and mypy green. Message share-trade-05 (session
session_01QzRcAu9Jqt3nyEcF2Rvb4B) with the PR number when ready, as before.

These are money/order paths: keep each change small, add tests for every refusal, and do not change defaults —
GROWW_LIVE_ORDERS stays false and AUTO_TRADE stays off unless the user sets them.

## The spec (agreed 10 Oct 2026)
- Before any live GTT on real holdings: corporate-action guard — the morning email flags holdings with a split, bonus
  or large special dividend ex-date within 7 calendar days (NSE corporate-actions feed), and the GTT manager
  suspends/cancels the GTT the session before the ex-date and re-creates it at the adjusted level afterwards (a 1:1
  bonus halves the price and would otherwise trigger the stop at the open).

## Execution edge cases (added 10 Oct 2026, for later — required before GROWW_LIVE_ORDERS=true)
- Corporate-action normaliser (extends the GTT guard): at 08:30 IST, for any holding with a split/bonus ex-date today,
  rescale cost basis, stop, trailing high and expected quantity by the ratio before watch runs; no stop evaluation or
  GTT change on that holding until Groww's holding quantity reflects the new shares (or end of day); morning email notes it.
- Opening collar: no buys before 09:17 IST; buy limit = min(the existing 0.5% cap, pre-open/indicative price + 30 bps)
  rounded to tick; an unfilled buy is cancelled after 120 s. (Market orders are already never sent.)
- Kill switch: `python -m trading_agent kill-switch` (and a dashboard button needing a typed confirmation):
  1) write state/HALT (persistent), 2) stop the watch service, 3) cancel all open agent orders at Groww (GTTs listed and,
  with --cancel-gtt, cancelled), 4) delete the cached Groww token, 5) alert by Telegram and email. While HALT exists every
  order path, the watch loop, the forward job and the CLI refuse (exit 1); removed only by `kill-switch --release`.
- Portfolio day brake: if the live account's value falls ≥ 3.5% from the previous close intraday, no new buys and no
  rebalancing for the rest of the session; only stops already at Groww (GTT) act; alert once.
- Clearing holidays: use NSE's clearing/settlement holiday list; on those days proceeds of T-1 sells are not counted as
  available cash in sizing, so a buy is never sized from unsettled money.

## Build notes
1. Corporate-action guard + normaliser: NSE corporate-actions feed through the existing NSE client (breaker, back-off,
   cached daily). Morning email + dashboard flag holdings with a split/bonus/large special dividend ex-date within 7
   calendar days. The GTT manager suspends/cancels the GTT the session before the ex-date and re-creates it at the
   adjusted level after Groww shows the new quantity. At 08:30 IST on the ex-date rescale cost basis, stop, trailing
   high and expected quantity (practice account too). Health tab row for the feed. Fail safe: when the feed is
   missing, flag "corporate actions unknown" and leave GTTs as they are (never cancel blindly).
2. Opening collar: no buys before 09:17 IST; buy limit = min(existing 0.5% cap, indicative/pre-open price + 30 bps),
   rounded to tick; unfilled buy cancelled after 120 s. Applies to live orders and, for realism, practice orders.
   Settings: OPEN_COLLAR_START=09:17, OPEN_COLLAR_BPS=30, BUY_TIMEOUT_S=120.
3. Kill switch: CLI python -m trading_agent kill-switch [--cancel-gtt] [--release] and a dashboard button (Health
   or Settings) requiring the typed word HALT; steps exactly as the spec. HALT file checked by EVERY order path, the
   watch loop, the forward job and the CLI (exit 1). Release only via kill-switch --release. Alerts through the
   existing notifier (Telegram + email). Tests: every path refuses under HALT; cancel calls only cancel (fake API);
   the token file is deleted; nothing places an order.
4. Day brake: live account value down ≥ 3.5% from the previous close intraday → no new buys and no rebalancing for
   the rest of the session; GTT stops still act; one alert. Setting DAY_BRAKE_PCT=3.5. Shown on the mode strip.
5. Clearing holidays: NSE clearing/settlement holiday list (cached yearly); on those days T-1 sell proceeds are not
   counted as available cash in sizing (live and practice). Health row for the list.
6. Coverage of the existing "partial" items stays for the user's one-share live test (groww-check --live-test);
   do not mark them done in code.