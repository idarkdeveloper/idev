# Rerun addendum: three studies ran on monthly prices (18 Oct 2026)

Written and committed **before** any corrected run. The owner chose to rerun all three once each (option A) on 18 Oct
2026, after the fault was found and before any corrected data was simulated. The commit time is the proof.

## What went wrong

Asked for a stock's whole history (`range=max`), Yahoo quietly serves **one bar a month**, even with `interval=1d`.
For example, IJH came back as 318 bars since 2000 against 2,514 for the last ten years. Three studies loaded whole
histories, and the rules then ran on monthly bars:

- the rules' 274-bar minimum history meant about 22 years;
- the "6-month" return meant about 10 years;
- in the replications, the calendar itself was month ends.

| Study | First result | Status |
|---|---|---|
| Backward check (`Backward-batch`, Apr 2017 – Sep 2020) | A0 passed; A–D failed; E passed | **void** |
| US replication (`Replication-US`) | passed | **void** |
| Japan replication (`Replication-JP`) | did not pass | **void** |

Their rows stay in the trial log, marked void here. They carry no weight in any decision. Every other study loaded two,
five or ten years of history, which comes back daily, so it is unaffected. That includes the sealed holdout, batch 3
and the development studies.

## The reruns (once each, under new names)

`Backward-batch-v2`, `Replication-US-v2` and `Replication-JP-v2`. Each is claimed before it runs, logged before it
prints, and refused a second time. The rerun is all three, not only the ones whose first result was disliked.

Everything in the original plans stands unchanged:

- the rules and variants;
- the windows, costs and liquidity floors;
- the pass rules;
- what each outcome means.

That covers `preregistered-batch-2026-10-16.md` (+16b) and `preregistered-replication-2026-10-18.md`. Only the data
handling below changes.

1. **Daily prices.** Whole histories are requested by dates (`period1`/`period2`), which keeps daily bars. Old monthly
   cache files are never read.
2. **Data gate before the claim.** A run stops with nothing spent if any of these holds:
   - the benchmark is not daily;
   - the benchmark moves more than 40% in one day;
   - more than 10% of stocks are not daily;
   - the first day with 20 rankable stocks is too late (after 2017-01-01 for the replications, after 2017-07-01 for
     the backward check).
3. **Bad price points.** A zero or negative price is dropped, and that day carries the last good price. A stock whose
   close moves more than 50× in one day is left out and named in the report. Examples: 8303.T reads 2×10⁷ times its
   price in 2023, and pence/pound mix-ups. The UK keeps its own rule from the UK addendum: such a stock stops the run.
4. **Japan's benchmark.** NEXT FUNDS TOPIX ETF (1306.T) reads one tenth of its price for two days (30 Mar – 1 Apr
   2026). It is replaced by **MAXIS TOPIX ETF (1348.T)**, which tracks the same index and has clean daily data from
   May 2009 (checked 18 Oct). The choice was made on data quality alone.

## Readings

- **Backward check:** as pre-registered. Variant E's forward paper account, which starts 19 Oct, continues whatever
  happens: it is forward evidence and needs no backtest to run.
- **Replications:** as in the main plan and the UK addendum, across the US, Japan and the UK. No Indian rule changes in
  any case.

## Not allowed

- A third run of any of the three.
- Any change beyond the four data items above.
- Reading anything into the void first results.
