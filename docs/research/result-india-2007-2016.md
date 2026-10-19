# Result: the India test, Oct 2007 – Mar 2016 (run once, 19 Oct 2026)

Plan: `preregistered-india-2007-2016.md` (commit 75a27ba), addendum 1 (1fbce7e) and addendum 2 (696774f, owner-approved). The window was claimed and run once in the sandbox, with the data gate passed first; it is spent.

## Output, verbatim

```
DATA GATE (pre-registered; checked before any rule set is simulated):
  [ok] split and bonus ratios read >= 98% (601 of 606)
  [ok] universe >= 400 at every month start (smallest 500, 2007-10)
  [ok] price files for >= 99% of sessions (2417 of 2417)
  [ok] split and bonus records matching a traded symbol >= 95% (577 of 602)
  [ok] unexplained split-like moves <= 15 (14 of 35 flagged)
    split-like move without a record: RELMEDIA 2008-01-21 x0.7498 (crash day)
    split-like move without a record: AMTEKAUTO 2015-08-24 x0.7459 (crash day)
    split-like move without a record: APOLLOTYRE 2013-06-13 x0.7455 (unexplained)
    split-like move without a record: APTECHT 2008-01-21 x0.7457 (crash day)
    split-like move without a record: APTECHT 2009-01-07 x0.7557 (crash day)
    split-like move without a record: ARVIND 2008-01-21 x0.7474 (crash day)
    split-like move without a record: BAJAJHIND 2008-01-22 x0.7496 (crash day)
    split-like move without a record: ERAINFRA 2008-09-17 x0.1988 (unexplained)
    split-like move without a record: GLENMARK 2009-01-28 x0.7517 (unexplained)
    split-like move without a record: IFCI 2008-10-24 x0.7538 (crash day)
    split-like move without a record: IGL 2012-04-10 x0.661 (unexplained)
    split-like move without a record: IIFL 2008-10-10 x0.7508 (crash day)
    split-like move without a record: JINDALPOLY 2013-07-31 x0.7469 (unexplained)
    split-like move without a record: LAKSHVILAS 2009-09-24 x0.7524 (rights or scheme (BSE XR))
    split-like move without a record: LAKSHVILAS 2014-07-25 x0.739 (rights or scheme (BSE XR))
    split-like move without a record: NCC 2008-10-10 x0.7574 (crash day)
    split-like move without a record: FEL 2013-06-21 x0.6707 (rights or scheme (BSE SA))
    split-like move without a record: PFIZER 2013-12-05 x0.7499 (unexplained)
    split-like move without a record: POLARIS 2014-10-27 x0.7544 (unexplained)
    split-like move without a record: RENUKA 2011-11-14 x0.7439 (unexplained)
    split-like move without a record: SASKEN 2008-01-22 x0.7504 (crash day)
    split-like move without a record: SKUMARSYNF 2008-03-24 x0.7391 (unexplained)
    split-like move without a record: SKUMARSYNF 2008-10-06 x0.7532 (crash day)
    split-like move without a record: SUZLON 2013-02-28 x0.6598 (unexplained)
    split-like move without a record: TEXINFRA 2010-11-15 x0.4007 (unexplained)
    split-like move without a record: TRIVENI 2011-05-03 x0.4004 (rights or scheme (BSE SA))
    split-like move without a record: TULIP 2012-07-26 x0.7398 (unexplained)
    split-like move without a record: UNITECH 2008-10-24 x0.5032 (crash day)
    split-like move without a record: LITL 2008-01-21 x0.7466 (crash day)
    split-like move without a record: LITL 2009-01-07 x0.7465 (crash day)
    split-like move without a record: ORBITCORP 2008-10-24 x0.7545 (crash day)
    split-like move without a record: HGS 2008-01-21 x0.7495 (crash day)
    split-like move without a record: IRB 2013-08-01 x0.7476 (unexplained)
    split-like move without a record: REIAGROLTD 2010-06-07 x0.7478 (rights or scheme (BSE XR))
    split-like move without a record: NHPC 2013-03-04 x0.7587 (unexplained)
THE INDIA TEST, run once (docs/research/preregistered-india-2007-2016.md (commit 75a27ba) + addenda 1 and 2), 2007-10-01 to 2016-03-31, top-500 NSE universe by traded value, after charges x2 and tax as it applied then.
  Benchmark: Nifty 500 total return index less 0.20% a year: +64.6%, worst fall -63.8%.
  rule set                              return  vs bench  a year worst fall turnover  skill  verdict
  A0 the adopted rules                 +119.6%    +55.0%   +9.7%     -73.9%     244%   0.21  fail
  B top 10                              +70.7%     +6.1%   +6.5%     -76.0%     287%   0.10  fail
  E residual momentum                  +146.4%    +81.8%  +11.2%     -72.9%     259%   0.25  fail
  BRK drawdown brake (100-day)         +295.0%   +230.4%  +17.5%     -31.5%     348%   0.34  PASS
  STP stop-loss 3x ATR                  -51.8%   -116.4%   -8.2%     -59.5%    1022%   0.00  fail
  IVW inverse-volatility weights       +142.5%    +77.8%  +11.0%     -71.0%     244%   0.24  fail
  PLN plain top 20, no buffer            -0.6%    -65.3%   -0.1%     -77.2%     467%   0.02  fail
  A0 misses: worst fall within 10 points of the benchmark's.
  B misses: +2 points a year over A0.
  E misses: +2 points a year over A0.
  STP misses: beats the benchmark; +2 points a year over A0; turnover at most 1.5x A0's.
  IVW misses: +2 points a year over A0.
  PLN misses: beats the benchmark; +2 points a year over A0; turnover at most 1.5x A0's.
  A0 verdict: NOT passed. B and E are information for their paper accounts; BRK, STP or IVW passing earns a paper account; PLN failing supports the rank buffer. Nothing is adopted from this test.
  Probability of backtest overfitting: 0.75 (7 variants, 252 splits of 102 months): the chance the variant that looks best is at or below the median on months it was not picked on; 0.5 or more = picking the best here is no better than chance.
  Sensitivity, charges x1 (reported, not judged): A0 +160.3% (+95.7% vs bench); B +104.8% (+40.2% vs bench); E +199.6% (+135.0% vs bench); BRK +369.4% (+304.8% vs bench); STP +10.2% (-54.4% vs bench); IVW +195.1% (+130.4% vs bench); PLN +52.1% (-12.5% vs bench).
  Positions sold after their stock stopped trading (at its last close, no haircut): A0 20, B 10, E 11, BRK 13, STP 15, IVW 20, PLN 19.
  Unexplained split-like moves (addendum 2) held through by each rule set: A0 0, B 0, E 0, BRK 0, STP 0, IVW 0, PLN 0.
  Data: mended from BSE's flags (addendum 2): ENGINERSIN 2010-05-06 shares x5, SUNTV 2007-07-23 shares x4.
  Data: 2417 of 2417 daily files; 1936 stocks traded; 474 members a month on average.
  Data: 4 dividend record(s) not applied (amount over 30% of the price).
  Data: 0 stock(s) left out for a one-day move over 50x: none.
  Data: price bands and ASM/GSM lists not applied (not available for these years).
```

## Reading

- **A0, the adopted rules: NOT passed, by 0.1 point.**
  - It beat the benchmark by +55.0 points: +9.7% a year after double charges and tax.
  - But its worst fall (−73.9%) was 10.1 points deeper than the benchmark's (−63.8%); the rule allows 10.
  - The momentum edge showed up again in a market the rules never saw. The cost is deeper falls in a crash, the
    same pattern as the research years (−31% vs −21%).
  - At normal charges it beat the benchmark by 95.7 points.
- **BRK, the drawdown brake (100-day average): PASSED.**
  - +295.0% (+17.5% a year), worst fall −31.5%, half the benchmark's.
  - Most of its gain is from staying out of the 2008 crash.
  - In the research years (Oct 2020 – Sep 2025) the same brake lost to the plain rules (+204.8% vs +275.0%):
    selling into dips that recovered fast cost more than it saved.
  - So it is insurance. It pays in a long bear market and costs in V-shaped ones. Two windows, two answers.
  - Under the plan it earns its own paper account, under a separate dated note. It changes nothing live. Twelve
    months of paper trading in a calm market will likely show it lagging; only a real bear market tests it.
- **STP, 3×ATR stop-loss: failed badly.** −51.8%, with 1,022% turnover a year. Stops cut momentum to pieces in 2008–2016 as in 2020–2025. They stay out of the research rules.
- **PLN, plain top 20 with no rank buffer: −0.6%, against +119.6% for A0.** This strongly supports the rank buffer.
- **B, top 10:** behind A0 (+70.7%) with deeper falls. This is information for its paper account and argues against it.
- **E, residual momentum (+146.4%) and IVW, inverse-volatility weights (+142.5%):** both ahead of A0, but by less
  than the +2 points a year the plan requires.
- **Probability of overfitting:** 0.75 across the 7 rule sets. Picking the best one here is no better than chance;
  skill odds are 0.00–0.34.
- **Caveats:**
  - About 10–20 positions per rule set were sold at the last price of a stock that stopped trading in the EQ series,
    with no haircut. That flatters every rule set somewhat.
  - None of the 14 unexplained split-like moves was held through by any rule set.
  - Price bands and surveillance lists were not applied.
