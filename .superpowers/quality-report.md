# Quality report: lint, types, live integration check, canary and resilience

Branch `worktree-agent-a801c49c28f48f29f`, master 9a6834a merged. Commits: 06f8937, 98014d0 (merge), acf63cc, 3c1bd0a.

## 1. Lint and types
- `pyproject.toml`: ruff (py311, line-length 120, rules E9, F, B, BLE; B904/B905 ignored; tests ignore B011/B017/B018/B007/BLE001/F811). BLE enabled so the existing `# noqa: BLE001` stay meaningful. No formatter run.
- Fixed (ruff found 61): unused imports (bse.py, cli.py, replay/trial.py, ui.py + 12 test files), duplicate set items (digest_writer.py:26 `NIFTY`, test_digest), unused variables (digest_writer.py first_at, tests), unused loop var (groww_check.py:193), loop-variable closure (test_digest B023), two unmarked blind excepts (broker.py:371, cli.py:566).
- mypy (`ignore_missing_imports`, `check_untyped_defs=false`, trading_agent/ only): 170 errors -> 0. Real fixes in groww.py, broker.py, runner.py, ui.py (Optional broker, query-string variable reuse, cost-model union, `cast` of the simulator in the paper-only order/stop paths), costs.py/cli.py (`isinstance(m, FlatCosts)`), watch.py, forward.py, digest.py, index_history.py and others; `digest_rules._ok` is a TypeGuard. 23 narrow `# type: ignore[...]` remain.
- Overridden (`ignore_errors`, reasons in pyproject): `bulletin`, `signal_lab`, `factor_backtest`, `validation`. Every order/guard module (groww, broker, live, live_alerts, risk, stops, runner, config, ui, safety) has no override.
- Addition 1: `disallow_untyped_defs = true` for groww, broker, live, live_alerts, risk, stops, safety, integration (two annotations were missing: broker `_txn`, live_alerts `_locked`).
- CI: new `lint` job (ruff + mypy, pip cache); tests job unchanged. `requirements-dev.txt`; README "Development".

## 2. Integration check (`trading_agent/integration.py`, CLI `integration-check`)
Steps (each on a daemon thread, 45 s timeout, independent): NSE deals (strict CSV via `NSEClient.probe_deals`), NSE announcements, BSE deals (BSEClient throttle, one flow per deal type), Yahoo NIFTYBEES.NS and ^NSEI (no cache), NSE price bands, NSE bhavcopy, Groww holdings+orders, Groww available cash, server clock, Claude (only INTEGRATION_CLAUDE=true, max_tokens 5), Resend/Telegram presence. Groww: cached or env token only; "skipped (no cached token)" otherwise or while the cool-down file is active; client wrapped in `ReadOnlySession` (raises on any non-GET) and `ReadOnlyGroww` (holdings, order_list, available_cash only).
Result `state/integration_check.json`; one alert a day on failure; `IntegrationScheduler` (claim file, holiday-aware, retry) at 08:30 IST from the watch service; dashboard line "Integration: ok 08:30 · n/n" (red when failed or > 2 trading days old); Settings switches via the validated writer and a "Run integration check" button.

## Coordinator additions
1. disallow_untyped_defs: done.
2. Canary at 08:30 + Groww cash step. `canary_failed.json` is set by a failed run, or by the watch tick at/after 09:10 on a trading day with no run. `integration.buy_block` refuses agent buys (`place_paper_order`) and any live Groww buy (`GrowwBroker(buy_gate=...)`, wired in `runner.make_groww`) with "pre-market check failed: <steps>"; sells and stops are never gated. Cleared by the next passing run (watch, CLI, button). Amber banner; morning email line "Pre-market check: ok n/n" / "FAILED: ..." (also in the summary writer facts and allowlist); alert via the notifier (email, webhook, Telegram).
3. Heartbeat: 60 s ping / 3 min stall in 09:15-15:30 IST trading days (3 x loop interval if slower), 5 / 20 min outside; runbook: healthchecks.io period 1 min, grace 2 min, Telegram integration.
4. Circuit breaker (`circuit.py`): 3 consecutive refusals -> skip 30 s, 60 s, 300 s (cap), reset on success; logs degraded/recovered once; `nse_breaker.json` -> `nse_degraded` in `/api/freshness` and "NSE degraded" in the chip. Yahoo fetches use the same breaker (per YahooPrices instance).
5. Clock (`clockcheck.py`): `timedatectl` flag + offset from timesync-status or chronyc; fails if unsynced or > 1 s; Windows "not checked"; start-up check in `watch` with one alert a day; integration step.
6. Backups: `state.json.bak` on every state.json write; `backup.py` daily (16:00 IST, trading days, claim-guarded) copies `state/*.json` (never `.env`, token or secret names) and the price archive via the sqlite3 backup API into `state/backups/YYYYMMDD/`, 30 days kept; runbook restore steps (docs/runbook.md section 16).

## Tests
New: tests/test_integration.py (36), tests/test_canary.py (10), tests/test_resilience.py (28). Full suite: 1235 passed, 1 failed (freshness key assertion, fixed in 3c1bd0a; test_safety re-run 42 passed). ruff and mypy clean.

## Concerns
- The overdue flag is only set by the watch loop (no watch service, no flag), deliberately, so tests and laptop use do not depend on the clock.
- INTEGRATION_CHECK changes apply when the watch service restarts.
- The pre-market email line sits in the risk-gauge block, so it is missing if that block is unavailable.
- The NSE breaker covers everything through `NSEClient.session`; the integration run uses a fresh client so it always probes.
- Full suite takes about 7 minutes.


## Fix round 1
Master ccd5d5e merged first (clean). New tests are in tests/test_fix_round1.py (25) plus updated tests in test_resilience.py and test_integration.py. Full suite: 1265 passed; ruff and mypy clean.

1. Overdue vs a run in flight: `IntegrationScheduler.tick` skips `overdue_fn` while the run thread is alive or today's claim is held and the day is not done (`_in_flight`). The "did not run" alert uses its own marker (`integration_overdue_<day>.sent`, via `alert_once_a_day(prefix=...)`), separate from `integration_alert_<day>.sent`.
2. The canary job raises when the result is not ok and it is before 09:00 IST (`RETRY_UNTIL`; `make_scheduler(now_fn=...)` for tests), so the scheduler's retries run (about 08:30, 08:40, 08:50). After 09:00 a failure stands (no retry).
3. `buy_block(settings, now=None)`: with `groww_live_orders` true, on a weekday after 09:10 IST with no passing result for today it returns "pre-market check has not passed today". Read-only; paper/practice unaffected; a stored failure still wins.
4. Heartbeat: `PING_EVERY_S` is 60 s at all hours; only the stall limit varies (window: max(180, 3 x loop interval)). Fail messages are rate-limited to one per five ping periods (5 min). `Watcher.tick` stamps `_progress` after check_fn, after announcements and news, after the stop check and after sync_live. Runbook: cron advice dropped, "period 1 min, grace 2 min" around the clock.
5. `cmd_watch` wraps `startup_check` in try/except Exception with a log line; `clockcheck._run` also catches ValueError and UnicodeDecodeError.
6. Clock parser handles "min" ("+1min 2.345s"); an Offset line (or chrony System time line) that cannot be read fails with "offset unreadable".
7. `YahooPrices.latest_price` uses the raw session: exempt from the Yahoo breaker and does not feed it.
8. Breaker docstring now says there is no single probe. `make_data_source(settings, *, breaker_file=None)`; only `cmd_watch` passes `state/nse_breaker.json`.
9. The heartbeat window test uses `_CachedCalendar` -> `NSEHolidays.peek_trading_day`, which reads loaded or on-disk cached days (any age) and never fetches.
10. requirements-dev.txt pinned: ruff==0.17.0, mypy==2.4.0, types-requests==2.33.0.20261006.
11. Backups: `state/forward/*.json` included (under backups/<day>/forward/); price-archive copies are removed after 7 days (JSON after 30); `_copy_atomic` removes its `.tmp` after a failure.
12. Covered by 1.
