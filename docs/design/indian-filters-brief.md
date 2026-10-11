# Indian-market filters (build now; buys only)

## Indian-market filters (added 10 Oct 2026, for later; each a registered change where it affects the strategy)
- ASM/GSM surveillance: daily NSE surveillance lists (short- and long-term ASM stages, GSM). Hard exclusion from buys
  (agent, screen, digest ideas, practice, forward test from a recorded date); holdings that enter a list are flagged in
  the morning email for review. Sells never blocked. Backtests: only if a point-in-time history of the lists exists.
- Results blackout: NSE board-meeting calendar (financial results). No new buys when results are due within the next
  5 trading days; holdings show an amber "Results due <date>" tag on the dashboard and in the morning email (the email
  already lists results-due holdings — extend to the screen/agent rule). Tested as a variant in research first.
- Promoter pledge screen: SAST Reg 31 / shareholding-pattern encumbrance data. Exclude buys where pledged promoter
  shares > 20% of promoter holding, or pledges rose > 5 percentage points in the latest filing; point-in-time by
  filing date (15:00 rule).
- Tax-aware B2 buffer: if a holding is in profit, aged 335–364 days, and above its hard floor, defer a rank-buffer
  exit until day 366 unless its rank falls below 50 (STCG 20% vs LTCG 12.5% above ₹1.25 lakh/year). Variant of B2,
  registered; research reports after-tax returns for both.

## What to build in this job
- ASM/GSM, results blackout and promoter pledges as real filters: fetch through the existing NSE client (its breaker
  and back-off), cache daily, and apply them wherever a BUY is proposed or placed: the agent's rules, the factor
  screen and screener (a flag column + an "exclude" preset), the morning email's buy ideas, the practice order form
  (a warning that the user can override for practice), and the forward test (from a recorded start date).
- Holdings: flag (never sell) holdings that enter ASM/GSM, show "Results due <date>" and a pledge warning on the
  portfolio rows, stock page and morning email.
- The tax-aware buffer depends on the B2 rank buffer, which is not built yet: do NOT build it now; leave a note.
- Nothing here ever places, modifies or cancels an order or blocks a sell. Fail open with a visible "filter data
  unavailable" note (buys are then held back only by the existing rules), never crash.
- Settings to switch each filter on/off (default on), documented in .env.example and the README.
- Tests with fake NSE responses: each rule, point-in-time by filing time (the existing 15:00 rule), stale/missing
  data, the flags on holdings, no sell ever blocked, no order path touched.
- Work on a new branch from current master and open a draft PR; full suite (pytest -n 8), ruff and mypy green.