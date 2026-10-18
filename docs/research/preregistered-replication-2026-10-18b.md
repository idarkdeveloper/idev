# Pre-registered addendum: the adopted rules on UK midcaps (18 Oct 2026)

An addendum to `preregistered-replication-2026-10-18.md`. Written and committed **before** any UK price data has been
downloaded or looked at, and before the Japanese result is known; the US result (passed) is known. The commit time is
the proof. Status: **a plan**. Nothing is built until it is approved, and then it runs once. Everything in the main
plan applies unless changed here.

## Which market, and why only one

The candidates were chosen by data availability, never by expected results.

| Market | Decision | Reason |
|---|---|---|
| **UK, FTSE 250** | **Added** | Wikipedia lists all 250 members with tickers (checked 18 Oct 2026). The iShares FTSE 250 ETF (MIDD.L) has Yahoo history from Feb 2009. |
| Germany, MDAX | Dropped before any German price was read | Wikipedia lists 50 members. A top-20 rule needs far more than 20 eligible names, so the universe is too thin. |
| Australia, S&P/ASX MidCap 50 | Dropped | 50 members, thin for the same reason. No free member list was found. |
| Europe-wide midcaps (STOXX Mid 200) | Dropped | No free member list. |
| Emerging markets, Canada | Dropped | Poor free data or trading limits (emerging markets); no matching midcap fund (Canada). |

## The UK test

- **Universe:** today's FTSE 250 members (Wikipedia constituents table), for every date. No free change history is used, so stocks that left the index are missing and the result is flattered (survivorship). The report says so.
- **Prices:** Yahoo daily, `.L` suffix. A ticker containing `.` (such as `BT.A`) becomes `-` (`BT-A.L`). Prices are in pence.
- **Benchmark:** iShares FTSE 250 ETF (MIDD.L), adjusted close.
- **Window:** 1 Oct 2016 – 30 Sep 2026 (a lookback year from 1 Oct 2015), the same as the main plan.
- **Rules:** the adopted rules, unchanged, with no tax (both sides before tax), exactly as in the main plan.
- **Costs:**
  - **Buys:** 0.5% UK stamp duty plus 5 bps, so 55 bps.
  - **Sells:** 5 bps.
  - **The fund:** pays one buy at 5 bps, because ETF purchases carry no UK stamp duty.
- **Liquidity floor:** the Indian ₹1 crore a day at a fixed rate, £95,000. Yahoo quotes in pence, so the floor applies as 9.5 million pence of daily traded value.

## Pass rule

The same rule as the main plan: beat the fund over the window, **and** a worst fall no more than 10 points deeper than
the fund's.

## What the outcomes mean (fixed now)

- The main plan's reading of the US and Japan stands unchanged. That includes its rule that if both fail, live money waits for 12 months of forward testing instead of 6.
- Across the three markets (US, Japan, UK):
  - passing in at least two is mild support that the rules capture a general effect;
  - passing in none is evidence against it, and the 12-month wait applies.
- No Indian rule changes in any case.

## Not allowed

- Adding Germany, Australia or any other market later because of these results.
- Changing the universe, costs, floor, window or benchmark after the UK data step starts.
- Running the UK twice. It is claimed before it runs, logged before it prints, and refused a second time.
