# Pre-registered batch 2: residual momentum (16 Oct 2026)

Written and committed **before** any data for the backward window (Apr 2017 – Sep 2020) has been built, loaded or
looked at, and before residual momentum has been run on any period. The commit time is the proof. It adds one
variant, **E**, to the backward check of `preregistered-batch-2026-10-16.md`. Everything in that file (the control,
the window, the benchmark, the taxes, the pass rules, adoption only after 12 months of forward paper trading) applies
to E unchanged. E counts as a trial when it is run, pass or fail.

## Why

Plain momentum ranks stocks by their total return, which is partly just the market's move times the stock's beta. In
a rising market, high-beta stocks rank high for that reason alone, and they fall hardest when the market turns.
Residual momentum ranks stocks by the part of their return the market does not explain: Blitz, Huij and Martens,
"Residual momentum", Journal of Empirical Finance 18 (2011). It is reported to keep most of momentum's return with
far smaller crashes. Our research found a deeper worst fall than the fund (about −31.5% against −21.2%) and a
fragile result across settings, so this is a direct test of a known fix. It has not been run on any of our data.

## Variant E: residual momentum

The adopted rules (`research_studies.FINALIST`) with **only the ranking changed**. Nothing else changes:
- the same eligible names (above their 200-day average, liquid);
- top 20;
- buffer 35, tax-aware wait at 50, 25% floor;
- monthly;
- same-close fills, charges and tax.

On each decision day t, for every eligible name:

1. **Returns:** daily log returns of the stock and of the benchmark on the market's trading days. The benchmark is the backward check's index series, or the index fund in any later run.
2. **Window:** the sessions from t−252 to t−21. That is the 12-1 window: the latest month is skipped, as in 12-1 momentum.
3. **Regression:** an ordinary least squares fit through the origin (no intercept) of the stock's returns on the benchmark's returns over that window: b = Σ(r·r_benchmark) / Σ(r_benchmark²). It needs at least 150 days on which both have a return; otherwise the name is left unranked that day.
4. **Residuals:** e = r − b · r_benchmark on those days. The stock's own excess return stays in the residuals; an intercept fitted on the same days would absorb it and make every sum zero.
5. **Score:** the sum of the residuals divided by (their standard deviation × √n), where n is the number of days.
6. **Ranking:** by score, highest first. Ties go to the earlier symbol in alphabetical order.

**Difference from the paper:** Blitz, Huij and Martens estimate exposures over the 36 months before ranking. Here the benchmark index series starts in April 2016, only a year before the window, so beta is fitted on the ranking window itself, through the origin.

## Pass rule

Exactly as for variants A to D, against the same control A0 on the same window:
- beat the benchmark;
- at least +2 percentage points a year over the control;
- a worst fall no more than 5 points deeper than the control's;
- yearly one-way turnover at most 1.5 times the control's.

**Adoption:**
- A pass makes E a candidate for 12 months of forward paper trading beside the control.
- If more than one variant passes both stages, at most one is adopted, the one with the larger forward-test margin.
- A failure drops it, and it is not retried with other windows or a different factor.

## Not allowed

- Changing any number above after the backward data is built.
- Running E on the development years (Oct 2020 – Sep 2025) and quoting that as evidence.
- Adding further factors (size, value, sector) to the regression.
- Using the sealed holdout for E.
