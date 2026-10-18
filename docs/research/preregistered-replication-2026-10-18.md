# Pre-registered: the adopted rules on US and Japanese midcaps (18 Oct 2026)

Written and committed **before** any US or Japanese data has been downloaded or looked at, and before any code for
this test exists. The commit time is the proof. Status: **a plan**. Nothing is built until it is approved, and when it
is built, it runs once per market.

## Question

Do the adopted rules (`research_studies.FINALIST`), **unchanged**, beat each market's own midcap index fund in markets
they were never tuned on? Momentum is documented in US stocks (Jegadeesh and Titman, 1993, and decades since) and
famously weak in Japan (Asness, "Momentum in Japan", 2011). So this is a replication, not a search: one rule set, two
markets, one run each.

## Markets, data and window

| | United States | Japan |
|---|---|---|
| Universe | S&P MidCap 400 | TOPIX Mid400 |
| Prices | Yahoo daily, no suffix | Yahoo daily, `.T` suffix |
| Benchmark | iShares Core S&P Mid-Cap ETF (IJH), adjusted close | an ETF tracking TOPIX Mid400 if one has Yahoo history for the whole window; otherwise the TOPIX ETF 1306.T. Chosen in the data step, before any strategy run, and printed in the result |
| Currency | USD | JPY |

- **Window:** 1 Oct 2016 – 30 Sep 2026, 10 years. Decisions use only data up to each decision day, and the leakage
  guard runs before the result, as for every research command.
- **Membership:** point in time if a free, complete change history for the index is found in the data step. Otherwise
  today's members for every date. Every stock that left the index is then missing, which flatters the result
  (survivorship). The result states which of the two was used, and how many members have no prices.
- **Liquidity floor:** the Indian ₹1 crore a day, converted once at a fixed rate: US$120,000 and ¥18 million a day.

## Rules (unchanged)

The adopted rules, exactly as in the forward test:
- the same momentum score and eligibility (above the 200-day average, liquid);
- top 20, equal weight, monthly;
- keep a holding while it ranks 35 or better;
- the tax-aware wait at 50, kept although it is an Indian tax rule (it only delays some sales);
- a 25% disaster floor below the buy price;
- no stop.

Costs:
- US: 5 bps a side, for no commission plus slippage;
- Japan: 10 bps a side;
- the benchmark fund pays one buy at the start.

No taxes in either market: both sides are compared before tax. Indian charges and taxes do not apply. Residual
momentum (E) and the other variants are **not** run here.

## Pass rule (per market)

1. The adopted rules beat the benchmark fund over the window (total return, after the costs above), **and**
2. their worst fall is no more than 10 points deeper than the fund's.

The same rule as the sealed year. CAGR, turnover, months ahead and the skill odds are reported as information.

## What each outcome means (fixed now)

| US | Japan | Reading |
|---|---|---|
| pass | pass | Mild support that the rules capture a general effect. Nothing changes in India. |
| pass | fail | What the literature predicts. Nothing changes. |
| fail | pass | Unexpected; recorded as weak evidence against. Nothing changes. |
| fail | fail | Evidence against a general effect. The live-money decision then waits for **12** months of forward testing instead of 6, and the one-share live test is not affected. |

In no case is an Indian rule changed because of this test.

## Not allowed

- Changing any rule, cost, window, universe or benchmark after the data step has started.
- Running other variants, other markets or other windows and quoting them.
- Re-running a market. Each runs once, is logged in the trial log before printing, and is refused a second time
  (the once-only lock, as for the backward check).
- Using the result to tune the Indian rules.
