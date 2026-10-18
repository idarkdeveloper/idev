# UK data note: stocks with pence/pound errors are left out (18 Oct 2026)

Written and committed **before** the UK replication has produced any result. The owner decided on 18 Oct 2026, after
the data gate stopped the first UK attempt and before any UK simulation ran (option 1).

## What happened

The UK addendum (`preregistered-replication-2026-10-18b.md`) was built to stop the run if any London price moved more
than 50× in one day, which is the sign of Yahoo mixing pence and pounds. The first attempt stopped on six stocks:
**BCG, CLDN, MEGP, PNL, ROSE and VSVS**. Nothing was claimed or logged, so the UK test is still unused.

## The rule from now on

- A stock whose close moves more than 50× in one day is **left out** and named in the report. This is the same rule the
  rerun addendum (`rerun-addendum-2026-10-18c.md`) applies in the US, Japan and India.
- If more than **10%** of stocks show such moves, the data as a whole cannot be trusted. The run then stops before its
  claim, and nothing is spent.
- The prices are **not** converted. Converting them would mean guessing, day by day, which unit Yahoo used.

The six stocks are 6 of 250 members, 2.4%. Everything else in the UK addendum stands unchanged: members, benchmark,
window, costs, floor, pass rule and readings. The UK runs once.

## Bias this adds

Stocks with bad data are dropped no matter how they performed. The result already uses today's members, so it is
flattered by survivorship, and the report says so. Dropping six more names cannot be steered towards a pass, because no
UK result exists yet.
