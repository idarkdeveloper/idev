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

## Global context, announcements and watch mode

- **Global regime** (`trading_agent/regime.py`): Nifty vs its 200-day MA and 20-day move,
  S&P 500 and Nasdaq futures overnight, Nikkei, India VIX, USD/INR and Brent, all free from
  Yahoo, condensed into `risk_on` / `neutral` / `risk_off` with sizing guidance. It heads
  every prompt and is a tool (`get_global_context`). Cross-market moves are priced at the
  Indian open, so this is used for sizing and drawdown control, never for direction.
- **NSE announcements** (`NSEClient.announcements`): results, board meetings, pledges,
  regulatory orders, business updates, with text and PDF link. Claude must check them
  before a buy (`get_announcements`); the dashboard shows them in the stock lookup.
- **Watch mode**: `python -m trading_agent watch --every 60` (or the *Start watch* button)
  polls deals and announcements for your positions and recent recommendations every
  minute between 08:45 and 18:30 IST on weekdays, notifying on anything new. Meant for a
  machine with a fixed IP, which the April 2026 SEBI rules require for live orders.

Everything the CLI does is also in the dashboard:

| CLI | Dashboard |
|---|---|
| `check`, `check --force`, `check --dry-run` | **Run check now** and its options menu |
| `watch`, `loop` | **Start watch** (interval and auto-exit on stops in Settings) |
| `portfolio`, `history`, `reset` | Paper portfolio, order history, recommendations, run log, Reset in Settings |
| `momentum` | Look up a stock (also click any ticker) |
| `size` | Position size panel, and *Suggest size* in the paper order ticket |
| `costs` | Trade cost panel with the full buy/sell breakdown |
| `screen`, `backtest` (+ `--json`) | Factor screen and Backtest panels, each with *Export JSON* |
| `factor-backtest` | *Backtest the screen as a portfolio* under the factor screen, with a growth-of-100 chart |
| `signal-lab` | *Signal lab* card: which algo-trading signals predicted the next week, month or quarter |
| `index-history` | Built automatically when the signal lab or portfolio backtest uses a broad index |
| `scorecard` | *Claude's track record* panel |
| `groww-token` | *Test Groww connection* in Settings (the token itself is never shown) |
| `--market`, `.env` strategy keys | Settings: investor, disclosures, market, starting cash, order mode, notifications |

The dashboard can also place paper buys and sells for any ticker and close positions;
these only ever touch the local paper account.

## What the big firms do, applied at retail size

- **Factor screen** (`trading_agent/screen.py`, `python -m trading_agent screen --universe NIFTY200`):
  ranks every constituent on 12-1 momentum, 6-month return and low 60-day volatility,
  requires price above the 200-day MA and a liquidity floor. The dashboard has the same
  panel. Quality and value factors need fundamentals and are not wired in yet.
- **Volatility-based sizing** (`trading_agent/risk.py`, `python -m trading_agent size SENCO`):
  risk 1% of equity on a 2x ATR(14) move, capped at 10% of equity, whole shares. Claude
  must call `suggest_position_size` for every buy and quote the stop.
- **Trailing stops**: the tighter of 3x ATR or 15% below the high since entry. The
  simulator tracks the high-water mark; the portfolio table shows the stop; watch mode
  alerts when a position breaks it and, with `AUTO_TRADE=true`, sells it in paper.
- **Trend filter**: the regime strip now carries the Nifty trend (50-day vs 200-day MA).
  In a downtrend Claude recommends no new buys.
- **Verified cost model** (`trading_agent/costs.py`, `python -m trading_agent costs`): Groww
  delivery brokerage (lower of ₹20 or 0.1%, min ₹5), STT 0.1% each side, NSE 0.00297%,
  SEBI 0.0001%, stamp 0.015% on buys, 18% GST, ₹20 DP charge per sell, plus an assumed
  15 bps slippage each way. Round trip is about 69 bps on ₹10,000 and 29 bps on ₹1,00,000
  before slippage. The paper simulator deducts it on every fill and the backtest uses it
  as the default cost.

## Keeping score

- **Claude's track record** (`python -m trading_agent scorecard`, dashboard panel): every
  stored recommendation is priced from the first close after it was made, over 5, 20 and
  60 trading days, against NIFTY 50. A buy counts as right only if it beat the index by
  more than its round-trip trading cost; a sell if the stock then lagged. Watch and hold
  calls are shown but not scored.
- **Account value over time**: every check and every paper order records the paper
  account's value (at most one point per 10 minutes), drawn as a curve in the dashboard,
  with the current and worst fall from peak (also printed by `portfolio`).
- **Factor portfolio backtest** (`python -m trading_agent factor-backtest --universe NIFTY50 --top 10 --years 4`):
  each month, rank the universe exactly as `screen` does on data available that day,
  hold the top N equal-weight, pay Indian delivery charges on every trade, and compare
  with NIFTY 50 and an equal-weight hold of the index members. Two things keep it honest:
  - **Past members, not today's.** For NIFTY 50, every change since March 2021 is built in
    (`trading_agent/membership.py`), so each month ranks only the stocks in the index that
    day, including the ones later dropped (UPL, IndusInd Bank, Wipro, ...). Using today's
    list instead lets the screen "buy" BSE, Trent or BEL years before they joined, on
    momentum it could not have seen as an index stock. For other indices, pass a change
    log with `--changes changes.csv` (`date,added,removed`); without one the printout warns
    that the result is survivorship-biased.
  - **A benchmark with dividends.** NIFTY 50 is the NIFTYBEES ETF, which reinvests
    dividends, like the stock returns do. The price-only index (`^NSEI`), which lags by
    about 1.2% a year, is still shown for reference.

  On NIFTY 50, top 10, Dec 2022 to Oct 2026, testing on past members took the factor
  portfolio from +68.8% to +20.8% after charges, against +24.8% for NIFTY 50 with
  dividends, with a worst fall of 26.7% against 14.0%. Treat any screen result on today's
  members as an upper bound.

The dashboard draws all of it: account value, a one-year price chart with the 200-day
average and your cost and stop for held stocks, the spread of backtest returns, and the
factor portfolio against its benchmarks.

## Can algo trading predict a share? The signal lab

`python -m trading_agent signal-lab --universe NIFTY50 --years 5` (or the *Signal lab*
card) measures whether the rules algo traders use actually predicted anything:

- **12 signals**: 12-1 momentum, 6-month return, price vs 200-day average, 50/200-day
  cross, MACD, nearness to the 52-week high, 1-month reversal, RSI, Bollinger bands,
  low volatility, volume surge, and the factor screen's score. A **walk-forward model**
  (ridge regression) also learns the best mix of them, retrained each period on past
  periods only.
- Each signal ranks that day's index members (past members for NIFTY 50). The trade
  starts at the next close and is held 5, 20 or 60 trading days, measured against
  NIFTY 50 with dividends.
- The score is the rank correlation between signal and result, its t-statistic, and
  the gap between the top and bottom fifth after Indian charges (about 0.8% a round
  trip at ₹25,000). Because a dozen signals at three horizons give one lucky t ≥ 2 in
  twenty, "predictive" needs t ≥ 3 and a gap bigger than the charges.

On NIFTY 50, Sept 2021 to Oct 2026, every signal and the model came out **no edge** at
every horizon. The best (RSI oversold, next month) reached t = 1.2. Large Indian stocks
priced these patterns in long ago.

Smaller companies are different. With past members rebuilt from NSE's own notices (below),
trend signals clear the t ≥ 3 bar in mid and small caps, and buying last month's losers
loses money:

| Sept 2021 to Oct 2026 | Midcap 150 | Smallcap 250 |
|---|---|---|
| Best next-month signal | 12-1 momentum, t = 3.2 | Factor screen score, t = 3.9 |
| Best next-quarter signal | Factor screen score, t = 3.4 | Near 52-week high, t = 4.6 |
| 1-month reversal, next quarter | t = −2.6 (reversed) | t = −2.9 (reversed) |

Whether that survives trading costs is a separate question, answered by the factor
portfolio backtest against each universe's own index fund:

| Top 20, monthly, after charges | Factor portfolio | Index fund |
|---|---|---|
| Midcap 150, Dec 2022 to Oct 2026 | +114.0% (worst fall −24.5%) | MID150BEES +89.8% (−20.5%) |
| Smallcap 250, Oct 2023 to Oct 2026 | +26.1% (−32.5%) | HDFCSML250 +44.1% (−25.7%) |

Midcaps kept an edge of about 3.7% a year after charges, with deeper falls. In smallcaps
the signal was real but monthly trading costs and wider swings ate it. One five-year
window is not proof; rerun it as new data arrives.

### Past members for the broad indices

`python -m trading_agent index-history NIFTYMIDCAP150 NIFTYSMALLCAP250 NIFTY100 NIFTY200`
downloads NSE Indices' "Replacements in indices" press releases since 2021 (once, cached
as text), parses every index's exclusions and inclusions, including revoked changes and
delistings, maps old tickers to today's using NSE's symbol-change file, and checks the
result. Walking back from today's list, the index must keep its exact size and every change
must fit. Midcap 150 and Smallcap 250 pass on every date. NIFTY 100 and 200 show two
known quirks: NSE ran them with 101 and 201 stocks for five months in 2024 while the
Tata Motors DVR was a second share class, and NIFTY 100 is off by one stock swap before
Sept 2021. The signal lab and the factor backtest build these histories automatically the
first time a broad index is used; any remaining inconsistency is printed with the result.
Temporary demerger placeholders (DUMMY symbols) are ignored.

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

Cost control: each check runs Claude at `effort: high` (Opus 5.5 would otherwise default
to medium) for at most 20 tool steps, with prompt caching on, so the conversation resent
at every step bills at the cache-read rate. Every run records its tokens and an estimated
cost at list prices. The dashboard's *Recent runs* card and `history` show the total.
If a run hits the step limit or the output limit, or a fallback model answered, the run
log says so instead of ending with a blank summary.

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
