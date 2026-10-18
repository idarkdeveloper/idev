# Phone app for the trading agent: design

Status: DRAFT for the owner's approval. Nothing is built. Written 2026-10-18.

## Intent

The owner wants an Android app that opens straight onto their real Groww account, looks like a modern trading
app (dark, big numbers, charts), and can be talked to ("show this share", "show latest news"). The agent and
its server already exist; the app is a window onto them. The reason: the owner is away from the laptop and
wants to see their money and the agent's practice portfolios at a glance, and ask for a stock or the news
without opening a browser dashboard that was not built for a phone.

## Goals

1. **Stage 1, the live portfolio screen:** the owner's available Groww cash, total value, today's change, and
   each holding with its profit or loss, each stamped "as of <time>". Read-only.
2. **Look and feel (the owner's stated "wince" point):** modern dark theme, large numbers, green/red profit and
   loss, a portfolio value chart with ranges (1D / 1W / 1M / 1Y) and a small trend line per holding. It must not
   feel like a web page in a frame: smooth screens, pull to refresh, and instant open from the last saved
   figures.
3. **Voice and typed commands (stage 3):** the owner speaks or types a short command; the app shows the
   answer. English and Hindi. Read-only commands only: show a share, show the news, show the portfolio, show
   cash, show the AI (practice) portfolios.
4. **Later stages, in this order of the owner's list:** the AI (practice) portfolios (forward test, residual
   momentum, top-10); run a replay from the phone; US, UK and Japan stocks and trends (display only); five news
   items as phone notifications.

## Non-goals and anti-goals

- No buying, selling, order changes, or setting changes from the app in stages 1 to 5. Whatever is typed or said.
  Live orders stay off on the server (`GROWW_LIVE_ORDERS` and `AUTO_TRADE` unchanged). Orders from the app are a
  LATER stage (the owner wants them eventually): one gated route in the separate app service that sends an order
  intent into the agent's existing order path (kill switch, Indian filters, collar, day brake, the
  `GROWW_LIVE_ORDERS` switch all still apply), with a confirm tap plus a second check (phone biometrics or the
  authenticator code planned for Telegram), practice account first. It needs its own design and security
  review; nothing in stages 1 to 5 places an order.
- The phone never holds Groww keys or tokens and never calls Groww. Only the owner's server does.
- No Play Store release. The app is for the owner only, installed as a file (APK).
- Anti-goal: anything that makes the owner's Groww account or money less safe than today, even if the app works.
  A feature that needs a weaker lock is dropped, not shipped.
- Anti-goal: Ollama (or any model) slowing or starving the trading agent's services.

## What exists now (built on, not rebuilt)

- Server: Oracle, Ubuntu 24.04, 2 ARM cores (Neoverse-N1), 11 GB RAM (about 10 GB available), no swap, 33 GB
  disk free. Load about 0.1. Services `trading-agent-watch` and `trading-agent-dashboard`; the dashboard listens
  on 127.0.0.1:8787 only.
- The dashboard already has read endpoints: `/api/my-portfolio`, `/api/quote`, `/api/candles`, `/api/stock`,
  `/api/news`, `/api/search`, `/api/lookup`, `/api/state`. It also has write endpoints (`/api/order`,
  `/api/stop`, `/api/settings`, `/api/practice/*`, `/api/kill-switch`). It has no login; it is protected only by
  being bound to localhost plus a Host/Origin check.
- Ollama 0.40.2 is installed and running on the server as a service (enabled), bound to 127.0.0.1:11434, with the
  model `qwen2.5:3b` (1.9 GB). The project already uses it for news tagging and the digest
  (`OLLAMA_URL`, `OLLAMA_MODEL`, `NEWS_TAGGER`).
- Groww is read by the server only in market hours (about 08:30 to 16:00 IST), with a daily login cap, and a
  closing snapshot is taken at about 15:35 to 15:50 IST. The Telegram bot already answers `/lookup`,
  `/portfolio`, `/brief`, `/stops`, `/status` with company-name matching, to the owner's private chat only.

## Constraints

- Read-only interface, separate from the dashboard: a NEW small server-side API with a fixed allowlist of read
  routes. The phone must never be pointed at the existing dashboard, because that has write routes and no login.
- Login: a long random secret stored on the phone, sent on every request, checked in constant time, with a
  request limit and a block after repeated failures. The secret is created by the owner on the server and is
  never printed in chat, logs or code.
- The app shows saved figures with their "as of" time outside market hours. It must never force extra Groww
  logins.
- Android only. Built locally on the owner's Windows PC (no cloud build service, no Expo account), as a
  Flutter app (see "Decisions I made"). Tried on the owner's phone over a USB cable or wireless debugging while
  building, installed as an APK at the end.
- No real keys in code, tests or logs. Tests use fakes only.

## Decisions made by the owner

- Android, not iPhone. Native phone app. Modern trading-app look.
- Real Groww account first (cash and holdings), the practice portfolios second.
- Commands by speaking or typing, both. The model runs on the Oracle server (Ollama).
- Read-only for now; orders from the app later, practice first, behind a confirm step and a second check.
- Separate read-only app service (option B), not the existing dashboard (chosen 2026-10-18).
- Build method: subagent-driven (a fresh helper per task, each checked before the next).
- **Reaching the server: Tailscale** (chosen 2026-10-18). The server stays closed to the internet. A public
  address (Cloudflare Tunnel or a domain) was considered and not chosen; it can come back only as a separate,
  hardened step with its own approval.
- **Build: locally on the owner's PC**, no Expo or other cloud account. Framework: the owner named Flutter or
  Kotlin and left the pick to me.

## Decisions I made (the owner has not agreed to these yet)

- **Framework: Flutter (Dart).** Reasons: fast, smooth drawing for the trading look and charts; mature chart
  libraries; one project that builds from the same PC with the Android SDK; hot reload while building; and iOS
  stays possible later at no extra rewrite. The honest alternative is Kotlin with Jetpack Compose: the standard
  choice for Android-only apps, with the best access to Android speech and notification features. Flutter
  reaches those through well-used plugins, so I judge the difference small for this app.
- **Language:** English and Hindi, spoken and typed (my guess from "both").
- **Speech to text:** Android's built-in speech recognition on the phone, so the server only receives text.
- **Meaning of a command:** a small fixed set of commands is matched by rules first (no model needed). Ollama is
  asked only when the rules do not match, and its answer must be one of a fixed list of actions with a stock
  or news topic; anything else is refused. The model picks a screen, nothing more.
- **Ollama limits:** low priority, short answers, a timeout, one request at a time, and a memory cap, so the
  trading services always win. If Ollama is off or slow the common commands still work.
- **Charts:** a charting library on the phone drawing server-supplied candles; no chart images from the server.
- **Where the code lives:** a new `app/` folder in the existing repository (the phone app) and a new
  `trading_agent/app_api.py` (the read-only interface), with tests, on a branch with a pull request.

## More calls of mine (made after a reviewer read this document; the owner can overrule any)

- **Data source:** the new API never calls Groww itself. It serves what the agent already stored: the same
  portfolio snapshot the dashboard's `/api/my-portfolio` uses (shared code, not a copy). Pull to refresh only
  re-reads that stored snapshot. During market hours the existing capped refresh keeps it current. "As of" is
  the snapshot's own timestamp. The daily Groww login cap is never touched by the app.
- **Process:** the API is a separate small process with its own port and its own systemd unit, bound only to the
  private address. It has no write routes at all, so even a flaw in it cannot place an order or change a setting.
  It reads the agent's files read-only. Deploying the unit on the Oracle box is part of stage 1.
- **Portfolio chart data:** there is no stored history of the account's value today. Stage 1 starts saving one
  value per day from the closing snapshot (a small new file). So 1M and 1Y ranges fill up over time and are
  short at first; 1D and per-holding trend lines are drawn from the stocks' own price candles, not from account
  history. The app says so on screen instead of drawing an empty chart.
- **Practice portfolios (stage 2):** read from the agent's existing files (forward test, residual momentum,
  top-10 accounts) through new read-only readers in the API. No practice write routes are exposed.
- **Language:** English first. Hindi is added when stage 3 starts, with a short test list of Hindi phrases and
  company-name aliases; if on-device Hindi speech or the small model is too weak, Hindi stays typed-only.
- **Phone setup:** a normal local debug and release build of the Flutter app, so push messages and speech can be
  added later without redoing stage 1. The PC needs the Flutter SDK, Android Studio (for the Android SDK) and a
  phone with developer mode on. These are large downloads (several GB); nothing is installed until the owner
  approves this document.
- **Lost phone:** the secret is per device. A server command revokes one device's secret or all of them and
  makes a new one. A lost phone is handled by revoking, with no change to anything else.
- **Minimum Android version and "instant open":** recent Android (about the last five years of phones); the
  app shows the last saved figures immediately and updates when fresh ones arrive.

## Left to the builder

Folder names, the exact library choices, screen layout details, the exact JSON shapes, how the saved figures are
cached on the phone, how commands are matched by rules, and the test structure.

## Staging

1. Read-only interface + login + the live portfolio screen (cash, value, holdings, chart). Try it on the phone.
2. The AI (practice) portfolios screen, and the stock page with candles.
3. Typed commands, then speech, with the rules-first matching and Ollama as the fallback.
4. News list and the five-item notifications (needs a free Google push service; decide then).
5. Replay from the phone (spends money and server time: needs a spend limit, a confirmation and a separate
   approval) and the US / UK / Japan market pages (display only).
