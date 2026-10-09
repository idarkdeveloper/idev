# idev25 — Claude trading agent (Groww + NSE)

An AI trading agent that **watches one investor's publicly disclosed trades on the NSE,
compares new activity against your Groww portfolio, and sends you a recommendation when
something changes** — the setup from the "Claude can now build an AI trading agent" reel,
as code you own, adapted for the Indian market. (A US mode with QuiverQuant + Alpaca is
also included.)

```
NSE bulk / block deals, insider filings ──▶ diff vs. remembered trades
                                                   │ new trade by the watched investor
                                                   ▼
                        Claude (tool use) ── get_portfolio (your Groww holdings) / get_latest_price (Groww LTP)
                                                   │  send_recommendation ──▶ console / email / webhook
                                                   │  place_paper_order (opt-in) ──▶ paper simulator (or Groww, double opt-in)
                                                   ▼
                                          state/state.json (seen trades, run log)
```

| Reel step | Here |
|---|---|
| Connect a brokerage | `trading_agent/groww.py` — Groww Trade API (holdings, live prices, orders) |
| Connect market data | `trading_agent/nse.py` — NSE bulk deals, block deals, SEBI insider (PIT) filings |
| Give Claude a strategy | `trading_agent/agent.py` — system prompt + tools (`send_recommendation`, `place_paper_order`, …) |
| Create a routine | `.github/workflows/routine.yml` (cron) or `python -m trading_agent loop` |
| Paper-trade first | Groww has no sandbox, so orders fill in a local simulator seeded with your real holdings. Real orders need an explicit double opt-in. |

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env        # add ANTHROPIC_API_KEY; Groww keys optional

# 1. See the pipeline on bundled sample deals, no API calls at all
python -m trading_agent check --demo --dry-run

# 2. Let Claude analyse the sample deals (needs ANTHROPIC_API_KEY)
python -m trading_agent check --demo

# 3. Real NSE data: who did ASHISH KACHOLIA trade this month? (no key needed for NSE data)
python -m trading_agent check --investor "ASHISH KACHOLIA"

# 4. Keep checking every 30 minutes
python -m trading_agent loop --every 30
```

Other commands: `portfolio` (account + P&L), `history` (past recommendations),
`reset` (forget seen trades, reset the paper account), `groww-token` (mint a daily token),
`check --json`, and `--market us` to switch to the US stack.

## The dashboard

```bash
python -m trading_agent ui            # opens http://127.0.0.1:8787 in your browser
python -m trading_agent ui --demo     # same, on bundled sample deals
```

A local web page served by the package itself (no extra dependencies) that shows the
watched investor's disclosed deals with new ones flagged, your paper portfolio with live
P&L, Claude's recommendations with a one-click paper order, and the run log. The
**Run check now** button runs the same check as the CLI in the background. **Settings**
edits the investor, disclosure source, order mode and notification targets and writes
them to `.env`; API keys stay in `.env` by hand, and live Groww orders can never be
switched on from the page.

## Linking Groww

**Which plan?** The **Free Trial** (₹0) is enough. It includes holdings, positions, margin
and order APIs; it excludes *Live Data* and *Backtesting*, which this agent does not need:
prices come from Yahoo Finance for free (`trading_agent/prices.py`, NSE symbols with
`.NS`). Pick the ₹499/month plan only if you want Groww's own real-time quotes; the agent
will then use them automatically and keep Yahoo as a fallback.

1. In Groww, open **Trading APIs** (https://groww.in/trade-api) and either copy the daily
   **access token** (expires 06:00 IST) or create an **API key** with a secret or TOTP.
2. Put it in `.env` as `GROWW_ACCESS_TOKEN`, or `GROWW_API_KEY` + `GROWW_API_SECRET`
   (approval flow, needs a daily tap in the app), or `GROWW_API_KEY` + `GROWW_TOTP_SECRET`
   (no daily approval). `python -m trading_agent groww-token` prints a fresh token.
3. Run `python -m trading_agent portfolio`. The first run mirrors your real holdings and
   cash into `state/paper_broker.json`; from then on paper fills happen there while prices
   stay live from Groww.

Real orders on Groww are sent only when **both** `AUTO_TRADE=true` and
`GROWW_LIVE_ORDERS=true`. Even then, each order is capped at 10% of equity, uses whole
shares, market type, CNC product on NSE, and Claude is told it is trading real money.

## Which investors can I follow?

`WATCH_INVESTOR` is matched against the **client name** in NSE bulk and block deals, or the
acquirer name in insider filings. Matching is case-insensitive and ignores word order,
because the exchange prints names surname-first and inconsistently (`KACHOLIA ASHISH`,
`MUKUL MAHAVIR AGRAWAL`, `ESTATE OF LATE MR. RAKESH JHUNJHUNWALA`). Bulk deals only show
trades above 0.5% of a company's shares, so famous investors appear only a few times a
year; the names that appear weekly are mostly prop desks and operators. Run a backtest
before trusting anyone:

```bash
python -m trading_agent backtest --investor "MUKUL AGRAWAL" --days 365
python -m trading_agent backtest --demo
```

It replays every disclosed deal: entry at the first close after the deal date (NSE
publishes that evening), hold 5 / 20 / 60 trading days, excess return over NIFTY 50
(`^NSEI`) after a round-trip cost (`--cost-bps`, default 50), hit rate, and a split by
who traded. Prices come from Yahoo Finance and are cached under `state/cache/`.

## Momentum and "who traded"

Two filters sit between a disclosure and a recommendation, based on what the evidence
on Indian markets supports:

- **Momentum** (`trading_agent/momentum.py`): trailing 1/3/6/12-month returns, 12-1
  momentum, 200-day MA position, 60-day turnover and a verdict (strong / neutral / weak).
  Claude is told to turn a disclosed buy in a weak-momentum stock into a *watch*, not a
  buy. `python -m trading_agent momentum SENCO RELIANCE` prints it.
- **Client type** (`trading_agent/investors.py`): promoter/insider, institution,
  individual, corporate or broker desk, from the name as NSE prints it. Promoter and
  institutional deals are weighted up; broker desks and corporate treasuries down.

`WATCH_SOURCE`: `deals` (bulk + block, default), `bulk`, `block`, or `insider`.

## Configuration (`.env`)

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Claude. Model defaults to `claude-opus-5-5` (`CLAUDE_MODEL`). |
| `MARKET` | `in` (default: NSE + Groww) or `us` (QuiverQuant + Alpaca). |
| `WATCH_INVESTOR`, `WATCH_SOURCE` | Who to follow and which disclosures to read. |
| `GROWW_ACCESS_TOKEN` / `GROWW_API_KEY` + `GROWW_API_SECRET` or `GROWW_TOTP_SECRET` | Groww access. Unset = local simulator only. |
| `GROWW_LIVE_ORDERS` | `false` (default) = paper fills with real holdings/prices. `true` = real orders. |
| *(prices)* | Yahoo Finance is used automatically when Groww Live Data is unavailable or no broker is linked. |
| `AUTO_TRADE` | `false` (default) = recommendations only. `true` = Claude may place orders. |
| `PAPER_STARTING_CASH` | Cash for the simulator when no brokerage is linked (default ₹5,00,000). |
| `RESEND_API_KEY` + `NOTIFY_EMAIL_TO` | Email each recommendation via Resend. |
| `NOTIFY_WEBHOOK_URL` | POST `{"text": ...}` to Slack/Discord/n8n/etc. |
| `QUIVER_API_KEY`, `ALPACA_*` | US mode only. |

## The routine (runs by itself)

`.github/workflows/routine.yml` runs the check every 30 minutes on weekdays from NSE open
through the evening bulk/block-deal publication, keeping `state/` between runs with the
Actions cache. A run with nothing new exits before calling Claude; a run without the
Claude secret degrades to a dry run instead of failing.

Setup, in the repository's **Settings → Secrets and variables → Actions**:

| Kind | Name | Required? | Notes |
|---|---|---|---|
| Secret | `ANTHROPIC_API_KEY` | **yes** | Without it the routine only lists new deals. |
| Secret | `GROWW_API_KEY` + `GROWW_TOTP_SECRET` | no | Mirrors your real Groww holdings. TOTP flow needs no daily approval; `GROWW_API_SECRET` works too but needs a daily tap in the app. |
| Secret | `RESEND_API_KEY`, `NOTIFY_WEBHOOK_URL` | no | Email / chat delivery. |
| Variable | `WATCH_INVESTOR` | no | Defaults to `ASHISH KACHOLIA`. |
| Variable | `NOTIFY_EMAIL_TO`, `NOTIFY_EMAIL_FROM` | no | With `RESEND_API_KEY`. |
| Variable | `AUTO_TRADE`, `GROWW_LIVE_ORDERS` | no | Both default to `false`. |
| Variable | `MARKET`, `WATCH_SOURCE`, `PAPER_STARTING_CASH` | no | Defaults: `in`, `deals`, `500000`. |

Then open **Actions → trading-agent routine → Run workflow** (tick *dry_run* for a first
look) and check the job log.

## How a check works

1. Fetch the last 30 days of bulk and block deals (and insider filings if selected),
   keep the watched investor's rows, drop the ones already in `state/state.json`.
2. Nothing new → exit without calling Claude.
3. Something new → Claude gets the new deals and tools to inspect your holdings, live
   prices and the investor's history in that stock. It calls `send_recommendation`
   (buy / sell / hold / watch with rationale and confidence) and, with `AUTO_TRADE=true`,
   may call `place_paper_order`.
4. Trades are remembered only after a successful analysis, so API hiccups are retried.

Safety rails: paper fills by default, Groww live orders behind a double opt-in, per-order
size cap, concentration rule in the prompt, whole-share rounding, and server-side refusal
fallback on the Claude request.

## Notes on the data

NSE's JSON endpoints are public but undocumented; the client sends browser-like headers.
Bulk/block deals are reliable. The insider (PIT) endpoint sometimes returns an empty list
without a browser session, so treat `WATCH_SOURCE=insider` as best-effort.

## Development

```bash
python -m pytest -q
```

Tests run fully offline with a scripted fake Claude runner and fake HTTP sessions.

**Not financial advice.** Bulk-deal client names can be brokers acting for someone else,
and a disclosed trade tells you nothing about the investor's reasons. Treat this as a
research assistant, not a signal.
