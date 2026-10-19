# Addendum 2 to the India Oct 2007 – Mar 2016 test: the unrecorded-split check (19 Oct 2026)

Approved by the owner on 19 Oct 2026 and committed **before any rule set was simulated on this window**. No return
on the window exists.

## What happened

The data gate ran once and failed one check: **37** split-like one-day moves had no corporate-action record, against
a limit of 15. The other four checks passed:
- split and bonus ratios read: 601 of 606;
- universe: at least 500 members at every month start;
- price files: 2,417 of 2,417;
- symbol matches: 575 of 600.

Nothing was simulated, and nothing is spent.

## Why the check failed: a design error

The check treats a one-day move near ×¾ (a 1:3 bonus) as split-like. That is also the size of an ordinary fall on
India's crash days, when stocks closed at their 20–25% lower limits. Mending data cannot remove a real crash, so as
written the check could never pass.

This is a change to a pre-registered rule, not a data mend. It was found from data alone: the benchmark's daily
move, BSE's ex-date flags and member counts. No return was computed.

## The 37 flagged moves, classified with data only

Columns: stock, date, one-day close ratio, the Nifty 500 total return index's move that day, the share of universe
members down more than 10% that day, and the classification.

The classification uses BSE's daily-file flag (`TDCLOINDI`) for the same stock within ±3 sessions. "Same stock"
means a BSE row whose close is within 3% of the NSE close that day.

| Stock | Date | Ratio | Index | Members down >10% | Class |
|---|---|---|---|---|---|
| RELMEDIA | 2008-01-21 | ×0.7498 | -10.2% | 54% | crash day |
| AMTEKAUTO | 2015-08-24 | ×0.7459 | -6.7% | 46% | crash day |
| APOLLOTYRE | 2013-06-13 | ×0.7455 | -1.2% | 1% | unexplained |
| APTECHT | 2008-01-21 | ×0.7457 | -10.2% | 54% | crash day |
| APTECHT | 2009-01-07 | ×0.7557 | -6.6% | 29% | crash day |
| ARVIND | 2008-01-21 | ×0.7474 | -10.2% | 54% | crash day |
| BAJAJHIND | 2008-01-22 | ×0.7496 | -6.6% | 40% | crash day |
| ENGINERSIN | 2010-05-06 | ×0.2 | -0.3% | 0% | BSE XB (bonus): mended |
| ERAINFRA | 2008-09-17 | ×0.1988 | -1.9% | 1% | unexplained |
| GLENMARK | 2009-01-28 | ×0.7517 | +2.8% | 0% | unexplained |
| IFCI | 2008-10-24 | ×0.7538 | -10.9% | 40% | crash day |
| IGL | 2012-04-10 | ×0.661 | +0.1% | 0% | unexplained |
| IIFL | 2008-10-10 | ×0.7508 | -6.9% | 33% | crash day |
| JINDALPOLY | 2013-07-31 | ×0.7469 | -0.2% | 1% | unexplained |
| LAKSHVILAS | 2009-09-24 | ×0.7524 | +0.3% | 0% | BSE XR (rights): genuine under the plan |
| LAKSHVILAS | 2014-07-25 | ×0.739 | -0.8% | 0% | BSE XR (rights): genuine under the plan |
| NCC | 2008-10-10 | ×0.7574 | -6.9% | 33% | crash day |
| FEL | 2013-06-21 | ×0.6707 | -0.2% | 1% | BSE SA (scheme): genuine under the plan |
| PFIZER | 2013-12-05 | ×0.7499 | +1.1% | 1% | unexplained |
| POLARIS | 2014-10-27 | ×0.7544 | -0.4% | 0% | unexplained |
| RENUKA | 2011-11-14 | ×0.7439 | -0.6% | 1% | unexplained |
| SASKEN | 2008-01-22 | ×0.7504 | -6.6% | 40% | crash day |
| SKUMARSYNF | 2008-03-24 | ×0.7391 | -0.0% | 4% | unexplained |
| SKUMARSYNF | 2008-10-06 | ×0.7532 | -5.9% | 26% | crash day |
| SUNTV | 2007-07-23 | ×0.2482 | +1.1% | 0% | BSE XB (bonus): mended |
| SUZLON | 2013-02-28 | ×0.6598 | -2.0% | 2% | unexplained |
| TEXINFRA | 2010-11-15 | ×0.4007 | +0.6% | 0% | unexplained |
| TRIVENI | 2011-05-03 | ×0.4004 | -2.2% | 0% | BSE SA (scheme): genuine under the plan |
| TULIP | 2012-07-26 | ×0.7398 | -1.4% | 2% | unexplained |
| UNITECH | 2008-10-24 | ×0.5032 | -10.9% | 40% | crash day |
| LITL | 2008-01-21 | ×0.7466 | -10.2% | 54% | crash day |
| LITL | 2009-01-07 | ×0.7465 | -6.6% | 29% | crash day |
| ORBITCORP | 2008-10-24 | ×0.7545 | -10.9% | 40% | crash day |
| HGS | 2008-01-21 | ×0.7495 | -10.2% | 54% | crash day |
| IRB | 2013-08-01 | ×0.7476 | -0.6% | 2% | unexplained |
| REIAGROLTD | 2010-06-07 | ×0.7478 | -1.7% | 0% | BSE XR (rights): genuine under the plan |
| NHPC | 2013-03-04 | ×0.7587 | -0.7% | 1% | unexplained |

Totals: 16 crash days, 5 rights or scheme moves, 2 bonuses mended, and 14 unexplained.

## The changed rule

All other gate checks and every other number in the plan are unchanged.

1. **Crash days.** A flagged move on a session when the Nifty 500 total return index fell **5% or more** is a genuine
   fall, not counted.
   - This line was chosen after seeing this table. Every crash day above is −5.9% or worse, and every other day is
     −2.2% or better, so any line from −2.5% to −5.8% classifies them identically.
2. **Rights and schemes.** A flagged move that BSE marks `XR` (rights) or `SA` (scheme of arrangement) within ±3
   sessions is genuine under the plan's own rule ("rights, demergers, schemes are not adjusted"), not counted.
3. **Mended bonuses and splits.** A flagged move that BSE marks `XB`, `XS`, `SS` or `CS` within ±3 sessions is added as
   a corporate action on BSE's flag date. Its ratio is the nearest of the pre-registered split ratios to the observed
   move. Prices are then rebuilt and the gate re-run.
   - Here: SUNTV, 23 Jul 2007 ×0.25 (shares ×4); ENGINERSIN, 6 May 2010 ×0.2 (shares ×5).
4. **Threshold.** The threshold stays at **15** unexplained moves.

The 14 unexplained moves stay unadjusted and are listed in the run's report. Several look like stock-specific
crashes (IGL, IRB, Apollo Tyres). Others look like demergers (Polaris, Texmaco Infra) or a split (Era Infra).

Left unadjusted, a missed split or demerger looks like a fall, so these mostly work against the strategy. The run
reports how many of them a rule set ever held.
