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
    for key in ("text", "html"):
        assert out[key].count("3×ATR") == 1 and out[key].count("buy price − 3×ATR") == 1
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
    assert t.index("MARKET MOOD") < t.index("HOLDINGS TO WATCH") < t.index("CANDIDATE SCREEN: 7 PASS, HELD BACK BY THE MARKET FILTER")
    assert t.index("CANDIDATE SCREEN") > t.index("HOLDINGS TO WATCH")
    html = digest_render.render(data(wait=True))["html"]
    i = html.index("Candidate screen: 7 pass, HELD BACK by the market filter")
    assert "#d1d5db" in html[:i + 200] and "#15803d" not in html[i - 400:]
    assert "Would pass, but" not in t


def test_buy_day_keeps_todays_order():
    d = data(wait=False)
    d["mood"]["no_new_buys"] = False
    t = digest_render.render(d)["text"]
    assert t.index("BUY IDEAS") < t.index("HOLDINGS TO WATCH") and "CANDIDATE SCREEN" not in t
