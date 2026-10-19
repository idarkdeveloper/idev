# Addendum to the India Oct 2007 – Mar 2016 test: four more rule sets (19 Oct 2026)

Added to `preregistered-india-2007-2016.md` at the owner's request, **before** the data gate ran and before any
return on the window existed. The first run attempt was stopped while it was still downloading daily files (289 of
2,417). It had not reached the gate or claimed the window, and no trial was logged. Everything else in the original
plan is unchanged: the data, universe, window, costs, gate and the A0 verdict.

## Added rule sets

These four ideas failed in the research years (Oct 2020 – Sep 2025). Those years had only 5 bear months, so they are
re-run here on a window that includes the 2008 crash. Each changes exactly one thing in A0
(`research_studies.FINALIST`), with the same setting it had in the research runs:

| Id | Idea | The one change |
|---|---|---|
| BRK | Drawdown brake, 100-day average | `brake="sma100"` |
| STP | Stop-loss at 3× ATR (today's practice-account stop) | `stop="atr3"` |
| IVW | Inverse-volatility weights | `weights="inverse_vol"` |
| PLN | Plain top 20 with no rank buffer | `buffer=None, tax_aware=False` |

They run beside A0, B and E in the same single run, at charges ×2 (judged) and ×1 (reported only).

## How they are judged

Each is judged by the same four variant tests as B and E, against A0 at charges ×2 and after tax:
1. it beats the benchmark;
2. it gains **+2 points a year** over A0;
3. its worst fall is within **5 points** of A0's;
4. its turnover is at most **1.5×** A0's.

PLN is the reverse case: it checks whether the rank buffer helps. If PLN fails test 2 (it does not beat A0 by
2 points a year), the buffer's place in the adopted rules is supported.

The probability of overfitting now counts **7 trials** (A0, B, E, BRK, STP, IVW, PLN).

## What a result means

Nothing is adopted from this window.
- **BRK, STP or IVW passes:** it earns its own 12-month paper account beside the adopted rules, under a separate,
  dated note. It does not change the forward test or live trading.
- **It fails:** it stays dropped.
