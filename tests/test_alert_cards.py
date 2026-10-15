"""The HTML cards of the deal-recommendation and announcement emails. Fakes only, no network."""
import json
from types import SimpleNamespace as NS

from trading_agent import alert_cards as ac
from trading_agent.broker import LocalPaperBroker
from trading_agent.notify import Notifier


def deal(investor, qty, price=1824.0, ticker="AXISCADES"):
    return NS(ticker=ticker, investor=investor, transaction="Purchase", transaction_date="2026-09-29", size="",
              raw={"qty": qty, "watp": price} if price else {"qty": qty})


ABAKKUS = [deal("ABAKKUS DIVERSIFIED ALPHA FUND", 600000), deal("ABAKKUS INVESTMENT MANAGERS", "4,00,000"),
           deal("ABAKKUS MUTUAL FUND", 290000), deal("SOMEONE ELSE LLP", 999999), deal("ABAKKUS MUTUAL FUND", 5, ticker="TCS")]
REC = {"action": "watch", "ticker": "AXISCADES", "headline": "ABAKKUS bought ~1.29M AXISCADES shares",
       "rationale": "Large, researched buy. Momentum is strong.", "confidence": "medium",
       "suggested_notional_usd": 0, "investor": "ABAKKUS"}
DOWN = {"regime": "risk_off", "markets": {"nifty50": {"above_200dma": False, "ret_20d": -0.039}}}


# --------------------------------------------------------------------------- #
# facts
# --------------------------------------------------------------------------- #
def test_deal_facts_add_up_the_followed_investors_rows_for_the_ticker():
    d = ac.deal_facts(ABAKKUS, "AXISCADES", "ABAKKUS")
    assert d == {"shares": 1290000.0, "price": 1824.0, "value": 1290000 * 1824.0, "entities": 3, "date": "2026-09-29",
                 "side": "Purchase"}
    assert ac.deal_facts(ABAKKUS, "AXISCADES", None)["shares"] == 1290000.0 + 999999   # no investor named: every row
    assert ac.deal_facts(ABAKKUS, "AXISCADES.BO", "ABAKKUS")["shares"] == 1290000.0      # the BSE code is the same stock
    assert ac.deal_facts(ABAKKUS, "INFY", "ABAKKUS") is None
    assert ac.deal_facts([deal("ABAKKUS MUTUAL FUND", None)], "AXISCADES", "ABAKKUS") is None
    unpriced = ac.deal_facts([deal("ABAKKUS MUTUAL FUND", 100, price=None)], "AXISCADES", "ABAKKUS")
    assert unpriced["price"] is None and unpriced["value"] is None and unpriced["shares"] == 100


def test_market_facts():
    assert ac.market_facts(DOWN) == {"regime": "risk_off", "nifty_up": False, "nifty_20d": -0.039, "wait": True}
    up = ac.market_facts({"regime": "risk_on", "markets": {"nifty50": {"above_200dma": True}}})
    assert up["wait"] is False and up["nifty_up"] is True
    assert ac.market_facts({"error": "down"}) == {"regime": None, "nifty_up": None, "nifty_20d": None, "wait": False}
    assert ac.market_facts(None)["wait"] is False


def test_money_formats():
    assert ac.money(1824.0) == "₹1,824" and ac.money(2087.4) == "₹2,087" and ac.money(12.5) == "₹12.50"
    assert ac.money(1234567) == "₹12,34,567" and ac.money(99.0, "USD") == "$99.00"
    assert ac.big_money(1290000 * 1824.0) == "₹235 Cr" and ac.big_money(4.5e5) == "₹4.5 L" and ac.big_money(5e7) == "₹5.0 Cr"
    assert ac.shares(1290000) == "1.29M" and ac.shares(45000) == "45.0K" and ac.shares(1200) == "1,200"


# --------------------------------------------------------------------------- #
# the recommendation card
# --------------------------------------------------------------------------- #
def test_watch_card_has_badges_tiles_and_the_market_filter_callout():
    h = ac.recommendation_card(REC, deal=ac.deal_facts(ABAKKUS, "AXISCADES", "ABAKKUS"), market=ac.market_facts(DOWN),
                               price=2087.0, holding="No current holding · cash ₹96,120")
    assert "AXISCADES · WATCH" in h and "⛔ WATCH ONLY (NO NEW BUYS)" in h
    assert "CONFIDENCE: MEDIUM" in h and "NIFTY TREND: DOWN" in h
    for tile in ("1.29M", "across 3 entities", "₹1,824", "on 29 Sep", "₹235 Cr", "₹2,087", "+14.4% vs deal"):
        assert tile in h, tile
    assert ("⚠️ <b>Market filter:</b> The market regime is risk-off and the Nifty is below its 200-day average "
            "(-3.9% over 20 days), so the rules make no new buys. The price is 14% above the deal price of ₹1,824.") in h
    assert "No current holding · cash ₹96,120 · Investor: ABAKKUS" in h and "Not advice" in h


def test_card_without_facts_and_in_a_rising_market():
    rising = ac.market_facts({"regime": "risk_on", "markets": {"nifty50": {"above_200dma": True}}})
    h = ac.recommendation_card({**REC, "action": "buy", "suggested_notional_usd": 15000}, market=rising)
    assert "AXISCADES · BUY" in h and "NIFTY TREND: UP" in h and "Market filter" not in h
    assert "SHARES" not in h and "Suggested size: ₹15,000" in h
    bare = ac.recommendation_card({**REC, "action": "hold", "confidence": "unknown"})
    assert "HOLD" in bare and "CONFIDENCE" not in bare and "NIFTY TREND" not in bare


def test_card_escapes_the_models_text():
    h = ac.recommendation_card({**REC, "headline": "<script>x</script>", "rationale": "a & <b>b</b>",
                                "investor": '"><img src=x>', "ticker": "<i>"})
    assert "<script>" not in h and "<b>b</b>" not in h and "<img" not in h and "<i>" not in h
    assert "&lt;script&gt;" in h and "a &amp; &lt;b&gt;" in h


# --------------------------------------------------------------------------- #
# announcements
# --------------------------------------------------------------------------- #
ROWS = [{"at": "2026-10-03 17:21:36", "symbol": "VOGL", "category": "Updates",
         "text": "Vedanta Oil and Gas Limited has informed the Exchange regarding ' Production Release Q2 FY27'."},
        {"at": "2026-10-07 20:02:20", "symbol": "ADANIPORTS",
         "category": "Certificate under SEBI (Depositories and Participants) Regulations, 2018",
         "text": "Adani Ports and Special Economic Zone Limited has informed the Exchange about Certificate under SEBI "
                 "(Depositories and Participants) Regulations, 2018"},
        {"at": "2026-10-06 13:57:25", "symbol": "AXISCADES", "category": "Acquisition",
         "text": "AXISCADES Technologies Limited has informed the Exchange about Incorporation of a Subsidiary Company"}]


def test_timeline_newest_first_without_boilerplate_or_repeats():
    h = ac.announcements_card(ROWS)
    assert h.index("ADANIPORTS") < h.index("AXISCADES") < h.index("VOGL")             # newest first
    assert "has informed the Exchange" not in h
    assert "<b>Acquisition:</b> Incorporation of a Subsidiary Company" in h
    assert "<b>Certificate under SEBI (Depositories and Participants) Regulations, 2018</b></div>" in h   # said once
    assert "<b>Updates:</b> Production Release Q2 FY27." in h and "07 Oct 20:02" in h and "3 new announcements" in h


def test_priority_by_kind_of_filing():
    assert ac.priority("Financial Results") == "high" and ac.priority("Acquisition") == "high"
    assert ac.priority("Sale or disposal") == "high" and ac.priority("Credit Rating") == "high"
    assert ac.priority("Trading Window") == "notable" and ac.priority("Board Meeting Intimation") == "notable"
    assert ac.priority("Copy of Newspaper Publication") == "routine"
    assert ac.priority("Certificate under SEBI (Depositories and Participants) Regulations, 2018") == "routine"
    assert ac.priority("Updates", "Outcome of board meeting: financial results") == "high"   # a vague category reads the text


def test_announcements_subject_and_escaping():
    assert ac.announcements_subject(ROWS) == "[NEWS] 3 new announcements: VOGL, ADANIPORTS, AXISCADES"
    many = [{"at": "2026-10-01", "symbol": f"S{i}", "category": "x", "text": "y"} for i in range(6)]
    assert ac.announcements_subject(many) == "[NEWS] 6 new announcements: S0, S1, S2, S3 +2"
    assert ac.announcements_subject(ROWS[:1]) == "[NEWS] 1 new announcement: VOGL"
    h = ac.announcements_card([{"at": "x", "symbol": "<b>", "category": "<script>", "text": "a <img src=x>"}])
    assert "<script>" not in h and "<img" not in h and "&lt;b&gt;" in h
    assert ac.short_text("X has submitted the Exchange a copy Srutinizers report") == "Srutinizers report"


# --------------------------------------------------------------------------- #
# wiring
# --------------------------------------------------------------------------- #
class Recorder(Notifier):
    def __init__(self):
        super().__init__()
        self.kw = []

    def send(self, subject, body, **kw):
        self.kw.append({"subject": subject, "body": body, **kw})
        return ["console"]


class TextOnly:
    def __init__(self):
        self.sent = []

    def send(self, subject, body):
        self.sent.append((subject, body))
        return ["console"]


def test_send_with_html_only_when_the_notifier_takes_it():
    from trading_agent.agent import send_with_html
    r, t = Recorder(), TextOnly()
    send_with_html(r, "s", "b", "<p>h</p>")
    send_with_html(t, "s", "b", "<p>h</p>")
    send_with_html(r, "s2", "b2", None)
    assert r.kw[0]["html"] == "<p>h</p>" and "html" not in r.kw[1] and t.sent == [("s", "b")]


def test_send_recommendation_sends_the_card_with_the_text(settings):
    from trading_agent.agent import AgentContext, RunResult, build_tools
    from trading_agent.state import State
    settings.market, settings.investors = "in", ["ABAKKUS"]
    broker = LocalPaperBroker(settings.state_dir / "pb.json", starting_cash=96_120, price_fn=lambda s: 2087.0)
    rec = Recorder()

    class Ctx:
        def fetch(self):
            return DOWN
    ctx = AgentContext(settings=settings, broker=broker, data=None, notifier=rec,
                       state=State(settings.state_dir / "state.json"),
                       result=RunResult("ABAKKUS", ABAKKUS[:3], investors=["ABAKKUS"]), context=Ctx())
    tool = {t.name: t for t in build_tools(ctx)}["send_recommendation"]
    out = json.loads(tool.call({k: v for k, v in REC.items() if k != "suggested_notional_usd"}))
    assert out["ok"] is True
    (sent,) = rec.kw
    assert sent["body"].startswith("Investor: ABAKKUS\nAction: WATCH AXISCADES (confidence: medium)")   # text unchanged
    assert "₹235 Cr" in sent["html"] and "+14.4% vs deal" in sent["html"] and "Market filter" in sent["html"]
    assert "No current holding · cash ₹96,120" in sent["html"]


def test_a_broken_fact_never_stops_the_alert(settings):
    from trading_agent.agent import AgentContext, RunResult, build_tools
    from trading_agent.state import State

    class Broken:
        name = "local-paper"

        def latest_price(self, s):
            raise RuntimeError("no price")

        def positions(self):
            raise RuntimeError("no positions")

    class Ctx:
        def fetch(self):
            raise RuntimeError("regime down")
    rec = Recorder()
    ctx = AgentContext(settings=settings, broker=Broken(), data=None, notifier=rec,
                       state=State(settings.state_dir / "state.json"), result=RunResult("ABAKKUS", []), context=Ctx())
    tool = {t.name: t for t in build_tools(ctx)}["send_recommendation"]
    assert json.loads(tool.call({k: v for k, v in REC.items() if k != "suggested_notional_usd"}))["ok"] is True
    assert "AXISCADES · WATCH" in rec.kw[0]["html"] and "CURRENT PRICE" not in rec.kw[0]["html"]


def test_watch_sends_the_announcement_timeline(settings):
    from trading_agent.state import State
    from trading_agent.watch import Watcher

    class Data:
        def announcements(self, t, limit=5):
            return [{**a, "id": a["symbol"] + a["at"]} for a in ROWS if a["symbol"] == t]
    st = State(settings.state_dir / "state.json")
    for t in ("VOGL", "AXISCADES"):
        st.record_recommendation({"action": "watch", "ticker": t})
    st.save()
    rec = Recorder()
    w = Watcher(settings, every=60, data=Data(), notifier=rec)
    assert len(w.poll_announcements()) == 2
    (sent,) = rec.kw
    assert sent["subject"] == "[NEWS] 2 new announcements: VOGL, AXISCADES"
    assert "EXCHANGE FILINGS" in sent["html"] and "2026-10-03 17:21:36 VOGL [Updates]" in sent["body"]
