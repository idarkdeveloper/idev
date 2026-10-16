# Pre-registered batch: four variants of the adopted rules (16 Oct 2026)

Written and committed **before** any data for the test window below has been built, loaded or looked at. The
commit time is the proof. Nothing in this file may be changed after that data exists; a change means a new batch
with a new date. Every variant here counts as a trial in `state/research/trials.jsonl` when it is run, pass or fail.

## Why

Five years of research (1 Oct 2020 to 30 Sep 2025) produced four ideas that looked better than the adopted rules:
- keeping only the larger half of stocks (#46);
- holding 10 names instead of 20 (research-meta);
- ranking stocks against their own industry (sector-relative strength);
- capping how many picks one industry may hold.

All four were seen on those same five years. The probability of backtest overfitting across the momentum variants
was 0.87, so choosing among them there would be chance. They can only be judged on data that none of this research
has used.

## The adopted rules (the control, A0)

`research_studies.FINALIST`, unchanged:
- momentum composite ranking;
- top 20, equal slots;
- no stop;
- rank buffer 35;
- tax-aware wait with floor 50;
- 25% disaster floor;
- monthly rebalance;
- same-close fills;
- charges and tax included.

Each variant changes exactly one thing.

| Id | Variant | The one change |
|---|---|---|
| A0 | Control | none |
| A | Larger half | each rebalance ranks only the larger half of that day's ranked members by 60-day average traded value (`size_ranker("larger")`, as run in #46) |
| B | Top 10 | `top=10`, `buffer=18`, `tax_aware_floor=25` (the 20-name settings halved, rounded up); equal slots of 10% |
| C | Sector-relative strength | the composite score is z-scored within each NSE industry that has at least 3 eligible names that day (other names keep their overall z); ranked on that; same trend and liquidity filters |
| D | Industry cap | the adopted ranking, but a new buy is skipped while 5 holdings already share its NSE industry (the next name in the ranking takes the slot); holdings are never sold for the cap; names with no known industry are not capped |

Industries are NSE's labels (NIFTY 500, Midcap 150 and Smallcap 250 lists) as fetched when the test is run.

## Test windows

1. **Backward window (primary).**
   - **Trading period:** from the first month start on or after 1 Apr 2017 with at least 20 ranked names, to 30 Sep 2020.
   - **Lookback:** the Nifty Midcap 150 was launched in April 2016, and the ranking's lookback year comes before the trading period.
   - **Why it's unseen:**
     - Returns before 1 Oct 2020 have never been judged in any research run.
     - 1 Oct 2019 to 30 Sep 2020 was loaded only as ranking lookback for the development window.
     - It includes the 2018 midcap fall and the 2020 crash.
   - **Benchmark:** the Nifty Midcap 150 total return index from NSE (niftyindices), less 0.20% a year for a fund's costs (MID150BEES did not trade before 2019).
   - **Membership:** point in time, from NSE's index notices since launch. The run reports how many past members lack prices; survivorship is worse here than in the development window and the report must say so.
   - **Taxes and charges:** taxes as they applied then (no long-term gains tax before 1 Apr 2018, grandfathered after; short-term 15% before 23 Jul 2024). Charges as the simulator models them.
2. **Forward test (confirmation).** From the month this batch is merged, each variant runs in its own paper account beside the forward test, practice money only. It never changes the live rules.

## Pass rules

**The control A0** (a second, backward check of the adopted rules, judged by the same rule as the sealed holdout):
- passes when it beats the benchmark after charges and tax over the backward window;
- **and** its worst fall is no more than 10 points deeper than the benchmark's.

**A variant (A, B, C or D)** passes the backward window when **all** of these hold, after charges and tax:
1. it beats the benchmark;
2. its yearly return (CAGR) beats the control's by **at least 2 percentage points**;
3. its worst fall is no more than **5 points deeper** than the control's;
4. its yearly one-way turnover is no more than **1.5 times** the control's.

Skill odds and the probability of overfitting are reported for every variant, counting all trials, as information only. A 3.5-year window cannot reach 0.95.

**Adoption:**
- A variant that passes the backward window may replace the control only if it **also** beats the control in the forward test after **12 months** of paper trading.
- If more than one passes both, at most one is adopted: the one with the larger forward-test margin over the control.
- A variant that fails the backward window is dropped and not re-tried with other settings.

## Not allowed

- Changing any number above after the backward data is built.
- Running any variant on the development years again and quoting that as evidence.
- Opening the sealed holdout (1 Oct 2025 onwards) for these variants: it is used once, for the adopted rules only.
