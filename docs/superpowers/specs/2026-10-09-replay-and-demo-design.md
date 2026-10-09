# Replay and Demo pages — design

Date: 9 Oct 2026 · Status: approved in chat, awaiting review of this write-up

## Goal

Split the dashboard into three pages:

- **Live**: today's dashboard, unchanged.
- **Replay**: start a practice portfolio on a past date, step forward in time, and race the agent's rules and the Nifty. Every number shown is as of the replay date.
- **Demo**: the bundled sample-data dashboard, on its own page instead of a `--demo` restart.

Replay serves two purposes:

- **Learning:** would these methods have worked, and how do my own picks compare?
- **Building trust before real trades.** Anything that could later influence a real trade must come with an honest, leak-free track record.

A trend-prediction model is **out of scope**. It gets its own spec after Replay exists, because Replay provides the leak-free history it must be tested on.

## Non-negotiables

1. **No future data.** Nothing dated after the replay clock reaches the computation or the browser until End trial.
2. **No real orders.** Replay and Demo never construct a Groww broker. Claude gets no order tool there.
3. **No credentials in code, tests or logs.** Tests use fakes only, with no network.

## 1. Clock and data

`ReplayClock` holds one date, the replay's "today". It only moves forward, and only on a step.

Every data source the Replay page uses goes through a clocked wrapper:

| Source | Behaviour |
|---|---|
| Prices (Yahoo daily) | `history(symbol, range)` returns bars on or before the clock. `latest_price` is the close on the last trading day on or before the clock. Underneath, one long range (`10y`) is fetched and cached; the wrapper slices it. No bar means no price: an order is refused with "not listed until …" or "last traded …". |
| Index membership | `members_on(clock)` from `state/index_history` (NSE notices since 2021). |
| NSE announcements | Fetched by date range up to the clock, cached on disk. Stock views show the last 60 days. |
| Results filings | Point in time by broadcast date (`fundamentals_history`). Quality and value are used only from 2023; before that the page says "momentum only". |
| Dividends | Yahoo dividend events with dates on or before the clock. Used only in "take as cash" mode. |

Two guards against future data:

- Each wrapper raises `FutureDataError` when asked for anything after the clock.
- The Replay snapshot sent to the browser is built only from the wrappers.

Reused unchanged, because they already take a `prices` object: `run_screen`, `run_signal_lab`, `run_factor_backtest`, `momentum_stats`, `atr`, `trailing_stop`, and the cost models.

**Earliest start date: 4 Jan 2021.** That is where point-in-time membership begins; an earlier start would let the screen pick from today's survivors. Momentum needs a year of history before the start, which Yahoo has.

**Known approximations, stated on the page:**

- Fills are at the step date's close.
- Old prices are split-adjusted, so they can differ from the quotes on that day.

## 2. Trials, portfolios and steps

**New trial inputs:**

| Input | Default |
|---|---|
| Name | — |
| Start date | ≥ 4 Jan 2021 |
| Practice money | ₹1,00,000 |
| Universe | MIDCAP150; also NIFTY50 / NIFTY500 when their membership history exists |
| Agent's N | 10 |
| Dividends | reinvest; or take as cash |

Each trial is stored in `state/replay/<slug>/trial.json`: settings, clock, the three portfolios, daily equity, the order log, Claude answers, and an `ended` flag.

**Portfolios.** All three start with the same money and pay the same Indian charges (the existing `cost_model_for("in")`), in whole shares:

- **You:** buy and sell on the clock date at its close. Trailing stops are shown as warnings. An optional *auto-sell at stop* switch is off by default.
- **Agent (rules):** the forward-test rules. Top N of the screen, equal weight, rebalanced on the first trading day of each month, trimmed above 125% of target, sold at a 3×ATR trailing stop.
- **Nifty:** buys the universe's index fund on the start date and holds it (NIFTYBEES, MID150BEES, …).

**Dividends.** The setting applies to all three portfolios:

- *Reinvest*: values use adjusted closes.
- *Cash*: values use split-adjusted closes. On each ex-date, dividend × shares is credited to cash. It is credited in full, without tax, and the scorecard notes this.

**Step** (+1 week, +1 month or +1 year): walk every trading day from the day after the clock to the target date. On each day:

1. On the first trading day of a month, the agent rebalances using data as of that day.
2. Check stops: the agent's always, yours if auto-sell is on.
3. Credit dividends (cash mode).
4. Record the day's value for all three portfolios.

The step is computed in memory and the trial is saved only if every day succeeds, so a failure leaves the saved trial unchanged. Your own orders happen only on the clock date, not mid-step.

A one-year step must give the same result as twelve one-month steps. Long steps run as a background job in the existing single job slot, with progress shown.

**End trial** freezes the trial and shows a scorecard per portfolio:

- final value, total return, CAGR and maximum drawdown,
- charges paid and number of trades,
- best and worst pick and the share of profitable trades (for you and for the agent).

After End trial, a **"What happened next"** chart holds each portfolio from the end date to today. This is the only point where post-clock data reaches the page.

## 3. Pages

**Navigation.** Header tabs: **Live | Replay | Demo**, served at `/`, `/replay` and `/demo` by the same stdlib server. All three use the same Nocturne design, fonts and components, with no new libraries.

**Replay.**

- A permanent banner with its own accent colour: "Replay · Mon 1 Mar 2021".
- With no trial open: a list of saved trials (name, start, clock, three returns) and the New-trial form.
- Inside a trial:
  1. Step bar.
  2. Race strip: three tiles (value, return, worst fall) and one chart with three lines.
  3. Your portfolio: the paper-portfolio table with total row, and an order box with company-name search.
  4. Agent's picks as of today, with "copy" buttons that fill your order box.
  5. Look-up and "What this means", as of the clock, with the last 60 days of announcements.
  6. Signal lab and factor backtest as of the clock.
  7. Ask Claude.

**Demo.**

- Banner: "Demo · sample data", in its own colour.
- Bundled sample deals and prices.
- Its own paper account in `state/demo/`.
- A Reset demo button.
- Never touches Groww.
- `--demo` opens on this tab.

## 4. Ask Claude (Replay)

The button **Ask Claude about this day** is charged per press on the user's `ANTHROPIC_API_KEY`. It is disabled with an explanation when no key is set. The trial shows a running count of presses.

**Input,** clocked only:

- the date and market backdrop (Nifty trend and distance from its 200-day average; India VIX if available),
- your portfolio and cash, and the agent's holdings and picks,
- for each of those stocks: momentum, trend, ATR stop, and 60 days of NSE announcements,
- optionally, the looked-up stock.

**Output:** the Live recommendation card (action, confidence, headline, reasoning), saved in the trial with its date and graded at End trial.

**Tools:** read-only clocked-data tools only. There is no order tool.

**Hindsight caveat,** on every answer: Claude's training data covers events after most replay dates, so replay answers may reflect hindsight. Its replay record is labelled "may include hindsight" and is never used to justify trust on Live. The prompt tells Claude to use only the provided data.

## 5. Errors

| Situation | Handling |
|---|---|
| Stock not yet listed, or delisted or suspended | The order is refused with a reason. A holding is valued at its last price and marked "suspended"; it is closed at the delisting price when known. |
| Ticker renamed | Mapped through the NSE rename list (`load_symbol_changes`, `current_symbol`). |
| Yahoo unavailable or throttled | The step aborts and the saved trial is unchanged. The message names the stock. History is cached on disk, so a retry is fast. |
| NSE announcements blocked | The step continues; news panels say "news unavailable for this period". |
| No membership history for an index | That index is not offered when creating a trial. |

## 6. Testing

All tests use fakes, as the suite does today.

1. **Future data:**
   - Every wrapper raises `FutureDataError` past the clock.
   - Scanning every date in a Replay snapshot finds none later than the clock.
2. **Steps:**
   - A one-month step on fake prices gives the exact expected values, charges and rebalances.
   - A one-year step equals twelve one-month steps.
   - Stops trigger on the right day.
3. **Dividends:** with one fake dividend, reinvest and cash give the same total value (within rounding). Cash mode credits exactly dividend × shares on the ex-date.
4. **Edge cases:** an unlisted stock is refused; a delisted holding is valued and closed; a rename keeps history; a failed download leaves the trial file unchanged.
5. **Isolation:**
   - Demo writes only to `state/demo/` and Replay only to `state/replay/`; neither touches `paper_broker.json`.
   - No Groww broker is constructed for either page.
6. **Ask Claude:** the prompt contains only clocked data, there is no order tool, and the button is disabled without a key (fake Anthropic client).
7. **UI:** a browser check in demo mode that all three tabs load without console errors and the race chart renders.

## 7. Build order

1. Clock, clocked wrappers and step engine (code and tests only).
2. Replay page: trials, steps, your portfolio, race chart, End trial, What happened next.
3. Agent picks, as-of look-up with news, and signal lab / factor backtest as of the clock.
4. Ask Claude.
5. Demo tab.

## To verify during planning

- Whether the NSE `api/corporate-announcements` endpoint accepts `from_date`/`to_date` for old periods. If it doesn't, find another archive source or fall back to "news unavailable" for those periods.
- Which indices have stored membership history besides MIDCAP150.
- Yahoo dividend events (`events=div`) in the chart response for `.NS` symbols.
