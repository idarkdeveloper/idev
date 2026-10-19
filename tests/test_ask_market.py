"""Ask: "how is the market today" (market_summary, from the Markets strip data) and the reasons for skipped days in "is the market open"."""
from __future__ import annotations

from datetime import date, datetime

import pytest

from trading_agent import ask, ask_info
from trading_agent.timezones import IST

from .test_ask import S, search, with_model
from .test_ui import _post, server  # noqa: F401  (fixture)

SUMMARY = ["how is the market today", "how is the market", "how are the markets", "how are markets doing", "market today", "market update", "market summary",
           "market now", "market mood", "market report", "what is the market doing", "what's the market doing", "how is nifty", "how is nifty today", "how is sensex",
           "nifty today", "sensex now", "market kaisa hai", "market kaisa chal raha hai", "share market kaisa hai", "stock market today", "how is the stock market",
           "how is the indian market", "market overview", "market snapshot"]


@pytest.mark.parametrize("text", SUMMARY)
def test_market_questions_are_a_market_summary_not_the_news(text):
    r = ask.parse_rules(text)
    assert r["intent"] == "market_summary" and r["args"] == {}, text
    assert ask.interpret(text, search, S(), use_ollama=False)["intent"] == "market_summary"


def test_neighbouring_questions_keep_their_own_intent():
    for t, want in (("market news", "news_today"), ("news about the market", "news_today"), ("any news on the market today", "news_today"), ("read today's news", "news_today"),
                    ("how is my portfolio", "portfolio_summary"), ("how are my holdings doing today", "portfolio_summary"), ("is the market open", "market_status"),
                    ("is market close or open", "market_status"), ("when does the market open", "market_status"), ("health", "show_health"), ("which stock to buy today", "ideas"),
                    ("what investors are looking in the market", "followed_deals"), ("go to screener", "open_tab"), ("buy tcs", "refused")):
        assert ask.parse_rules(t)["intent"] == want, t
    for t in SUMMARY + ["market", "markets", "the market is a strange place today", "is market close or open"]:
        r = ask.parse_rules(t)
        assert r is None or r["intent"] != "show_health", t


def test_ollama_enum_and_validation_for_market_summary():
    text = "that thing about the shares of the company that makes cars"
    out, sess = with_model({"intent": "market_summary", "args": {"x": 1, "topic": "sell"}}, text=text)
    assert out["intent"] == "market_summary" and out["args"] == {} and out["via"] == "ollama"
    assert "market_summary" in sess.posts[0]["format"]["properties"]["intent"]["enum"] and "market_summary" in sess.posts[0]["messages"][0]["content"]
    assert "market_summary" in ask.INTENTS


# ---- the answer ----
def strip(**over):
    m = {"nifty50": {"last": 22889.4, "ret_1d": -0.0102, "as_of": "2026-10-19"}, "sp500": {"last": 7725.2, "ret_1d": 0.009, "as_of": "2026-10-16"},
         "nasdaq_fut": {"last": 26000.0, "ret_1d": 0.0187}, "nikkei": {"last": 50123.0, "ret_1d": -0.004}, "india_vix": {"last": 14.8, "ret_1d": 0.02},
         "usdinr": {"last": 88.24, "ret_1d": 0.002}, "brent": {"last": 109.3, "ret_1d": -0.011}}
    m.update(over)
    return m


REGIME = {"regime": "risk_off", "score": -2, "guidance": "No new buys; existing positions at most half size; prefer watch over buy."}


def test_summary_with_full_data_and_a_closed_market():
    out = ask_info.compose_market_summary({"regime": REGIME, "markets": strip(), "nse_open": False, "as_of": "21:06 IST"})
    say = out["say"]
    assert say.startswith("The market stance is Risk-off, score minus 2: no new buys; existing positions at most half size; prefer watch over buy. ")
    for part in ("Nifty 50 is down 1.0 percent at 22,889.", "S&P 500 is up 0.9 percent at 7,725.", "Nasdaq futures are up 1.9 percent.", "Nikkei is down 0.4 percent at 50,123.",
                 "India VIX is 14.8.", "USD/INR is 88.2, up 0.2 percent.", "Brent is 109.3 dollars, down 1.1 percent.",
                 "The NSE is closed, so Nifty prices are from the 19 Oct close; US and Asian indices and futures can be live.", "As of 21:06 IST."):
        assert part in say, part
    f = {x["label"]: x for x in out["facts"]}
    assert f["Market stance"] == {"label": "Market stance", "value": "Risk-off (-2)", "tone": "neg"}
    assert f["Nifty 50"] == {"label": "Nifty 50", "value": "22,889 (-1.0%)", "tone": "neg"} and f["S&P 500"]["tone"] == "pos" and f["India VIX"]["tone"] == ""
    assert f["Nasdaq futures"]["value"] == "26,000 (+1.9%)" and f["As of"]["value"] == "21:06 IST"
    assert all(set(x) <= {"label", "value", "tone"} for x in out["facts"])
    assert "I cannot" not in say and "buy" not in out["facts"][1]["value"]


def test_summary_open_market_risk_on_and_indian_grouping():
    out = ask_info.compose_market_summary({"regime": {"regime": "risk_on", "score": 3, "guidance": "Normal sizing."}, "nse_open": True, "as_of": "11:00 IST",
                                           "markets": strip(nifty50={"last": 123456.0, "ret_1d": 0.0, "as_of": "2026-10-19"})})
    assert out["say"].startswith("The market stance is Risk-on, score 3: normal sizing.") and "Nifty 50 is unchanged at 1,23,456." in out["say"]
    assert "NSE is closed" not in out["say"] and out["facts"][0]["tone"] == "pos"


def test_summary_leaves_out_missing_quotes_and_says_so():
    out = ask_info.compose_market_summary({"regime": REGIME, "markets": strip(nikkei={"last": None}, brent={}, usdinr={"last": 88.0, "ret_1d": None}), "nse_open": True, "as_of": ""})
    say = out["say"]
    assert "Nikkei is" not in say and "Brent is" not in say and "No quote for Nikkei and Brent." in say and "USD/INR is 88." in say and "As of" not in say
    assert [x["label"] for x in out["facts"]] == ["Market stance", "Nifty 50", "S&P 500", "Nasdaq futures", "India VIX", "USD/INR"]
    none = ask_info.compose_market_summary({"regime": None, "markets": {}, "nse_open": None})
    assert none["say"].startswith("I do not have the market stance right now. No quote for Nifty 50, S&P 500, Nasdaq futures, Nikkei, India VIX, USD/INR and Brent.")
    assert none["facts"] == [{"label": "Market stance", "value": "not available", "tone": ""}]
    three = ask_info.compose_market_summary({"regime": REGIME, "markets": strip(nikkei={}, sp500={}, brent={})})
    assert "No quote for S&P 500, Nikkei and Brent." in three["say"]


def test_the_route_composes_from_the_strip_cache_and_the_clock(server, monkeypatch):  # noqa: F811
    base, app = server
    app._ask_limit = None
    app.context = object()
    calls = []
    monkeypatch.setattr(app, "_regime_view", lambda: type("R", (), {"fetch": lambda self: calls.append(1) or {"regime": "risk_off", "score": -2, "guidance": REGIME["guidance"], "markets": strip()}})())
    monkeypatch.setattr(app, "clock", lambda: datetime(2026, 10, 19, 17, 0, tzinfo=IST))
    out = _post(base + "/api/ask", {"text": "how is the market today"})[1]
    assert out["intent"] == "market_summary" and out["say"].startswith("The market stance is Risk-off, score minus 2:") and "Nifty 50 is down 1.0 percent at 22,889." in out["say"]
    assert "As of 17:00 IST." in out["say"] and "The NSE is closed" in out["say"] and calls == [1] and out["facts"][1]["label"] == "Nifty 50"
    assert _post(base + "/api/ask", {"text": "market news"})[1]["intent"] == "news_today"


# ---- the reasons for skipped days ----
def cal(holidays: dict[str, str | None], weekdays_only=True):
    day = lambda d: d.weekday() < 5 and d.isoformat() not in holidays  # noqa: E731
    return day, (lambda d: holidays.get(d.isoformat()))


def test_next_open_says_why_days_were_skipped():
    at = lambda d, h, m: datetime(2026, 10, d, h, m, tzinfo=IST)  # noqa: E731
    d, n = cal({"2026-10-20": "Diwali"})
    say = ask_info.compose_market_status("IN", at(19, 16, 0), d, n)["say"]
    assert say == ("The NSE is closed now. It closed at 3:30 pm IST today. It opens on Wednesday 21 October, at 9:15 am IST. "
                   "Tuesday 20 October is a market holiday (Diwali).")
    d, n = cal({"2026-10-20": "Diwali", "2026-10-21": "Balipratipada"})
    say = ask_info.compose_market_status("IN", at(19, 16, 0), d, n)["say"]
    assert "It opens on Thursday 22 October, at 9:15 am IST. Tuesday 20 October and Wednesday 21 October are market holidays (Diwali, Balipratipada)." in say
    d, n = cal({"2026-10-20": None})
    say = ask_info.compose_market_status("IN", at(19, 16, 0), d, n)["say"]
    assert say.endswith("It opens on Wednesday 21 October, at 9:15 am IST. Tuesday 20 October is a market holiday.")
    d, n = cal({})
    say = ask_info.compose_market_status("IN", at(16, 18, 0), d, n)["say"]                         # Friday evening
    assert say == "The NSE is closed now. It closed at 3:30 pm IST today. It opens on Monday 19 October, at 9:15 am IST. Saturday and Sunday are weekend days."
    d, n = cal({"2026-10-19": "Test Holiday"})
    say = ask_info.compose_market_status("IN", at(16, 18, 0), d, n)["say"]                         # Friday evening, Monday a holiday
    assert say.endswith("It opens on Tuesday 20 October, at 9:15 am IST. Saturday and Sunday are weekend days. Monday 19 October is a market holiday (Test Holiday).")
    # tomorrow is a plain trading day: nothing to explain; Saturday itself: the weekend is already said
    d, n = cal({})
    assert ask_info.compose_market_status("IN", at(19, 16, 0), d, n)["say"].endswith("It opens tomorrow, Tuesday 20 October, at 9:15 am IST.")
    sat = ask_info.compose_market_status("IN", at(17, 11, 0), d, n)["say"]
    assert sat == "The NSE is closed now. It is closed today, it is the weekend. It opens on Monday 19 October, at 9:15 am IST."
    # a holiday today and another tomorrow
    d, n = cal({"2026-10-20": "Diwali", "2026-10-21": "Balipratipada"})
    say = ask_info.compose_market_status("IN", at(20, 11, 0), d, n)["say"]
    assert say.startswith("The NSE is closed now. It is closed today for Diwali. It opens on Thursday 22 October, at 9:15 am IST. Wednesday 21 October is a market holiday (Balipratipada).")
    # more than two items: only two are named
    d, n = cal({"2026-10-19": "A", "2026-10-20": "B"})
    assert ask_info.compose_market_status("IN", at(16, 18, 0), d, n)["say"].count(".") <= 6
    # US, UK and Japan: weekends only, no holiday claims
    us = ask_info.compose_market_status("US", datetime(2026, 10, 17, 10, 0, tzinfo=IST))["say"]
    assert "market holiday" not in us


def test_the_skipped_day_helper_names_at_most_two_items():
    day, name = cal({"2026-10-19": "A", "2026-10-20": "B", "2026-10-21": "C"})
    text = ask_info._skipped(date(2026, 10, 16), date(2026, 10, 22), day, name, True)
    assert text == " Saturday and Sunday are weekend days. Monday 19 October and Tuesday 20 October are market holidays (A, B)."
    assert ask_info._skipped(date(2026, 10, 19), date(2026, 10, 20), day, name, True) == ""


# ---- why is the market closed tomorrow / the next holidays ----
WHY = [("why is the market closed tomorrow", "tomorrow"), ("why is market close tomorrow", "tomorrow"), ("is tomorrow a holiday", "tomorrow"), ("is market open tomorrow", "tomorrow"),
       ("market holiday tomorrow", "tomorrow"), ("is the nse open tomorrow", "tomorrow"), ("why is market close today", "today"), ("why is the nse closed", "today"),
       ("is today a market holiday", "today"), ("when is the next holiday", "holidays"), ("upcoming market holidays", "holidays"), ("next market holiday", "holidays"),
       ("market holidays", "holidays"), ("holiday list", "holidays")]


@pytest.mark.parametrize("text,when", WHY)
def test_why_and_holiday_questions(text, when):
    r = ask.parse_rules(text)
    assert r["intent"] == "market_status" and r["args"] == {"market": "IN", "when": when}, text


def test_the_plain_status_questions_and_foreign_markets_have_no_when():
    for t in ("is the market open", "is market close or open", "when does the market open", "is the US market open tomorrow", "is japan open"):
        r = ask.parse_rules(t)
        assert r["intent"] == "market_status" and "when" not in r["args"], t
    text = "that thing about the shares of the company that makes cars"
    out, _ = with_model({"intent": "market_status", "args": {"market": "IN", "when": "tomorrow"}}, text=text)
    assert out["args"] == {"market": "IN", "when": "tomorrow"}
    out, _ = with_model({"intent": "market_status", "args": {"market": "IN", "when": "../x"}}, text=text)
    assert out["args"] == {"market": "IN"}
    out, _ = with_model({"intent": "market_status", "args": {"market": "US", "when": "tomorrow"}}, text=text)
    assert out["args"] == {"market": "US"}


def test_tomorrow_today_and_the_next_holidays_from_a_fake_calendar():
    at = lambda d, h, m: datetime(2026, 10, d, h, m, tzinfo=IST)  # noqa: E731
    days = {"2026-10-20": "Diwali", "2026-10-22": "Bhai Dooj", "2026-11-10": "Gurunanak Jayanti", "2026-12-25": "Christmas"}
    day = lambda d: d.weekday() < 5 and d.isoformat() not in days  # noqa: E731
    name = lambda d: days.get(d.isoformat())  # noqa: E731
    hol = ask_info.compose_market_when("tomorrow", at(19, 16, 0), day, name, days)
    assert hol["say"] == "The NSE is closed tomorrow, Tuesday 20 October, for Diwali. It opens on Wednesday 21 October, at 9:15 am IST."
    assert [f["label"] for f in hol["facts"]] == ["NSE", "Time now", "Reason", "Opens"] and hol["facts"][2]["value"] == "Diwali" and hol["facts"][0]["tone"] == "neg"
    op = ask_info.compose_market_when("tomorrow", at(20, 11, 0), day, name, days)
    assert op["say"] == "The NSE is open tomorrow, Wednesday 21 October, 9:15 am to 3:30 pm IST." and [f["label"] for f in op["facts"]] == ["NSE", "Time now", "Opens", "Closes"]
    wk = ask_info.compose_market_when("tomorrow", at(16, 18, 0), day, name, days)
    assert wk["say"] == "The NSE is closed tomorrow, Saturday 17 October: it is a weekend. It opens on Monday 19 October, at 9:15 am IST."
    unknown = ask_info.compose_market_when("tomorrow", at(19, 16, 0), lambda d: d.weekday() < 5 and d != date(2026, 10, 20), lambda d: None, {})
    assert unknown["say"] == ("The NSE is closed tomorrow, Tuesday 20 October. I do not see a reason in my holiday list; tomorrow may be a special closure. "
                              "It opens on Wednesday 21 October, at 9:15 am IST.")
    today_hol = ask_info.compose_market_when("today", at(20, 11, 0), day, name, days)
    assert today_hol["say"].startswith("The NSE is closed today, Tuesday 20 October, for Diwali.")
    today_open = ask_info.compose_market_when("today", at(19, 12, 0), day, name, days)
    assert today_open["say"] == "The NSE is open today, Monday 19 October, 9:15 am to 3:30 pm IST. It is open now."
    assert ask_info.compose_market_when("today", at(19, 16, 0), day, name, days)["say"].endswith("It has closed for the day.")
    assert ask_info.compose_market_when("today", at(19, 8, 0), day, name, days)["say"].endswith("It opens at 9:15 am IST.")
    nxt = ask_info.compose_market_when("holidays", at(19, 12, 0), day, name, days)
    assert nxt["say"] == "The next NSE holidays: Tuesday 20 October, Diwali; Thursday 22 October, Bhai Dooj; Tuesday 10 November, Gurunanak Jayanti."
    assert [f["value"] for f in nxt["facts"][:3]] == ["Tue 20 Oct · Diwali", "Thu 22 Oct · Bhai Dooj", "Tue 10 Nov · Gurunanak Jayanti"] and nxt["facts"][3]["label"] == "Time now"
    later = ask_info.compose_market_when("holidays", at(11, 12, 0) if False else datetime(2026, 12, 1, 12, 0, tzinfo=IST), day, name, days)
    assert later["say"] == "The next NSE holidays: Friday 25 December, Christmas."
    assert ask_info.compose_market_when("holidays", datetime(2027, 1, 5, 12, 0, tzinfo=IST), day, name, days)["say"] == "I do not see any further NSE holidays in my holiday list."
    assert ask_info.compose_market_when("holidays", at(19, 12, 0), day, name, None)["say"] == "I do not have the NSE holiday list right now."
    assert "2026-10-24" not in str(ask_info.compose_market_when("holidays", at(19, 12, 0), day, name, {"2026-10-24": "A Saturday"}))   # weekends are not holidays


def test_the_route_answers_why_from_the_cached_calendar_and_never_fetches(server, monkeypatch, tmp_path):  # noqa: F811
    base, app = server
    app._ask_limit = None
    monkeypatch.setattr(app, "clock", lambda: datetime(2026, 10, 19, 17, 0, tzinfo=IST))

    class Hol:
        cache_dir = tmp_path
        _days = {"2026-10-20": "Diwali", "2026-11-10": "Gurunanak Jayanti"}
        def peek_trading_day(self, d):
            return d.weekday() < 5 and d.isoformat() not in self._days
        def days(self):
            raise AssertionError("must not fetch")
        is_trading_day = holiday = days
    monkeypatch.setattr(app, "_safe_holidays", lambda: Hol())
    out = _post(base + "/api/ask", {"text": "why is the market closed tomorrow"})[1]
    assert out["intent"] == "market_status" and out["args"] == {"market": "IN", "when": "tomorrow"}
    assert out["say"] == "The NSE is closed tomorrow, Tuesday 20 October, for Diwali. It opens on Wednesday 21 October, at 9:15 am IST."
    nxt = _post(base + "/api/ask", {"text": "upcoming market holidays"})[1]
    assert nxt["say"] == "The next NSE holidays: Tuesday 20 October, Diwali; Tuesday 10 November, Gurunanak Jayanti."
    plain = _post(base + "/api/ask", {"text": "is the market open"})[1]
    assert plain["say"].endswith("Tuesday 20 October is a market holiday (Diwali).") and "It opens on Wednesday 21 October" in plain["say"]
