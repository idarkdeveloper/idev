"""The morning email's HTML (digest_morning.py): headline, three figures, summary, holdings to watch, buy ideas, deals and
market context, on a normal day and on a no-buy day. Fixture data only."""
import re

from trading_agent import digest_morning, digest_render
from trading_agent.digest import unavailable


def sample(wait=False):
    mood = {"regime": "risk_off" if wait else "risk_on", "score": -3 if wait else 3, "trend": "down" if wait else "up",
            "nifty": {"last": 25184.2, "ret_1d_pct": 0.62, "ret_20d_pct": 2.1, "above_200dma": not wait},
            "no_new_buys": wait, "why": ["the market regime is risk-off (score -3)", "Nifty is below its 200-day average"] if wait else [],
            "rules": "no new buys when the regime is risk-off, Nifty is below its 200-day average, or Nifty is in a downtrend"}
    row = lambda i, c, d1, d5, d20, t: {"index": i, "close": c, "d1_pct": d1, "d5_pct": d5, "d20_pct": d20, "trend": t}
    world = {"us": [row("S&P 500", 6742.1, 0.31, 1.2, 3.4, "UP")], "asia": [row("Nikkei", 47320.0, 1.04, 2.1, 5.8, "UP")],
             "futures": [row("Nasdaq fut", 24910.0, -0.18, 0.9, 4.1, "UP")], "vix": None, "india": [], "skipped": 0,
             "region_lines": ["US closed higher; Asia mixed this morning."], "note": "Current trends from daily closes, not a forecast."}
    gauges = {"gauges": [{"gauge": "India VIX", "value": 12.4, "d20_pct": -9.2, "vs_50d_pct": -8.1, "range": "10.2–21.4", "reading": "calm", "warning": False},
                         {"gauge": "USD/INR", "value": 84.12, "d20_pct": 0.3, "vs_50d_pct": 0.2, "range": "near 1-year high", "reading": "rupee not near its weakest of the year", "warning": False}],
              "warnings": [], "skipped": 0, "note": "Readings, not forecasts.",
              "flows_line": "FII net −₹1,240 cr, DII net +₹2,180 cr (provisional); 5-day FII net −₹4,860 cr.",
              "breadth_line": "NIFTY 500 breadth: 61% rose, 58% above their 50-day average."}
    idea = lambda s, n, p, q, st, r: {"symbol": s, "name": n, "price": p, "qty": q, "notional": p * q, "stop": st, "ret_6m_pct": r}
    ideas = {"ideas": [idea("BSE", "BSE Limited", 2684.0, 18, 2412.4, 61.0), idea("DIXON", "Dixon Technologies (India) Limited", 17420.0, 2, 15690.0, 44.0),
                       idea("PERSISTENT", "Persistent Systems Limited", 6210.0, 8, 5640.0, 38.0)],
             "universe": "NIFTYMIDCAP150", "scored": 148, "universe_size": 150, "eligible": 41, "errors": 0,
             "sizing": "risk 1% on a 2× ATR move, max 10% each", "equity": 500000.0, "equity_basis": "practice equity", "wait": wait}
    watch = {"items": [{"symbol": "TATAMOTORS", "name": "Tata Motors Limited", "source": "Groww", "price": 942.1, "loss_pct": -6.9, "stop": 948.3, "ma200": 1018.4,
                        "reasons": ["below the stop level 948.30 (buy price minus 3×ATR)", "below its 200-day average (1,018.40)", "results due 14 Oct"]},
                       {"symbol": "NTPC", "name": "NTPC Limited", "source": "Groww", "price": 402.6, "loss_pct": 14.4, "stop": 388.4, "ma200": 408.9,
                        "reasons": ["below its 200-day average (408.90)"]}],
             "total": 2, "more": 0, "healthy": 6, "checked": 8, "places": ["Groww"], "notes": [],
             "portfolio": {"value": 482310.0, "pl": 61310.0, "pl_pct": 14.56}}
    deals = {"since": "2026-10-09", "total": 2, "following": ["ASHISH KACHOLIA"], "deals": [
        {"investor": "ASHISH KACHOLIA", "who": ["ASHISH KACHOLIA"], "ticker": "BEL", "exchange": "NSE", "transaction": "BUY", "size": "19,20,000", "reported": "2026-10-10"},
        {"investor": "ASHISH KACHOLIA", "who": ["ASHISH KACHOLIA"], "ticker": "SENCO", "exchange": "BSE", "transaction": "BUY", "size": "2,40,000", "reported": "2026-10-10"}]}
    return {"kind": "morning", "date": "2026-10-12", "generated_at": "2026-10-12T09:00:00+05:30", "delayed": False,
            "mood": mood, "world": world, "gauges": gauges, "buy_ideas": ideas, "watch": watch, "deals": deals}


def html(d, summary=None, writer="none"):
    return digest_render.render(d, summary, writer)["html"]


def test_headline_says_what_to_do_today():
    assert digest_morning.headline(sample()) == "Risk-on. 3 buy ideas, 2 holdings to watch."
    assert digest_morning.headline(sample(wait=True)) == "No new buys today. 2 holdings to watch."
    d = sample()
    d["watch"] = {**d["watch"], "items": [], "total": 0}
    assert digest_morning.headline(d) == "Risk-on. 3 buy ideas, nothing to watch."
    d = sample()
    d["mood"], d["watch"] = unavailable("x"), unavailable("y")
    assert digest_morning.headline(d) == "Market data unavailable. 3 buy ideas, holdings not checked."
    d = sample()
    d["buy_ideas"]["ideas"] = d["buy_ideas"]["ideas"][:1]
    d["watch"] = {**d["watch"], "total": 1, "items": d["watch"]["items"][:1]}
    assert digest_morning.headline(d) == "Risk-on. 1 buy idea, 1 holding to watch."


def test_the_three_figures_market_portfolio_and_needs_a_look():
    h = html(sample())
    top = h[:h.index("Holdings to watch")]
    assert "Mon 12 Oct 2026 · 09:00 IST" in top and "Trading Agent · Morning brief" in top
    assert ">Risk-on<" in top and "Nifty 25,184 · +0.62%" in top
    assert "₹4,82,310" in top and "+₹61,310 (+14.56%)" in top
    assert "2 of 8 holdings" in top and "1 below its stop" in top
    w = html(sample(wait=True))
    assert ">Risk-off<" in w and "Nifty below 200-day avg" in w and 'color:#c1272d;padding-top:3px">Risk-off' in w


def test_summary_box_names_its_writer():
    h = html(sample(), "Three stocks pass.", "claude:claude-haiku-4-5")
    assert "In short" in h and "· written by Claude Haiku, check the numbers below" in h and "Three stocks pass." in h
    assert "In short" not in html(sample())


def test_holdings_rows_badge_breached_first_and_keep_the_reasons_on_hover():
    h = html(sample())
    assert h.index("BELOW STOP") < h.index(">TATAMOTORS<") < h.index("TREND CAUTION") < h.index(">NTPC<")
    assert "Review for exit · below 200-day avg (₹1,018.40) · results due" in h
    assert "Below 200-day avg (₹408.90) · ₹14 above its stop" in h
    assert "−6.9% · stop ₹948.30" in h and "+14.4% · stop ₹388.40" in h
    assert 'title="below the stop level 948.30 (buy price minus 3×ATR); below its 200-day average (1,018.40); results due 14 Oct"' in h
    assert "8 stocks checked; 6 with nothing to flag · stop = buy price − 3×ATR" in h


def test_buy_ideas_and_the_held_back_list_on_a_no_buy_day():
    h = html(sample())
    ideas = h[h.index(">Buy ideas<"):h.index("New deals")]
    assert "Momentum screen of NIFTYMIDCAP150: 41 of 150 pass" in ideas and "18 shares" in ideas and "₹48,312" in ideas
    assert "Dixon Technologies (India) · ₹17,420.00" in ideas and "#0a7a3c" in ideas and "+61%" in ideas
    w = html(sample(wait=True))
    held = w[w.index("Would pass, held back by the market filter"):w.index("New deals")]
    assert ">Buy ideas<" not in w and "#0a7a3c" not in held and "No new buys today: the market regime is risk-off" in held
    assert w.index("Holdings to watch") < w.index("Would pass")


def test_deals_list_and_the_market_context():
    d = sample()
    d["deals"]["deals"][1]["transaction"] = "SELL"
    h = html(d)
    assert "2 since 9 Oct" in h and "· 19,20,000 shares · ASHISH KACHOLIA" in h and "10 Oct · NSE" in h and "10 Oct · BSE" in h
    assert re.search(r"color:#c1272d;border:1px solid #c1272d;[^>]*>SELL<", h)
    ctx = h[h.index("Market context"):]
    for needle in ("Nifty 50", "25,184.20", "S&amp;P 500", "Nikkei", "Nasdaq fut", "−0.18%", "India VIX", ">calm<", "USD/INR",
                   "−9.2% in 20d", "FII net −₹1,240 cr", "NIFTY 500 breadth", "US closed higher"):
        assert needle in ctx, needle
    assert "rupee not near its weakest of the year" in ctx   # a long reading sits under the gauge's name
    none = sample()
    none["deals"] = {"since": "2026-10-09", "total": 0, "following": ["A", "B"], "deals": []}
    assert "No disclosures by A, B since 9 Oct." in html(none)


def test_warnings_premarket_and_saved_holdings_are_stated_in_words():
    d = sample()
    d["gauges"]["warnings"] = ["India VIX"]
    d["gauges"]["gauges"][0].update(warning=True, reading="elevated")
    d["gauges"]["premarket_line"] = "Pre-market check: FAILED: NSE deals. New buys are paused."
    d["watch"]["notes"] = ["Using saved holdings from 09 Oct 15:30"]
    h = html(d)
    assert h.index("Pre-market check: FAILED") < h.index("Your portfolio")
    assert "Using saved holdings from 09 Oct 15:30." in h
    assert "⚠ Warning: India VIX is above 20. A reason to size smaller." in h and "⚠ warning: elevated" in h


def test_everything_is_escaped_and_the_email_is_inline_styled():
    d = sample()
    d["buy_ideas"]["ideas"][0]["name"] = "Evil <script>x</script> Ltd"
    d["deals"]["deals"][0]["who"] = ["<b>who</b>"]
    d["watch"]["items"][0]["name"] = "Bad & <i>co</i>"
    h = html(d, "<img src=x onerror=1>", "rules")
    assert "<script" not in h and "<b>who" not in h and "<i>co" not in h and "<img" not in h
    assert "&lt;script&gt;" in h and "&lt;b&gt;who&lt;/b&gt;" in h and "Bad &amp; &lt;i&gt;co&lt;/i&gt;" in h
    assert "<style" not in h and "http" not in h and "max-width:640px;width:100%" in h
    assert set(re.findall(r'class="([^"]+)"', h)) <= {"pl-profit", "pl-loss"}


def test_the_text_part_and_subject_stay_the_shared_ones():
    m = digest_render.render(sample())
    assert m["text"].startswith("Today: what to buy and what to watch, Mon 12 Oct 2026") and "BUY IDEAS" in m["text"]
    assert m["subject"] == "Today: risk-on · 3 buy ideas · 2 to watch"
