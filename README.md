# idev25 — Claude trading agent (Groww + NSE)

An AI trading agent that **watches one or several investors' publicly disclosed trades on the NSE,
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
# ...or several at once (comma list, or repeat the flag); default: everyone in INVESTORS
python -m trading_agent check --investor "ASHISH KACHOLIA,VIJAY KEDIA"

# 4. Keep checking every 30 minutes
python -m trading_agent loop --every 30
```

If Groww refuses a new login token (HTTP 429, or a rejected key), the agent remembers it in
`state/groww_token.block` and stops asking until the wait is over; `groww-token` / `groww-check`
print when to try again (`--force` asks anyway and may extend Groww's wait). Meanwhile the watch keeps
alerting on new deals ("analysis paused: Groww unavailable"), announcements and news, and the full
check analyses those deals once Groww is back. Stop and order checks pause during the wait.

Other commands: `portfolio` (account + P&L), `history` (past recommendations),
`reset` (forget seen trades, reset the paper account), `groww-token` (mint a daily token),
`check --json`, and `--market us` to switch to the US stack.

## The dashboard

```bash
python -m trading_agent ui            # opens http://127.0.0.1:8787 in your browser
python -m trading_agent ui --demo     # offline sample dashboard (bundled sample deals), kept in state/demo-sample/
```

A local web page served by the package itself (no extra dependencies) that shows the
watched investor's disclosed deals with new ones flagged, your real Groww holdings with live
P&L, Claude's recommendations with a one-click paper order, and the run log. The
**Run check now** button runs the same check as the CLI in the background. **Settings**
edits the followed investors (one name per line, up to 10), disclosure source, order mode and notification targets and writes
them to `.env`; API keys stay in `.env` by hand, and live Groww orders can never be
switched on from the page.

## Replay and Demo

The dashboard has three tabs: **Live** (today), **Replay** and **Demo**.

**Replay** (`/replay`) starts a practice portfolio on a past date, from 4 January 2021 (where the record of
index members begins). You, the agent's rules and the index fund each get the same practice money (₹1,00,000 by
default; the agent holds 10 stocks by default and dividends are reinvested unless you choose cash):

- **You** buy and sell at the replay day's closing price, in whole shares, with Indian delivery charges.
- **The agent** buys the momentum screen's top N from the index members of that day and rebalances on the first
  trading day of each month (the forward-test rules), selling at a 3×ATR trailing stop. When it sells a holding at
  its trailing stop, it keeps the cash until the next monthly rebalance.
- **The Nifty** line buys the index fund (MID150BEES for the Midcap 150, NIFTYBEES for Nifty 50/100/200/500,
  HDFCSML250 for the Smallcap 250) and holds.

Step forward a week, a month or a year; every trading day in between is simulated. Prices, the screen, the signal
lab, the factor backtest and NSE announcements only ever see data up to the replay date; asking for anything later
raises an error in the code, and tests check this. **End replay** shows the scorecard and what each portfolio did
from then to today. Dividends are reinvested or taken as cash, chosen when you start.

**Ask Claude about this day** sends Claude only the data available that day and charges your Anthropic account per
press. Claude's training data runs past most replay dates, so its replay answers may use hindsight; they are
labelled so and are never a reason to trust it with real money.

Replays are saved in `state/replay/<name>/`. Nothing on the Replay or Demo pages can place a real order.

**Live** (`/`) shows only real things: market regime, the watched deals, your My Groww portfolio, recommendations,
real orders, settings, Run check and the watch. Its top tiles are your Groww holdings value, profit / loss and holding
count ("n/a" until Groww is linked).

**Demo** (`/demo`) is the same page on the same real data (same regime, deals, recommendations, look-up, news and
My Groww portfolio) with the practice account added: the practice portfolio (account-value chart, positions, order
form), practice tiles and "Paper buy/sell" buttons on recommendations. The practice account is
`state/paper_broker.json` (PAPER_STARTING_CASH, prices from Groww when linked, otherwise Yahoo). Demo can never place a
Groww order, write `.env` or send a notification, even with GROWW_LIVE_ORDERS=true; Run check, Settings and Start watch
are only on the Live page. **Reset practice account** (asks first) resets only `paper_broker.json`, not the Live state.

`python -m trading_agent ui --demo` is the offline sample dashboard: bundled sample deals and prices with its own
state and practice account in `state/demo-sample/`, no Groww, no `.env` writes, no notifications. CI and tests use it.

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
   (no daily approval). A token generated from the key is cached in
   `state/groww_token.json` (owner-only permissions where the OS allows, tied to a
   fingerprint of the key, never the key itself) and reused until it expires at 06:00 IST,
   because Groww allows only 150 token generations a day. `GROWW_ACCESS_TOKEN` in `.env`
   always wins. `python -m trading_agent groww-token` prints the token (`--fresh` forces a
   new one).
3. Run `python -m trading_agent portfolio`. With Groww linked (and live orders off) the
   agent trades a separate practice account in `state/paper_broker.json`: it starts with
   `PAPER_STARTING_CASH` and no stocks, fills are simulated, and prices are live from Groww.
   Your real holdings are never copied into it; the dashboard shows them on their own
   ("My Groww portfolio"). An untouched copy left by an older version is replaced
   automatically.

Real orders on Groww are sent only when **both** `AUTO_TRADE=true` and
`GROWW_LIVE_ORDERS=true`. Every call that can place, change or cancel an order (orders and
GTT smart orders) refuses with `LiveOrdersDisabled` before touching the network unless
`GROWW_LIVE_ORDERS=true`. When live:

- **Limit orders, not market orders.** Each order is a DAY **LIMIT** order, CNC, cash
  segment: buys at LTP × (1 + `MAX_SLIPPAGE_PCT`), sells at LTP × (1 − `MAX_SLIPPAGE_PCT`)
  (default 0.5%), rounded to the stock's tick size from Groww's public instrument list
  (0.05 if unknown), towards the LTP so the price never gives away more than the slippage.
  Capped at 10% of equity, whole shares, and Claude is told it is trading real money.
- **No duplicates on retry.** Each order carries an `order_reference_id` (8–20 characters,
  e.g. `TA-3F9A1C07B24E5D`). If the request times out, the agent looks the order up by
  that reference before retrying once with the same reference.
- **Every order is confirmed.** After placing, the agent polls the order status (4 tries,
  0.5–3 s apart) and records `groww_order_id`, `order_status`, `filled_quantity`,
  `average_fill_price` and `remark` in `state/state.json` (`live_orders`). REJECTED,
  FAILED and CANCELLED orders, and orders Groww refuses outright, are sent to your email /
  webhook. An unfilled DAY limit order stays **open**: `python -m trading_agent orders
  --refresh` (and watch mode, every tick) re-checks open orders. The dashboard's order
  history shows live orders with their status.
- **Only free shares are sold.** Sells use `demat_free_quantity + t1_quantity` from your
  holdings; pledged, repledged and locked shares are never sold. Positions expose this as
  `sellable_qty`.
- **Real stop-losses at Groww (opt-in, `GROWW_GTT_STOPS=true`).** For each live CNC
  holding the agent keeps one GTT SELL smart order: trigger direction DOWN at the trailing
  stop (`risk.trailing_stop`: the tighter of 3× ATR or 15% below the high), LIMIT price
  `MAX_SLIPPAGE_PCT` below the trigger. When the stop rises the GTT is modified upwards;
  it is never moved down. When the holding is sold the GTT is cancelled. The
  `smart_order_id` is kept in `state/state.json` (`gtt_stops`). It syncs after each check,
  on every watch tick, after a live sell, and with `python -m trading_agent gtt --sync`.
  The dashboard has the toggle in Settings and shows each holding's GTT status. The
  toggle does nothing unless `GROWW_LIVE_ORDERS=true`, which the dashboard cannot set.
- **Practice stop-loss (Demo page, practice money).** Each practice position has its own
  stop: *trailing* (the default: the tighter of 3× ATR or 15% below the highest price since
  you bought), *fixed* (a price), *% below buy* (0.5 to 50% under your average buy price) or
  *none*. Set it in the order box or with *Edit stop* in the positions table. While the
  dashboard is running (a browser tab is not needed) a checker looks every minute between
  09:15 and 15:30 IST on NSE trading days and sells the whole practice position at the
  latest price, with the usual charges, once the price is at or below the stop; the fill is
  kept in `state.json` (`practice_stop_fills`), shown as a notice on the Demo page and marked
  "stop hit" in the order history. Watch mode's auto-exit uses the same rule, and the two
  do not sell the same position twice: each re-checks the position under a lock on the
  practice account file (`paper_broker.json.lock`) after re-reading it, so this also holds
  against a separate `watch` process using the same file. Unlike the GTT above this is not an order at any
  broker: it only acts while the dashboard process runs, uses delayed Yahoo prices unless
  Groww is linked, and never touches Groww or sends a notification.

**Check it against your account before relying on it.** These paths are tested offline
against Groww's documented formats. `python -m trading_agent groww-check` reads your
account to confirm the rest: token source and expiry, that holdings carry
`demat_free_quantity` and `t1_quantity`, tick sizes, and that the order and GTT lists
are readable. Every run also prints a **manual DDPI check** (below). With
`GROWW_LIVE_ORDERS=true`, `groww-check --live-test [SYMBOL] --i-understand-real-orders`
(SYMBOL defaults to ITC; `--symbol` overrides) first prints today's order list (count, open
orders, any not placed by the agent), then places **real** but harmless orders: a 1-share limit
buy 3% below the last price (`--offset-pct`), read back by id and by reference, its limit moved up
0.5% through Groww's modify-order endpoint (still at least 2% below the price, so it cannot
fill) and read back, then cancelled (the cancel runs even if the modify fails).
If you hold a free share of SYMBOL, it also creates a 1-share GTT 20% below the price,
raises it once and cancels it. Results go to `state/state.json` (`groww_checks`). If
anything fails to cancel, the output says so and names the id to cancel in the Groww app.
**DDPI (needed for sells).** A delivery SELL needs DDPI on your demat account (or a daily CDSL
TPIN/OTP, which a headless agent cannot give). Groww's trade API has no DDPI status call, so
confirm it yourself in the Groww app (Profile, Settings, Demat / DDPI authorisation) and tick
"I confirmed DDPI" in Settings (`GROWW_DDPI_CONFIRMED=true`). While it is false, live sells are
still allowed but alert once a day ("DDPI not confirmed: sells may be rejected"). A sell that
Groww rejects for TPIN / e-DIS / DDPI / CDSL authorisation raises `GrowwAuthorisationError`
("Groww rejected the sell: demat authorisation (DDPI/e-DIS) is missing. Enable DDPI in the Groww
app.") and alerts once a day per symbol.

**Free shares only.** Live sells use `demat_free_quantity` only. T1 shares (bought yesterday,
not yet in the demat) are included only with `GROWW_SELL_T1=true`, which makes the sale a BTST
sale with short-delivery / auction risk. A holding with only T1 shares is not sold; the agent
alerts "only T1 shares; not sold until they settle". Paper and practice accounts are unchanged.

For live orders from a fixed IP see `docs/hosting.md`; `groww-check --ip` (no credentials needed)
prints this machine's public IP and whether it matches `GROWW_ALLOWED_IP`.

## Which investors can I follow?

`INVESTORS` (comma separated, up to 10, for example `INVESTORS=ASHISH KACHOLIA,VIJAY KEDIA`) lists
everyone to follow; if it is unset the single `WATCH_INVESTOR` is used. Each name is matched against the **client name** in NSE bulk and block deals, or the
acquirer name in insider filings. Matching is case-insensitive and ignores word order,
because the exchange prints names surname-first and inconsistently (`KACHOLIA ASHISH`,
`MUKUL MAHAVIR AGRAWAL`, `ESTATE OF LATE MR. RAKESH JHUNJHUNWALA`). Bulk deals only show
trades above 0.5% of a company's shares, so famous investors appear only a few times a
year; the names that appear weekly are mostly prop desks and operators. Run a backtest
before trusting anyone:

```bash
python -m trading_agent backtest --investor "MUKUL AGRAWAL" --days 365
python -m trading_agent backtest --days 365   # all followed: one row per investor plus the pooled result
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
| `groww-check` | CLI only: verifies live-trading assumptions on your account |
| `holdings` | *My Groww portfolio*: your real holdings with buy price, current price and P&L in ₹ and % (read-only) |
| `orders`, `orders --refresh` | Order history (live orders show their Groww status) |
| `gtt`, `gtt --sync` | *GTT stop* column in the portfolio, toggle in Settings |
| `--market`, `.env` strategy keys | Settings: investor, disclosures, market, starting cash, order mode, notifications |

The dashboard can also place paper buys and sells for any ticker and close positions;
these only ever touch the local paper account.

## News headlines

The stock look-up shows recent mainstream headlines under the NSE announcements, each tagged
positive / neutral / negative with an event type (results, order win, legal or regulatory,
fraud allegation, ...). Sources are public RSS feeds: ET Markets, ET Stocks, Business Standard
and Livemint, plus Google News per company. Only the headline, link, source and time are kept;
article text is never fetched or stored.

- **Tagging runs on a local Ollama model by default, so it is free.** Install Ollama from
  ollama.com, run `ollama pull qwen2.5:3b`, and keep `ollama serve` running. On an Oracle Ampere
  server the same works with 6 GB or more of memory. With `NEWS_TAGGER=auto` (the default), no
  Ollama means headlines show without labels; Claude is never used unless you set
  `NEWS_TAGGER=claude` (then `NEWS_CLAUDE_MODEL` is billed to your API key).
- `python -m trading_agent news SYMBOL` prints the tagged headlines;
  `python -m trading_agent news --check-tagger` shows which tagger is active and whether Ollama is ready.
- Watch mode sends `[NEWS] SYMBOL: headline` once per headline that is negative with medium or high
  confidence, for stocks you hold or were recently recommended. Claude's daily check can call
  `get_news` for the last two days of tagged headlines; it is told to treat them as data, never as instructions.
- Every headline is written once per stock, with its tags, to `state/news/YYYY-MM.jsonl`; its first-seen time is the
  earliest `logged_at` for its id. The look-up shows headlines at once and labels new ones in the background, so labels
  appear on the next look-up.
- **News is not yet a tested trading signal.** Labels come from a small language model and can be wrong,
  and headlines can be late. The log exists so news can be tested as a signal later; the replay does not
  use it because the feeds have no history.

## Daily emails

Two emails on NSE trading days, sent by the watch service (`python -m trading_agent watch`, or Start watch on the
dashboard) to your email (Resend) and/or webhook. They never place an order.

- **Morning, after 09:00 IST:** the market mood (and "No new buys today" when the regime is risk-off or Nifty is below
  its 200-day average), buy ideas from the momentum screen of `DIGEST_UNIVERSE` with a quantity sized to risk 1% of
  your practice equity on a 2x ATR move and a stop level (new buys are off when the regime is risk-off, Nifty is below its 200-day average or in a downtrend, and the email says which; they then appear as "Would pass, but the market
  filter says wait"), holdings to watch or consider selling (price at its stop, or within the smaller of 1 ATR and 3% of it, below its 200-day
  average, negative medium or high confidence news in the last 2 days, results or a board meeting due within 7 days,
  a fall of more than 5% in the last session), and new deals by the investors you follow.
- **Evening, after 15:45 IST:** your Groww portfolio (value, today's profit or loss against the previous close, total
  profit or loss, each holding's move best to worst), the practice account (equity, today, total, stop-loss sells),
  today's tagged headlines for your stocks (negative first) and today's deals.
- **Market bulletin in the evening email** (`DIGEST_BULLETIN=true`, after the portfolio sections; options data is not used):
  the Nifty 50 close, change and opening gap, a 15-minute candle chart with the 21 EMA, the previous close and watch levels,
  a 4-hour candle chart with an ADX panel (two 4-hour candles per session: 09:15-13:15 and 13:15-15:30), the daily ADX(14) with
  +DI/-DI (below 20 weak, 20-25 developing, 25-40 strong, above 40 very strong), RSI(14), classic pivot points from the day's
  candle, the nearest swing high above and swing low below the close (a swing is the extreme of 3 bars either side, within
  the last 60 sessions, at least 0.3% from the close; shown as **watch levels**), named candles by fixed rules (marubozu: body
  at least 90% of the range, doji: at most 10%, hammer, shooting star, engulfing), global markets with the day's move and
  trend (plus one real headline from the last 24 hours when one names the market), a commodities corner (gold, silver, WTI,
  Brent, natural gas) and a concept of the day from a fixed human-written library (a longer concept of the week on Fridays).
  Everything is a reading of past prices by rules, never a forecast; a part that cannot be read is left out. Charts need
  `matplotlib` (in `requirements.txt`) and are sent as inline pictures through Resend; `DIGEST_CHARTS=false` sends the
  bulletin without them, and an email service that refuses inline pictures gets the email without them. Both switches are in Settings.
- **"In short" summary on top.** The rules build a correct 3-5 sentence summary from the same data (so it is always
  right about the numbers). By default **Claude Haiku** (`DIGEST_CLAUDE_MODEL=claude-haiku-4-5`, needs `ANTHROPIC_API_KEY`,
  about a cent a day; the token use and cost of each email are logged) rewrites it in plain English, and its text is used
  only if it passes the checks (no ticker, number or amount that is not in the data, no advice or forecast, "today"
  figures must be today's move, the market regime must match). With no key, an error, a timeout or a rejected text, the
  email uses the rules summary. The email says which one wrote it ("written by the rules" or "written by Claude Haiku
  — check the numbers below"). Choose with `DIGEST_WRITER`: `claude` (default), `auto` (Ollama, then Claude), `ollama`,
  `rules` (no AI), `none` (no summary). The numbers in the tables always come from rules, not from a model.
- Stops for your Groww holdings are estimated from your buy price (Groww does not tell us the highest price since you
  bought): the tighter of 3x ATR or 15% below it. Practice positions use their own stop. Prices come from Yahoo and
  may be delayed. If Groww refuses a login, the emails use your saved holdings (`state/groww_holdings.json`, written
  after every successful read) and say so (with how many trading days old they are when more than one).
- Each email is sent once per trading day (remembered in `state/digest_state.json`, with a claim file so two processes cannot both send it).
  The morning time must be 06:00-11:00 and the evening 15:30-19:00 IST. A late start still sends the morning one before 12:00 and the evening one before 20:00. Amounts use Indian grouping (₹5,00,000). Switch each on or off and
  set its time under Settings, where Preview shows the rendered email on the Live or Demo page.
- `python -m trading_agent digest morning|evening [--send] [--writer claude|auto|ollama|rules|none]` prints the email
  (and with `--send` mails it). Keys: `DIGEST_ENABLED`, `DIGEST_MORNING_ON`, `DIGEST_EVENING_ON`, `DIGEST_MORNING`,
  `DIGEST_EVENING`, `DIGEST_UNIVERSE`, `DIGEST_TOP`, `DIGEST_WRITER`, `DIGEST_CLAUDE_MODEL`, `DIGEST_BULLETIN`, `DIGEST_CHARTS` (see `.env.example`).
  Generated by rules from public data; not financial advice.

## What the big firms do, applied at retail size

- **Factor screen** (`trading_agent/screen.py`, `python -m trading_agent screen --universe NIFTY200`):
  ranks every constituent on 12-1 momentum, 6-month return and low 60-day volatility,
  requires price above the 200-day MA and a liquidity floor. The dashboard has the same
  panel.
- **Quality and value, opt-in** (`trading_agent/fundamentals.py`, `screen --quality --value`,
  or the two checkboxes on the dashboard's factor screen): fundamentals come from Yahoo
  Finance, cached for a day. Quality is return on equity (EPS ÷ book value per share),
  low debt to equity (ignored for banks and other lenders) and earnings growth; value is
  earnings yield and book-to-price. Loss-makers and non-financials with debt above 2×
  equity are dropped. Yahoo gives **today's** numbers only, and with equal weights they
  can outweigh momentum (a cheap stock with a flat trend can rank first). They are kept
  out of the forward test.
- **Point-in-time fundamentals for backtests** (`trading_agent/fundamentals_history.py`):
  `python -m trading_agent fundamentals-history --universe NIFTYMIDCAP150` downloads every
  quarterly results filing NSE has as XBRL (old feed to Dec 2024, SEBI's integrated
  filing after that) for the universe and its past members, and caches each as a small
  record. It is resumable: rerun it until it says nothing is left. Then
  `factor-backtest --universe NIFTYMIDCAP150 --quality --value` ranks each month on the
  results **broadcast by that day**: trailing-twelve-month profit (from year-to-date
  figures), ROE, debt to equity, earnings growth, earnings yield and book-to-price.
  Limits: earnings growth starts around 2020, and ROE and debt around 2022-23, when
  balance sheets appear in the filings. Companies are valued on today's share count
  (Yahoo prices are split-adjusted), so later share issues leak in slightly. The
  backtest prints how many eligible names had results on an average rebalance.
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

**Is it skill or luck?** Add `--validate` to `factor-backtest` (the dashboard does it
automatically) and the result gets three plain checks:
- *Walk-forward*: each year it picks the portfolio size (10, 20 or 30 stocks) that did best
  in the earlier years only, then scores that pick on the year that followed. The first
  year is only for learning, so it is never reported.
- *Deflated Sharpe ratio*: measured on the **excess** return over the comparison (the
  universe's index fund where it existed for the whole window, otherwise a named
  benchmark), because beating zero is easy in a rising market. A good-looking result is
  less impressive when you tried several variants and kept the best, so this gives the
  odds it truly beats the comparison after counting those tries (only the portfolio
  sizes are counted, so treat it as an upper bound).
- *Monte Carlo*: the monthly returns are resampled in short blocks 5,000 times, giving the
  range of worst falls and final returns that the same luck could have produced.

It ends with one verdict: likely skill (only against the universe's own index fund, with at
least 3 full out-of-sample years), could be luck, or no edge. The signal lab applies
the same deflation to every signal and horizon, shown as "Real-edge odds".

The dashboard draws all of it: account value, a one-year price chart with the 200-day
average and your cost and stop for held stocks, the spread of backtest returns, and the
factor portfolio against its benchmarks.

## Forward test: the screen on months it has never seen

The factor backtest beat its index fund after charges in one place, the Midcap 150, over
one five-year window. A backtest can be fitted to its own past, so
`python -m trading_agent forward` runs the same screen forward in a **separate paper
account** (`state/forward/`), never touching the investor-following account or Groww:

- **First run:** puts the capital (`PAPER_STARTING_CASH`, or `--capital`) into the top 20
  (`--top`) of the universe (`--universe`, default `NIFTYMIDCAP150`) in equal weights, whole
  shares, with real delivery charges. The same capital goes into the index fund
  (MID150BEES for the Midcap 150), charges included, as the benchmark.
- **Each month:** the first run after the close re-ranks today's members with the same
  score. It sells names that dropped out, buys new ones, and trims a kept name only when
  it is more than 25% over its target weight, so small trims don't each pay a ₹20 DP charge.
- **Every weekday:** one point is recorded for the strategy and the fund, so the gap builds
  up over months the backtest never saw. `forward --status` shows it without trading. The
  dashboard has a *Factor screen, forward* card with both curves.

The server's watch service runs `forward --if-due` once per trading day after the close
(16:10 IST, holiday-aware, guarded by a claim file so a restart cannot run it twice); it
does nothing unless a rebalance is due or today's point is missing. If the account has to be
recreated (it used to live in the GitHub Actions cache), `forward --rebuild-from 2026-10-09
--universe NIFTYMIDCAP150` rebuilds it as the first run made it on that date (top 20 of the
screen as of that day, same charges, fills at that day's close) and refuses if one already
exists unless `--force`. Give it several months before reading anything into the gap.

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

`BSE_DEALS` (default `true`): also read BSE's bulk and block deals (public page, no login) beside NSE's,
so mid and small-cap deals made only on BSE are followed too. They show in the deals table with an
Exchange column, in alerts ("on BSE"), the Claude check, the daily emails and the deal backtest. Set
`false` for NSE only (the Settings page has the same switch). A BSE failure is logged once a day and
never stops the NSE read. Today's BSE deals are published after 16:00 IST, so they reach the next morning's email, not the 15:45 evening one.

One client buying (or selling) the same stock on NSE and BSE the same day is one event for every signal: the who-traded
counts, investor alerts, the agent's view and the deal backtest add the quantities, use the quantity-weighted average
price and show "NSE + BSE". The records themselves stay separate. Client names are compared after normalising company forms
(`PRIVATE LIMITED` = `PVT LTD`, `LLP` = `LIMITED LIABILITY PARTNERSHIP`); `LTD` and `PVT LTD` stay different clients.

`FLOWS_BREADTH` (default `true`, India): the morning email's Risk gauges section adds a line with the latest FII and DII
net flows (NSE's FII/DII report, provisional numbers, rupees crore) and the 5-day FII net, and a NIFTY 500 breadth line
(share of stocks that rose, the run of days below 35%, share above their 50-day average). The watch service fetches both
once per trading day after 19:00 IST from public NSE files (the FII/DII JSON and the full bhavcopy). Information only:
neither is a trading rule. NSE serves only the latest FII/DII session, so the flow history starts the day the service
first runs.

`PRICE_BAND_FILTER` (default `true`, India): NSE's daily price band list is fetched once per trading day before 09:00 IST.
Stocks with a 2% or 5% band are skipped by the screen, the agent's buy recommendations and orders, the email's buy ideas
and practice buys ("price band 5%: liquidity can vanish in a fall; not bought"); a 10% band is a caution. Sells and
stop exits are never blocked. The band shows as a small pill in Look up and the holdings table. No list for the day means
no filtering. Both switches are on the Settings page.

Closed daily bars are also kept in `state/prices/archive.sqlite`: if Yahoo drops or rewrites a ticker's history the saved
bars are still served, and a split Yahoo applies later is detected and recorded (the bars as first seen are kept too).
Filings are usable from the next trading day when disseminated at or after 15:00 IST (`filing_time.usable_from`); the
fundamentals history, Replay's announcements and the insider backtest all use that one rule. Price levels (ATR, stops,
fills) use Yahoo's `close` (split-adjusted); `adj_close` is for returns and momentum only.

The archive is on for every price source built from the settings (the NSE and BSE quotes, the world indices, the digest, the
dashboard, Replay). Indian bars are saved once final (before today, or today after 18:00 IST); other markets only when two
days old. A split that Yahoo applies later, even one that happened after the archive already held newer bars, rescales only
the older saved bars. The same client's bulk and block deals in a stock on one day are merged into one event along with the
NSE and BSE ones. The forward test's screen uses the price-band filter too, from the date shown on its card (earlier
rebalances were made without it). Replay is deliberately NOT band-filtered: there is no point-in-time band history.

Replay prices: new replays run on the real (split-adjusted) close for fills, stops, values and whole-share rounding. Dividends
come from Yahoo's dividend events on their ex-dates: "cash" credits quantity x amount; "reinvest" credits it and buys extra
whole shares at that day's close (charges applied, the rest stays cash). Replays saved before this change keep their original
dividend-adjusted basis (they carry no `price_basis` marker), so their saved fills stay consistent; start a new replay for
exact real-price results.

`python -m trading_agent market-data` shows the stored flows, breadth and band list; `--fetch flows|breadth|bands|all` fetches
them now from the public NSE files. A missed FII/DII session is retried the next morning before 09:00, a missed breadth
session is backfilled from the bhavcopy (last 10 trading days), and five-day sums and streaks only count consecutive
trading days (a gap is labelled, e.g. "5 of the last 7 sessions"). A price band list more than 2 trading days old is
treated as missing (no filtering).

## Configuration (`.env`)

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Claude. Model defaults to `claude-opus-5-5` (`CLAUDE_MODEL`). |
| `ANTHROPIC_WORKSPACE_ID` | Only for a key that isn't scoped to one workspace: the API then rejects every request until this is set. |
| `MARKET` | `in` (default: NSE + Groww) or `us` (QuiverQuant + Alpaca). |
| `INVESTORS`, `WATCH_SOURCE` | Who to follow (comma separated, max 10; falls back to `WATCH_INVESTOR` when unset) and which disclosures to read. |
| `GROWW_ACCESS_TOKEN` / `GROWW_API_KEY` + `GROWW_API_SECRET` or `GROWW_TOTP_SECRET` | Groww access. Unset = local simulator only. |
| `GROWW_LIVE_ORDERS` | `false` (default) = paper fills with real holdings/prices. `true` = real orders. |
| `MAX_SLIPPAGE_PCT` | Live limit price distance from the LTP, in percent (default `0.5`, max 5). |
| `GROWW_GTT_STOPS` | `false` (default). `true` = keep a GTT stop-loss at Groww per live holding (needs `GROWW_LIVE_ORDERS=true`). |
| `GROWW_DDPI_CONFIRMED` | `false` (default). Your own confirmation that DDPI is active in the Groww app; while false, live sells alert once a day that they may be rejected. |
| `GROWW_SELL_T1` | `false` (default) = live sells use free demat shares only. `true` = also T1 shares (BTST: short-delivery / auction risk). |
| *(prices)* | Yahoo Finance is used automatically when Groww Live Data is unavailable or no broker is linked. |
| `AUTO_TRADE` | `false` (default) = recommendations only. `true` = Claude may place orders. |
| `PAPER_STARTING_CASH` | Cash for the simulator when no brokerage is linked (default ₹5,00,000). |
| `RESEND_API_KEY` + `NOTIFY_EMAIL_TO` | Email each recommendation via Resend. |
| `NOTIFY_WEBHOOK_URL` | POST `{"text": ...}` to Slack/Discord/n8n/etc. |
| `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` | Mirror every alert and daily email to Telegram (`TELEGRAM_ALERTS=false` = off). Runbook section 13. |
| `HEARTBEAT_URL` | https ping URL called every 5 minutes by the watch service (healthchecks.io style dead-man's switch). Runbook section 12. |
| `QUIVER_API_KEY`, `ALPACA_*` | US mode only. |
| `NEWS_TAGGER`, `OLLAMA_URL`, `OLLAMA_MODEL`, `NEWS_CLAUDE_MODEL` | Headline tagging: `auto` (default, local Ollama `qwen2.5:3b` if running) / `ollama` / `claude` / `none`. See *News headlines*. |

## One engine: the server (runs by itself)

The Oracle server's `trading-agent-watch` service is the only engine: it checks deals,
stops, announcements and news every minute in market hours, sends the daily emails, runs
the paper forward test after the close, and (optionally) pings a dead-man's-switch URL
every 5 minutes (`HEARTBEAT_URL`) and mirrors every alert to Telegram
(`TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`). See `docs/runbook.md` sections 11 to 14.

`.github/workflows/routine.yml` used to run a second engine every 30 minutes. It no longer
has a schedule: it is a **manual dry run** (Actions, *trading-agent manual dry run*, Run
workflow) that fetches deals and prints what a check would do, with no Groww login, no
Claude call, no email and no order. It needs none of the Groww, Anthropic or Resend
secrets, so all of them can be deleted from GitHub (list in the runbook, section 11). Its
optional variables `INVESTORS` (or `WATCH_INVESTOR`), `MARKET`, `WATCH_SOURCE` and
`PAPER_STARTING_CASH` only steer the dry run.

## How a check works

1. Fetch the last 30 days of bulk and block deals (and insider filings if selected),
   keep the followed investors' rows (a deal that matches two names is one deal), drop the ones already in `state/state.json`.
2. Nothing new → exit without calling Claude.
3. Something new → Claude gets the new deals and tools to inspect your holdings, live
   prices and the investor's history in that stock. It calls `send_recommendation`
   (buy / sell / hold / watch with rationale and confidence) and, with `AUTO_TRADE=true`,
   may call `place_paper_order`.
4. Trades are remembered only after a successful analysis, so API hiccups are retried.

Safety rails: paper fills by default; Groww live orders behind a double opt-in
(`GROWW_LIVE_ORDERS` + `AUTO_TRADE`), with every order-changing call refusing unless
`GROWW_LIVE_ORDERS=true`; DAY limit orders within `MAX_SLIPPAGE_PCT` of the last price,
rounded to the tick; an `order_reference_id` so retries can't duplicate; every live order
confirmed and failures notified; sells limited to free (unpledged, unlocked) shares;
optional GTT stop-losses at Groww that only ever move up; the Groww token cached on disk
(owner-only) and never written to the Actions cache; per-order size cap, concentration
rule in the prompt, whole-share rounding, and server-side refusal fallback on the Claude
request.

Cost control: each check runs Claude at `effort: high` (Opus 5.5 would otherwise default
to medium) for at most 20 tool steps, with prompt caching on, so the conversation resent
at every step bills at the cache-read rate. Every run records its tokens and an estimated
cost at list prices. The dashboard's *Recent runs* card and `history` show the total.
If a run hits the step limit or the output limit, or a fallback model answered, the run
log says so instead of ending with a blank summary.

## Notes on the data

NSE's JSON endpoints are public but undocumented; the client sends browser-like headers.
Bulk/block deals are reliable. Insider (PIT) filings moved in May 2026: NSE's old
`api/corporates-pit` JSON ends on 2 May 2026, and newer filings are XBRL documents listed by
`api/corporates-pit-gg`. The client reads both (the old feed only for dates before May
2026). Each XBRL filing is downloaded once and its parsed rows are cached in
`state/cache/nse_pit/` (about 1.5 KB a filing). NSE's archive host blocks bursts for about
a minute, so filings are fetched one at a time with a short pause, at most 400 new ones
per run, and a run stops early if NSE refuses; the rest are read on the next run. A
30-day window is about 750 filings: the first run reads 400 (about 4 minutes), the next
run the rest, and after that only new filings, which takes seconds.

## Development

```bash
python -m pytest -q
```

Tests run fully offline with a scripted fake Claude runner and fake HTTP sessions.

**Not financial advice.** Bulk-deal client names can be brokers acting for someone else,
and a disclosed trade tells you nothing about the investor's reasons. Treat this as a
research assistant, not a signal.
