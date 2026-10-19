"""The Telegram daily brief: phone-sized HTML from fixture data. Fakes only, no network."""

import requests

from trading_agent.digest_schedule import send_digest
from trading_agent.notify import Notifier
from trading_agent.telegram_brief import LIMIT, heat_bar, heat_line, telegram_brief

TOKEN = "123456789:AAH_fake-token-for-tests-0123456789abcd"


def item(sym, loss, price, reasons, name=None):
    return {"symbol": sym, "name": name or f"Co{sym[1:]} Limited", "price": price, "loss_pct": loss, "reasons": reasons}


def watch(n=13, total=None):
    both = ["below the stop level 90 (buy price minus 3×ATR)", "below its 200-day average (120)"]
    items = [item(f"S{i:02d}", -30.0 + i, 100.0 + i, both if i % 2 == 0 else ["below its 200-day average (120)"]) for i in range(n)]
    return {"items": items, "total": total or n}


def morning(wait=True, w=None, summary="Quiet start. Nothing odd."):
    ideas = [{"symbol": "AAA", "name": "Alpha Limited"}, {"symbol": "BBB", "name": "Beta Ltd"}]
    return {"subject": "s", "summary": summary, "data": {
        "kind": "morning", "date": "2026-10-12",
        "mood": {"regime": "neutral", "no_new_buys": wait,
                 "why": ["Nifty is below its 200-day average", "Nifty is in a downtrend (50-day ...)"] if wait else []},
        "world": {"region_lines": ["US: uptrend (2 of 3 up)", "Asia: mixed (x)"]},
        "gauges": {"warning_texts": ["the rupee is near its weakest of the year"]},
        "buy_ideas": {"ideas": ideas, "wait": wait},
        "watch": w if w is not None else watch(),
        "deals": {"total": 0, "deals": []}}}


def evening():
    return {"subject": "s", "summary": "Calm close.", "data": {
        "kind": "evening", "date": "2026-10-12",
        "groww": {"value": 123456.0, "pl": -2000.0, "pl_pct": -1.6, "day_pl": 500.0, "day_pct": 0.4},
        "bulletin": {"nifty": {"close": 24100.5, "change_pct": -0.4}},
        "deals": {"total": 1, "deals": [{"investor": "X Fund", "who": ["X"], "ticker": "INFY", "name": "Infosys Limited",
                                         "exchange": "NSE", "transaction": "BUY", "size": "₹12 cr"}]}}}


def test_morning_wait_day_shape():
    h = telegram_brief(morning())["html"]
    assert h.startswith("<b>Morning brief · Mon 12 Oct IST</b>") and "<b>Market</b>" in h and "<blockquote expandable>" in h and h.index("<b>Market</b>") < h.index("<blockquote")
    assert "🟡 <code>NEUTRAL</code> · Nifty below 200-day avg, downtrend" in h
    assert "⛔ No new buys (2 pass the screen, held back)" in h
    assert "🌍 US 🟢 · Asia 🟡\n🇮🇳 The rupee is near its weakest of the year" in h
    assert "Quiet start" not in h   # morning: the lines above say it; the paragraph stays in the email
    assert "⚠️ <b>Holdings to review (13 flagged)</b>" in h and "🤝 <b>Deals:</b> 0 new" in h


def test_morning_buy_day_and_regime_icon_follows_data():
    e = morning(wait=False)
    e["data"]["mood"]["regime"] = "risk_on"
    h = telegram_brief(e)["html"]
    assert "🟢 <code>RISK-ON</code>" in h and "✅ 2 buy ideas: Alpha (AAA), Beta (BBB)" in h
    e["data"]["mood"]["regime"] = "risk_off"
    assert "🔴 <code>RISK-OFF</code>" in telegram_brief(e)["html"]


def test_review_lines_criteria_once_and_rollover():
    h = telegram_brief(morning())["html"]
    assert "<pre>" not in h   # no fixed-width block: narrow screens wrap the lines instead
    rows = [ln for ln in h.split("\n") if ln.startswith("🔻 S")]
    assert len(rows) == 5 and all(len(r) <= 40 for r in rows)
    assert rows[0] == "🔻 S00 · 100 (−30%) ▰▰▰▱▱ · stop+avg" and rows[1].endswith(" · avg")
    assert h.count("3×ATR") == 1 and h.count("200-day average") == 1
    assert "<i>+ 8 more: S05, S06, S07" in h   # codes only, so the line stays short


def test_heat_bar_is_proportional_and_fixed_width():
    assert heat_bar(-2.4) == "▰▰▰▱▱" and heat_bar(2.4) == "▰▰▰▱▱"
    assert heat_bar(0) == "▱▱▱▱▱" and heat_bar(0.05) == "▰▱▱▱▱" and heat_bar(1.0) == "▰▱▱▱▱"
    assert heat_bar(-17.0) == "▰▰▰▰▰" and heat_bar(-23.0, step=10) == "▰▰▰▱▱"
    assert heat_bar(None) == "▱▱▱▱▱" and heat_bar(float("nan")) == "▱▱▱▱▱"
    assert all(len(heat_bar(v)) == 5 for v in (-99, -5, -0.1, 0, 0.1, 3.3, 500))


def test_heat_line_matches_the_phone_format_and_escapes():
    assert heat_line("TATAMOTORS", 940.0, -2.4) == "🔻 TATAMOTORS · 940 (−2.4%) ▰▰▰▱▱"
    assert heat_line("INFY", 1023.4, 1.5) == "🔺 INFY · 1,023 (+1.5%) ▰▰▱▱▱"
    assert heat_line("X", 264.35, 0.0) == "▫️ X · 264.4 (0.0%) ▱▱▱▱▱"
    assert heat_line("A<b>&", 10.0, -1.0, tag="stop<") == "🔻 A&lt;b&gt;&amp; · 10 (−1.0%) ▰▱▱▱▱ · stop&lt;"
    assert len(heat_line("Z" * 200, 1e9, -1e6)) < 80   # a silly code or number never makes a long line


def test_escaping_of_names_and_codes():
    w = watch(7)
    w["items"][0]["symbol"] = "<b>X"
    w["items"][6]["symbol"] = "E<b>&"
    h = telegram_brief(morning(w=w))["html"]
    assert "&lt;b&gt;X" in h and "E&lt;b&gt;&amp;" in h
    assert "<b>X" not in h and "E<b>" not in h


def test_evening_lines_and_deals():
    h = telegram_brief(evening())["html"]
    assert h.startswith("<b>Evening close · Mon 12 Oct IST</b>") and "<b>Your portfolio</b>" in h
    assert ("💼 <b>Portfolio</b>\n━━━━━━━━━━━━\nValue: <b>₹1,23,456</b>\nToday: +₹500 (+0.4%)\n"
            "Total P&amp;L: −₹2,000 (−1.6%)\n━━━━━━━━━━━━\n📈 Nifty 24,100.50 (−0.40%)") in h
    assert "🤝 <b>Deals:</b> 1 new\nX bought Infosys (INFY) ₹12 cr (NSE)" in h


def test_huge_fixture_stays_under_4000():
    w = watch(15, total=500)
    for i in w["items"]:
        i["name"] = "Very Long Company Name Holdings " * 3
    e = morning(w=w, summary="A long sentence goes here. " * 40)
    h = telegram_brief(e)["html"]
    assert len(h) <= LIMIT and "+ 495 more" in h


def test_huge_evening_with_many_movers_stays_under_4000():
    ev = evening()
    ev["data"]["groww"]["holdings"] = [{"symbol": f"VERYLONGCODE{i:03d}", "price": 123456.78, "day_pct": (i - 50) / 3, "pl_pct": -i}
                                       for i in range(300)]
    ev["data"]["news"] = {"items": [{"symbol": f"N{i}", "sentiment": "negative"} for i in range(200)]}
    h = telegram_brief({**ev, "summary": "Long. " * 400, "writer": "claude"})["html"]
    assert len(h) <= LIMIT and h.count(" ▰") + h.count(" ▱") == 6   # three falls and three gains


def test_button_only_with_https_allowed_host():
    assert telegram_brief(morning())["button"] is None
    assert telegram_brief(morning(), ["127.0.0.1", "10.0.0.5"])["button"] is None
    b = telegram_brief(morning(), ["10.0.0.5", "box.tail1234.ts.net"])["button"]
    assert b == {"inline_keyboard": [[{"text": "📊 Dashboard", "url": "https://box.tail1234.ts.net/"}]]}


class Resp:
    def __init__(self, code):
        self.status_code = code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError("bad", response=self)


class Sess:
    def __init__(self, first=200):
        self.calls, self.first = [], first

    def post(self, url, **kw):
        self.calls.append((url, kw))
        return Resp(self.first if len(self.calls) == 1 else 200)


def notifier(s):
    return Notifier(session=s, telegram_token=TOKEN, telegram_chat_id="42")


def test_payload_is_html_with_button_and_html_rejection_retries_plain_once():
    e = morning()
    e["subject"], e["text"], e["html"] = "S", "t", "<p>t</p>"
    s = Sess()
    send_digest(notifier(s), e, "morning", "2026-10-12", ["box.example.com"])
    kw = s.calls[0][1]["json"]
    assert len(s.calls) == 1 and kw["parse_mode"] == "HTML"
    assert kw["reply_markup"]["inline_keyboard"][0][0]["url"] == "https://box.example.com/"
    s2 = Sess(first=400)
    d = send_digest(notifier(s2), e, "morning", "2026-10-12")
    assert len(s2.calls) == 2 and "telegram" in d
    plain = s2.calls[1][1]["json"]
    assert "parse_mode" not in plain and "<b>" not in plain["text"] and "Morning brief" in plain["text"]
    s3 = Sess(first=500)   # other errors are not retried
    assert "telegram" not in send_digest(notifier(s3), e, "morning", "2026-10-12") and len(s3.calls) == 1


def test_the_rules_summary_is_left_out_of_telegram_but_a_model_one_stays():
    ev = evening()
    assert "Calm close" not in telegram_brief({**ev, "writer": "rules"})["html"]
    assert "Calm close" in telegram_brief({**ev, "writer": "claude:claude-haiku-5-5"})["html"]
    assert "Quiet start" not in telegram_brief({**morning(), "writer": "claude:claude-haiku-5-5"})["html"]


def test_cockpit_lines_portfolio_india_gauges_and_events():
    e = morning()
    d = e["data"]
    d["mood"]["score"], d["mood"]["nifty"] = 0, {"last": 22520.45}
    d["gauges"] = {"gauges": [{"gauge": "India VIX", "value": 14.38, "d20_pct": 20.6, "reading": "calm, rising fast", "warning": False},
                              {"gauge": "USD/INR", "value": 96.73, "d20_pct": 1.8, "reading": "rupee near its weakest of the year", "warning": False}],
                   "warning_texts": []}
    d["watch"]["portfolio"] = {"value": 168993.2, "pl": -21721.6, "pl_pct": -11.39, "holdings": 16}
    d["watch"]["events_checked"], d["watch"]["events"] = True, [{"symbol": "TCS", "what": "results", "date": "2026-10-14"}]
    h = telegram_brief(e)["html"]
    assert "💼 <b>Portfolio</b> · 16 holdings\n━━━━━━━━━━━━\nValue: <b>₹1.69L</b>\nTotal P&amp;L: −₹21,722 (−11.4%)\n━━━━━━━━━━━━" in h
    assert "Today:" not in h   # no day move before the open
    assert "(+0) · Nifty below 200-day avg, downtrend · Nifty 22,520" in h
    assert "🇮🇳 India VIX 14.4 (+21% 20d) 🟡 · USD/INR 96.7 🔴" in h
    assert "📅 Events: TCS results 14 Oct" in h
    d["watch"]["events"] = []
    assert "📅 Events: none due in the next days" in telegram_brief(e)["html"]


def test_evening_stale_day_says_no_trading_once():
    ev = evening()
    ev["data"]["stale_close"] = {"reason": "no trading today"}
    h = telegram_brief(ev)["html"]
    assert "Today: no trading" in h and h.split("<blockquote")[0].lower().count("no trading") == 1 and h.lower().count("no trading") == 2   # said once up top, once in the collapsed detail
    ev["data"]["stale_close"] = {"reason": "not closed"}
    assert "Today: not closed yet" in telegram_brief(ev)["html"]


def test_evening_has_movers_heat_lines_and_negative_news_line():
    ev = evening()
    ev["data"]["groww"]["holdings"] = [
        {"symbol": "VEDL", "price": 264.0, "day_pct": -3.2, "pl_pct": -28.7},
        {"symbol": "INFY", "price": 1023.4, "day_pct": 1.5, "pl_pct": -20.4},
        {"symbol": "TCS", "price": 2156.0, "day_pct": -0.4, "pl_pct": -15.3}]
    ev["data"]["news"] = {"items": [{"symbol": "VEDL", "sentiment": "negative", "title": "x"}]}
    h = telegram_brief(ev)["html"]
    assert "<b>Today&#x27;s movers</b>" in h or "<b>Today's movers</b>" in h
    assert "<pre>" not in h
    assert "🔻 VEDL · 264 (−3.2%) ▰▰▰▰▱\n🔻 TCS · 2,156 (−0.4%) ▰▱▱▱▱\n🔺 INFY · 1,023 (+1.5%) ▰▰▱▱▱" in h
    assert "📰 <b>News:</b> 1 negative for your stocks (VEDL)" in h


def test_stale_evening_lists_worst_total_losses_on_the_ten_percent_scale():
    ev = evening()
    ev["data"]["stale_close"] = {"reason": "no trading today"}
    ev["data"]["groww"]["holdings"] = [{"symbol": "VEDL", "price": 264.0, "day_pct": -3.2, "pl_pct": -28.7},
                                       {"symbol": "INFY", "price": 1023.4, "day_pct": 1.5, "pl_pct": -4.0}]
    h = telegram_brief(ev)["html"]
    assert "<b>Worst total loss</b>\n🔻 VEDL · 264 (−28.7%) ▰▰▰▱▱\n🔻 INFY · 1,023 (−4.0%) ▰▱▱▱▱" in h
