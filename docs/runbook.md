# Trading Agent runbook

How to run the dashboard (GUI) on your laptop, how the `.env` settings work, how to run things through
Claude Code, and how to operate the Oracle Cloud server. Commands are copy-paste ready.

> Safety first: nothing sends a real order unless **both** `GROWW_LIVE_ORDERS=true` and `AUTO_TRADE=true` are set
> (or you press a live-order button with `GROWW_LIVE_ORDERS=true`). Keep both `false` until the live test is done.
> Never paste keys, secrets or `.env` contents into chats, screenshots or GitHub.

---

## 1. Where things are

| What | Laptop | Server |
|---|---|---|
| Code | `C:\projects\Share Trade` (your working copy) and `C:\projects\Share Trade\idev` (a clean clone) | `/opt/trading-agent` (owned by user `agent`) |
| Python | `.venv` inside the project | `/opt/trading-agent/.venv` |
| Settings | `.env` in the project folder | `/opt/trading-agent/.env` (mode 600, only `agent` can read it) |
| State (accounts, caches, logs) | `state\` in the project folder | `/opt/trading-agent/state/` |
| Dashboard | http://127.0.0.1:8787/ | http://127.0.0.1:8788/ on the laptop, through the SSH tunnel (section 4) |
| Repo | https://github.com/idarkdeveloper/idev (public) | same, pulled over HTTPS |

Server: Oracle Cloud, region India West (Mumbai), Ubuntu 24.04 on Ampere A1 (2 cores, 12 GB), reserved public
IP **130.210.18.7** (registered with Groww as the static IP). Login user `ubuntu`, SSH key `C:\Users\affaf\.ssh\oracle_agent`.

---

## 2. Run the dashboard on the laptop

Open **PowerShell** (prompt starts with `PS C:\`):

```powershell
cd "C:\projects\Share Trade"
.\.venv\Scripts\Activate.ps1          # prompt now starts with (.venv)
python -m trading_agent ui            # opens http://127.0.0.1:8787/ in your browser
```

Useful variants:

```powershell
python -m trading_agent ui --no-open         # don't open a browser tab
python -m trading_agent ui --port 8790       # another port (e.g. if 8787 is busy)
python -m trading_agent ui --demo            # offline sample data, never touches Groww or your .env
```

Pages: **Live** `/` (your real account, real orders only), **Demo** `/demo` (same real data, practice money),
**Replay** `/replay` (practise on a past date). Theme switch (Dark / Light / Auto) is top right.

Stop it with **Ctrl+C** in that PowerShell window.

First time on a new laptop (or after `git pull` brought new packages):

```powershell
cd "C:\projects\Share Trade"
py -3.12 -m venv .venv                       # only if .venv does not exist yet
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env                       # then fill it in (section 3)
python -m pytest -q                          # optional: all tests should pass, offline
```

If PowerShell refuses to run `Activate.ps1`: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, once.

> Run **one** watch at a time. If the server runs `trading-agent-watch`, don't also run `watch` on the laptop,
> or every alert and daily email arrives twice. The laptop dashboard alone (`ui`) is fine.

---

## 3. The `.env` settings

The `.env` file holds your keys and switches. It is in `.gitignore` and must never be committed. Each machine has
its own (laptop: project folder; server: `/opt/trading-agent/.env`). Edit on the laptop:

```powershell
notepad "C:\projects\Share Trade\.env"
```

Edit on the server (after `ssh`, section 4):

```bash
sudo -u agent nano /opt/trading-agent/.env      # Ctrl+W find · Ctrl+O then Enter save · Ctrl+X exit
sudo systemctl restart trading-agent-watch trading-agent-dashboard   # apply
```

### Keys (secrets — never share)

| Setting | What |
|---|---|
| `ANTHROPIC_API_KEY` | Claude, for the daily check and (fallback) email summaries |
| `GROWW_API_KEY` + `GROWW_TOTP_SECRET` | Groww login without daily approval (recommended, used on the server). The TOTP secret is 32 characters, A–Z and 2–7 only. |
| `GROWW_API_SECRET` | Older login: needs **Approve** on Groww's API page every day. Leave empty when using TOTP. |
| `GROWW_ACCESS_TOKEN` | Optional: a token you generated yourself (expires 06:00 IST) |
| `RESEND_API_KEY` | Email delivery |
| `NOTIFY_WEBHOOK_URL` | Optional Slack / Discord / n8n webhook |
| `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` | Optional Telegram alerts (section 13); `TELEGRAM_ALERTS=false` switches them off |
| `HEARTBEAT_URL` | Optional dead-man's switch ping URL, healthchecks.io style (section 12) |
| `TA_ALLOWED_HOSTS` | Optional, extra dashboard Host names for phone access (section 15) |

### Safety switches (keep as shown until the live test)

| Setting | Value | Meaning |
|---|---|---|
| `GROWW_LIVE_ORDERS` | `false` | `true` = orders go to Groww with real money |
| `AUTO_TRADE` | `false` | `true` = the agent may place orders by itself |
| `GROWW_GTT_STOPS` | `false` | `true` = keep a stop-loss (GTT) at Groww for each live holding |
| `GROWW_ALLOWED_IP` | `130.210.18.7` | live orders are refused from any other IP |
| `MAX_SLIPPAGE_PCT` | `0.5` | live limit orders are placed within this % of the last price |

### Strategy, alerts and daily emails

| Setting | Example | Meaning |
|---|---|---|
| `MARKET` | `in` | India (NSE + Groww) |
| `INVESTORS` | `ASHISH KACHOLIA, VIJAY KEDIA` | up to 10 names, comma separated |
| `WATCH_SOURCE` | `deals` | `deals` (bulk + block), `bulk`, `block`, `insider` |
| `PAPER_STARTING_CASH` | `100000` | practice account size |
| `NOTIFY_EMAIL_TO` / `NOTIFY_EMAIL_FROM` | `affafhashmi02@gmail.com` / `Trading Agent <onboarding@resend.dev>` | with Resend's test sender, mail only reaches your Resend account address; verify a domain at resend.com/domains to use any address |
| `NEWS_TAGGER` | `auto` | Ollama if running, else none (Claude only if set to `claude`) |
| `OLLAMA_URL` / `OLLAMA_MODEL` | `http://127.0.0.1:11434` / `qwen2.5:3b` | local language model for news tags and email summaries |
| `DIGEST_ENABLED`, `DIGEST_MORNING_ON`, `DIGEST_EVENING_ON` | `true` | daily emails on/off |
| `DIGEST_MORNING` / `DIGEST_EVENING` | `09:00` / `15:45` | IST send times (before the open / after the close) |
| `DIGEST_UNIVERSE` / `DIGEST_TOP` | `NIFTYMIDCAP150` / `10` | where the morning buy ideas come from |
| `DIGEST_WRITER` | `auto` | summary by Ollama, else Claude (`DIGEST_CLAUDE_MODEL`), else none |

The dashboard's **Settings** (Live page only) can change the non-secret settings safely; keys are edited in `.env`.

---

## 4. Use the server's dashboard from the laptop (GUI from the cloud)

The server's dashboard listens only on the server itself. Reach it through an SSH tunnel.

**One click** (recommended):

```powershell
powershell -ExecutionPolicy Bypass -File "C:\projects\Share Trade\deploy\windows\open-server-dashboard.ps1"
```

It opens a window titled *Trading Agent tunnel* and then your browser at http://127.0.0.1:8788/. Close that window
when you're done. To make a desktop icon: right-click the desktop → New → Shortcut → paste
`powershell -ExecutionPolicy Bypass -File "C:\projects\Share Trade\deploy\windows\open-server-dashboard.ps1"`.

**By hand** — on the **laptop**, not inside the server:

```powershell
ssh -i $HOME\.ssh\oracle_agent -N -L 8788:127.0.0.1:8787 ubuntu@130.210.18.7
```

The window stays silent; that is the tunnel. Browse to http://127.0.0.1:8788/, Ctrl+C to stop.

Which prompt am I at? `PS C:\...>` = laptop. `ubuntu@trading-agent:~$` = server (type `exit` to go back).

---

## 5. Operate the server

Log in (from the laptop):

```powershell
ssh -i $HOME\.ssh\oracle_agent ubuntu@130.210.18.7
```

Everyday commands (on the server):

```bash
# are the services running?
systemctl status trading-agent-watch trading-agent-dashboard --no-pager

# live logs (Ctrl+C to stop); last 100 lines
sudo journalctl -u trading-agent-watch -f
sudo journalctl -u trading-agent-watch -n 100 --no-pager
sudo journalctl -u trading-agent-dashboard -n 100 --no-pager

# restart after editing .env
sudo systemctl restart trading-agent-watch trading-agent-dashboard

# run any agent command as the agent user (same CLI as on the laptop)
cd /opt/trading-agent
sudo -u agent .venv/bin/python -m trading_agent groww-check --ip   # which IP Groww sees (no login)
sudo -u agent .venv/bin/python -m trading_agent groww-check        # read-only Groww check (one login)
sudo -u agent .venv/bin/python -m trading_agent holdings           # your Groww holdings
sudo -u agent .venv/bin/python -m trading_agent news --check-tagger
sudo -u agent .venv/bin/python -m trading_agent digest morning     # preview the morning email (prints)
sudo -u agent .venv/bin/python -m trading_agent digest evening --send   # build and email it now

# disk, memory, Ollama
df -h / && free -h
systemctl status ollama --no-pager && ollama list
```

**Update the server to the latest code** (from the laptop, one line):

```powershell
ssh -i $HOME\.ssh\oracle_agent ubuntu@130.210.18.7 "cd /opt/trading-agent && sudo -u agent git pull && sudo -u agent .venv/bin/pip install -q -r requirements.txt && sudo systemctl restart trading-agent-watch trading-agent-dashboard"
```

**Copy a file to the server** (example: a holdings snapshot):

```powershell
scp -i $HOME\.ssh\oracle_agent $HOME\Downloads\groww_holdings.json ubuntu@130.210.18.7:/tmp/
ssh -i $HOME\.ssh\oracle_agent ubuntu@130.210.18.7 "sudo install -o agent -g agent -m 600 /tmp/groww_holdings.json /opt/trading-agent/state/groww_holdings.json && rm /tmp/groww_holdings.json"
```

**Back up the server's state** to the laptop (accounts, saved holdings, logs; not `.env`):

```powershell
ssh -i $HOME\.ssh\oracle_agent ubuntu@130.210.18.7 "sudo tar czf /tmp/agent-state.tgz -C /opt/trading-agent state && sudo chown ubuntu /tmp/agent-state.tgz"
scp -i $HOME\.ssh\oracle_agent ubuntu@130.210.18.7:/tmp/agent-state.tgz $HOME\Downloads\
```

### What runs on the server

| Service | Does | Starts |
|---|---|---|
| `trading-agent-watch` | deals, announcements, news, stops every minute in market hours; daily emails; the paper forward test after the close; heartbeat | at boot, restarts on failure |
| `trading-agent-dashboard` | the web page on 127.0.0.1:8787 (tunnel only) | at boot |
| `ollama` | local language model (news tags, email summaries) on 127.0.0.1:11434 | at boot |

Only port 22 (SSH) is open to the internet. Unit files: `deploy/systemd/*.service`.

### Oracle Cloud console notes

- Region **India West (Mumbai)**; instance **trading-agent**; reserved IP **trading-agent-ip = 130.210.18.7**.
- Always Free: Ampere A1 up to 4 cores / 24 GB, 200 GB disk. Check Billing after changes.
- Don't release the reserved IP: Groww allows changing the registered static IP only once every 7 days.
- Rebooting the instance is safe; the services come back by themselves.

---

## 6. Groww login: what to know

- A Groww login token expires every day at **06:00 IST**. The agent creates one per day and reuses it.
- Groww allows **150 token requests per 24 hours** and **30 per minute** (account-wide, any key, any machine).
  Too many → `429 Too Many Requests`. The agent then **waits** (15 min, doubling, up to 6 h; until 06:00 IST for
  the daily cap) instead of retrying, and keeps working on Yahoo prices.
- Never loop `groww-token` / `groww-check`. `--force` skips the wait and can lengthen Groww's block.
- When Groww is unavailable the dashboard and emails use the **saved holdings** (`state/groww_holdings.json`),
  priced from Yahoo and marked "saved".
- TOTP login (`GROWW_API_KEY` + `GROWW_TOTP_SECRET`) needs no daily Approve; keep the server clock synced (it is).

---

## 7. Running things through Claude Code

Open Claude Code in `C:\projects\Share Trade` and ask in plain words, for example:

| Ask Claude | What happens |
|---|---|
| "start the dashboard" | runs `python -m trading_agent ui` from the project's venv |
| "open the server dashboard" | runs `deploy\windows\open-server-dashboard.ps1` |
| "check the server" / "show the watch log" | `ssh` + `systemctl status` / `journalctl`, read-only |
| "update the server" | the one-line update from section 5 |
| "preview tomorrow's morning email" | `digest morning` on the server |
| "update coverage" | refreshes the coverage page |

Claude never sees your `.env` values: checks report only "filled / empty". Claude does not place real orders or
call Groww with your keys unless you explicitly ask for a specific live action.

---

## 8. Times (India vs Germany)

| Event | IST | Germany until 25 Oct 2026 (CEST) | Germany from 25 Oct (CET) |
|---|---|---|---|
| Groww token reset | 06:00 | 02:30 | 01:30 |
| Morning email | 09:00 | 05:30 | 04:30 |
| Market open | 09:15 | 05:45 | 04:45 |
| Market close | 15:30 | 12:00 | 11:00 |
| Evening email | 15:45 | 12:15 | 11:15 |

Markets are closed on weekends and NSE holidays; the agent knows the holiday list and sends nothing then.

---

## 9. Troubleshooting

| Symptom | Fix |
|---|---|
| `Identity file ... not accessible` | you ran a laptop command inside the server: type `exit`, run it in `PS C:\` |
| Dashboard tunnel: `bind ... address already in use` | a tunnel is already open: just browse to http://127.0.0.1:8788/ |
| "Groww refused a new login token (429)" | wait until the time shown; don't retry; see section 6 |
| Email not arriving | check Spam; with `onboarding@resend.dev` only your Resend account address gets mail |
| Holdings card says "saved" | Groww is unavailable right now; numbers use Yahoo prices |
| `.env` changes ignored | restart the services (section 5) |
| News tags missing | `news --check-tagger`; on the server `systemctl status ollama` |
| Alerts arrive twice | a watch runs on both laptop and server: stop one |

## 10. Before the first live trade

1. `groww-check` works on the server (holdings read, IP 130.210.18.7).
2. During NSE hours, run once on the server:
   `sudo -u agent .venv/bin/python -m trading_agent groww-check --live-test TATASTEEL --i-understand-real-orders`
   (places and cancels a 1-share limit order below the market and a 1-share GTT).
3. Only then consider `GROWW_LIVE_ORDERS=true`; `AUTO_TRADE=true` is a separate, later decision.
4. If you live in Germany long-term you may count as NRI: check with Groww or a tax adviser whether your account
   type is right before trading live.

---

## 11. One engine: the server (and what is left of GitHub Actions)

The Oracle server (`trading-agent-watch`) is the **only** engine: deals, stops, announcements, news, the two daily
emails and the paper forward test (once per trading day after the close, 16:10 IST; a claim file
`state/forward_<day>.claim` stops a restart from running it twice). `.github/workflows/routine.yml` no longer has a
schedule. It is a **manual dry run**: Actions, *trading-agent manual dry run*, **Run workflow**. It fetches deals and
prints what a check would do, with no Groww login, no Claude call, no email and no order.

**One-time steps after deploying this version**

1. On the server, update and restart:
   ```bash
   cd /opt/trading-agent && sudo -u agent git pull
   sudo -u agent .venv/bin/pip install -r requirements.txt
   sudo systemctl restart trading-agent-watch trading-agent-dashboard
   ```
2. Recreate the forward test (its state used to live only in the GitHub Actions cache), once:
   ```bash
   cd /opt/trading-agent
   sudo -u agent .venv/bin/python -m trading_agent forward --rebuild-from 2026-10-09 --universe NIFTYMIDCAP150
   ```
   It ranks the index members **on that date** (point-in-time membership from the NSE index notices; it stops with a
   clear message if that history only starts after the date), buys the top 20 in equal weights with the same charges
   at the close of 9 Oct 2026 and puts the same capital into MID150BEES, the way the first routine run did. Fills and
   the account start are stamped 9 Oct, not today. Every pick and the benchmark need a price bar dated exactly 9 Oct:
   a pick without one is left out and listed in the output (no older close is used); weekends and NSE holidays are
   refused. It refuses if a forward account already exists; add `--force` to delete that account and rebuild. Check
   with `... forward --status`. Days between 9 Oct and today have no daily point (the curves start again from the first
   server run). Use the same `--top` and `--capital` (default `PAPER_STARTING_CASH`) as before if you changed them.
   Also put `FORWARD_START=2026-10-09` in `/opt/trading-agent/.env` (it is in `.env.example`). The server never
   starts a fresh forward account silently: with no account it rebuilds from `FORWARD_START`, or, without that
   setting, does nothing and sends one `[FORWARD]` alert a day until you run the rebuild.
3. In GitHub, **Settings, Secrets and variables, Actions**: delete these secrets: `GROWW_API_KEY`,
   `GROWW_API_SECRET`, `GROWW_TOTP_SECRET`, `GROWW_ALLOWED_IP`, `GROWW_PROXY_URL`, `ANTHROPIC_API_KEY`,
   `RESEND_API_KEY`, `NOTIFY_WEBHOOK_URL`, `QUIVER_API_KEY`, `ALPACA_API_KEY_ID`, `ALPACA_API_SECRET_KEY` (the dry run
   uses none of them). Delete these variables: `AUTO_TRADE`, `GROWW_LIVE_ORDERS`, `GROWW_GTT_STOPS`,
   `MAX_SLIPPAGE_PCT`, `NOTIFY_EMAIL_TO`, `NOTIFY_EMAIL_FROM`, `FORWARD_UNIVERSE`. Keep `MARKET`, `INVESTORS`,
   `WATCH_INVESTOR`, `WATCH_SOURCE`, `PAPER_STARTING_CASH` if you set them (they only steer the dry run).
4. Optional: *Actions, Caches*: delete the old `trading-agent-state-*` caches.

If you ever see two engines (alerts arriving twice, Groww 429), check that nothing else runs `watch`.

---

## 12. Dead-man's switch (heartbeat)

If the server or the watch service dies, nothing tells you. Set `HEARTBEAT_URL` and a monitoring site will email you
when the pings stop. A separate heartbeat thread calls the URL **every 60 seconds, at any hour** (weekends too), with a 10
second timeout, and only while the watch loop is making progress (the loop stamps its progress between the steps of a
tick, so a long tick that keeps moving is not a stall). If the loop has not moved for 3 minutes in the market window
(09:15 to 15:30 IST on a trading day; 3 x the check interval if that is longer), or for max(3 x the check interval, 20
minutes) outside it, the thread sends `/fail` with "watch loop stalled N min" instead of an
ok ping, and a tick that raised sends `/fail` with the error text at once. The next healthy moment pings ok at once.
`/fail` is sent only to hc-ping.com addresses (or when `HEARTBEAT_FAIL=true`); other providers just stop getting
pings, which their own grace period turns into an alert. The URL and the bot token are never logged (redacted even
with `-v`), only "heartbeat ok" / "heartbeat failed". After changing `HEARTBEAT_URL` or the Telegram settings, restart
`trading-agent-watch` (the dashboard Settings dialog saves them to `.env` but the running service keeps its old ones).

**Free healthchecks.io check**

1. Sign up at https://healthchecks.io, **Add Check**. Name: `trading-agent-watch`.
2. **Period** 1 minute, **Grace** 2 minutes. (The service pings every minute around the clock, so a dead service is
   reported about 3 minutes after the last ping, at any hour. No schedule or cron setting is needed.)
3. **Integrations**: make sure *Email* has your address, and add the **Telegram** integration (healthchecks.io:
   Integrations, *Telegram*, follow its bot link) so a missed ping reaches your phone at once.
4. Copy the check's **ping URL** (`https://hc-ping.com/<uuid>`), then on the server:
   ```bash
   sudo -u agent nano /opt/trading-agent/.env     # add the line: HEARTBEAT_URL=https://hc-ping.com/xxxxxxxx-...
   sudo systemctl restart trading-agent-watch
   ```
   (The dashboard Settings dialog can set it too, but keep the URL out of screenshots.)
5. Within a minute the check turns green. Test it: `sudo systemctl stop trading-agent-watch`, wait about 3 minutes in
   market hours for the Telegram message and email, then start it again.

Better Stack and UptimeRobot heartbeat monitors work the same way: paste their heartbeat URL (it must be `https://`).

---

## 13. Telegram alerts

With a bot token and a chat id every alert (deals, stops, news, orders) and both daily emails also go to Telegram:
the short summary and the key lines (portfolio total, buy ideas, holdings to watch), and the evening bulletin's chart
pictures as one album. A Telegram failure never stops an email. The token is part of Telegram's request URL, so it is
redacted (`bot***`) from every log line.

1. In Telegram, open **@BotFather**, send `/newbot`, pick a name and a username ending in `bot`. It replies with a
   token like `123456789:AAH...`. Keep it secret.
2. Open your new bot and press **Start** (send `/start`). A bot can only message people who started it.
3. Get your chat id: open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser (put the token in) and find
   `"chat":{"id":123456789`. A group id is negative (`-100...`); a public channel can use `@channelname` (add the bot as
   an admin). Do this on a private computer: the URL contains the token.
4. On the server:
   ```bash
   sudo -u agent nano /opt/trading-agent/.env
   #   TELEGRAM_BOT_TOKEN=123456789:AAH...
   #   TELEGRAM_CHAT_ID=123456789
   sudo systemctl restart trading-agent-watch
   ```
   `TELEGRAM_ALERTS=false` (or the dashboard's *Telegram alerts* switch) turns it off without removing the token.

---

## 14. Email that lands in the inbox: your own sender domain

With Resend's test sender (`onboarding@resend.dev`) mail only reaches your own Resend address and is easily marked as
spam. With a verified domain, the agent also adds the `List-Unsubscribe` and `List-Unsubscribe-Post` headers (Gmail and
Yahoo expect them). It does so only when `NOTIFY_EMAIL_FROM` is not on `resend.dev`.

1. resend.com, **Domains, Add Domain** (a subdomain such as `mail.yourdomain.com` is best).
2. Add the DNS records Resend shows at your DNS host: **SPF** (a `TXT` record on the `send` subdomain), **DKIM** (a `TXT`
   record named `resend._domainkey`), and the MX record for bounces. Press **Verify** until it shows *Verified*.
3. Add **DMARC** as a `TXT` record named `_dmarc.yourdomain.com`. Start in monitoring mode:
   `v=DMARC1; p=none; rua=mailto:you@yourdomain.com`. After two to four weeks of clean reports (everything passes SPF
   and DKIM) change it to `p=quarantine`.
4. In `.env`: `NOTIFY_EMAIL_FROM=Trading Agent <agent@mail.yourdomain.com>`, then restart the watch service.

Note: the headers are `List-Unsubscribe: <mailto:...>` and `List-Unsubscribe-Post: List-Unsubscribe=One-Click`. A
mailto-only `List-Unsubscribe` with the `-Post` header is not RFC 8058 one-click (that needs an https URL that accepts
the POST); it still gives Gmail and Yahoo an unsubscribe address, but do not count on the one-click button. These are
single-recipient alerts to yourself, so this is a deliverability nicety, not a mailing-list feature.

Subjects stay plain and free of promotional words (no "free", "offer", "!!!"); a test lists the current ones.

## 15. Phone access (Tailscale)

The dashboard only answers to `127.0.0.1`, `localhost` and `[::1]` (with or without its port). A request with any other
`Host` header gets HTTP 421: that is what stops a malicious website from reaching the dashboard through DNS rebinding.
To use it from a phone, run `tailscale serve` on the machine (it proxies `https://<host>.<tailnet>.ts.net` to
`127.0.0.1:8787`) and add that exact name to `.env`:

    TA_ALLOWED_HOSTS=box.tail1234.ts.net

Several names are comma separated; wildcards are refused. Restart the dashboard after editing. Every POST must also be
JSON and same-origin: a browser's `Origin` must match an allowed host, and `X-Forwarded-Host` / `X-Forwarded-Proto` are
trusted only from a proxy on localhost and only for an allowed host. Page loads and API reads from another site are
refused too (`Sec-Fetch-Site`).

---

## 16. Pre-market check, clock, circuit breaker and backups

**Pre-market check (the canary).** At 08:30 IST on trading days the watch service reads NSE, BSE, Yahoo, NSE's archive
files, the server clock, and (with an already cached token only) your Groww holdings, order list and available cash. It
never places an order and never asks Groww for a new token. The result is `state/integration_check.json`; the dashboard
shows `Integration: ok 08:30 · 9/9` beside the freshness chip, and the morning email carries `Pre-market check: ok 9/9`
or `FAILED: ...`. If a step fails, or the check has not run by 09:10, `state/canary_failed.json` is written: **automated
buys are refused** (the agent's paper and live buys and any live Groww buy) with the reason `pre-market check failed:
<steps>`, an amber banner shows on the dashboard, and one alert goes out by email, webhook and Telegram. Sells and stop
exits are never blocked. It clears on the next passing run. To clear it after fixing the cause:

```bash
sudo -u agent /opt/trading-agent/.venv/bin/python -m trading_agent integration-check
```

(or the *Run integration check* button in Settings). Turn the whole thing off with `INTEGRATION_CHECK=false`.

**Server clock.** Groww TOTP logins need a correct clock. At start-up, and as a pre-market step, the service reads
`timedatectl show -p NTPSynchronized` and the offset from `timedatectl timesync-status` or `chronyc tracking`; it fails
when NTP is not synchronised or the offset is over 1 second (one alert a day). Fix: `sudo timedatectl set-ntp true`,
then restart `trading-agent-watch`. On Windows nothing is checked.

**NSE / Yahoo circuit breaker.** After 3 refusals in a row (403, 429, 5xx, a timeout) NSE calls are skipped for 30 s, then
60 s, then 300 s instead of retried, and resume after the first success; the log says `NSE connection degraded` and
`NSE connection recovered` once each, and the dashboard chip shows `NSE degraded`. Yahoo history fetches work the same way.

**Backups.** Every save of `state/state.json` first keeps the old file as `state/state.json.bak`. Once a trading day after
the close (16:00 IST) the watch service copies `state/*.json` and the price archive (`state/prices/archive.sqlite`, via
SQLite's backup API) into `state/backups/YYYYMMDD/`, and keeps 30 days. `.env` and anything with "token" or "secret" in
its name are never copied. **Restore:**

```bash
sudo systemctl stop trading-agent-watch trading-agent-dashboard
cd /opt/trading-agent/state
ls backups                                   # pick a day
sudo -u agent cp backups/20261012/state.json state.json          # or any *.json from that folder
sudo -u agent cp backups/20261012/archive.sqlite prices/archive.sqlite
sudo systemctl start trading-agent-watch trading-agent-dashboard
```

If only the latest save went wrong, `cp state.json.bak state.json` is enough.
