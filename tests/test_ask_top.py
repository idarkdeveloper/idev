"""Ask: the top stocks, gainers and losers of a market (market_top), answered from the Screener's tables. Fake tables and services only."""
from __future__ import annotations

import threading
import time
from datetime import datetime
from pathlib import Path

import pytest

from trading_agent import ask, ask_info
from trading_agent.timezones import IST

from .test_ask import S, search, with_model
from .test_ui import _post, server  # noqa: F401  (fixture)

UI = Path(__file__).resolve().parent.parent / "trading_agent" / "ui"

PHRASES = [  # (text, market, kind, n)
    ("top 10 usa stocks", "US", "leaders", 10), ("which are the top US stocks", "US", "leaders", 5), ("best US stocks", "US", "leaders", 5),
    ("top us stocks", "US", "leaders", 5), ("top 5 american stocks", "US", "leaders", 5), ("top stocks in america", "US", "leaders", 5), ("best nasdaq stocks", "US", "leaders", 5),
    ("top three nyse stocks", "US", "leaders", 3), ("best wall street stocks", "US", "leaders", 5), ("best US stocks to buy today", "US", "leaders", 5),
    ("top uk stocks", "UK", "leaders", 5), ("best british stocks", "UK", "leaders", 5), ("top 3 london stocks", "UK", "leaders", 3), ("top ftse stocks", "UK", "leaders", 5),
    ("best japanese stocks", "JP", "leaders", 5), ("top japan stocks", "JP", "leaders", 5), ("top 7 tokyo stocks", "JP", "leaders", 7), ("nikkei top performers", "JP", "leaders", 5),
    ("us gainers today", "US", "gainers", 5), ("top gainers in america", "US", "gainers", 5), ("uk gainers", "UK", "gainers", 5), ("top 10 gainers in japan", "JP", "gainers", 10),
    ("us winners today", "US", "gainers", 5), ("biggest losers in japan", "JP", "losers", 5), ("us losers today", "US", "losers", 5), ("worst stocks in the us", "US", "losers", 5),
    ("biggest uk losers", "UK", "losers", 5), ("best performing indian stocks", "IN", "leaders", 5), ("top indian stocks", "IN", "leaders", 5), ("top stocks", "IN", "leaders", 5),
    ("top 10 stocks", "IN", "leaders", 10), ("best nifty stocks", "IN", "leaders", 5), ("top gainers in india", "IN", "gainers", 5), ("indian losers today", "IN", "losers", 5),
    ("top 20 us stocks", "US", "leaders", 10), ("top 1 us stocks", "US", "leaders", 3), ("tell us the top stocks", "IN", "leaders", 5),
    ("show the top performers in the us", "US", "leaders", 5)]


@pytest.mark.parametrize("text,market,kind,n", PHRASES)
def test_top_stock_phrasings(text, market, kind, n):
    r = ask.parse_rules(text)
    assert r["intent"] == "market_top" and r["args"] == {"market": market, "kind": kind, "n": n}, text


def test_neighbours_keep_their_own_intent():
    for t, want in (("top gainer today", "portfolio_summary"), ("best performer", "portfolio_summary"), ("biggest loser", "portfolio_summary"),
                    ("which stock to buy today", "ideas"), ("best stock now", "ideas"), ("best stocks to buy today", "ideas"), ("suggest me to buy a stock today", "ideas"),
                    ("how is the us market", "market_summary"), ("is the us market open", "market_status"), ("us market news", "news_today"), ("top stocks news", "news_today"),
                    ("buy top us stocks", "refused"), ("buy tcs", "refused"), ("tell me tata motors detail", "open_stock"), ("health", "show_health")):
        assert ask.parse_rules(t)["intent"] == want, t


def test_ollama_enum_validation_and_a_stuck_model_never_holds_the_answer():
    text = "that thing about the shares of the company that makes cars"
    out, sess = with_model({"intent": "market_top", "args": {"market": "us", "kind": "gainers", "n": 99}}, text=text)
    assert out["intent"] == "market_top" and out["args"] == {"market": "US", "kind": "gainers", "n": 10} and out["via"] == "ollama"
    assert "market_top" in sess.posts[0]["format"]["properties"]["intent"]["enum"] and "market_top" in sess.posts[0]["messages"][0]["content"]
    out, _ = with_model({"intent": "market_top", "args": {"market": "../x", "kind": "sell", "n": "x"}}, text=text)
    assert out["args"] == {"market": "IN", "kind": "leaders", "n": 5}
    assert "market_top" in ask.INTENTS
    # a model that never answers: the answer comes back after the limit, not after the model
    release = threading.Event()
    class Stuck:
        def get(self, *a, **k):
            release.wait(30)
            raise OSError("never")
        post = get
    old = ask.OLLAMA_TIMEOUT
    ask.OLLAMA_TIMEOUT = 0.2
    try:
        started = time.monotonic()
        res = ask.interpret(text, search, S(), session=Stuck())
        took = time.monotonic() - started
    finally:
        ask.OLLAMA_TIMEOUT = old
        release.set()
    assert took < 3.0 and res["intent"] == "unknown" and "I did not understand that." in res["say"]


# ---- the answer from fake tables ----
def row(sym, name, price, chg, ret1y, score=None, eligible=True, **extra):
    return {"symbol": sym, "name": name, "price": price, "chg_1d": chg, "ret_1y": ret1y, "mom_score": score, "mom_eligible": eligible, **extra}


NOW = datetime(2026, 10, 19, 16, 0, tzinfo=IST)
FETCHED = datetime(2026, 10, 19, 14, 2, tzinfo=IST).timestamp()
US = {"rows": [row("AAPL", "Apple Inc.", 231.4, 3.2, 24.1, 2.4), row("MSFT", "Microsoft Corporation", 410.0, -1.1, 18.0, 2.1), row("NVDA", "NVIDIA Corporation", 1130.55, 5.5, 90.2, 3.0),
               row("JUNK", "Junk Inc", 1.0, -4.0, -30.0, 0.1, eligible=False), row("ERR", "Err", None, None, None, error="x"), row("TSLA", "Tesla, Inc.", 180.0, -3.4, 5.0, 1.0)],
      "total": 6, "pending": 0, "fetched_at": FETCHED}


def test_us_leaders_are_ranked_by_momentum_with_dollar_formatting_and_no_indian_text():
    out = ask_info.compose_market_top({"market": "US", "kind": "leaders", "n": 3}, US, NOW, regime={"regime": "risk_off"})
    assert out["say"] == ("The top 3 US stocks by momentum right now: NVDA (NVIDIA), up 90 percent over 12 months, at 1,130.55 dollars; "
                          "AAPL (Apple), up 24 percent over 12 months, at 231.40 dollars; MSFT (Microsoft), up 18 percent over 12 months, at 410.00 dollars. "
                          "Ranked by momentum, the Screener's own measure; not market size. These are the Screener's rankings, not recommendations. As of 14:02 IST, prices delayed.")
    assert [f["label"] for f in out["facts"]] == ["NVDA · NVIDIA", "AAPL · Apple", "MSFT · Microsoft"]
    assert out["facts"][0] == {"label": "NVDA · NVIDIA", "value": "+90.2% 12m · $1,130.55", "tone": "pos"} and out["loading"] is False
    assert "risk-off" not in out["say"] and "stance" not in out["say"] and "Indian" not in out["say"]
    assert "JUNK" not in out["say"] and "ERR" not in out["say"]                       # not eligible / no price


def test_gainers_and_losers_use_the_one_day_change_and_skip_the_wrong_sign():
    g = ask_info.compose_market_top({"market": "US", "kind": "gainers", "n": 5}, US, NOW)
    assert g["say"].startswith("The top 2 US gainers today: NVDA (NVIDIA), up 5.5 percent today, at 1,130.55 dollars; AAPL (Apple), up 3.2 percent today, at 231.40 dollars.")
    assert [f["value"] for f in g["facts"]] == ["+5.5% today · $1,130.55", "+3.2% today · $231.40"] and all(f["tone"] == "pos" for f in g["facts"])
    lo = ask_info.compose_market_top({"market": "US", "kind": "losers", "n": 3}, US, NOW)
    assert lo["say"].startswith("The biggest 3 US losers today: JUNK (Junk), down 4.0 percent today, at 1.00 dollars; TSLA (Tesla), down 3.4 percent today, at 180.00 dollars; MSFT (Microsoft), down 1.1 percent today")
    assert all(f["tone"] == "neg" for f in lo["facts"])
    flat = ask_info.compose_market_top({"market": "US", "kind": "gainers"}, {"rows": [row("A", "A Inc", 1.0, -1.0, 0.0, 1.0)], "total": 1, "pending": 0, "fetched_at": FETCHED}, NOW)
    assert flat["say"].startswith("No US stock is up today.") and flat["facts"] == []


def test_uk_pounds_and_japan_whole_yen_and_india_rupees():
    uk = {"rows": [row("AZN.L", "AstraZeneca PLC", 112.5, 2.0, 8.0, 1.0), row("SHEL.L", "Shell plc", 27.46, 1.0, 4.0, 0.5)], "total": 2, "pending": 0, "fetched_at": FETCHED}
    out = ask_info.compose_market_top({"market": "UK", "kind": "gainers", "n": 3}, uk, NOW)
    assert out["say"].startswith("The top 2 UK gainers today: AZN.L (AstraZeneca), up 2.0 percent today, at 112.50 pounds; SHEL.L (Shell), up 1.0 percent today, at 27.46 pounds.")
    assert out["facts"][0]["value"] == "+2.0% today · £112.50" and out["facts"][1]["value"] == "+1.0% today · £27.46"
    jp = {"rows": [row("7203.T", "Toyota Motor Corporation", 2815.4, 1.5, 12.0, 1.0), row("6758.T", "Sony Group", 14250.0, 0.4, 30.0, 2.0)], "total": 2, "pending": 0, "fetched_at": FETCHED}
    out = ask_info.compose_market_top({"market": "JP", "kind": "leaders", "n": 3}, jp, NOW)
    assert out["say"].startswith("The top 2 Japanese stocks by momentum right now: 6758.T (Sony Group), up 30 percent over 12 months, at 14,250 yen; 7203.T (Toyota Motor), up 12 percent over 12 months, at 2,815 yen.")
    assert out["facts"][0]["value"] == "+30.0% 12m · ¥14,250" and out["facts"][1]["value"] == "+12.0% 12m · ¥2,815"
    ind = {"rows": [row("RELIANCE", "Reliance Industries", 123456.5, 1.0, 10.0, 1.0), row("TCS", "Tata Consultancy Services", 3500.0, 0.2, 5.0, 0.2)], "total": 2, "pending": 0, "fetched_at": FETCHED}
    out = ask_info.compose_market_top({"market": "IN", "kind": "leaders"}, ind, NOW, regime={"regime": "risk_off"})
    assert "RELIANCE (Reliance Industries), up 10 percent over 12 months, at 1.23 lakh rupees" in out["say"] and out["facts"][0]["value"] == "+10.0% 12m · ₹1,23,456.50"
    assert out["say"].count("The agent's risk-off stance applies to Indian buying only.") == 1
    assert "stance" not in ask_info.compose_market_top({"market": "IN", "kind": "leaders"}, ind, NOW, regime={"regime": "risk_on"})["say"]


def test_loading_partial_and_stale_tables():
    for mk, adj, tab in (("US", "US", "United States"), ("UK", "UK", "United Kingdom"), ("JP", "Japanese", "Japan"), ("IN", "Indian", "India")):
        out = ask_info.compose_market_top({"market": mk}, {"rows": [], "total": 0, "pending": 0, "fetched_at": None}, NOW)
        assert out == {"say": f"The {adj} list is loading, about 2 minutes. Ask again shortly, or open the Screener and pick {tab}.", "facts": [], "loading": True}
    nothing = ask_info.compose_market_top({"market": "US"}, {"rows": [row("X", "X", None, None, None)], "total": 1, "pending": 0}, NOW)
    assert nothing["loading"] is True
    part = ask_info.compose_market_top({"market": "US", "kind": "leaders"}, {**US, "pending": 4, "total": 10}, NOW)
    assert "The list is still filling (4 of 10 stocks to come), so this is partial." in part["say"]
    old = ask_info.compose_market_top({"market": "US", "kind": "leaders"}, {**US, "fetched_at": datetime(2026, 10, 15, 9, 30, tzinfo=IST).timestamp()}, NOW)
    assert old["say"].endswith("As of 09:30 IST on 15 Oct, prices delayed.")
    none_eligible = ask_info.compose_market_top({"market": "US", "kind": "leaders"}, {"rows": [row("A", "A", 1.0, 1.0, 1.0, 0.1, eligible=False)], "total": 1, "pending": 0, "fetched_at": FETCHED}, NOW)
    assert none_eligible["say"].startswith("No US stock passes the Screener's momentum filter yet.")
    odd = ask_info.compose_market_top({"market": "ZZ", "kind": "x", "n": "bad"}, US, NOW)
    assert odd["say"].startswith("The top ") and "Indian" in odd["say"]


# ---- the route: fake services, a table is never refreshed because someone asked ----
class FakeSvc:
    def __init__(self, table, running=False):
        self.table, self.running, self.started, self.peeked = table, running, [], []

    def peek(self, key):
        self.peeked.append(key)
        return self.table

    def is_running(self, key=None):
        return self.running

    def start(self, key):
        self.started.append(key)
        self.running = True


def test_the_route_answers_from_a_table_without_starting_anything(server, monkeypatch):  # noqa: F811
    base, app = server
    app._ask_limit = None
    monkeypatch.setattr(app, "demo_trades", None)
    monkeypatch.setattr(app, "clock", lambda: NOW)
    svc = FakeSvc(US)
    monkeypatch.setattr(app, "foreign_screener", lambda key: svc)
    out = _post(base + "/api/ask", {"text": "which are the top US stocks"})[1]
    assert out["intent"] == "market_top" and out["args"] == {"market": "US", "kind": "leaders", "n": 5} and out["loading"] is False
    assert out["say"].startswith("The top 4 US stocks by momentum right now: NVDA") and "stance" not in out["say"] and svc.started == []
    stale = FakeSvc({**US, "fetched_at": time.time() - 40 * 3600})     # a stale but present table is still answered, never refreshed
    monkeypatch.setattr(app, "foreign_screener", lambda key: stale)
    assert "As of " in _post(base + "/api/ask", {"text": "us gainers today"})[1]["say"] and stale.started == []


def test_an_empty_table_starts_the_fill_once_and_says_it_is_loading(server, monkeypatch):  # noqa: F811
    base, app = server
    app._ask_limit = None
    monkeypatch.setattr(app, "demo_trades", None)
    empty = FakeSvc({"rows": [], "total": 0, "pending": 0, "fetched_at": None})
    monkeypatch.setattr(app, "foreign_screener", lambda key: empty)
    monkeypatch.setattr(app, "_screener_foreign", {})
    first = _post(base + "/api/ask", {"text": "top 10 usa stocks"})[1]
    assert first["say"] == "The US list is loading, about 2 minutes. Ask again shortly, or open the Screener and pick United States." and first["loading"] is True
    assert empty.started == ["SP500_US"]
    _post(base + "/api/ask", {"text": "top 10 usa stocks"})                       # asked again while it runs: not started twice
    assert empty.started == ["SP500_US"]
    jp = FakeSvc({"rows": [], "total": 0, "pending": 0, "fetched_at": None}, running=True)   # another list is already filling: one at a time
    monkeypatch.setattr(app, "foreign_screener", lambda key: jp)
    assert "loading" in _post(base + "/api/ask", {"text": "top japanese stocks"})[1]["say"] and jp.started == []


def test_india_prefers_the_biggest_existing_table_and_the_sample_has_none(server, monkeypatch):  # noqa: F811
    base, app = server
    app._ask_limit = None
    ind = {"rows": [row("TCS", "Tata Consultancy Services", 3500.0, 0.2, 5.0, 0.2)], "total": 1, "pending": 0, "fetched_at": FETCHED}
    tables = {"NIFTY200": {"rows": [], "total": 0, "pending": 0, "fetched_at": None}, "NIFTY100": ind}
    fake = type("Svc", (), {"peek": lambda self, k: tables.get(k, {"rows": [], "total": 0, "pending": 0, "fetched_at": None}), "is_running": lambda self, k=None: False, "start": lambda self, k: None})()
    monkeypatch.setattr(app, "_screener", fake)
    monkeypatch.setattr(app, "demo_trades", None)
    out = _post(base + "/api/ask", {"text": "top stocks"})[1]
    assert out["say"].startswith("The top 1 Indian stocks by momentum right now: TCS (Tata Consultancy Services)")
    monkeypatch.setattr(app, "demo_trades", [object()])
    assert _post(base + "/api/ask", {"text": "top us stocks"})[1]["say"] == "The Screener needs live market data, which the offline sample does not use."


def test_ideas_say_they_are_for_indian_stocks():
    out = ask_info.compose_ideas({"regime": {"regime": "risk_off", "score": -2, "guidance": "No new buys."}, "recs": [], "picks": []})
    assert out["say"].startswith("For Indian stocks, the agent's market stance is Risk-off, score minus 2.")
    assert ask_info.compose_ideas({})["say"].startswith("For Indian stocks, I do not have the agent's market stance right now.")


def test_screener_peek_never_starts_a_fill_and_reports_missing_and_the_time():
    from trading_agent.screener import ScreenerService

    class P:
        def history(self, *a, **k):
            raise AssertionError("peek must not fetch")
        history_ohlc = history_intraday = history
    svc = ScreenerService(prices=P(), fundamentals=object(), load_universe=lambda k: (_ for _ in ()).throw(AssertionError("no universe fetch")), clock=lambda: 1000.0)
    assert svc.peek("NIFTY50") == {"universe": "NIFTY50", "rows": [], "total": 0, "pending": 0, "loading": False, "fetched_at": None}
    svc._universes["NIFTY50"] = (1000.0, [{"symbol": "AAA", "name": "A", "industry": ""}, {"symbol": "BBB", "name": "B", "industry": ""}])
    svc._metrics["AAA"] = (990.0, {"price": 10.0, "chg_1d": 1.0, "stats": {}})
    snap = svc.peek("NIFTY50")
    assert snap["total"] == 2 and snap["pending"] == 1 and snap["fetched_at"] == 990.0 and snap["loading"] is False and len(snap["rows"]) == 2 and snap["rows"][0]["price"] == 10.0
    assert svc._running == {}                                                        # nothing was started


def test_the_voice_card_shows_a_retry_for_a_slow_answer_and_chips_for_the_list():
    js = (UI / "voice.js").read_text(encoding="utf-8")
    assert 'err.kind === "net" || err.kind === "ask"' in js and "submit(lastQ)" in js and '"Open " + w + " screener"' in js and "r.loading" in js
    ask_js = (UI / "ask.js").read_text(encoding="utf-8")
    assert 'out.intent === "market_top"' in ask_js and 'r.intent === "market_top"' in ask_js and "timed(\"/api/ask\", {text}, 20000" in ask_js
