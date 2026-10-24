# Survivorship: filling missing names and pricing delistings (22 Oct 2026)

Written and committed before the code exists. It fixes two data repairs that every later Indian study uses, starting
with batch 4 (`preregistered-batch-2026-10-22.md`). No earlier result is rerun or revised: one-time studies stay spent.

## The problem

Almost every result so far carries a note that it is flattered:

- 12 of 290 Midcap 150 members in the development years, and 23 of 321 in the 2017–2020 check, have no price history
  on Yahoo. Most are stocks that were delisted, merged or renamed; the ones that did badly are over-represented among
  them, so leaving them out flatters every rule set.
- In the India test, 10 to 20 positions per rule set were sold "at the last close" after their stock stopped trading,
  with no haircut. In reality a suspended or delisted share is often sold for much less, or not at all.
- The US, Japan and UK replications used today's members for every date (no free change history exists). This note
  does not fix those; it says so.

## Repair 1: fill missing members from NSE's own daily files

For each member of the point-in-time index history that has **no Yahoo bars at all**:

1. NSE's daily EQ-series file (the bhavcopy archive, `nsearchives.nseindia.com`) is loaded for every trading day of the
   study's calendar, from the ranking's lookback year to the window's end, and cached under
   `state/research/nse_bhav/`. This is the India test's pipeline (`research_india.fetch_bhav`, `parse_bhav`,
   `build_series`), unchanged.
2. Rows are renamed to today's symbols with NSE's symbol-change file (`index_history.load_symbol_changes`,
   `current_symbol`), so a stock renamed during the window is one series.
3. Splits and bonuses adjust close and volume; dividends adjust the adjusted close; both from NSE's corporate
   actions for the same period (`research_india.parse_actions`), the India test's rules unchanged (a dividend over 30%
   of the price is not applied and is counted).
4. The series is inserted under the member's symbol. A member still not found is reported by name and stays missing.

Only members with no Yahoo history are filled. A member whose Yahoo series ends early is not patched from a second
source (two adjustment bases in one series would be a guess); it is handled by repair 2 and listed.

A stock that moves out of the EQ series (to BE, BZ, or a suspension) disappears from the EQ file and so, under repair
2, counts as having stopped trading. The India test treated it the same way.

**Gate.** A study stops before its claim, with nothing spent, if fewer than 99% of the trading days' files could be
loaded or the corporate actions could not be fetched.

## Repair 2: a delisting haircut

In the simulator (`research_sim.simulate`, rule `delist_haircut`):

- a stock is taken to have **stopped trading** on the session after its last bar, when that last bar is more than 5
  sessions before the end of the loaded data (so a live stock whose history was fetched a day late is not cut);
- on that session a holding in it loses **30%** of its value and is **sold** at the rest, with the reason
  "delisted"; the floor and the stop do not see it, so their counts stay honest;
- the slot stays in cash until the next rebalance, as after a floor sale;
- the sale realises its loss for tax like any other sale.

30% is Shumway's delisting return for performance-related delistings on US exchanges (Journal of Finance, 1997). No
Indian figure of the same kind is published, so the US number is used as it stands and is **not varied**. It also hits
stocks that stopped trading because they were merged, where the holder usually received fair value; so the haircut
leans against every rule set, which is the safe direction. The report names each stock cut, with the date.

The haircut is a rule, off by default so that earlier tests and spent studies stay bit for bit as they were; every new
study turns it on and says so in its plan. The adopted rules themselves (`FINALIST`) are unchanged: the haircut is a
statement about the data, not about what the strategy does.

## What it changes

- Batch 4 and every later Indian study run on the filled universe with the haircut, and their plans say so.
- The control in batch 4 is simulated both ways and the two numbers are printed side by side, as a description of
  how much the missing names flattered the development years. That is a diagnostic, not a trial.
- The forward test and the paper accounts are not affected: they trade real, current names and need no repair.

## Clarification, 22 Oct 2026 (before any run)

The "stopped trading" label in repair 2 is read off the **whole loaded series**: on the session after a stock's last
bar the simulator already knows that no later bar exists in the data. On that session itself nobody could have known
whether the stock was delisted or merely halted for a day. So the timing of the haircut is stylised, not something known
on the day. It is also conservative: it cuts and sells on the earliest possible session, which leans against every rule
set; and it never changes a buy or a ranking made before that session. The leakage test, which checks that decisions use
only data up to their day, runs the adopted rules without the haircut and is unaffected. No number changes.

## Clarification, 24 Oct 2026 (before any run): NSE's daily file changed format in July 2024

Probing NSE before the first run showed that the legacy daily file (`cmDDMONYYYYbhav.csv.zip`) exists up to 5 Jul 2024
and answers 404 from 8 Jul 2024. NSE publishes the same day's figures as its "UDiFF" file
(`content/cm/BhavCopy_NSE_CM_0_0_0_YYYYMMDD_F_0000.csv.zip`), available from 5 Jul 2024. Left as built, repair 1 would
have failed its own gate (about 20% of the days in the window are after the change) and the study could never have run.

From 8 Jul 2024 the fill therefore reads the UDiFF file. It carries the same fields for the EQ series: the ticker, the
close, the total traded volume, the traded value and the ISIN, and the rows are turned into exactly the shape the legacy
file gives. Nothing else changes: the gate is still 99% of the days' files, the symbol changes, the corporate actions,
the adjustments and the haircut are as written above. Days before 8 Jul 2024 are read as before, and a cached legacy
file is never mixed with a UDiFF one. No rule, number or pass condition of the batch 4 plan changes.

## Clarification, 24 Oct 2026 (after the first attempt stopped at the gate, nothing claimed): a throttled 403 was cached as "no file"

The first attempt downloaded all 1,533 daily files and stopped at the data gate: 1,513 loaded (98.7%), against the
gate's 99%. Nothing was claimed, no rule set was simulated, no result was seen. The 20 missing days were all cached
403 answers, spread over Aug 2019 – Nov 2021. Fetched again minutes later, every one I tried answered 200 with a
file: NSE had throttled the burst of about 1,500 requests, and the download code had remembered each 403 as "NSE has
no file for this day".

Two things followed, and neither touches a rule, a number or a pass condition:

- The 20 cached 403 markers were deleted and the identical command was run again on the same pinned commit (`0dc80b2`).
  The gate is still 99% of the days' files.
- The download code now retries a 403, an error page or a 5xx three times with growing pauses, and never remembers
  one as "no file": only a 404 is. This change is on the branch and was **not** in the code that ran the study; it
  only makes a future download less likely to stop at the gate. The result document says which commit ran.
