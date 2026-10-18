# Phone App, Stage 1 (live Groww portfolio) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An Android app that opens on the owner's real Groww cash and holdings (value, profit/loss, day change, charts), reading from a new read-only service on the Oracle server over Tailscale.

**Architecture:** The watch service (which already holds Groww access) gains a small `AppFeed` component that, inside the Groww window, writes one feed file (`state/app_feed.json`) and, once a day, one value-history row. A separate read-only process `app_api` serves those two files over HTTP with a per-device secret; it never reads `.env` and never imports Groww code. A Flutter app shows the feed, caches the last answer and says how old it is.

**Tech Stack:** Python 3.15 stdlib (`http.server`, `hmac`, `hashlib`), pytest with fakes. Flutter (stable, Dart 3), `http`, `flutter_secure_storage`, `shared_preferences`, `fl_chart`, `intl`, `provider`; `flutter test`.

**Spec:** `docs/superpowers/specs/2026-10-18-phone-app-design.md`

## Spec amendments found while planning (owner must accept these)

1. **The stored Groww snapshot is not kept fresh today.** It is rewritten only when the dashboard page is open, by the daily emails, and once at the close (`CloseSnapshot`, 15:35-15:50 IST). It holds no cash and no prices. The spec assumed "the existing capped refresh keeps it current"; that does not exist. So this plan adds `AppFeed` to the **live watch service**: during the Groww window, every `APP_FEED_EVERY_S` seconds (default 300), it calls the existing `runner.read_groww_portfolio` (same window, cool-down and login-cap rules as the dashboard) and one extra read-only `available_cash()` GET. It is **off by default** (`APP_FEED=false`); deploying the code changes nothing until the owner turns it on.
2. **The API process never loads `.env`.** It takes only a state folder, host and port. It cannot reach Groww keys even if compromised.
3. **"Today's change"** needs the previous session's close. The feed fills it from the free price source (`day_brake.prev_close_fn`) and leaves it `null` when unknown; the app shows "-".

## Global Constraints

- Read-only: no order, no setting change, no write route in the API. `GROWW_LIVE_ORDERS` and `AUTO_TRADE` are untouched.
- The phone never holds Groww keys or tokens and never calls Groww. The API never calls Groww.
- The Groww daily login cap is never touched: the feed reuses `read_groww_portfolio` and never builds a second Groww client for portfolio data except the one `available_cash` read inside the existing client path.
- Device secret: `secrets.token_urlsafe(32)`, stored on the server only as a SHA-256 hash, compared with `hmac.compare_digest`, never printed by Claude, never logged, never in chat.
- Network: Tailscale. The API binds to the server's Tailscale address (`APP_API_HOST`), default `127.0.0.1`. No public port is opened.
- Android only, local build on the owner's Windows PC, installed as an APK. No Expo, no cloud build.
- No real keys in code, tests or logs; tests use fakes only. Do not run the full test suite on the server; run only the new test files plus `ruff`/`mypy` locally.
- Times are IST (`trading_agent.timezones.IST`).

## Contract between server and app

`GET /v1/portfolio` (header `Authorization: Bearer <secret>`):
```json
{"as_of": "2026-10-19T10:35:02+05:30", "age_s": 123, "stale": false, "market_closed": false,
 "source": "groww", "cash": 12345.67, "value": 0.0, "invested": 0.0, "pl": 0.0, "pl_pct": 0.0,
 "day_change": null, "day_change_pct": null,
 "holdings": [{"symbol": "TATASTEEL", "name": "Tata Steel", "qty": 10, "avg_price": 150.0, "price": 160.0,
               "value": 1600.0, "pl": 100.0, "pl_pct": 0.0667, "day_change": 5.0, "day_change_pct": 0.003,
               "trend": [150.0, 151.2]}],
 "unpriced": [], "error": null}
```
`source` is `groww` or `saved`; `cash`, `day_change*`, `pl_pct`, `price` may be `null`; `trend` holds at most 30 closes, oldest first. `GET /v1/history?range=1m|3m|1y|all` returns `{"points": [{"date": "2026-10-19", "value": 0.0, "cash": null}]}`, oldest first. `GET /v1/health` returns `{"ok": true, "feed_age_s": 17}` (`null` when no feed yet). Errors are `{"error": text}` with 401 (bad or missing secret), 404 (unknown path), 405 (any method but GET), 429 (lockout or rate limit), 503 (no feed yet).

## Review Focus

1. **Groww refuses, is in cool-down, or the account is not linked:** the feed keeps the last good file and records `error`; the API answers with the old figures and `stale: true`, never a blank screen. (Tasks 1, 5, 11)
2. **Wrong, missing or malformed secret, and guessing:** 401 with no detail, lockout after 5 failures per client address in 10 minutes for 15 minutes, no timing difference. (Tasks 4, 5)
3. **Any non-GET request or unknown path:** no effect, no body parsing, nothing written; the API module imports no Groww, broker, config, runner or ui code. (Task 5)
4. **Empty portfolio, unpriced holdings, `null` prices or cash:** no crash on server or phone; the screen shows an empty state or "-". (Tasks 1, 9, 11)
5. **Phone offline, server down, or opened outside market hours:** the last saved answer shows at once with its age and a banner; nothing spins forever. (Task 11)

---

### Task 1: AppFeed builds and writes the feed

**Files:**
- Create: `trading_agent/app_feed.py`
- Test: `tests/test_app_feed.py`

**Interfaces:**
- Consumes: `runner.read_groww_portfolio(settings, prices, stamp, *, force=False) -> dict` (shape in its docstring); `groww_hours.window_open(settings, now, calendar)`, `groww_hours.default_calendar(settings)`, `groww_hours.now_ist()`; `state.atomic_write(path, text)`; `day_brake.prev_close_fn(prices, today)`.
- Produces: `FEED_FILE = "app_feed.json"`, `HISTORY_FILE = "app_value_history.json"`; `class AppFeed(settings, read_fn: Callable[[], dict], cash_fn: Callable[[], float | None], prev_close_fn: Callable[[str], float | None], trend_fn: Callable[[str], list[float]], holidays=None)` with `tick(now: datetime | None = None) -> dict | None` (writes at most once per `settings.app_feed_every_s`; returns the written feed or `None`); `load_feed(state_dir: Path) -> dict | None`.

- [ ] **Step 1: Write the failing tests** in `tests/test_app_feed.py` with a fake clock, fake holidays and fake `read_fn`/`cash_fn` (pattern of `tests/test_groww_hours.py`): `test_writes_the_feed_in_the_window`, `test_no_read_outside_the_window_or_on_a_holiday` (read count stays 0), `test_does_not_read_again_before_the_interval`, `test_feed_has_the_contract_fields_and_day_change_from_prev_close`, `test_unlinked_or_error_keeps_the_last_good_feed_and_sets_error` (previous file untouched, `error` text set, no token or key text in the file), `test_empty_holdings_and_none_prices_do_not_raise`, `test_feed_file_is_owner_only_and_atomic` (mode `0o600`; no `.tmp` left).
- [ ] **Step 2: Run** `python -m pytest tests/test_app_feed.py -q -p no:cacheprovider`. Expected: FAIL (module missing).
- [ ] **Step 3: Implement `AppFeed` and `load_feed`** exactly to the contract above. `tick` never raises (log a warning). It uses `read_fn()` for holdings and totals, adds `cash` from `cash_fn()` (`None` on any error), `day_change` per holding from `prev_close_fn`, `trend` from `trend_fn`, and totals for day change only over holdings that have it.
- [ ] **Step 4: Run** the same command. Expected: PASS. Then `python -m ruff check trading_agent tests` and `python -m mypy`. Expected: clean.
- [ ] **Step 5: Commit** `git add trading_agent/app_feed.py tests/test_app_feed.py && git commit -m "feat: AppFeed writes the phone app feed file"`.

### Task 2: Daily value history

**Files:**
- Modify: `trading_agent/app_feed.py`
- Test: `tests/test_app_feed.py`

**Interfaces:**
- Consumes: Task 1's `AppFeed.tick` and feed dict.
- Produces: `record_daily_value(state_dir: Path, feed: dict, session: date) -> bool`; `load_history(state_dir: Path, range_: str) -> list[dict]` (`range_` in `1m|3m|1y|all`); `AppFeed.tick` calls `record_daily_value` once per session, only at or after 15:35 IST, only when the feed came from a live read (`source == "groww"`).

- [ ] **Step 1: Write failing tests:** `test_one_row_per_session_and_a_second_call_replaces_not_duplicates`, `test_not_recorded_before_15_35_or_from_a_saved_source`, `test_history_ranges_cut_by_date_and_come_oldest_first`, `test_history_file_survives_garbage_content` (returns `[]`).
- [ ] **Step 2: Run** them; expected FAIL.
- [ ] **Step 3: Implement** both functions in `app_feed.py`, rows `{"date", "value", "cash"}`, file `state/app_value_history.json`, owner-only, written with `atomic_write`.
- [ ] **Step 4: Run** `python -m pytest tests/test_app_feed.py -q -p no:cacheprovider`; expected PASS; ruff and mypy clean.
- [ ] **Step 5: Commit** `feat: daily value history for the phone app`.

### Task 3: Wire AppFeed into the watch service (off by default)

**Files:**
- Modify: `trading_agent/config.py` (add `app_feed: bool`, env `APP_FEED`, default `False`; `app_feed_every_s: int`, env `APP_FEED_EVERY_S`, default `300`, minimum `60`), `trading_agent/watch.py` (new optional `app_feed` parameter beside `close_snapshot`; `tick` calls `self._app_feed.tick(now)` right after the close-snapshot block, inside `try/except`, result in `info["app_feed"]`), `trading_agent/cli.py` (build `AppFeed` next to `close_snapshot` only when `settings.app_feed and settings.use_groww and settings.has_groww_credentials`), `.env.example` (document both new variables, no values of secrets).
- Test: `tests/test_app_feed.py` (settings and `Watcher` tests), plus `tests/test_watch.py` for the `Watcher` parameter; env parsing tests follow the way `tests/test_ops.py` / `tests/conftest.py` build settings from a fake environment

**Interfaces:**
- Consumes: Task 1 `AppFeed`; `GrowwBroker.available_cash()` through the same client construction `read_groww_portfolio` uses (a read-only client, `live_orders=False`).
- Produces: `Settings.app_feed`, `Settings.app_feed_every_s`; `Watcher(..., app_feed=None)`.

- [ ] **Step 1: Write failing tests:** `test_app_feed_defaults_off` and `test_app_feed_env_parsing_and_minimum` (bad value raises `SystemExit` like the other settings); `test_watcher_tick_calls_the_feed_once_and_survives_its_error`; `test_watcher_without_a_feed_behaves_as_before`.
- [ ] **Step 2: Run** them; expected FAIL.
- [ ] **Step 3: Implement** the settings, the `Watcher` parameter and the `cli.py` construction. The `cash_fn` builds a read-only Groww client (`live_orders=False`) and calls `available_cash()`; it returns `None` on any exception (including `GrowwTokenUnavailable`).
- [ ] **Step 4: Run** `python -m pytest tests/test_app_feed.py tests/test_watch.py tests/test_groww_hours.py -q -p no:cacheprovider`; expected PASS; ruff and mypy clean.
- [ ] **Step 5: Commit** `feat: AppFeed in the watch service, off by default (APP_FEED)`.

### Task 4: Device secrets

**Files:**
- Create: `trading_agent/app_auth.py`
- Modify: `trading_agent/cli.py` (subcommand `app-device` with `add NAME`, `list`, `revoke NAME`, `revoke --all`)
- Test: `tests/test_app_auth.py`

**Interfaces:**
- Produces: `DEVICES_FILE = "app_devices.json"`; `add_device(state_dir: Path, name: str) -> str` (returns the secret once; stores only its SHA-256 hex and `created`); `list_devices(state_dir) -> list[dict]` (names and dates only); `revoke(state_dir, name: str | None = None, *, all_: bool = False) -> int`; `class Authenticator(state_dir, clock=time.time)` with `check(secret: str | None, client: str) -> tuple[str | None, int]` returning `(device_name_or_None, http_status)` where status is 200, 401 or 429. Lockout: 5 failures per `client` in 600 s blocks that client for 900 s. Rate limit: 60 requests per minute per device.

- [ ] **Step 1: Write failing tests:** `test_add_returns_a_long_secret_and_stores_only_a_hash` (file contains no secret text, mode `0o600`), `test_correct_secret_accepted_wrong_or_missing_rejected`, `test_five_failures_lock_the_client_for_fifteen_minutes_then_release` (fake clock), `test_lockout_is_per_client_address`, `test_rate_limit_61st_request_in_a_minute_gets_429`, `test_revoke_one_and_all`, `test_duplicate_name_is_refused`, `test_garbage_devices_file_means_nobody_is_accepted`.
- [ ] **Step 2: Run** `python -m pytest tests/test_app_auth.py -q -p no:cacheprovider`; expected FAIL.
- [ ] **Step 3: Implement** `app_auth.py` (stdlib only: `secrets`, `hashlib`, `hmac`) and the `app-device` subcommand. The command prints the new secret to the terminal once with the line "Copy it now; it is not shown again." The CLI test uses a temp state folder and a fake secret check only.
- [ ] **Step 4: Run** the test file; expected PASS; ruff and mypy clean.
- [ ] **Step 5: Commit** `feat: per-device secrets for the phone app`.

### Task 5: The read-only API process and its service unit

**Files:**
- Create: `trading_agent/app_api.py`, `deploy/systemd/trading-agent-app-api.service`
- Modify: `trading_agent/cli.py` (subcommand `app-api` with `--state-dir` default `state`, `--host` default env `APP_API_HOST` or `127.0.0.1`, `--port` default env `APP_API_PORT` or `8788`; it must not call `_settings`/`load_settings`)
- Test: `tests/test_app_api.py`

**Interfaces:**
- Consumes: Task 1 `load_feed`; Task 2 `load_history`; Task 4 `Authenticator`.
- Produces: `make_server(state_dir: Path, host: str, port: int, authenticator: Authenticator | None = None) -> ThreadingHTTPServer`; routes exactly `/v1/health`, `/v1/portfolio`, `/v1/history` as in the Contract. `age_s` is computed at response time from `as_of`; `stale` is true when `age_s > 2 * 300` while the Groww window is open (read from the feed's `market_closed` flag, never recomputed with Groww code), and false when `market_closed` is true.

- [ ] **Step 1: Write failing tests** (a real server on port 0 in a thread, temp state folder, `urllib` client): `test_health_and_portfolio_with_a_valid_secret`, `test_no_or_wrong_secret_is_401_with_no_detail`, `test_every_method_but_get_is_405_and_writes_nothing` (POST, PUT, DELETE, PATCH; state folder listing unchanged), `test_unknown_path_is_404`, `test_no_feed_yet_is_503`, `test_stale_flag_and_age_s`, `test_history_ranges`, `test_bad_range_falls_back_to_1m`, `test_lockout_returns_429`, `test_module_imports_no_groww_broker_config_runner_or_ui` (a subprocess imports `trading_agent.app_api` and asserts none of `trading_agent.groww`, `trading_agent.broker`, `trading_agent.config`, `trading_agent.runner`, `trading_agent.ui` is in `sys.modules`; if the package `__init__` pulls one in, move the offending import rather than weakening the test), `test_runs_without_any_env_file` (cwd is a temp folder with no `.env`).
- [ ] **Step 2: Run** `python -m pytest tests/test_app_api.py -q -p no:cacheprovider`; expected FAIL.
- [ ] **Step 3: Implement** `app_api.py` on `http.server` (same style as `ui.py`); responses are JSON with `Cache-Control: no-store`; the secret and the Authorization header are never logged (access log lines carry method, path, status and device name only). The unit file follows `trading-agent-dashboard.service`: `User=agent`, `ExecStart=/opt/trading-agent/.venv/bin/python -m trading_agent app-api --state-dir /opt/trading-agent/state`, `Environment=APP_API_HOST=<set in Task 6>`, `NoNewPrivileges=true`, `ProtectSystem=strict`, `ReadOnlyPaths=/opt/trading-agent`. It has no write paths (the API only reads; the lockout state lives in memory) and no `EnvironmentFile`, so `.env` is never loaded.
- [ ] **Step 4: Run** the test file; expected PASS; ruff and mypy clean.
- [ ] **Step 5: Commit** `feat: read-only phone app API (app-api)`.

### Task 6: Server rollout (owner approves each step; Claude never sees the secret)

**Files:**
- Modify: `docs/runbook.md` (new section "Phone app")

- [ ] **Step 1:** Write the runbook section: install Tailscale on the server and the phone (owner signs in; Claude does not), note the server's Tailscale address, set `APP_API_HOST=<that address>` in the unit's `Environment=`, copy the unit file, `systemctl enable --now trading-agent-app-api`.
- [ ] **Step 2 (owner, on the owner's own terminal):** run `python -m trading_agent app-device add phone` once and copy the secret straight into the phone. Claude does not run this command and the secret never enters the chat.
- [ ] **Step 3 (owner's word):** Claude updates the server to the merged commit, checks both existing services are active, and sets `APP_FEED=true` in `.env` only on the owner's explicit word (a live-service change, read-only Groww calls). Verify: after the next market-hours tick, `state/app_feed.json` exists and `curl` with a test secret from the owner's terminal returns the Contract.
- [ ] **Step 4: Commit** `docs: phone app rollout steps`.

### Task 7: Flutter environment (large downloads; owner approves first)

- [ ] **Step 1:** Install the Flutter SDK (stable) and Android Studio (for the Android SDK and platform tools) on the owner's PC; accept the Android licences with `flutter doctor --android-licenses`.
- [ ] **Step 2:** Turn on developer options and USB debugging on the phone; connect it. Run `flutter doctor -v` and `flutter devices`. Expected: no red items for Android toolchain, and the phone listed.

### Task 8: Project scaffold and theme

**Files:**
- Create: `app/` (via `flutter create --platforms=android --org local.tradingagent --project-name trading_app app`), `app/lib/theme.dart`, `app/lib/main.dart`
- Test: `app/test/theme_test.dart`

- [ ] **Step 1: Write the failing test** `dark theme uses the trading palette`: `buildTheme()` returns a dark `ThemeData`; `gain` is a green and `loss` a red from named constants; text scale for the main number is at least 32.
- [ ] **Step 2: Run** `cd app && flutter test test/theme_test.dart`. Expected: FAIL.
- [ ] **Step 3: Implement** `theme.dart` (`ThemeData buildTheme()`, `const Color gain`, `const Color loss`) and a `main.dart` that shows a placeholder home. Add the dependencies listed in the Tech Stack to `pubspec.yaml`. Set `minSdk` to 26 in `android/app/build.gradle`.
- [ ] **Step 4: Run** the test; expected PASS. `flutter analyze`; expected no issues.
- [ ] **Step 5: Commit** `feat(app): Flutter scaffold and dark theme`.

### Task 9: Models and API client

**Files:**
- Create: `app/lib/models.dart`, `app/lib/api_client.dart`
- Test: `app/test/models_test.dart`, `app/test/api_client_test.dart`

**Interfaces:**
- Produces: `class Portfolio` and `class Holding` with `fromJson` matching the Contract (all nullable fields typed nullable); `class ValuePoint`; `class ApiClient(String baseUrl, String secret, {http.Client? client})` with `Future<Portfolio> portfolio()`, `Future<List<ValuePoint>> history(String range)`; `class ApiException(int status, String message)`.

- [ ] **Step 1: Write failing tests:** parse the full Contract example; parse with `cash`, `price`, `day_change`, `pl_pct` all `null` and `holdings: []`; client sends `Authorization: Bearer <secret>` (fake `MockClient`); 401 raises `ApiException(401, ...)`; 503 raises `ApiException(503, ...)`; the exception message never contains the secret.
- [ ] **Step 2: Run** `flutter test test/models_test.dart test/api_client_test.dart`; expected FAIL.
- [ ] **Step 3: Implement** the models and client.
- [ ] **Step 4: Run** the tests; expected PASS; `flutter analyze` clean.
- [ ] **Step 5: Commit** `feat(app): models and API client`.

### Task 10: Connection screen and secure storage

**Files:**
- Create: `app/lib/connection_store.dart`, `app/lib/connection_screen.dart`
- Test: `app/test/connection_store_test.dart`

**Interfaces:**
- Produces: `class ConnectionStore` with `Future<void> save(String baseUrl, String secret)`, `Future<({String baseUrl, String secret})?> load()`, `Future<void> clear()`; the secret goes to `flutter_secure_storage`, the address to `shared_preferences`. `connection_screen.dart` has two fields (server address like `http://100.x.y.z:8788`, secret), a "Test connection" button calling `/v1/health`, and shows the result without ever echoing the secret.

- [ ] **Step 1: Write failing tests** with in-memory fakes: save then load round-trips; `clear` empties it; load returns `null` when nothing saved; address without `http://` is rejected by `validateAddress`.
- [ ] **Step 2: Run** `flutter test test/connection_store_test.dart`; expected FAIL.
- [ ] **Step 3: Implement** the store and the screen. Android blocks plain `http://` by default, and its network security config cannot name an IP range, so set `android:usesCleartextTraffic="true"` in `AndroidManifest.xml`. This is acceptable only because the traffic runs inside Tailscale's encrypted tunnel; the connection screen refuses addresses that are not `http(s)://` and warns when the host is not in `100.64.0.0/10` or `localhost`.
- [ ] **Step 4: Run** the tests; expected PASS.
- [ ] **Step 5: Commit** `feat(app): connection screen and secure storage`.

### Task 11: Portfolio screen, charts, cache and stale banner

**Files:**
- Create: `app/lib/portfolio_screen.dart`, `app/lib/portfolio_cache.dart`, `app/lib/value_chart.dart`
- Modify: `app/lib/main.dart`
- Test: `app/test/portfolio_screen_test.dart`, `app/test/portfolio_cache_test.dart`

**Interfaces:**
- Produces: `class PortfolioCache` with `save(Portfolio, DateTime fetchedAt)` and `load() -> ({Portfolio portfolio, DateTime fetchedAt})?`; `PortfolioScreen(ApiClient api, PortfolioCache cache)` which shows the cached answer immediately, then refreshes (pull to refresh re-reads the API only); `ValueChart(List<ValuePoint>, String range)` with chips 1M / 3M / 1Y / All.

- [ ] **Step 1: Write failing widget tests** with fake `ApiClient` and cache: shows cash, total value and profit/loss in green or red; shows each holding row with a trend line; empty holdings show "No holdings yet"; `null` values show "-"; the cached answer shows before the network answer arrives; an `ApiException` or a timeout keeps the old figures and shows a banner "Offline: figures from <age>"; `market_closed` shows "Market closed - as of <time>"; `stale: true` shows "Figures are older than expected"; history with one point shows "History builds up day by day" instead of an empty chart.
- [ ] **Step 2: Run** `flutter test`; expected FAIL.
- [ ] **Step 3: Implement** the screen (big total value, today's change, cash, list of holdings), `fl_chart` line charts, the cache (JSON in `shared_preferences`), and a 10-second request timeout.
- [ ] **Step 4: Run** `flutter test` and `flutter analyze`; expected all pass, no issues.
- [ ] **Step 5: Commit** `feat(app): live portfolio screen`.

### Task 12: Release APK and phone check

- [ ] **Step 1:** `cd app && flutter build apk --release`; expected `build/app/outputs/flutter-apk/app-release.apk`.
- [ ] **Step 2:** Install with `flutter install` (or `adb install -r`), open the app, enter the server address and the secret (typed by the owner), tap Test connection. Expected: "Connected".
- [ ] **Step 3 (market hours, owner present):** the portfolio screen shows the owner's real cash and holdings with an "as of" time under 10 minutes old; pull to refresh keeps working; with the phone in flight mode the cached figures show with the offline banner.
- [ ] **Step 4: Commit** `chore(app): stage 1 complete`, open a pull request. Merge only on the owner's word.

---

## Later stages (outline only; each gets its own plan after stage 1 is on the phone)

2. **Practice portfolios and the stock page:** read-only readers for `state/forward/*.json` and `state/paper_broker.json` behind new `/v1/practice` and `/v1/stock/<symbol>` routes (candles from the free price source); a candlestick page.
3. **Commands:** rules-first command matcher (reusing the Telegram company-name matching) behind `/v1/command`; Ollama (`qwen2.5:3b`, `OLLAMA_URL`) as the fallback with a fixed action list, `nice` priority, timeout and single request at a time; typed first, then Android speech (English first, Hindi if the on-device recogniser and test list are good enough).
4. **News and notifications:** `/v1/news` from the existing news store; five-item push needs a free Google push service and a device token endpoint; decided at that stage.
5. **Replay from the phone and US/UK/Japan pages:** replay needs a spend limit, a confirmation step and the owner's separate approval; market pages are display-only.
6. **Orders from the phone (the owner wants this eventually):** one gated route in the app service that sends an order intent to the agent's existing practice order path; confirm tap plus a second check; practice first; live only after the one-share live test and the owner's own `GROWW_LIVE_ORDERS` decision. Needs its own design and security review.
