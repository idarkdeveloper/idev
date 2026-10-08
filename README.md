# idev25 — Claude trading agent

An AI trading agent that **watches one investor's publicly disclosed trades, compares new
activity against your paper-trading portfolio, and sends you a recommendation when something
changes** — the setup shown in the "Claude can now build an AI trading agent" reel, as code
you own instead of a chat window.

```
QuiverQuant ──(congress / insider trades)──▶ diff vs. remembered trades
                                                   │ new trade found
                                                   ▼
                             Claude (tool use) ── get_portfolio / get_latest_price
                                                   │  send_recommendation ──▶ console / email / webhook
                                                   │  place_paper_order (opt-in) ──▶ Alpaca paper or local sim
                                                   ▼
                                          state/state.json (seen trades, run log)
```

| Reel step | Here |
|---|---|
| Connect a brokerage ("Liquid") | `trading_agent/broker.py` — Alpaca **paper** API, or a built-in local paper simulator |
| Connect QuiverQuant | `trading_agent/quiver.py` — congress (politician) and insider trades |
| Give Claude a strategy | `trading_agent/agent.py` — system prompt + tools (`send_recommendation`, `place_paper_order`, …) |
| Create a routine | `.github/workflows/routine.yml` (cron) or `python -m trading_agent loop` |
| Paper-trade first | Everything is paper money. The Alpaca client refuses non-paper endpoints. |

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env        # fill in ANTHROPIC_API_KEY (+ QUIVER_API_KEY for live data)

# 1. See the pipeline run on bundled sample data without touching any API
python -m trading_agent check --demo --dry-run

# 2. Let Claude analyse the sample trades (needs ANTHROPIC_API_KEY)
python -m trading_agent check --demo

# 3. Real data: watch an investor's disclosures (needs QUIVER_API_KEY)
python -m trading_agent check --investor "Nancy Pelosi"

# 4. Keep checking every 30 minutes
python -m trading_agent loop --every 30
```

Other commands: `portfolio` (paper account + P&L), `history` (past recommendations),
`reset` (forget seen trades, reset the local paper account), `check --json`.

## Configuration (`.env`)

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Claude. Model defaults to `claude-opus-5-5` (`CLAUDE_MODEL`). |
| `QUIVER_API_KEY` | QuiverQuant market data (congress + insider trades). |
| `WATCH_INVESTOR`, `WATCH_SOURCE` | Who to follow (name substring) and where: `congress` or `insider`. |
| `ALPACA_API_KEY_ID`, `ALPACA_API_SECRET_KEY` | Optional. Alpaca **paper** account. Unset = local simulator (`state/paper_broker.json`, starts with `PAPER_STARTING_CASH`). |
| `AUTO_TRADE` | `false` (default) = recommendations only. `true` = Claude may place paper orders, capped at 10% of equity each. |
| `RESEND_API_KEY` + `NOTIFY_EMAIL_TO` | Email each recommendation via Resend. |
| `NOTIFY_WEBHOOK_URL` | POST `{"text": ...}` to Slack/Discord/n8n/etc. |

## The routine (runs by itself)

`.github/workflows/routine.yml` runs the check every 30 minutes during US market hours and
keeps `state/` between runs with the Actions cache. To enable it, add these repository
**secrets**: `ANTHROPIC_API_KEY`, `QUIVER_API_KEY` (optional: `ALPACA_API_KEY_ID`,
`ALPACA_API_SECRET_KEY`, `RESEND_API_KEY`, `NOTIFY_WEBHOOK_URL`) and **variables**:
`WATCH_INVESTOR`, `NOTIFY_EMAIL_TO`, `AUTO_TRADE`. Trigger it manually from the Actions tab.

## How a check works

1. Fetch the investor's disclosed trades and drop the ones already in `state/state.json`.
2. Nothing new → exit without calling Claude (cheap, safe to run often).
3. Something new → Claude gets the new trades and tools to inspect the portfolio and prices.
   It calls `send_recommendation` (buy / sell / hold / watch with rationale and confidence).
   With `AUTO_TRADE=true` it may also call `place_paper_order`.
4. Trades are remembered only after a successful analysis, so API hiccups are retried.

Safety rails: paper money only, no forced tool use, per-order size cap, concentration rule in
the prompt, and server-side refusal fallback enabled on the Claude request.

## Development

```bash
python -m pytest -q
```

Tests run fully offline with a scripted fake Claude runner and fake HTTP sessions.

**Not financial advice.** Disclosures lag the real trades by days to weeks; treat this as a
research assistant, not a signal.
