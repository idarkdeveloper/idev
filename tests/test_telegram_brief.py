"""The Telegram daily brief: phone-sized HTML from fixture data. Fakes only, no network."""
import re

import requests

from trading_agent.digest_schedule import send_digest
from trading_agent.notify import Notifier
from trading_agent.telegram_brief import telegram_brief

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
    assert h.startswith("<b>🌅 Morning brief · Mon 12 Oct</b>")
    assert "🟡 <code>NEUTRAL</code> · Nifty below 200-day avg, downtrend" in h
    assert "⛔ No new buys (2 pass the screen, held back)" in h
    assert "US 🟢 · Asia 🟡 · The rupee is near its weakest of the year" in h
    assert "<i>Quiet start. Nothing odd.</i>" in h
    assert "⚠️ <b>Holdings to review (13 flagged)</b>" in h and "🤝 <b>Deals:</b> 0 new" in h


def test_morning_buy_day_and_regime_icon_follows_data():
    e = morning(wait=False)
    e["data"]["mood"]["regime"] = "risk_on"
    h = telegram_brief(e)["html"]
    assert "🟢 <code>RISK-ON</code>" in h and "✅ 2 buy ideas: Alpha (AAA), Beta (BBB)" in h
    e["data"]["mood"]["regime"] = "risk_off"
    assert "🔴 <code>RISK-OFF</code>" in telegram_brief(e)["html"]


def test_table_rows_criteria_once_and_rollover():
    h = telegram_brief(morning())["html"]
    table = re.search(r"<pre>(.*?)</pre>", h, re.S).group(1).split("\n")
    assert len(table) == 6 and all(len(r) <= 32 for r in table)   # header + 5
    assert table[1].startswith("S00") and table[1].endswith("stop+avg") and table[2].endswith(" avg")
    assert h.count("3×ATR") == 1 and h.count("200-day average") == 1
    assert "<i>+ 8 more: Co05 (S05), Co06 (S06)" in h


def test_escaping_of_names_and_codes():
    w = watch(7)
    w["items"][0]["symbol"] = "<b>X"
    w["items"][6]["name"] = "Evil <b>bold</b> & Co Limited"
    h = telegram_brief(morning(w=w))["html"]
    assert "&lt;b&gt;X" in h and "Evil &lt;b&gt;bold&lt;/b&gt; &amp; Co (S06)" in h
    assert "<b>X" not in h


def test_evening_lines_and_deals():
    h = telegram_brief(evening())["html"]
    assert h.startswith("<b>🌆 Close · Mon 12 Oct</b>")
    assert "💼 <b>₹1,23,456</b>" in h and "today +₹500 (+0.4%)" in h and "📈 Nifty 24,100.50 (−0.40%)" in h
    assert "🤝 <b>Deals:</b> 1 new\nX bought Infosys (INFY) ₹12 cr (NSE)" in h


def test_huge_fixture_stays_under_4000():
    w = watch(15, total=500)
    for i in w["items"]:
        i["name"] = "Very Long Company Name Holdings " * 3
    e = morning(w=w, summary="A long sentence goes here. " * 40)
    h = telegram_brief(e)["html"]
    assert len(h) <= 4000 and "+ 495 more" in h


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
    base = {"summary": "UNIQUE SUMMARY TEXT.", "data": {"kind": "morning", "date": "2026-10-12"}}
    assert "UNIQUE SUMMARY TEXT" not in telegram_brief({**base, "writer": "rules"})["html"]
    assert "UNIQUE SUMMARY TEXT" in telegram_brief({**base, "writer": "claude:claude-haiku-5-5"})["html"]
