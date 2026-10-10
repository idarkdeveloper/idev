"""Email layout: one holdings table, and the held-back candidates last on a no-buy day. Fixture data only."""
import re

from trading_agent import digest_render
from trading_agent.digest import unavailable


def items(n=13):
    out = []
    for i in range(n):
        out.append({"symbol": f"S{i:02d}", "name": f"Co{i:02d} Limited" if i != 3 else "Evil <b>x</b> Limited", "source": "Groww",
                    "price": 100.0 + i if i != 5 else None, "loss_pct": -30.0 + i, "stop": 90.0 + i, "ma200": 120.5,
                    "reasons": ["below the stop level 90.00 (buy price minus 3×ATR)", "below its 200-day average (120.50)"] if i % 2 else
                               ["down 29% from your buy price (well past any stop)", "negative news: x (y)", "fell 5.0% in the last session"]})
    return out


def ideas(n=2):
    rows = [{"symbol": "AAA", "name": "Alpha", "price": 10.0, "qty": 1, "notional": 10.0, "stop": 9.0, "ret_6m_pct": 5.0} for _ in range(n)]
    return {"ideas": rows, "universe": "N", "scored": 5, "universe_size": 9, "eligible": 7, "errors": 0, "sizing": "x", "equity": 1000.0,
            "equity_basis": "b", "wait": True}


def data(wait=True, n=13):
    return {"kind": "morning", "date": "2026-10-12", "generated_at": "2026-10-12T08:00:00+05:30",
            "mood": {"regime": "risk_off", "score": -3, "trend": "down", "no_new_buys": wait, "why": ["w"], "rules": "r", "nifty": {}},
            "buy_ideas": {**ideas(), "wait": wait},
            "watch": {"items": items(n), "total": n, "more": 0, "healthy": 2, "checked": n + 2, "places": ["Groww"], "notes": []},
            "deals": unavailable("x")}


def titles(text):
    return [ln for ln in text.splitlines() if ln.isupper() or ln.startswith(("CANDIDATE", "HOLDINGS", "MARKET"))]


def test_every_flagged_holding_has_a_row_and_the_formula_is_stated_once():
    out = digest_render.render(data())
    visible_html = re.sub(r' title="[^"]*"', "", out["html"])   # the hover text keeps the full reasons
    for body in (out["text"], visible_html):
        assert body.count("3×ATR") == 1 and body.count("buy price − 3×ATR") == 1
    text = out["text"]
    for i in range(13):
        assert f"(S{i:02d})" in text or f"S{i:02d}" in text
    assert text.count("Stock") >= 2   # the holdings table and the candidate table
    html = out["html"]
    assert all(f"S{i:02d}" in html or f"({'S%02d' % i})" in html for i in range(13))
    assert html.count('class="pl-loss"') == 12 and 'class="pl-profit"' not in html


def test_values_escaped_unpriced_and_short_flags():
    out = digest_render.render(data())
    assert "Evil &lt;b&gt;x&lt;/b&gt;" in out["html"] and "Evil <b>x" not in out["html"]
    assert "no price" in out["text"] and "no price" in out["html"]
    for flag in ("below stop", "below avg", "news", "big drop", "−30%"):
        assert flag in out["text"]


def test_no_buy_day_puts_holdings_after_mood_and_candidates_last_in_grey():
    t = digest_render.render(data(wait=True))["text"]
    assert t.index("MARKET MOOD") < t.index("HOLDINGS TO WATCH") < t.index("HELD BACK BY THE MARKET FILTER")
    assert t.index("CANDIDATE SCREEN") > t.index("HOLDINGS TO WATCH")
    html = digest_render.render(data(wait=True))["html"]
    i = html.index("HELD BACK by the market filter")
    assert "#d1d5db" in html[:i + 200] and "#15803d" not in html[i - 400:]
    assert "Would pass, but" not in t


def test_buy_day_keeps_todays_order():
    d = data(wait=False)
    d["mood"]["no_new_buys"] = False
    t = digest_render.render(d)["text"]
    assert t.index("BUY IDEAS") < t.index("HOLDINGS TO WATCH") and "CANDIDATE SCREEN" not in t


def test_premarket_line_leads_the_email_and_both_venues_show_and_flags_keep_the_full_reason():
    d = data()
    d["gauges"] = {"gauges": [], "warnings": [], "warning_texts": [], "skipped": 0, "note": "n",
                   "premarket_line": "Pre-market check: FAILED: NSE deals. New buys are paused."}
    d["deals"] = {"since": "2026-10-09", "total": 1, "following": ["X"], "deals": [
        {"investor": "X", "who": ["X"], "ticker": "INFY", "exchange": "NSE + BSE", "transaction": "BUY", "size": "5 sh", "reported": "2026-10-12"}]}
    out = digest_render.render(d)
    assert out["text"].index("Pre-market check: FAILED") < out["text"].index("MARKET MOOD")
    assert out["text"].count("Pre-market check") == 1 and "INFY (NSE + BSE)" in out["text"]
    assert 'title="below the stop level 90.00 (buy price minus 3×ATR); below its 200-day average (120.50)"' in out["html"]
    from tests.test_telegram_brief import evening
    from trading_agent.telegram_brief import telegram_brief
    b = telegram_brief({"summary": "", "data": d})["html"]
    assert "⏰ Pre-market check: FAILED: NSE deals. New buys are paused." in b and "(NSE + BSE)" in b
    e = evening()
    e["data"]["deals"]["deals"][0]["exchange"] = "NSE + BSE"
    assert "(NSE + BSE)" in telegram_brief(e)["html"]


def test_stops_breached_and_trend_caution_are_separate_and_worst_first():
    from trading_agent.digest_render import _watch_blocks
    items = [
        {"symbol": "VEDL", "price": 264.0, "stop": 354.12, "ma200": 456.19, "loss_pct": -28.7, "reasons": ["down 29%"]},
        {"symbol": "VOGL", "price": 30.1, "stop": 149.68, "loss_pct": -80.2, "reasons": ["down 80%"]},
        {"symbol": "JSWSTEEL", "price": 1168.0, "stop": 1147.16, "ma200": 1233.83, "loss_pct": -2.7,
         "reasons": ["below its 200-day average (1,233.83)"]},
    ]
    watch = {"items": items, "total": 3, "checked": 15, "healthy": 2, "places": ["Groww"],
             "notes": ["1 Groww holding(s) without a market price (bonds or unlisted) were not checked",
                       "Practice account: unavailable (no practice account yet)"],
             "portfolio": {"value": 168993.2, "pl": -21721.6, "pl_pct": -11.39, "holdings": 16}}
    blocks = _watch_blocks(watch)
    assert blocks[0]["title"].startswith("Holdings to watch: stops breached") and "(2)" in blocks[0]["title"]
    assert [r[0] for r in blocks[0]["table"]["rows"]] == ["VOGL", "VEDL"]          # worst loss first
    assert blocks[1]["title"].startswith("Holdings to watch: trend caution") and "(1)" in blocks[1]["title"]
    assert "(₹21 above stop)" in blocks[1]["table"]["rows"][0][5]
    assert ("Total P&L", "−₹21,722 (−11.4%)") in blocks[0]["kv"] and ("Invested", "₹1,90,715") in blocks[0]["kv"]
    meta = blocks[0]["lines"][1]
    assert "15 stocks checked; 2 with nothing to flag" in meta and "1 unpriced" in meta and "Practice: none" in meta
