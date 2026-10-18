# Pre-registered batch 3: smooth momentum and institutional buying (18 Oct 2026)

Written and committed **before** either idea has been coded or run on any data. The commit time is the proof. Status:
**a plan**. Nothing is built until it is approved, and each variant then runs once.

## Why these two

- **F: smooth momentum ("frog in the pan").** Da, Gurun and Warachka, "Frog in the Pan: Continuous Information and
  Momentum", Review of Financial Studies 27 (2014). Stocks that rose through many small gains keep rising for longer than
  stocks that rose through a few large jumps. It uses only prices. It could also lengthen holding periods, which suits
  the 12-month long-term tax wait.
- **G: institutional buying in bulk and block deals.** These disclosures are what the agent was built to follow, but they
  have never been tested as a signal. NSE serves their history (checked on 18 Oct 2026: deals on 1 Jun 2018 and 2 Nov
  2020 were returned). This uses information momentum does not have.

## Where they can be tested, and what that is worth

No unseen Indian history is left: the sealed year (from Oct 2025) and Apr 2017 – Sep 2020 are both spent. So each
variant runs **once** on the development years, **1 Oct 2020 – 30 Sep 2025**, beside the adopted rules on the same run.
That is weak evidence, because the adopted rules were chosen on these years. A pass only earns 12 months of forward
paper trading beside the adopted rules, as for residual momentum (E). Both runs count as trials in the trial log, pass
or fail.

## The control

The adopted rules, `research_studies.FINALIST`, unchanged:
- top 20, monthly;
- a rank buffer of 35, and the tax-aware wait at 50;
- a 25% disaster floor, no stop;
- Indian charges and tax;
- point-in-time Midcap 150 members;
- against MID150BEES.

## Variant F: smooth momentum

Only the order of the eligible names changes. Everything else is the control's. On each decision day t:

1. Take the eligible names in the control's momentum order.
2. For each of the **top 40**, over the sessions t−252 to t−21 (the 12-1 window):
   - PRET = adjusted close at t−21 / adjusted close at t−252 − 1;
   - ID = sign(PRET) × (share of days with a negative daily return − share of days with a positive daily return), using
     only days that have both closes.
   - With fewer than 150 such days, a name gets no ID and sorts after every name that has one.
3. Reorder those 40 by ID, lowest (smoothest) first. Ties keep their momentum order.
4. Names ranked 41 and lower keep their momentum order after the 40.

The buffer (35) and the tax-aware wait (50) apply to this new order.

## Variant G: institutional buying

Only the order of the eligible names changes. Everything else is the control's.

**Deals.** NSE bulk and block deals, from NSE's history endpoint, 1 Oct 2019 – 30 Sep 2025.

**Institutional buyer.** A client whose name contains `FUND`, `INSURANCE` or `PENSION`, matched case-insensitively. The
list is fixed here and is not edited after the data is loaded.

**On each decision day t:**
1. Net institutional value for a stock = the sum over that stock's deals with these clients, dated in the 60 calendar
   days before t (strictly before t's date), of +quantity × price for a buy and −quantity × price for a sell.
2. Take the eligible names in the control's momentum order.
3. Within the **top 40**, names with a positive net institutional value move ahead of the rest of the 40. The order
   inside each group is the momentum order.
4. Names ranked 41 and lower keep their momentum order after the 40.

The buffer and the tax-aware wait apply to this new order.

**Data rule.** If any 90-day window of deals cannot be downloaded after the usual retries, G does **not** run, and
nothing is spent. Rows without a quantity or price are skipped and counted, and the count is printed.

## Pass rule (each variant against the control, same run)

1. at least **+2 points a year** (CAGR) over the control;
2. a worst fall no more than **5 points** deeper than the control's;
3. one-way turnover no more than **1.5 times** the control's.

These are the backward check's variant rules, unchanged. Skill odds, counting every trial, are reported as information.

**Adoption.**
- A pass earns a 12-month paper account beside the adopted rules, built only on the owner's approval.
- If F, G and E all pass their paper tests, at most one replaces the adopted rules: the one with the largest margin
  over them.
- A failure drops the idea. It is not retried with another window, threshold, list of names or top-N.

## Not allowed

- Changing any number, window, name list or rule above after the code first loads data.
- Running either variant a second time, or a combination of F and G, or F or G together with E.
- Quoting a development-years pass as proof. The paper account decides.
- Using the sealed year or 2017–2020, which are spent.
