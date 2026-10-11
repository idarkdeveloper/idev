# Evening email: implement the "2b Proposed" design

Design: docs/design/evening-email.dc.html — a Claude Design canvas with two columns: "2a Current"
(today's email) and "2b Proposed" (lines 55–130). Implement 2b in the evening email HTML. Treat `sc-raw-table`, `sc-raw-tr`
etc. as plain `table`, `tr`… (the design tool's wrappers). Values in the design are sample numbers only.

Work on a new branch from current master and open a draft PR; no subagents; fakes only, NO
network, never call Groww/Telegram/Anthropic/Resend, never read .env. Trailer: Co-Authored-By with your model.
Tests: single files with plain pytest; full suite `-n 8` exactly ONCE at the end in the background (limit >= 10 min);
ruff and mypy; Stop background commands before reporting.

## Follow the morning email's pattern
PR #12 added trading_agent/digest_morning.py (new morning layout, used by digest_render.render for the morning HTML).
Do the same for the evening: a new trading_agent/digest_evening.py producing the evening HTML from the evening data,
and route digest_render.render to it for kind == "evening". Keep the plain-text part, the subject, the Telegram brief and
the digest data unchanged. Reuse the morning module's helpers/palette where they fit (share, don't copy).

## Layout (top to bottom, as in 2b)
1. Eyebrow "Trading Agent · Close report" + date/time right; one-sentence headline from data (e.g. "Down ₹2,140 today
   while the Nifty rose 0.62%"; stale day: "No trading today — figures from the Fri 9 Oct close").
2. Three figure tiles: Today (₹ and %, coloured), Total P&L (₹, % · value), Nifty 50 (close, points and %).
3. "In short" box (light accent background) — only when a model wrote the summary (writer starts with "claude:");
   the label says who wrote it.
4. "Your Groww portfolio today": best to worst by today's %, each row: name/code + price · shares; Today % with a small
   bar scaled to the biggest move; Total ₹ and %. Stale days: sort by total %, no bars, say so. Then the practice account
   in one grey strip (equity · cash · today · since start · stop-loss sells).
5. "News for your stocks": negative first, NEGATIVE (filled) / POSITIVE (outlined) badges, stock in bold, source · time.
   Then "Deals today:" one line (followed investors, counted when more than 3), and the BSE-after-16:00 note.
6. "Market bulletin": headline line with close and %, open gap + candle pattern; the 15-minute chart (existing cid
   image); a 2×2 grid: Watch levels, Pivots, ADX (14), RSI (14) (existing bulletin data); the 4-hour chart; one table
   "Global and commodities" (Last, 1d coloured, 5d muted); "A reading of past prices by rules, never a forecast."
7. Concept of the day in the accent box. 8. Footer line.
- Colours from the design: text #1a1c28, muted #6a6e82, secondary #474b5e, lines #e4e6ee/#f1f2f7, accent #5546b8,
  accent box #f5f4ff, grey strip #f5f5f8, profit #0a7a3c, loss #c1272d. Arial/Helvetica. Email-safe only: tables,
  inline styles, no <style>, no JS, max-width 640, works at 360 px (Gmail app) — check by rendering at 360 and 640 px.
- Charts keep their cid images (and the data-URL inlining used for previews).
- Missing data: a section is left out (never an empty box); "n/a" only inside tables.

## Tests
Section order, headline wording (up/down/flat/stale), tiles colours, best-to-worst sort and bar widths, news order and
badges, deals line, In-short only for a model summary, no <style>/<script>, cid images present, phone width.
Commit a sample with fake data to docs/design/evening-email-sample.html.

## Report
In the PR description.
