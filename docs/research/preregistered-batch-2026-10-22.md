# Pre-registered batch 4: quarterly rebalance and volatility scaling (22 Oct 2026)

Written and committed **before** either idea has been coded or run on any data, and before the data repairs in
`survivorship-2026-10-22.md` exist in code. The commit time is the proof. Each variant runs **once**, beside the
control, in one run. Both count as trials in `state/research/trials.jsonl`, pass or fail.

## Why these two

Everything bolted on top of the adopted rules has failed (stops, sizing models, earnings drift, promoter buying,
larger half, sector-relative, industry cap, residual momentum, smooth momentum, institutional deals). Two facts from
the studies point elsewhere:

1. **Trading less is where value has shown up.** The rank buffer halved turnover (341% to 168% a year) and raised the
   after-tax return in 2020–2025; without it the same top 20 made −0.6% against +119.6% in 2007–2016.
2. **The recurring weakness is the depth of falls, not the return.** −31.5% against −21.2% for the fund in 2020–2025;
   −73.9% against −63.8% in 2007–2016, missing that test by 0.1 point.

- **H, quarterly rebalance**, tests 1 directly: the same rules, a third as many decision days.
- **V, volatility scaling**, tests 2 with the published fix for momentum crashes: Barroso and Santa-Clara, "Momentum has
  its moments", Journal of Financial Economics 116 (2015), scale the momentum portfolio by its own recent realised
  volatility. It is different from the per-stock inverse-volatility weights (IVW), which failed in 2007–2016, and from the
  drawdown brake, which is all-or-nothing.

## Where they run, and what that is worth

No unseen Indian window is left: the sealed year, Apr 2017 – Sep 2020 and Oct 2007 – Mar 2016 are all spent and are never
rerun. So each variant runs **once** on the development years, **1 Oct 2020 – 30 Sep 2025**, beside the control in the
same run, as batch 3 did. That is weak evidence, because the adopted rules were chosen on these years. A pass only earns
12 months of forward paper trading beside the adopted rules.

## The data (new for this run, and for every later study)

The run uses the two repairs fixed in `survivorship-2026-10-22.md`:

- **Members with no Yahoo history are filled from NSE's own daily files** (the bhavcopy archive), split-, bonus- and
  dividend-adjusted from NSE's corporate actions, under today's symbols. The daily files are loaded from 1 Oct 2019
  (the ranking's lookback year) to 30 Sep 2025.
- **A holding whose stock stops trading loses 30%** of its last value on the session after its last bar and is sold
  then, instead of being sold at its last price.

Both apply to the control and to both variants alike. Because of them the control's number **will differ** from the
+363.8% of earlier reports. The run also simulates the adopted rules on Yahoo's names alone, without the haircut, and
prints the two side by side as a **survivorship diagnostic**: how much the missing names flattered the earlier
development-years results. That line is a description, not a trial; no earlier result is revised by it.

**Data gate, before the claim.** The run stops with nothing spent if any of these holds:
- fewer than 99% of the trading days' NSE daily files could be loaded;
- NSE's corporate actions for the period could not be fetched;
- the leakage guard fails (as for every research command).

Everything else is as in batch 3: point-in-time Midcap 150 members, MID150BEES as the benchmark, Indian charges and
capital-gains tax, same-close fills, cash at the liquid-fund rate.

## The control

The adopted rules, `research_studies.FINALIST`, with the delisting haircut above and nothing else changed:
- the composite momentum score; eligible names above their 200-day average and liquid;
- top 20, equal slots, monthly rebalance on the first session of each month;
- a rank buffer of 35; the tax-aware wait at 50 for a profitable holding aged 335–365 days;
- a 25% disaster floor, no stop;
- charges and tax.

All three rule sets make their first rebalance on the window's first ranked day (the first month start on which 20
names can be ranked).

## Variant H: quarterly rebalance

The control with **only the calendar changed**:

- after the first rebalance, the portfolio is rebalanced on the **first session of each calendar quarter** (January,
  April, July, October) instead of each month;
- the buffer (35), the floor (25%, checked daily), the slots and the fills are unchanged;
- the tax-aware wait's age band widens from **335–365 days to 273–365 days**. Quarterly checks are up to 92 days
  apart, and a band narrower than that could miss a holding entirely; 365 − 92 = 273. The number follows from the
  cadence and is not tuned.

Two consequences, fixed here so they are not read as surprises:
- a profitable holding ranked 36–50 can now be kept for up to three extra months, not one, to reach long-term tax;
- a slot emptied by a floor sale stays in cash for up to a quarter, not a month.

## Variant V: volatility scaling

The control with **only the invested share changed**. On each monthly rebalance day t, after the buffer has chosen what
to keep and what to buy (the "basket", up to 20 names):

1. **Returns.** For each name in the basket, daily returns of its adjusted close over the last **756 sessions** up to
   t (about three years), or as many as it has if that is at least **378**. A name with fewer is left out of the
   estimate (its weight in the portfolio is not affected). If fewer than **10** names remain, the share is 1.
2. **Basket return.** On each of those days, the mean of the available names' returns, with equal weights.
3. **Two volatilities.** σ_recent = the standard deviation of the basket's returns over the last **126 sessions**
   (Barroso and Santa-Clara's six months); σ_ref = the same over all the days in step 1 (up to 756).
4. **Share.** target = min(1, σ_ref / σ_recent). Never above 1: no leverage. No floor.
5. **Dead band.** The share actually applied changes only when the target differs from the share currently applied by
   **0.10 or more**; otherwise the current share stays. This limits charges; 0.10 is fixed here and not varied.
6. **Trades.** When the share changes, every holding is scaled to it pro rata (a trim when it falls, a top-up from cash
   when it rises), then new names are bought at share × account ÷ 20 each, as the control buys at account ÷ 20. Between
   rebalances the share is not revisited; the floor and the buffer act as in the control.

What to expect, written down now: in calm months the target sits mostly between 0.85 and 1.0, so V carries a small
cash drag in good years; after a volatility spike it can fall to 0.5 or lower and will miss part of a fast rebound,
the same cost the brake pays. Rule 3 below measures the first; rule 2 measures what it buys.

## Pass rules (each variant against the control, same run, after charges and tax)

**H passes** when all four hold:
1. it beats the fund (total return);
2. its yearly return (CAGR) is no more than **1.0 point** below the control's;
3. its worst fall is no more than **5 points** deeper than the control's;
4. its yearly one-way turnover is at most **two-thirds** of the control's.

**V passes** when all four hold:
1. it beats the fund;
2. its worst fall is at least **5 points shallower** than the control's;
3. its yearly return (CAGR) is no more than **1.0 point** below the control's;
4. its yearly one-way turnover is at most **1.5 times** the control's.

Skill odds (counting every trial in the log) and the probability of backtest overfitting across the three rule sets are
reported as information. Five years cannot reach the 0.95 bar.

## Adoption (fixed now)

- **A pass earns only a 12-month paper account** beside the adopted rules, built on the owner's approval, under a dated
  note like the brake's and the cluster account's. Nothing changes live.
- **H** replaces the adopted rules only if, after 12 months on paper, its return is within 1 point of the forward
  test's and its turnover is at least a third lower, and the owner decides so.
- **V** is read as the brake is: a verdict needs a fall of **20% or more** in MID150BEES while it runs, at least 12 months
  in, and the fund back above its 100-day average. In calm months it should trail the forward test by at most **2 points
  a year**; that is its cost, not a failure. Adoption needs that verdict and the owner's decision.
- **A failure drops the variant.** It is not retried with other settings.

| H | V | Reading |
|---|---|---|
| pass | pass | Both go to paper. They are not combined; a combination would be a new idea under its own note. |
| pass | fail | Trading less helps; scaling does not. H goes to paper. |
| fail | pass | V goes to paper. The monthly cadence stands. |
| fail | fail | The adopted rules stand. Nothing is retried. |

## Not allowed

- Changing any number above after the data step has started: the quarter calendar, 273–365, 126, 756, 378, 10,
  the 0.10 dead band, the 30% haircut, the pass margins.
- Retrying H as bimonthly or any other cadence, or V with another window, a fixed volatility target, the fund's
  volatility as the target, a floor on the share, or without the dead band.
- Running either variant on the sealed year, on Apr 2017 – Sep 2020 or on Oct 2007 – Mar 2016.
- Reading the survivorship diagnostic as a trial result, or revising any earlier result from it.
- A second run. The batch is claimed before it runs, logged before it prints, and refused a second time.
