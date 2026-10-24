# Result: batch 4, quarterly rebalance (H) and volatility scaling (V) (run once, 24 Oct 2026)

Plan: `preregistered-batch-2026-10-22.md` (merged in #90), with the survivorship repairs of `survivorship-2026-10-22.md` and
its two dated clarifications of 24 Oct (the UDiFF file, written before any run; the throttled 403, written after the
first attempt stopped at the gate). The window was claimed and run once; it is spent. **Neither variant passed. The
adopted rules stay as they are and no paper account is opened.**

## Where and how it ran

- In the Claude Code sandbox on 24 Oct 2026, **not** on the Oracle server (no way in from here), the same way the India
  test ran. Code: commit `0dc80b2`, run from a detached worktree so the code could not change under the run. The claim
  marker, written at 12:08 UTC: `{"study": "Batch4", "claimed_at": "2026-10-24T12:08:03+00:00", "code": "0dc80b208719", "pid": 7573}`.
- **First attempt, earlier the same day: stopped at the data gate, nothing claimed.** It downloaded all 1,533 daily
  files and loaded 1,513 (98.7%; the gate is 99%). The 20 missing days were 403 answers that NSE gave to a burst of
  about 1,500 requests; the same files answered 200 minutes later. The old code had cached each 403 as "no file". The 20
  markers were deleted and the identical command ran again on `0dc80b2`; all 1,533 files then loaded. Nothing else was
  touched, and the first attempt saw no result. The download code now retries and no longer remembers a 403 (a later
  commit on this branch, not in the code that ran the study).
- Because the trial log and the once-marker of this run live in the sandbox, they must be handed to the server (below).

## Output, verbatim

The NSE download progress counters and the closing JSON dump are left out; the JSON is in the run log and the three
trial-log rows below carry its numbers.

```
Loading NIFTYMIDCAP150 prices for 5 years before 2025-10-01 (this takes a few minutes)…
Filling 13 member(s) with no Yahoo history from NSE's daily files, 2019-02-04 to 2025-09-30 (cached under /tmp/claude-0/-home-user-idev/5fdbbbb5-0840-5854-8884-a37272e7f65b/scratchpad/india/state/research/nse_bhav)…
  filled 7 of 13: DHANI, GSPL, ICICILIFE, ISEC, MINDTREE, PEL, SHRIRAMCIT; still missing GROWW, ICICIAMC, LENSKART, LGEINDIA, MEESHO, PIRAMALFIN
Filled 7 past member(s) from NSE's daily files: DHANI, GSPL, ICICILIFE, ISEC, MINDTREE, PEL, SHRIRAMCIT
No price history for 6 past member(s): GROWW, ICICIAMC, LENSKART, LGEINDIA, MEESHO, PIRAMALFIN
Checking for look-ahead (six decision days, two simulation cuts)…
Claimed Batch4 (/tmp/claude-0/-home-user-idev/5fdbbbb5-0840-5854-8884-a37272e7f65b/scratchpad/india/state/research/once/Batch4.claimed): this is the only run. If it stops before printing a result, the window is still spent.
BATCH 4, run once (docs/research/preregistered-batch-2026-10-22.md), 2020-10-01 to 2025-09-30, after charges and tax, against the adopted rules on the same run and the same repaired data. Development years: weak evidence; a pass only earns a paper account.
  variant                        return  vs fund  a year worst fall turnover  skill  verdict
  control the adopted rules     +334.5%  +103.8%  +34.2%     -32.4%     177%   0.88  control
  H quarterly rebalance         +343.9%  +113.1%  +34.8%     -33.6%     137%   0.89  fail
  V volatility scaling          +333.3%  +102.6%  +34.1%     -31.1%     181%   0.87  fail
  H misses: turnover at most two-thirds of the control's.
  V misses: worst fall at least 5 points shallower than the control's.
  V's share: applied below 1 in 15 of 60 rebalances, changed 6 time(s); mean 0.96, lowest 0.76.
  Probability of backtest overfitting: 0.96 (3 variants, 252 splits of 60 months): the chance the variant that looks best is at or below the median on months it was not picked on; 0.5 or more = picking the best here is no better than chance.
  Survivorship diagnostic (a description, not a trial): the adopted rules on Yahoo's names alone, no haircut: +361.8% (+131.1% vs the fund, worst fall -31.8%); on the repaired data: +334.5% (+103.8%, -32.4%). Filled: DHANI, GSPL, ICICILIFE, ISEC, MINDTREE, PEL, SHRIRAMCIT; still missing: GROWW, ICICIAMC, LENSKART, LGEINDIA, MEESHO, PIRAMALFIN.
  Survivorship (survivorship-2026-10-22.md): 7 member(s) with no Yahoo history filled from NSE's daily files (1533 of 1533 days loaded); still missing: GROWW, ICICIAMC, LENSKART, LGEINDIA, MEESHO, PIRAMALFIN.
  Verdict: passing none; the adopted rules stay as they are. Skill odds are information only. This batch is now spent.
```

## Trial log rows (sandbox `state/research/trials.jsonl`, lines 8 to 10)

```
{"at": "2026-10-24T12:08:04+00:00", "study": "Batch4", "params": {"variant": "control", "name": "the adopted rules", "from": "2020-10-01", "to": "2025-09-30", "rules": "floor25%, buffer35+tax-aware, delisting haircut 30%"}, "summary": {"return": 3.3453888992547958, "excess": 1.037755270286301, "cagr": 0.3418165102298134, "max_drawdown": -0.3241184993336178, "turnover": 1.7689489772193772, "passes": null, "delistings": 0, "skill": 0.8798060076735853}, "code": "0dc80b208719"}
{"at": "2026-10-24T12:08:04+00:00", "study": "Batch4", "params": {"variant": "H", "name": "quarterly rebalance", "from": "2020-10-01", "to": "2025-09-30", "rules": "floor25%, buffer35+tax-aware, quarterly, delisting haircut 30%"}, "summary": {"return": 3.438658223276061, "excess": 1.1310245943075663, "cagr": 0.34753174718596536, "max_drawdown": -0.33629367253718545, "turnover": 1.3732713071975826, "passes": false, "delistings": 0, "skill": 0.8926656838717328}, "code": "0dc80b208719"}
{"at": "2026-10-24T12:08:04+00:00", "study": "Batch4", "params": {"variant": "V", "name": "volatility scaling", "from": "2020-10-01", "to": "2025-09-30", "rules": "floor25%, buffer35+tax-aware, vol-scaled 126/756, delisting haircut 30%"}, "summary": {"return": 3.333205745756872, "excess": 1.0255721167883771, "cagr": 0.3410627398486261, "max_drawdown": -0.31058692049486947, "turnover": 1.8103072515183878, "passes": false, "delistings": 0, "skill": 0.8743506976864915}, "code": "0dc80b208719"}
```

## Reading

- **H, quarterly rebalance: failed one of four checks, and not by much.**
  - It beat the fund (+343.9% against +230.8%), stayed within 1 point a year of the control (+34.8% against +34.2%)
    and kept its worst fall within 5 points (−33.6% against −32.4%).
  - It missed the turnover bar: 137% a year against 177% for the control is 0.78 of it; the bar was two-thirds (118%).
  - Everything else it did was a little better: 139 trades against 184, charges ₹65.7k against ₹82.6k, tax paid ₹172.6k
    against ₹190.9k. It held through the tax wait 10 times against once. The bar was set before the run and is not
    moved: H earns nothing, not even a paper account. If quarterly checks are ever wanted for convenience, this is the
    measured price: about the same return and a small saving in costs.
- **V, volatility scaling: failed one of four checks.**
  - Return and turnover were fine (+333.3%, 181% against 177%), but the worst fall was only 1.3 points shallower
    (−31.1% against −32.4%); the bar was 5.
  - The scaling hardly acted: the invested share was below 1 at 15 of 60 rebalances, changed 6 times, averaged 0.96 and
    was never below 0.76. I did not test why. The 126-session volatility has to rise well above the 756-session volatility
    before it cuts, and in this window it rarely did, but that is a reading, not a measured cause.
- **Probability of overfitting: 0.96** across three variants that are nearly the same strategy. Picking the best of them
  is no better than chance, which is what the table shows: +333% to +344%, all within noise of each other.
- **The survivorship repairs cost the adopted rules 27 points.** On Yahoo's names alone the adopted rules made +361.8%
  (+131.1 points over the fund, worst fall −31.8%). With the 7 filled members and the 30% delisting haircut they made
  +334.5% (+103.8 points, −32.4%): 1.6 points a year less. That is the size of the flattery the repairs removed; this
  is now the honest development-years number for the adopted rules. Its edge over the fund (+103.8 points after charges
  and tax) and its worst fall (−32.4% against −21.2% for the fund) are both unchanged in kind.
- **Still missing:** GROWW, ICICIAMC, LENSKART, LGEINDIA, MEESHO, PIRAMALFIN have no history from Yahoo and the filler
  found none in the NSE files either, so whatever they would have done is not in any number here; the remaining bias is
  not measured.
- **Skill odds are information only: 0.87 to 0.89.** They are deflated for 10 trials, the length of this sandbox's own
  trial log (7 India rows plus these 3). The server's log is longer, so with its count they would be lower.
- **Nothing changes.** The adopted rules keep their monthly rebalance and full invested share. Development years are
  weak evidence and this batch is spent.

## Hand-over to the server

The server has no record of this run. To keep its once-only guard and its trial count honest, once, on the server:

1. Append the three trial-log rows above to `state/research/trials.jsonl`.
2. Create the claim marker `state/research/once/Batch4.claimed` containing the marker line shown at the top (only its
   existence is checked).
3. Do not run `research-batch4` there; its window is spent.
