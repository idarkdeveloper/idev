# The drawdown brake's paper account (19 Oct 2026)

Addendum 1 of the India test fixed in advance that a passing brake earns "its own 12-month paper account beside the
adopted rules, under a separate, dated note". This is that note. It is committed before the account's first run.

## Why

The drawdown brake (100-day average) has been judged twice, with opposite results:

| Window | Brake | Adopted rules | Worst fall, brake vs adopted |
|---|---|---|---|
| Research years, Oct 2020 – Sep 2025 (Midcap 150) | +204.8% | +275.0% (plain rules) | −22.5% vs −27.6% |
| India test, Oct 2007 – Mar 2016 (top 500), run once | +295.0% | +119.6% | −31.5% vs −73.9% |

It paid in a long bear market (2008) and cost in a market whose dips recovered within weeks (2020–2025). It is
insurance, not an edge.

## The account

- **Rules.** The forward test's screen, ranking, filters and research rules: top 20, buffer 35, tax-aware wait with
  floor 50, 25% floor, no stop. Plus the brake, as `research_sim` runs it:
  - after a close below MID150BEES's own 100-day average, it sells everything and holds cash;
  - while the brake is on, a monthly rebalance buys nothing;
  - after the fund closes back at or above its average, it buys again at the next monthly rebalance.
- **When it runs.** From `BRAKE_PAPER_START`, default 20 Oct 2026, after the close, from the watch service.
- **Where.** Its own files, `state/forward/brake_<universe>.json`, and a fifth line on the Paper accounts tab.
- **Money.** Practice money only. It never changes the forward test, the live rules or any order.

## How it is read

- **Comparison.** It is compared with the forward test (the adopted rules) over the same days.
- **A calm market proves nothing either way.** With no long fall, the brake will lag a little (sales and re-buys
  cost charges) or match the adopted rules. That is expected and is not a reason to drop it.
- **A verdict needs a real bear market.** That means one where MID150BEES falls **20% or more** from a peak while the
  account runs. The brake is judged at the first time all of these hold:
  - at least 12 months have passed since it started;
  - such a fall has happened;
  - the fund has climbed back above its 100-day average.

  The question then: did the account have a smaller worst fall than the forward test, without trailing it by more
  than its cost in calm months?
- **Adoption** of the brake into the live rules needs that verdict **and** the owner's decision. Nothing about it is
  automatic.
- **No tuning.** The 100-day average is not retuned on what the account shows. Another length would be a new idea,
  under its own pre-registration.
