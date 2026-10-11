"""The evening email's HTML (digest_evening.py): headline, three figures, the portfolio best to worst with bars, the
practice strip, news and deals, the market bulletin and the concept. Fake data only: no network, no Groww, no Telegram,
no Anthropic, no Resend. ``sample()`` also makes docs/design/evening-email-sample.html."""
import re

from trading_agent import bulletin, charts, concepts, digest_evening, digest_render
from trading_agent.digest import unavailable

from .test_bulletin import TODAY, daily_bars, sessions


def nifty():
    return bulletin.analyse_nifty(daily_bars(), sessions(), sessions(days=22, step=60))


def holding(sym, name, qty, price, day_pct, pl, pl_pct):
    prev = round(price / (1 + day_pct / 100), 2) if day_pct is not None else None
    return {"symbol": sym, "name": name, "qty": qty, "price": price, "avg": None, "value": round(qty * price, 2), "pl": pl, "pl_pct": pl_pct,
            "prev_close": prev, "day_pct": day_pct, "day_pl": None if prev is None else round(qty * (price - prev), 2)}


def sample(stale=None):
    hold = [holding("TATAMOTORS", "Tata Motors Limited", 60, 942.10, -2.80, -4194.0, -6.9),
            holding("BEL", "Bharat Electronics Limited", 300, 298.40, 1.90, 25080.0, 38.9),
            holding("NTPC", "NTPC Limited", 150, 402.60, -1.10, 7590.0, 14.4),
            holding("ITC", "ITC Limited", 120, 498.20, 0.40, 10284.0, 20.8)]
    d = {"kind": "evening", "date": TODAY.isoformat(), "generated_at": "2026-10-12T15:45:00+05:30", "delayed": False,
         "groww": {"value": 482310.0, "invested": 421000.0, "pl": 61310.0, "pl_pct": 14.56, "day_pl": -2140.0, "day_pct": -0.44,
                   "holdings": hold, "no_price": [], "no_prev_close": []},
         "practice": {"equity": 512480.0, "cash": 184200.0, "day_change": 1320.0, "day_change_pct": 0.26, "since": None,
                      "since_change": None, "since_change_pct": None, "same_as_groww": None, "total_pl": 12480.0, "total_pl_pct": 2.5,
                      "positions": [], "stop_fills_today": []},
         "news": {"items": [{"symbol": "BEL", "name": "", "sentiment": "positive", "source": "ET Markets", "when": "12 Oct",
                             "title": "BEL bags ₹1,092 crore defence orders", "link": "https://example.com/bel"},
                            {"symbol": "TATAMOTORS", "name": "", "sentiment": "negative", "source": "ET Stocks", "when": "12 Oct",
                             "title": "JLR wholesales fall 9% in Q2 on weaker China demand", "link": "https://example.com/jlr"}],
                  "total": 2, "symbols": 4},
         "deals": {"since": TODAY.isoformat(), "deals": [], "total": 0, "following": ["ASHISH KACHOLIA", "VIJAY KEDIA"]},
         "bulletin": {"note": bulletin.NOTE, "nifty": nifty(),
                      "global": {"markets": [{"market": "Nikkei", "close": 47320.0, "d1_pct": 1.04, "d5_pct": 2.10, "trend": "UP",
                                              "why": {"title": "Nikkei slips as exporters fall", "source": "ET"}},
                                             {"market": "Hang Seng", "close": 26140.0, "d1_pct": -0.62, "d5_pct": 0.40, "trend": "mixed"}],
                                 "note": "n"},
                      "commodities": {"rows": [{"name": "Gold", "unit": "$", "last": 2684.2, "d1_pct": 0.42, "d5_pct": 1.10, "trend": "UP"},
                                               {"name": "Brent", "unit": "$", "last": 71.4, "d1_pct": -0.92, "d5_pct": -2.40, "trend": "DOWN"}],
                                      "skipped": 0, "note": "n"},
                      "concept": concepts.concept_for(TODAY)}}
    if stale:
        d["stale_close"] = {"date": "2026-10-09", "reason": stale, "label": "Fri 9 Oct"}
    return d


SUMMARY = "Your Groww portfolio fell ₹2,140 today (−0.44%). TATAMOTORS led the fall at −2.8%."


def html(d, summary=None, writer="none", images=None):
    return digest_render.render(d, summary, writer, images)["html"]


def test_headline_up_down_flat_and_stale():
    d = sample()
    nf = d["bulletin"]["nifty"]
    nf["change_pct"] = 0.62
    assert digest_evening.headline(d) == "Down ₹2,140 today while the Nifty rose 0.62%."
    d["groww"]["day_pl"] = 3500.0
    assert digest_evening.headline(d) == "Up ₹3,500 today and the Nifty rose 0.62%."
    nf["change_pct"] = -1.234
    assert digest_evening.headline(d) == "Up ₹3,500 today while the Nifty fell 1.23%."
    d["groww"]["day_pl"] = 0.2
    assert digest_evening.headline(d) == "Flat today while the Nifty fell 1.23%."
    nf["change_pct"] = 0.001
    d["groww"]["day_pl"] = -10.0
    assert digest_evening.headline(d) == "Down ₹10 today; the Nifty was flat."
    d["bulletin"]["nifty"] = unavailable("x")
    assert digest_evening.headline(d) == "Down ₹10 today."
    d["groww"] = unavailable("y")
    assert digest_evening.headline(d) == "Today's close report."
    assert digest_evening.headline(sample("no trading today")) == "No trading today — figures from the Fri 9 Oct close."
    assert digest_evening.headline(sample("before the close")) == "The market has not closed yet — figures from the Fri 9 Oct close."


def test_sections_in_order_and_the_header():
    h = html(sample(), SUMMARY, "claude:claude-haiku-4-5", charts.bulletin_images(sample()["bulletin"]))
    order = ["Trading Agent · Close report", "Mon 12 Oct 2026 · 15:45 IST", ">Today<", ">Total P&amp;L<", ">Nifty 50<", "In short",
             "Your Groww portfolio today", "Practice account", "News for your stocks", "Deals today:", "Market bulletin",
             "Nifty 50 closed at", 'src="cid:nifty15"', ">Watch levels<", ">Pivots<", ">ADX (14)<", ">RSI (14)<", 'src="cid:nifty4h"',
             ">Global and commodities<", "never a forecast", "Concept of the", "Where you see it:", "not financial advice"]
    pos = [h.index(x) for x in order]
    assert pos == sorted(pos), [x for x, p in zip(order, pos)]
    assert h.count("<img") == 2


def test_tiles_are_coloured_by_sign():
    h = html(sample())
    tiles = h[h.index(">Today<"):h.index("Your Groww portfolio")]
    assert 'color:#c1272d;padding-top:3px">−\u2060₹2,140<' in tiles and "−0.44% vs previous close" in tiles   # the sign stays with the ₹
    assert 'color:#0a7a3c;padding-top:3px">+\u2060₹61,310<' in tiles and "+14.56% · value ₹4,82,310" in tiles
    nf = sample()["bulletin"]["nifty"]
    move = f"{bulletin.sgn(nf['change'])} ({digest_evening.pct_text(nf['change_pct'], 2)})"
    assert move in tiles and digest_evening.num(nf["close"], 2) in tiles
    stale = html(sample("no trading today"))
    assert ">Value<" in stale and "at the Fri 9 Oct close" in stale and ">Today<" not in stale and "invested ₹4,21,000" in stale


def test_holdings_best_to_worst_with_bars_scaled_to_the_biggest_move():
    h = html(sample())
    part = h[h.index("Your Groww portfolio today"):h.index("Practice account")]
    syms = re.findall(r'font-weight:bold;color:#1a1c28">([A-Z]+)</div>', part)
    assert syms == ["BEL", "ITC", "NTPC", "TATAMOTORS"]
    widths = [int(w) for w in re.findall(r"height:4px;width:(\d+)%", part)]
    assert widths == [68, 14, 39, 100]                                    # 1.9, 0.4, 1.1 and 2.8 of 2.8
    assert "Bharat Electronics · ₹298.40 · 300 shares" in part and "+₹25,080" in part and "−₹4,194" in part
    assert "Best to worst · invested ₹4,21,000 · today&#x27;s buys show from tomorrow (T+1)" in part


def test_stale_day_sorts_by_total_without_bars_and_says_so():
    h = html(sample("no trading today"))
    part = h[h.index("Your Groww portfolio"):h.index("Practice account")]
    assert re.findall(r'font-weight:bold;color:#1a1c28">([A-Z]+)</div>', part) == ["BEL", "ITC", "NTPC", "TATAMOTORS"]
    assert "height:4px" not in part and ">Today<" not in part and "Best to worst by total return" in part
    assert "no change for today on this snapshot" in part


def test_practice_strip_and_stop_sells():
    d = sample()
    h = html(d)
    assert "equity ₹5,12,480 · cash ₹1,84,200" in h and "+₹1,320 today" in h and "+2.50% since start" in h and "No stop-loss sells today." in h
    d["practice"]["stop_fills_today"] = [{"symbol": "NTPC", "qty": 10.0, "price": 380.0, "stop": 381.5, "label": "trailing"}]
    assert "Stop hit today: sold 10 NTPC at ₹380.00 (stop ₹381.50)." in html(d)
    d["practice"] = unavailable("no practice account yet")
    assert "Practice account" not in html(d)


def test_news_negative_first_with_badges_and_the_deals_line():
    h = html(sample())
    news = h[h.index("News for your stocks"):]
    assert news.index(">NEGATIVE<") < news.index(">TATAMOTORS</b>") < news.index(">POSITIVE<") < news.index(">BEL</b>")
    assert re.search(r"color:#ffffff;background:#c1272d;[^>]*>NEGATIVE<", h) and re.search(r"color:#0a7a3c;border:1px solid #0a7a3c;[^>]*>POSITIVE<", h)
    assert "<b style=\"color:#1a1c28\">Deals today:</b> none by ASHISH KACHOLIA or VIJAY KEDIA. BSE deals arrive after 16:00" in h
    d = sample()
    d["deals"]["following"] = ["A", "B", "C", "D"]
    assert "none by the 4 followed investors." in html(d)
    d["deals"] = {"since": "x", "total": 3, "following": ["A"], "deals": [
        {"ticker": "BEL", "transaction": "BUY", "size": "19,20,000", "who": ["A"], "investor": "A", "exchange": "NSE", "reported": "2026-10-12"}]}
    assert "BEL BUY 19,20,000 by A (NSE); and 2 more." in html(d)
    assert "BSE deals arrive" not in html(sample("no trading today"))
    d = sample()
    d["news"] = {"items": [], "note": "no holdings to look up"}
    h = html(d)
    assert "News for your stocks" not in h and "Deals today:" in h               # no empty news box


def test_in_short_only_for_a_model_summary():
    assert "· written by Claude Haiku, check the numbers below" in html(sample(), SUMMARY, "claude:claude-haiku-4-5")
    assert "In short" not in html(sample(), SUMMARY, "rules") and "In short" not in html(sample())
    stale = html(sample("no trading today"), "No trading today. The portfolio is unchanged.", "claude:claude-haiku-4-5")
    assert "The portfolio is unchanged." in stale and "No trading today. The portfolio" not in stale


def test_missing_parts_are_left_out():
    d = sample()
    d["bulletin"] = {"nifty": unavailable("x"), "global": unavailable("y"), "commodities": unavailable("z"), "concept": unavailable("c")}
    h = html(d)
    assert "Nifty 50 unavailable: x" in h and "Global and commodities" not in h and "Concept of the" not in h and ">Nifty 50<" not in h
    del d["bulletin"]
    assert "Market bulletin" not in html(d)
    d = sample()
    d["groww"] = unavailable("not logged in")
    h = html(d)
    assert "Unavailable: not logged in" in h and "Practice today" in h and ">Today<" not in h


def test_email_safe_escaped_and_the_charts_stay_cid_pictures():
    d = sample()
    d["news"]["items"][0]["title"] = "<script>x</script>"
    d["news"]["items"][1]["link"] = "javascript:alert(1)"
    d["groww"]["holdings"][0]["name"] = "Evil <b>co</b>"
    images = charts.bulletin_images(d["bulletin"])
    h = html(d, "<img src=x>", "claude:x", images)
    assert "<script" not in h and "<style" not in h and "javascript:" not in h and "<b>co" not in h and "&lt;script&gt;" in h
    assert "max-width:640px;width:100%" in h and set(re.findall(r'class="([^"]+)"', h)) <= {"pl-profit", "pl-loss"}
    assert re.findall(r'<img src="cid:(\w+)"', h) == ["nifty15", "nifty4h"]
    assert "data:image/png;base64," in digest_render.inline_data_urls(h, images)
    assert "<img" not in digest_render.strip_cid_images(h)


def test_text_part_subject_and_morning_are_unchanged():
    m = digest_render.render(sample(), SUMMARY, "claude:claude-haiku-4-5")
    assert m["subject"].startswith("Close: ") and "YOUR GROWW PORTFOLIO" in m["text"] and "IN SHORT" in m["text"]
    assert "Trading Agent · Close report" not in m["text"]
