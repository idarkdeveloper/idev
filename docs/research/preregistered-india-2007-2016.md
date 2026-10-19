# Pre-registered test: the adopted rules on India, Oct 2007 – Mar 2016 (19 Oct 2026)

Written and committed **before** the price data for this window was built. Until this commit, only 108 month-end
NSE files and a few single days (Infosys's bonus dates) were read, for counts and format checks in
`feasibility-2008-2016.md`. No strategy return on any year before April 2016
has ever been computed in this project. The commit time is the proof. Nothing in this file may be changed after the
data is built; a change means a new test with a new date. Every rule set run here counts as a trial in
`state/research/trials.jsonl`.

## Why

The adopted rules were found on Oct 2020 – Sep 2025. They passed the sealed year thinly, and the 2017–2020 backward
check. Both are short windows with few bad markets. Oct 2007 – Mar 2016 adds 8½ years nobody here has looked at,
including:
- the 2008 crash;
- the 2009 rebound;
- the 2011 fall;
- the 2013 taper scare.

## What is run

| Id | Rules | Status here |
|---|---|---|
| A0 | `research_studies.FINALIST`, unchanged: momentum composite, top 20, equal slots, no stop, buffer 35, tax-aware wait (floor 50), 25% floor, monthly rebalance, same-close fills, charges and tax | The test |
| B | As in the backward check: `top=10`, `buffer=18`, `tax_aware_floor=25` | Information for its paper account |
| E | As in the backward check: residual momentum on the benchmark (`research_backward.residual_momentum_ranker`) | Information for its paper account |

## Data

**Prices.** Every NSE daily file (`cmDDMONYYYYbhav.csv.zip` from `nsearchives.nseindia.com`) from **1 Jul 2006** to
**31 Mar 2016**. Each bar is read as:
- **close:** `CLOSE`;
- **volume:** `TOTTRDQTY`;
- **traded value:** `TOTTRDVAL`;
- **series:** `EQ` only.

Symbols are mapped to today's with NSE's `symbolchange.csv` (`index_history.current_symbol`). Corporate actions from
NSE already carry today's symbol: checked, Infosys shows as INFY in 2008.

**Adjustment.** Split, bonus and dividend records come from NSE's `api/corporates-corporateActions` for the same
dates. Ratios are read with `corp_actions.parse_subject`, extended for the two old phrasings
("Fv Split Rs.10/- To Rs.2/" and "Bonus - 1:1").
- **close:** the raw close divided by the product of the split and bonus factors whose ex-date is after the bar.
- **adj_close:** close, further multiplied on each cash dividend's ex-date by (previous close − dividend) / previous
  close, applied to all earlier bars.

Other actions (rights, demergers, schemes) are not adjusted.

**Universe, point in time.** At each month's first session, using bars up to the previous session, a stock is a
member for that month when all of these hold:
- it is in the EQ series;
- it traded on at least 100 of the last 126 sessions;
- it traded on at least one of the last 5 sessions;
- it is not a fund;
- it ranks in the top **500** by average daily traded value over those 126 sessions.

The screen then applies its own filters unchanged, including the ₹1 crore 60-day average traded value (nominal
rupees). So fewer than 500 names are eligible in thin months: about 260 cleared ₹1 crore in single-day value on
31 Dec 2008.

**Funds excluded.** Two rules:
- any symbol that carries an ISIN starting `INF` in any file from 2012 onward is excluded for all years;
- any symbol matching `BEES$|ETF|^GOLD|^LIQUID|^NIFTY|^BANKNIFTY|^JUNIOR|^PSUBNK|^INFRA|^SHARIA` is excluded.

**Delisted stocks.** A stock that stops trading keeps its last close; the simulator carries it. It leaves the
universe at the next month start (no trade in the last 5 sessions) and is sold there at that carried close. No
haircut, no buyback price. The report counts these exits and their value.

**Benchmark and calendar.** The Nifty 500 total return index (niftyindices) less **0.20% a year** for a fund's
costs. Its trading days are the calendar. The regime filter of the adopted rules reads this index's own averages,
not the Midcap 150's, so "no new buys in a downtrend" fires on different days than in the research years.

**Not rebuilt.** NSE price bands and ASM/GSM lists did not exist in usable form, so they are not applied, as in the
2017–2020 check. A stock whose close moves more than 50× in a day is left out as a data error and listed, as in the
backward check.

## Window

- **Data from:** 1 Jul 2006. NSE's corporate-action records are thin before then: 234 in Jan–Jun 2006, against 922
  in Jul–Dec 2006.
- **Trading from:** the first month start on or after **1 Oct 2007** with at least 20 ranked names. The ranking needs
  274 bars.
- **Trading to:** the last session before **1 Apr 2016**. Nothing dated on or after 1 Apr 2016 is loaded, so the
  2017–2020 backward check's lookback and traded months are not touched.

## Costs and tax

- **Charges (primary):** the simulator's charges **× 2**, for the higher brokerage of 2007–2016.
  Pass and fail are judged on this run only.
- **Charges (sensitivity):** charges × 1 is run and reported for comparison with the 2017–2020 check. It is never
  used for the verdict.
- **Tax:** as it applied then (`research_sim.tax_due`): 15% on short-term gains, long-term gains on listed shares
  exempt before 1 Apr 2018.
- **Capital:** ₹5,00,000.

## Data gate (checked before any rule set is simulated)

The run stops, printing the gate and no returns, if any of these fail:
1. **Ratios read:** at least **98%** of split and bonus records dated in the window have a ratio read.
2. **Universe size:** at least **400** members at every month start in the trading period.
3. **Price files:** a price file exists for at least **99%** of the benchmark's sessions.
4. **Symbol match:** at least **95%** of split and bonus records match a symbol seen in the price files that month.
5. **Unrecorded splits:** at most **15** member-days where a stock's one-day close ratio is within 1.5% of 1/2, 1/3,
   2/3, 1/4, 3/4, 1/5, 2/5, 1/10 or 1/2.5, with no split or bonus recorded within 3 sessions. Each is listed.
   Large falls alone are not a gate: Satyam's −78% on 7 Jan 2009 was real.

A failed gate may be fixed only by mending data, for example by reading BSE's ex-date flags. The fix is described
in an addendum committed before the re-gate. Returns stay unseen until the gate passes.

## Pass rules (the same as the 2017–2020 check)

**A0 passes** when, after charges × 2 and tax, both hold:
- it beats the benchmark over the window;
- its worst fall is no more than **10 points** deeper than the benchmark's.

**B and E** are judged by the backward check's four variant tests against A0:
- beat the benchmark;
- **+2 points a year** over A0;
- worst fall within **5 points** of A0's;
- turnover at most **1.5×** A0's.

Their result goes to their paper-account assessment only. Nothing is adopted from this test. Skill odds and the
probability of overfitting (3 trials) are reported as information.

## What the result means

- **A0 passes:** more support that the rules capture an effect beyond 2020–2025. No rule changes. Live money still
  waits on the forward test.
- **A0 fails:** recorded and reported plainly. No rule or live change follows from this test alone. The forward test
  and the paper accounts carry more weight than any backtest.
- **Universe caveat:** this is a liquidity-ranked top 500, not the Midcap 150. A pass says Indian momentum under
  these rules worked then. It does not say Midcap 150 momentum did.

## Running it

- `python -m trading_agent research-india-2007 --confirm`, once.
- The run writes its log before printing and refuses to run again.
- The output is committed to `docs/research/`.
- It runs in the sandbox, not on the server.

## Not allowed

- Changing any number above after the data is built.
- Running the rule sets on this window before the gate passes, or more than once.
- Tuning the universe, the costs or the exits after seeing a result, or quoting a second run.
