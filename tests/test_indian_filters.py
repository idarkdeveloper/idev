"""Indian-market filters (indian_filters.py): ASM/GSM, results blackout and promoter pledges block or flag BUYS only.
Fake NSE answers, a fixed clock and a fake holiday list: each rule, point-in-time by filing time (the 15:00 rule),
stale and missing data (fail open, with a note), flags on holdings, sells never blocked, no order path touched."""
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path


from trading_agent import indian_filters as fi
from trading_agent.agent import AgentContext, RunResult, build_tools
from trading_agent.broker import LocalPaperBroker
from trading_agent.state import State
from trading_agent.timezones import IST

from .test_ui import _post, server, two_pages  # noqa: F401  (fixtures)

MON = datetime(2026, 10, 12, 10, 0, tzinfo=IST)     # a Monday


class Cal:
    def __init__(self, closed=()):
        self.closed = set(closed)

    def is_trading_day(self, d):
        return d.weekday() < 5 and d not in self.closed


ASM = {"longterm": {"data": [{"symbol": "LTX", "asmSurvIndicator": "Stage II"}]},
       "shortterm": {"data": [{"symbol": "STX", "asmSurvIndicator": "Stage I"}]}}
GSM = {"data": [{"symbol": "GSX", "gsmStage": "Stage 2"}]}
MEETINGS = [
    {"bm_symbol": "RES", "bm_date": "16-Oct-2026", "bm_purpose": "Financial Results", "bm_desc": "Q2", "bm_timestamp": "05-Oct-2026 10:00:00"},
    {"bm_symbol": "FAR", "bm_date": "21-Oct-2026", "bm_purpose": "Financial Results", "bm_timestamp": "05-Oct-2026 10:00:00"},   # 7 trading days on
    {"bm_symbol": "DIV", "bm_date": "13-Oct-2026", "bm_purpose": "Dividend", "bm_timestamp": "05-Oct-2026 10:00:00"},          # not results
    {"bm_symbol": "LATE", "bm_date": "14-Oct-2026", "bm_purpose": "To consider financial results", "bm_timestamp": "12-Oct-2026 15:30:00"},
    {"bm_symbol": "EARLY", "bm_date": "14-Oct-2026", "bm_purpose": "Financial Results", "bm_timestamp": "12-Oct-2026 09:30:00"},
    {"bm_symbol": "PAST", "bm_date": "09-Oct-2026", "bm_purpose": "Financial Results", "bm_timestamp": "01-Oct-2026 10:00:00"},
]
PLEDGES = {"data": [{"symbol": "HIGH", "percPromoterShares": "34.5", "broadcastDt": "01-Oct-2026 11:00:00"},
                    {"symbol": "LOW", "percPromoterShares": "8", "broadcastDt": "01-Oct-2026 11:00:00"},
                    {"symbol": "NEWHIGH", "percPromoterShares": "40", "broadcastDt": "12-Oct-2026 16:00:00"},
                    {"symbol": "BAD", "percPromoterShares": "n/a"}]}


class FakeNSE:
    """Answers NSEClient._get from a dict path -> payload (or an exception). Records every call."""

    def __init__(self, answers=None):
        self.answers = answers if answers is not None else {"api/reportASM": ASM, "api/reportGSM": GSM,
                                                            "api/corporate-board-meetings": MEETINGS, "api/corporate-pledgedata": PLEDGES}
        self.calls = []

    def _get(self, path, params=None, referer=None):
        self.calls.append(path)
        a = self.answers[path]
        if isinstance(a, BaseException):
            raise a
        return a


def book(tmp_path, now=MON, cal=None, **on):
    enabled = {p: on.get(p, True) for p in fi.PARTS}
    return fi.FilterBook(tmp_path, enabled=enabled, now_fn=lambda: now, calendar=cal or Cal())


def filled(tmp_path, now=MON, **kw):
    b = book(tmp_path, now, **kw)
    assert b.tick(FakeNSE(), now) == {"asm": 2, "gsm": 1, "results": 5, "pledge": 3}
    return b


# ---------- parsing ----------
def test_parsers_read_nse_shapes_and_skip_what_they_cannot():
    assert fi.parse_asm(ASM) == {"LTX": "ASM long-term Stage II", "STX": "ASM short-term Stage I"}
    assert fi.parse_asm([{"symbol": "Z", "stage": "I"}]) == {"Z": "ASM I"}
    assert fi.parse_gsm(GSM) == {"GSX": "GSM Stage 2"} and fi.parse_gsm({"nothing": 1}) == {}
    m = fi.parse_meetings(MEETINGS)
    assert "DIV" not in m and m["RES"][0]["date"] == "2026-10-16" and m["LATE"][0]["filed"].startswith("2026-10-12T15:30")
    p = fi.parse_pledges(PLEDGES)
    assert p["HIGH"]["pct"] == 34.5 and "BAD" not in p


# ---------- each rule ----------
def test_asm_and_gsm_block_buys(tmp_path):
    b = filled(tmp_path)
    for sym in ("LTX", "STX", "GSX"):
        assert b.check(sym)["block"], sym
    assert "Additional Surveillance Measure" in b.refuse_buy("ltx") and b.refuse_buy("ltx").startswith("LTX:")
    assert "Graded" in b.refuse_buy("GSX.NS")
    assert b.refuse_buy("TCS") is None and b.check("TCS") == {"block": False, "reasons": [], "flags": [], "results_due": None}


def test_results_blackout_five_trading_days_point_in_time(tmp_path):
    b = filled(tmp_path)
    r = b.check("RES")                                  # Fri 16 Oct is the 4th trading day after Monday
    assert r["block"] and r["results_due"] == "2026-10-16" and "Results due 16 Oct" in r["flags"]
    assert not b.check("FAR")["block"]                  # Wed 21 Oct: 7 trading days on
    assert not b.check("DIV")["block"] and not b.check("PAST")["block"]
    assert b.check("EARLY")["block"]                    # notice disseminated 09:30 today: usable today
    assert not b.check("LATE")["block"]                 # disseminated 15:30 today: usable only tomorrow
    tue = filled(tmp_path / "t", now=MON + timedelta(days=1))
    assert tue.check("LATE")["block"]
    # a holiday inside the window stretches it: with Thursday and Friday shut, Wed 21 Oct is 5 trading days on
    hol = filled(tmp_path / "h", cal=Cal({date(2026, 10, 15), date(2026, 10, 16)}))
    assert hol.check("FAR")["block"]


def test_pledges_above_twenty_percent_or_rising_five_points(tmp_path):
    b = filled(tmp_path)
    assert b.check("HIGH")["block"] and "34.5%" in b.refuse_buy("HIGH")
    low = b.check("LOW")
    assert not low["block"] and low["flags"] == ["Pledge 8%"]                    # shown on a holding, never a block
    assert not b.check("NEWHIGH")["block"]                                       # filed 16:00 today: not usable until tomorrow
    # a rise of more than 5 points between two filings blocks, even under 20%
    nxt = book(tmp_path, now=MON + timedelta(days=3))
    nxt.tick(FakeNSE({**FakeNSE().answers, "api/corporate-pledgedata":
                      {"data": [{"symbol": "LOW", "percPromoterShares": "15", "broadcastDt": "14-Oct-2026 10:00:00"}]}}), MON + timedelta(days=3))
    r = nxt.check("LOW")
    assert r["block"] and "rose 7.0 percentage points" in r["reasons"][0] and r["flags"] == ["Pledge +7 pts"]


def test_switches_turn_each_filter_off(tmp_path):
    filled(tmp_path)
    b = book(tmp_path, asm=False, gsm=False, results=False, pledge=False)
    for sym in ("LTX", "GSX", "RES", "HIGH"):
        assert not b.check(sym)["block"]
    assert b.note() is None and not b.active()


# ---------- fetching, stale and missing data ----------
def test_tick_fetches_once_a_day_with_back_off_and_fails_open(tmp_path):
    b = book(tmp_path)
    nse = FakeNSE({**FakeNSE().answers, "api/reportGSM": ConnectionError("NSE down")})
    got = b.tick(nse, MON)
    assert "gsm" not in got and set(got) == {"asm", "results", "pledge"}
    assert b.note() == "filter data unavailable: ASM/GSM (buys are held back only by the other rules)"
    assert not b.check("GSX")["block"] and b.check("LTX")["block"]              # the ASM list still works
    assert b.due(MON + timedelta(minutes=1)) == []                              # waits 5 minutes after a failure
    assert b.due(MON + timedelta(minutes=6)) == ["gsm"]
    assert b.tick(FakeNSE(), MON + timedelta(minutes=6)) == {"gsm": 1}
    calls = FakeNSE()
    assert b.tick(calls, MON + timedelta(hours=1)) == {} and calls.calls == []   # all stored for today
    assert book(tmp_path).due(datetime(2026, 10, 10, 10, 0, tzinfo=IST)) == []   # Saturday: nothing


def test_tries_are_capped_and_a_stale_list_blocks_nothing(tmp_path):
    b = book(tmp_path)
    down = FakeNSE({k: ConnectionError("x") for k in FakeNSE().answers})
    t = MON
    for _ in range(fi.MAX_TRIES):
        b.tick(down, t)
        t += timedelta(hours=2)
    assert b.due(t) == [] and len(down.calls) == fi.MAX_TRIES * 4
    filled(tmp_path / "s")
    later = book(tmp_path / "s", now=datetime(2026, 10, 15, 10, 0, tzinfo=IST))   # three trading days on
    assert not later.check("LTX")["block"] and "ASM/GSM" in later.note()
    assert book(tmp_path / "none").note().startswith("filter data unavailable")  # nothing stored at all
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / fi.FILE).write_text("{not json")
    assert not book(tmp_path / "bad").check("LTX")["block"]                      # unreadable: never a crash


def test_filters_for_reads_the_settings_and_is_india_only(settings):
    settings.market = "in"
    settings.pledge_filter = False
    b = fi.filters_for(settings, calendar=Cal())
    assert b.enabled == {"asm": True, "gsm": True, "results": True, "pledge": False}
    settings.market = "us"
    assert not any(fi.filters_for(settings, calendar=Cal()).enabled.values())


# ---------- where buys happen ----------
def test_screen_excludes_flagged_stocks_and_says_why(tmp_path):
    from trading_agent.screen import run_screen

    from .test_marketdata2 import Prices
    b = filled(tmp_path)
    b._today = lambda: MON.date()
    members = [{"symbol": s, "name": s, "industry": ""} for s in ("AAA", "BBB", "CCC")]
    real_check = b.check
    b.check = lambda sym: {"block": True, "reasons": ["on NSE's Additional Surveillance Measure list (ASM long-term Stage II); not bought"],
                           "flags": ["ASM long-term Stage II"], "results_due": None} if sym == "AAA" else real_check(sym)
    res = run_screen(members, Prices(), top=10, min_turnover=0, filters=b)
    by = {r["symbol"]: r for r in res["all"]}
    assert res["filter_skipped"] == 1 and res["filters_applied"] and res["filters_note"] is None
    assert by["AAA"]["filter_skipped"] and "Surveillance" in by["AAA"]["filter_note"] and by["AAA"]["filter_flags"] == ["ASM long-term Stage II"]
    assert "AAA" not in {r["symbol"] for r in res["top"]} and {"BBB", "CCC"} <= {r["symbol"] for r in res["top"]}
    assert run_screen(members, Prices(), top=10, min_turnover=0)["filter_skipped"] == 0                 # off by default


def test_agent_buys_are_refused_but_sells_never(settings, tmp_path):
    settings.market, settings.auto_trade = "in", True
    filled(settings.state_dir)
    broker = LocalPaperBroker(settings.state_dir / "pb.json", starting_cash=1_000_000, price_fn=lambda s: 100.0)
    broker.submit_order("LTX", "buy", qty=10)
    st = State(settings.state_dir / "state.json")
    from trading_agent.notify import Notifier

    class Quiet(Notifier):
        def __init__(self):
            self.sent = []

        def send(self, subject, body, html=None):
            self.sent.append(subject)
            return ["console"]
    ctx = AgentContext(settings=settings, broker=broker, data=None, notifier=Quiet(), state=st, result=RunResult(investor="x", new_trades=[]))
    tools = {t.name: t for t in build_tools(ctx)}
    import trading_agent.indian_filters as mod
    real = mod.filters_for
    mod.filters_for = lambda s, now_fn=None, calendar=None: real(s, now_fn=lambda: MON, calendar=Cal())
    try:
        rec = json.loads(tools["send_recommendation"].call({"action": "buy", "ticker": "LTX", "headline": "h", "rationale": "r",
                                                           "confidence": "high"}))
        assert "Surveillance Measure" in rec["error"]
        order = json.loads(tools["place_paper_order"].call({"symbol": "LTX", "side": "buy", "notional_usd": 1000}))
        assert "Surveillance Measure" in order["error"]
        sell = json.loads(tools["place_paper_order"].call({"symbol": "LTX", "side": "sell", "notional_usd": 500}))
        assert "error" not in sell and sell["order"]["status"] == "filled"           # a sell is never blocked
        ok = json.loads(tools["send_recommendation"].call({"action": "sell", "ticker": "LTX", "headline": "h", "rationale": "r",
                                                          "confidence": "high"}))
        assert "error" not in ok
    finally:
        mod.filters_for = real


def test_practice_buy_warns_and_can_be_overridden_and_sells_are_never_blocked(two_pages, monkeypatch):  # noqa: F811
    base, app, fake, settings = two_pages
    filled(settings.state_dir)
    import trading_agent.indian_filters as mod
    real = mod.filters_for
    monkeypatch.setattr(mod, "filters_for", lambda s, now_fn=None, calendar=None: real(s, now_fn=lambda: MON, calendar=Cal()))
    monkeypatch.setattr(app.demo.practice_broker, "price_fn", lambda s: 100.0)
    status, j = _post(base + "/demo/api/order", {"symbol": "GSX", "side": "buy", "qty": 5})
    assert status == 409 and j["filter_warning"] and "Graded Surveillance" in j["error"]
    status, j = _post(base + "/demo/api/order", {"symbol": "GSX", "side": "buy", "qty": 5, "override_filters": True})
    assert status == 200 and j["order"]["status"] == "filled"
    status, j = _post(base + "/demo/api/order", {"symbol": "GSX", "side": "sell", "qty": 5})
    assert status == 200                                                          # sells: never asked
    status, j = _post(base + "/demo/api/order", {"symbol": "GSX", "side": "buy", "qty": 5, "override_filters": "yes"})
    assert status == 409                                                          # only a real true overrides


def test_holdings_get_review_flags_in_the_email_never_a_sell(tmp_path):
    from trading_agent.digest import filter_reasons
    from trading_agent.digest_render import _flags
    b = filled(tmp_path)
    assert filter_reasons(b, "LTX", []) == ["on NSE's surveillance list (ASM long-term Stage II): review it; never sold for this"]
    assert filter_reasons(b, "RES", []) == ["results due 16 Oct"] and filter_reasons(b, "RES", ["Results due 16 Oct"]) == []
    assert filter_reasons(b, "HIGH", []) == ["promoter pledge warning (34%)"] and filter_reasons(b, "LOW", []) == []
    assert _flags({"reasons": filter_reasons(b, "LTX", []) + filter_reasons(b, "HIGH", [])}) == "ASM/GSM, pledge"


def test_forward_test_records_when_the_filters_started(tmp_path):
    from trading_agent.forward import ForwardTest, format_forward
    ft = ForwardTest(tmp_path, universe="NIFTY50", capital=100000.0, price_fn=lambda s: 100.0)
    ft.now = lambda: MON
    ft.run(lambda: {"top": [], "eligible": 0, "filters_applied": True}, force_rebalance=True)
    assert ft.data["indian_filters_since"] == "2026-10-12"
    assert "results-blackout and pledge filters on since 2026-10-12" in format_forward(ft.summary())


def test_the_module_never_touches_an_order_path():
    src = (Path(fi.__file__)).read_text(encoding="utf-8")
    code = re.sub(r'"""[\s\S]*?"""', "", src)
    for word in (r"submit_order", r"cancel_", r"modify_", r"\bplace_", r"\.sell\(", r"GrowwBroker", r"make_broker", r"\.post\("):
        assert not re.search(word, code), word
    assert "client._get(" in code   # reads go through the NSE client only


def test_portfolio_rows_and_the_stock_page_carry_the_flags(server, monkeypatch):  # noqa: F811
    from .test_ui import _get
    base, app = server
    filled(app.settings.state_dir)
    import trading_agent.indian_filters as mod
    real = mod.filters_for
    monkeypatch.setattr(mod, "filters_for", lambda s, now_fn=None, calendar=None: real(s, now_fn=lambda: MON, calendar=Cal()))
    monkeypatch.setattr("trading_agent.ui.read_groww_portfolio", lambda *a, **k: {
        "linked": True, "at": "2026-10-12T10:00:00+05:30", "holdings": [{"symbol": "LTX"}, {"symbol": "RES"}, {"symbol": "TCS"}]})
    _, mp = _get(base + "/api/my-portfolio?refresh=1")
    assert mp["filter_flags"] == {"LTX": ["ASM long-term Stage II"], "RES": ["Results due 16 Oct"]}
    page = (Path(__file__).resolve().parents[1] / "trading_agent" / "ui" / "index.html").read_text(encoding="utf-8")
    assert "MP.filter_flags" in page and 'id="f-asm"' in page and 'id="f-results"' in page and 'id="f-pledge"' in page
    sp = (Path(__file__).resolve().parents[1] / "trading_agent" / "ui" / "stockpage.js").read_text(encoding="utf-8")
    assert "r.filter_block" in sp and "r.filters_note" in sp


# ---------- NSE's real answer shapes ----------
FX = Path(__file__).parent / "fixtures"
EQUITY = {"CASTROLIND": "Castrol India Limited", "IDFCFIRSTB": "IDFC First Bank Limited", "CRISIL": "CRISIL Limited",
          "CGCL": "Capri Global Capital Limited", "TWINA": "Twin Co Limited", "TWINB": "Twin Co Ltd"}


class RecordingNSE(FakeNSE):
    def __init__(self, answers=None):
        super().__init__(answers)
        self.params = {}

    def _get(self, path, params=None, referer=None):
        self.params[path] = params
        return super()._get(path, params, referer)


def real_answers():
    return {**FakeNSE().answers, "api/corporate-board-meetings": json.loads((FX / "nse_board_meetings_sample.json").read_text()),
            "api/corporate-pledgedata": json.loads((FX / "nse_pledge_sample.json").read_text())}


def test_board_meetings_are_asked_for_by_meeting_date_two_weeks_ahead(tmp_path):
    nse = RecordingNSE(real_answers())
    b = book(tmp_path)
    got = b.tick(nse, MON, names=EQUITY)
    assert nse.params["api/corporate-board-meetings"] == {"index": "equities", "from_date": "12-10-2026", "to_date": "26-10-2026"}
    m = b.load()["results"]["symbols"]
    assert set(m) == {"CASTROLIND", "IDFCFIRSTB", "CRISIL"} and got["results"] == 3          # fund raising is not results
    # "Board Meeting Intimation" rows whose text says results count too (CASTROLIND's financial-results notice is there)
    assert {x["date"] for x in m["CASTROLIND"]} == {"2026-10-24"}


def test_pledge_rows_name_the_company_and_are_mapped_to_symbols(tmp_path):
    p = fi.parse_pledges(json.loads((FX / "nse_pledge_sample.json").read_text()), EQUITY)
    assert p == {"CASTROLIND": {"pct": 25.0, "filed": "2026-10-07T18:05:12"}, "IDFCFIRSTB": {"pct": 0.0, "filed": "2026-09-29T17:00:00"},
                 "CRISIL": {"pct": 5.0, "filed": "2026-10-01T16:31:00"}}                       # "CRISIL Ltd." = "CRISIL Limited"
    assert fi.norm_name("The Tata Power Company Ltd.") == "tata power" and fi.norm_name("M&M Financial") == "m and m financial"
    assert "twin" not in fi.name_index(EQUITY)                                                  # two symbols share it: not guessed
    b = book(tmp_path)
    b.tick(RecordingNSE(real_answers()), MON, names=lambda: EQUITY)
    assert b.check("CASTROLIND")["block"] and "25.0%" in b.refuse_buy("CASTROLIND")
    assert not b.check("CRISIL")["block"] and b.check("CRISIL")["flags"] == ["Pledge 5%"]


def test_an_empty_answer_is_unavailable_data_not_an_empty_list(tmp_path):
    b = book(tmp_path)
    got = b.tick(RecordingNSE({**real_answers(), "api/corporate-pledgedata": {"comNameList": [], "data": []}}), MON, names=EQUITY)
    assert "pledge" not in got and b.load().get("pledge") is None
    assert b.note() == "filter data unavailable: promoter pledges (buys are held back only by the other rules)"
    assert "no usable rows" in b.load()["tries"]["2026-10-12"]["errors"]["pledge"]
    # names are only looked up when a row has no symbol
    called = []
    b2 = book(tmp_path / "x")
    b2.tick(FakeNSE(), MON, names=lambda: called.append(1) or {})
    assert called == []


def test_nse_publishing_no_pledges_is_named_and_the_last_list_stays_in_use(tmp_path):
    """From 16 Oct 2026 NSE's pledge list was empty on NSE's own page (its count read 0 companies). The error says so,
    and the last good pledge list keeps the check working for 10 trading days, while the 2-day rule stays for the rest."""
    b = book(tmp_path)
    empty = {**FakeNSE().answers, "api/corporate-pledgedata": {"comNameList": [], "data": []},
             fi.PLEDGE_COUNT_PATH: {"data": [{"NUMBER_OF_COMPANY": "0", "VALUE_OF_PLEDGE_HOLDING": None}]}}
    b.tick(FakeNSE(empty), MON)
    err = b.load()["tries"]["2026-10-12"]["errors"]["pledge"]
    assert "NSE is publishing no pledge data right now" in err and "10 trading days" in err
    unreadable = {**empty, fi.PLEDGE_COUNT_PATH: ConnectionError("x")}
    b2 = book(tmp_path / "u")
    b2.tick(FakeNSE(unreadable), MON)
    assert "no usable rows" in b2.load()["tries"]["2026-10-12"]["errors"]["pledge"]   # count unknown: the plain error
    filled(tmp_path / "s")                                                             # every list stored on Mon 12 Oct
    five_on = book(tmp_path / "s", now=datetime(2026, 10, 19, 10, 0, tzinfo=IST))     # five trading days on
    assert five_on.check("HIGH")["block"] and "promoter pledges" not in (five_on.note() or "")
    assert "ASM/GSM" in five_on.note()                                                # the other lists still go stale at 2
    eleven_on = book(tmp_path / "s", now=datetime(2026, 10, 27, 10, 0, tzinfo=IST))   # eleven trading days on
    assert not eleven_on.check("HIGH")["block"] and "promoter pledges" in eleven_on.note()
