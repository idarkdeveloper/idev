# Feasibility: an Indian test on 2008–2016 (19 Oct 2026)

**Question:** can the adopted momentum rules be tested once on 2008–2016, a period no Indian study here has touched,
without the survivorship bias that today's index lists would bring?

**Scope:** this note covers data availability only. **No strategy return was computed on any of these years.**
A test, if run, needs its rules pre-registered first and runs once, like the other studies.

## What was checked, and what was found

| Need | Source checked | Result |
|---|---|---|
| Daily prices for every stock, dead ones included | NSE bhavcopy archive (`nsearchives.nseindia.com/content/historical/EQUITIES/…`) | **Available from November 1994** (files for Nov 1994, Jan 1995, 1996, 2000, 2004 and 2008 downloaded and unzipped: 136 stocks in Nov 1994, 881 in Jan 1996). All 108 month-ends of 2008–2016 present. About 1,230 EQ/BE stocks a day in 2008, 1,550 in 2016. ISIN column from 2012. |
| Same from BSE (the user's suggestion: "BSE has the longer history") | BSE bhavcopy (`bseindia.com/download/BhavCopy/Equity/EQddmmyy_CSV.ZIP`) | The exchange is older, but its **downloadable files start only around 2006**: Jan 2006 and earlier return a web page; June 2006 onward are real files. BSE does flag ex-dates in each file (`XD`, `XB`, `SS`, `XR`), which is useful as a cross-check. For 2008–2016, NSE covers everything BSE does. |
| Point-in-time Nifty 500 (CNX 500) membership | NSE Indices press releases (`niftyindices.com/press-release`) | **Not reliable before 2016.** Releases exist from 1998, but none for 2010 or 2012 and only 2 in 2011. Before 2015 they list CNX 500 changes by company name only, and the text comes out of the PDF jumbled. Even after 2016 the rebuilt Nifty 500 log shows 54 size or fit problems when checked. |
| A point-in-time universe without index lists | The bhavcopy itself | **Feasible:** rank all EQ/BE stocks by trailing traded value (`TOTTRDVAL`) and take the top 500. This is point-in-time by construction. Market cap is not in the file (no shares outstanding), so this is a liquidity universe, not the Nifty 500. |
| Split and bonus adjustment | Bhavcopy `PREVCLOSE` | **Not adjusted:** Infosys on its 1:1 bonus ex-date (2 Dec 2014) shows `PREVCLOSE` ₹4,349.85 against a close of ₹2,126.60. Adjustment must come from corporate actions. |
| Corporate actions (splits, bonuses, dividends) | NSE `api/corporates-corporateActions`, by date range | **Available for every year 2007–2016**, 1,500–2,100 actions a year (but only 218 in 2000 and 77 in 2004, so **adjusted prices are only possible from 2007**; this, not the price files, is what stops the test going further back): bonuses and splits with ratios, and dividends with amounts (99%+ give the amount). The existing `corp_actions.parse_subject` reads 88–100% of bonus and split ratios from 2010. It reads only 30–40% for 2007–09, but every miss is one of two regular old phrasings ("Fv Split Rs.10/- To Rs.2/", "Bonus - 1:1"), which a small pattern fixes. |
| Benchmark with dividends | Nifty 500 total return index (niftyindices, existing `fetch_tri`) | **Available** for 2008 (246 days) and 2015 (248 days); Nifty Midcap 100 TRI too. |

## Why it matters: survivorship

At each 2008 month-end, about **150 of the top 500 stocks by traded value no longer trade on NSE today**. By 2016 that
falls to about 78. Examples include Satyam, Ranbaxy, Patni, Vijaya Bank, DHFL, Lakshmi Vilas Bank and Manpasand.
A few in that count are ETFs (LIQUIDBEES, NIFTYBEES, CPSEETF), which today's equity list leaves out, so the true
figure is slightly lower; the universe rule must exclude ETFs.

Only about **185 of that 2008 top 500 are in today's Nifty 500**. A test on today's list would miss almost two
thirds of the stocks an investor could actually have bought, and nearly all the failures. The bhavcopy route avoids
this, because the dead companies are in the daily files until they stop trading.

## Verdict

**Feasible from NSE's own files, with a liquidity-ranked universe instead of the Nifty 500.** BSE adds nothing for
these years. Three things remain open and should be fixed in the pre-registration, before any return is seen:

1. **The universe rule.** For example: the top 500 EQ/BE stocks by 6-month average traded value at each rebalance,
   ETFs and funds excluded. Then the momentum top 20 among them, under the adopted rules.
2. **Exits for delisted stocks.** For example, the last traded close, with a stated haircut for stocks that stop
   trading without a buyback.
3. **EQ series only, ETFs excluded by name before 2012.** BE is trade-to-trade and often under surveillance, the nearest 2008 analogue of the ASM/GSM lists the adopted rules exclude. Before 2012 the file has no ISIN to spot funds (`INF…`), so a name list must drop ETFs like LIQUIDBEES and NIFTYBEES, which otherwise top any traded-value ranking.
4. **State the ranking window.** Traded value rises with price, so a short window would quietly pre-select recent winners, an extra momentum filter.
5. **Keep the window apart from the backward check.** That check reads prices from 1 Apr 2016 as lookback and trades from Apr 2017. Ending trading in Dec 2016 does not overlap its traded months; ending in Mar 2016 keeps the two fully apart. Filters that cannot be rebuilt for those years (price bands, ASM lists) are handled as in the 2017–2020 plan.
6. **Costs and tax of the time.** STT existed from 2004. Long-term gains were tax-free until 2018, short-term gains
   were taxed at 15%, and brokerage was higher. The cost model must use the rules of each date, or say plainly that
   it does not.

**What a result would mean:** this would be a new test of "does Indian momentum, run by these rules, beat the
market after charges in 2008–2016". That period covers the 2008 crash, the 2009 rebound, the 2011 fall and the
2013 taper scare. It is not a replication on the Midcap 150, because the universe is different.

**Size of the job:** about 2,250 daily NSE files (≈100 MB), one corporate-actions download a year, and the Nifty 500
TRI. Run it in the sandbox or off-hours on the server (nice -n 19, after 15:30 IST, not overlapping the 21:32 queue).
