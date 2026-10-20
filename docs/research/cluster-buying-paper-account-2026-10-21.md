# Cluster buying on paper (21 Oct 2026)

Written and committed before the account's first run, at the owner's request ("Try cluster buying", rule chosen:
"3+ insiders"). It is judged only on paper, going forward. No backtest: the years with insider history have already
been used for the promoter-buying signals, which did not help (research-pead, 16 Oct).

## Why

Single insider buys did not help the momentum rules here:
- promoter buying combined with momentum pointed the wrong way (60-day IC −0.21);
- institutional buying (variant G) added nothing.

Published research finds the stronger signal is **cluster buying**: several different insiders buying in the open
market around the same time. That has not been tested here.

## How often it happens (counts only, no returns looked at)

From NSE's insider (PIT) feed, Apr 2025 – Mar 2026: new clusters at each month start, under the rule below.

| Rule | Median per month | Range | In the Nifty 500 |
|---|---|---|---|
| 3+ insiders | 4 | 0–14 | 0–1 |
| 4+ insiders | 1 | — | — |
| 2+ insiders | 17 | — | — |

The threshold was chosen from these event counts and the research, not from returns. Clusters are almost all small
companies, so this is a standalone account, not an overlay on the momentum lists.

## The rule

A stock has a **cluster** on a day when, among the insider trades **disclosed** in the 30 days up to that day (by
the filing's report date: `reported` in `state/insider_book.json`), all of these hold:
- each trade is an open-market purchase of equity shares: side buy, mode "Market Purchase" (`insider_watch.is_market`)
  and an equity instrument (`insider_watch.is_equity`);
- each buyer is a promoter or promoter-group member, a director, a key managerial person or an immediate relative.
  That is `insider_watch.is_insider`, or a category containing "relative". Designated persons, employees, trusts,
  "other" and connected persons do not count;
- at least **3 different buyers**, by name after case and space normalisation;
- buys on at least **2 different trade dates**, so a one-day family block such as eight relatives at one price does
  not count.

There is no value floor.

## The account

- **Universe:** every NSE stock with a cluster.
- **Filters:** the forward test's screen filters, applied to each candidate with the 200-day trend rule off:
  - 60-day average traded value of ₹1 crore or more;
  - not a 2% or 5% price-band stock;
  - not on the ASM or GSM surveillance lists;
  - no results within 5 trading days;
  - promoter pledges not high or rising.
- **Buying:**
  - after the close, at the first run of each month, it buys new clusters that pass the filters and are not held;
  - largest cluster value first, into free slots of **5% of the account's value** each (up to **20 names**).
  - Unused slots stay in cash. The report shows the invested share.
- **Selling:**
  - a holding is sold at the first monthly run **6 months (182 days) or more after it was bought**;
  - it is also sold any day it closes **25% or more below its buy price**, the same floor as the forward test;
  - there is no stop and no other exit.
- **Benchmark:** HDFCSML250, the Nifty Smallcap 250 index fund already used in the code, bought with the same
  capital on the start day.
- **When:** from `CLUSTER_PAPER_START`, default 22 Oct 2026; `off` disables it. Its own files are
  `state/forward/cluster_<universe>.json`, and it is the sixth line on the Paper accounts tab.
- **Money:** practice money only. It never changes the forward test, the live rules or any order.

## How it is read

- **After 12 months**, the verdict compares the account with HDFCSML250 after charges, with the forward test shown for
  comparison.
  - **It passes** only if it beats the fund. Even then, adoption needs the owner's decision.
  - **It fails:** the idea is dropped. Thin months are expected: with about 4 new clusters a month before the filters,
    the account may hold only a handful of names.
- **No retuning.** The 3 buyers, the 30 days, the 2 dates, the 6 months and the 5% slots are not changed on what
  the account shows. A different setting is a new idea with its own note.
