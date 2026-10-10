"""BSE bulk/block deals: form flow, CSV parsing, symbol mapping, cache, keys, wiring. Fakes only, no network.

The fixtures are real public BSE data captured by hand on 10 Oct 2026 (trimmed): the GET form, the
response after Submit, and the first rows of the bulk and block CSVs for 1-9 Oct."""
import hashlib
import json
from datetime import date, datetime
from pathlib import Path

import pytest
import requests

from trading_agent import bse
from trading_agent.bse import (BSEClient, BSELayoutError, IST, ScripResolver, build_download, build_submit,
                               parse_deals_csv, parse_form, to_trade)
from trading_agent.nse import NSEClient, _dedupe, _norm_deal
from .conftest import FakeSession

FX = Path(__file__).resolve().parents[1] / "trading_agent" / "fixtures"
FORM = (FX / "bse_deals_form.html").read_text(encoding="utf-8")
RESULT = (FX / "bse_deals_result.html").read_text(encoding="utf-8")
BULK = (FX / "bse_bulk_sample.csv").read_text(encoding="utf-8")
BLOCK = (FX / "bse_block_sample.csv").read_text(encoding="utf-8")
MONDAY_MORNING = datetime(2026, 10, 12, 10, 0, tzinfo=IST)
MONDAY_EVENING = datetime(2026, 10, 12, 17, 0, tzinfo=IST)
P = "ctl00$ContentPlaceHolder1$"
ALL = (date(2026, 10, 1), date(2026, 10, 9))
N_BULK, N_BLOCK = 28, 12


class Resp:
    def __init__(self, body, status=200):
        self.content = body.encode("utf-8") if isinstance(body, str) else body
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class BseSession:
    """GET answers the form; POST with btnSubmit answers the result page; POST with the download target
    answers the CSV of the chosen deal type (rblDT 1 = bulk, 2 = block)."""

    def __init__(self, page=FORM, bulk=BULK, block=BLOCK, fail=None):
        self.page, self.bulk, self.block, self.fail = page, bulk, block, fail
        self.calls = []

    def _page(self, url):
        if self.fail and self.fail in url:
            raise requests.ConnectionError("down")
        return self.page if not isinstance(self.page, dict) else next(v for k, v in self.page.items() if k in url)

    def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        return Resp(self._page(url))

    def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        d = kw["data"]
        if self.fail and self.fail in url:
            raise requests.ConnectionError("down")
        if P + "btnSubmit" in d:
            return Resp(RESULT)
        assert d["__EVENTTARGET"] == P + "btnDownload" and "RESULT-VIEWSTATE" in d["__VIEWSTATE"]
        return Resp(self.bulk if d[P + "rblDT"] == "1" else self.block)

    def posts(self):
        return [c for c in self.calls if c[0] == "POST"]


def client(session, tmp_path=None, now=MONDAY_MORNING, resolver=None, **kw):
    t = [0.0]
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        t[0] += s

    c = BSEClient(session=session, cache_dir=tmp_path, clock=lambda: now, sleep=sleep,
                  monotonic=lambda: t[0], resolver=resolver, **kw)
    c.sleeps = sleeps
    c.t = t
    return c


# -- the form flow -------------------------------------------------------------------------------
def test_form_parsing_collects_the_real_hidden_fields():
    form = parse_form(FORM)
    h = form.hidden()
    for name in ("__EVENTTARGET", "__EVENTARGUMENT", "__VIEWSTATE", "__VIEWSTATEGENERATOR", "__VIEWSTATEENCRYPTED",
                 "__EVENTVALIDATION", P + "hf_scripcode", P + "DDate", P + "Hidden1", P + "SmartSearch$hdnCode"):
        assert name in h, name
    assert h["__VIEWSTATE"] == "VIEWSTATE-PLACEHOLDER" and h["__VIEWSTATEGENERATOR"] == "43DF1E00"
    assert form.find("rblDT") == P + "rblDT" and form.find("txtToDate") == P + "txtToDate"
    assert form.kind_value("bulk") == "1" and form.kind_value("block") == "2"


def test_submit_post_has_hidden_fields_deal_type_all_markets_dates_and_submit():
    form = parse_form(FORM)
    d = build_submit(form, "block", date(2026, 10, 1), date(2026, 10, 9))
    assert d["__VIEWSTATE"] == "VIEWSTATE-PLACEHOLDER" and d["__EVENTTARGET"] == ""
    assert d[P + "rblDT"] == "2" and d[P + "chkAllMarket"] == "on"
    assert d[P + "txtDate"] == "01/10/2026" and d[P + "txtToDate"] == "09/10/2026"
    assert d[P + "btnSubmit"] == "Submit"


def test_download_post_uses_the_submit_answers_hidden_fields_and_no_submit_button():
    page, result = parse_form(FORM), bse.Form(*_parse_hidden(RESULT))
    d = build_download(result, page, "bulk", date(2026, 10, 1), date(2026, 10, 9))
    assert d["__VIEWSTATE"] == "RESULT-VIEWSTATE-PLACEHOLDER"
    assert d["__EVENTTARGET"] == P + "btnDownload"
    assert d[P + "rblDT"] == "1" and d[P + "chkAllMarket"] == "on" and d[P + "txtToDate"] == "09/10/2026"
    assert P + "btnSubmit" not in d


def _parse_hidden(html):
    p = bse._FormParser()
    p.feed(html)
    return p.inputs, p.selects


def test_missing_hidden_field_is_a_clear_error():
    broken = FORM.replace('name="__VIEWSTATE"', 'name="__SOMETHING"')
    with pytest.raises(BSELayoutError, match="__VIEWSTATE"):
        parse_form(broken)


def test_javascript_app_shell_is_a_layout_error():
    with pytest.raises(BSELayoutError, match="layout changed"):
        parse_form("<html><body><app-root></app-root></body></html>")


# -- the CSV -------------------------------------------------------------------------------------
def test_csv_parsing_from_the_real_samples():
    rows = parse_deals_csv(BULK, "bulk")
    assert len(rows) == N_BULK
    assert rows[0] == {"date": "2026-10-01", "code": "544953", "name": "MONEYVIEW",
                       "client": "IRAGE BROKING SERVICES LLP", "side": "P", "qty": "14880318", "price": "56.71",
                       "kind": "bulk"}
    assert rows[1]["side"] == "S" and rows[1]["qty"] == "10731778"
    assert "AMIT AGRAWAL" in [r["client"] for r in rows]   # double space collapsed
    block = parse_deals_csv(BLOCK, "block")
    assert len(block) == N_BLOCK and {r["kind"] for r in block} == {"block"}
    itc = [r for r in block if r["name"] == "ITC" and r["client"] == "SBI MUTUAL FUND"][0]
    assert (itc["date"], itc["code"], itc["side"], itc["qty"], itc["price"]) == (
        "2026-10-08", "500875", "P", "37604429", "257.35")
    t = to_trade(itc, "ITC", "ITC")
    assert (t.exchange, t.source, t.transaction, t.transaction_date, t.investor) == (
        "BSE", "block", "Purchase", "2026-10-08", "SBI MUTUAL FUND")
    assert t.size == "37604429 sh @ ₹257.35"
    assert to_trade(rows[1], "X").transaction == "Sale"


def test_unexpected_csv_columns_are_a_layout_error():
    with pytest.raises(BSELayoutError):
        parse_deals_csv("Foo,Bar\r\n1,2\r\n")


# -- symbol mapping ------------------------------------------------------------------------------
def test_symbol_mapping_hit_and_miss(tmp_path):
    mapping = {"500875": "ITC"}
    c = client(BseSession(), tmp_path, resolver=lambda code, name: mapping.get(code))
    deals = c.deals(*ALL, investors=["SBI Mutual Fund", "Amit Agrawal"])
    itc = [t for t in deals if t.raw["bse_code"] == "500875"][0]
    assert itc.ticker == "ITC" and itc.raw["nse_symbol"] == "ITC" and itc.exchange == "BSE" and itc.source == "block"
    roopa = [t for t in deals if t.raw["bse_code"] == "544954"][0]
    assert roopa.ticker == "544954.BO" and roopa.raw["bse_scrip_id"] == "ROOPA" and roopa.raw["nse_symbol"] is None
    assert roopa.source == "bulk" and roopa.investor == "AMIT AGRAWAL"


# -- cache and politeness ------------------------------------------------------------------------
def test_past_days_are_cached_and_not_fetched_again(tmp_path):
    s = BseSession()
    c = client(s, tmp_path)
    first = c.deals(*ALL)
    assert len(first) == N_BULK + N_BLOCK
    assert len(s.calls) == 6 and len(s.posts()) == 4   # per deal type: GET, Submit, Download
    n = len(s.calls)
    again = client(s, tmp_path).deals(*ALL)             # a new client, same cache dir
    assert len(s.calls) == n and len(again) == N_BULK + N_BLOCK
    assert (tmp_path / "bse_deals" / "2026-10-08.json").exists()


def test_requests_are_at_least_two_seconds_apart(tmp_path):
    c = client(BseSession(), tmp_path)
    c.deals(*ALL)
    assert c.requests_made == 6 and len(c.sleeps) == 5 and all(s >= 2.0 - 1e-9 for s in c.sleeps)


def test_today_is_only_read_after_the_close_with_today_as_from_and_to(tmp_path):
    s = BseSession()
    client(s, tmp_path, now=MONDAY_MORNING).deals(date(2026, 10, 12), date(2026, 10, 12))
    assert s.calls == []                            # nothing published yet: no request at all
    client(s, tmp_path, now=MONDAY_EVENING).deals(date(2026, 10, 12), date(2026, 10, 12))
    first = s.posts()[0][2]["data"]
    assert first[P + "txtDate"] == first[P + "txtToDate"] == "12/10/2026"
    assert not (tmp_path / "bse_deals" / "2026-10-12.json").exists()   # today is never final


def test_default_host_is_beta_and_a_fallback_host_is_tried(tmp_path):
    s = BseSession()
    client(s, tmp_path).deals(*ALL)
    assert all(c[1].startswith("https://beta.bseindia.com/") for c in s.calls)
    s2 = BseSession(page={"beta.": "<html><app-root></app-root></html>", "www.": FORM})
    c2 = client(s2, tmp_path / "x", fallback_host="www.bseindia.com")
    assert len(c2.deals(*ALL)) == N_BULK + N_BLOCK
    assert s2.posts()[0][1].startswith("https://www.")


def test_failure_does_not_raise_and_is_not_retried_at_once(tmp_path):
    s = BseSession(fail="bseindia")
    c = client(s, tmp_path)
    assert c.deals(*ALL) == []
    assert c.last_error
    n = len(s.calls)
    c.deals(*ALL)
    assert len(s.calls) == n                         # backing off
    c.t[0] += 3600
    c.deals(*ALL)
    assert len(s.calls) > n


def test_download_that_is_not_a_csv_is_reported_not_crashing(tmp_path):
    c = client(BseSession(bulk="<html><body>Error</body></html>"), tmp_path)
    assert c.deals(*ALL) == []
    assert "CSV" in (c.last_error or "")


# -- keys and dedupe -----------------------------------------------------------------------------
def _old_key(t):
    material = json.dumps([t.source, t.investor, t.ticker, t.transaction, t.transaction_date, t.report_date, t.size],
                          sort_keys=True)
    return hashlib.sha1(material.encode()).hexdigest()[:16]


def _itc_block_row():
    return [r for r in parse_deals_csv(BLOCK, "block") if r["client"] == "SBI MUTUAL FUND"][0]


def test_nse_keys_are_unchanged_and_bse_keys_are_distinct():
    nse = _norm_deal({"buySell": "BUY", "clientName": "SBI MUTUAL FUND", "date": "08-Oct-2026",
                      "symbol": "ITC", "qty": "37604429", "watp": "257.35"}, "block")
    assert nse.exchange == "NSE" and nse.key == _old_key(nse)
    b = to_trade(_itc_block_row(), "ITC", "ITC")
    assert b.size == "37604429 sh @ ₹257.35" == nse.size and b.ticker == nse.ticker
    assert b.key != nse.key
    assert len(_dedupe([nse, b])) == 2               # two real trades on two exchanges
    assert len(_dedupe([nse, nse])) == 1
    assert "on BSE" in b.summary() and "on BSE" not in nse.summary()


# -- NSEClient wiring ----------------------------------------------------------------------------
NSE_CSV = ('"Date ","Symbol ","Security Name ","Client Name ","Buy / Sell ","Quantity Traded ",'
           '"Trade Price / Wght. Avg. Price ","Remarks "\r\n'
           '"08-OCT-2026","ITC","ITC Limited","SBI MUTUAL FUND","BUY","3,76,04,429","257.35","-"\r\n')


def nse_with(bse_client):
    sess = FakeSession({("GET", "bulk-block-short-deals"): NSE_CSV})
    n = NSEClient(session=sess, pause=0)
    n.bse = bse_client
    return n, sess


def test_nse_client_adds_bse_deals_with_both_exchanges(tmp_path, monkeypatch):
    monkeypatch.setattr("trading_agent.nse.datetime", type("DT", (datetime,), {"now": classmethod(lambda cls, tz=None: MONDAY_MORNING)}))
    n, _ = nse_with(client(BseSession(), tmp_path, resolver=lambda c, nm: {"500875": "ITC"}.get(c)))
    got = n.trades_for_investors(["SBI Mutual Fund", "Amit Agrawal"], "deals", days=12)
    pairs = {(t.exchange, t.ticker) for t in got}
    assert ("NSE", "ITC") in pairs and ("BSE", "ITC") in pairs and ("BSE", "544954.BO") in pairs
    assert len({t.key for t in got}) == len(got)


def test_setting_off_means_no_bse_calls(tmp_path):
    from trading_agent.config import Settings
    from trading_agent.runner import make_data_source
    import dataclasses
    base = Settings(anthropic_api_key="t", claude_model="m", market="in", data_source="nse", broker="local",
                    quiver_api_key=None, groww_access_token=None, groww_api_key=None, groww_api_secret=None,
                    groww_totp_secret=None, groww_live_orders=False, groww_exchange="NSE", alpaca_key_id=None,
                    alpaca_secret=None, alpaca_base_url="x", watch_investor="A", watch_source="deals",
                    paper_starting_cash=1, auto_trade=False, resend_api_key=None, notify_email_to=None,
                    notify_email_from="a", notify_webhook_url=None, state_dir=tmp_path)
    assert base.bse_deals is True and make_data_source(base).bse is not None
    off = dataclasses.replace(base, bse_deals=False)
    assert make_data_source(off).bse is None


def test_off_client_makes_no_bse_requests():
    s = BseSession()
    n, _ = nse_with(None)
    assert [t.exchange for t in n.trades_for_investors(["SBI Mutual Fund", "Amit Agrawal"], "deals", days=12)] == ["NSE"]
    assert s.calls == []


def test_bse_failure_leaves_nse_deals_and_warns_once(tmp_path, caplog):
    class Boom:
        def deals(self, *a, **k):
            raise bse.BSELayoutError("BSE deals page layout changed: missing __VIEWSTATE")
    bse._warned.clear()
    n, _ = nse_with(Boom())
    for _ in range(3):
        got = n.trades_for_investors(["SBI Mutual Fund", "Amit Agrawal"], "deals", days=12)
        assert [t.exchange for t in got] == ["NSE"]
    assert len([r for r in caplog.records if "BSE deals unavailable" in r.message]) == 1


def test_insider_source_never_asks_bse(tmp_path):
    class Spy:
        called = False

        def deals(self, *a, **k):
            Spy.called = True
            return []
    n, _ = nse_with(Spy())
    assert n._bse_rows("insider", 5, None) == [] and Spy.called is False


# -- config and dashboard ------------------------------------------------------------------------
def test_bse_deals_setting_defaults_on_and_reads_env(tmp_path):
    from trading_agent.config import load_settings
    env = tmp_path / ".env"
    env.write_text("MARKET=in\n", encoding="utf-8")
    assert load_settings(env).bse_deals is True
    env.write_text("MARKET=in\nBSE_DEALS=false\n", encoding="utf-8")
    assert load_settings(env).bse_deals is False


def test_dashboard_deals_show_the_exchange_column(settings):
    from trading_agent.ui import App
    settings.market = "in"
    nse = _norm_deal({"buySell": "BUY", "clientName": "SBI MUTUAL FUND", "date": "08-Oct-2026",
                      "symbol": "ITC", "qty": "37604429", "watp": "257.35"}, "block")
    b = to_trade(_itc_block_row(), "ITC", "ITC")
    app = App(settings, demo_trades=[nse, b], dotenv=settings.state_dir / ".env")
    deals = app.snapshot()["deals"]
    assert sorted(d["exchange"] for d in deals) == ["BSE", "NSE"]
    html = (Path(__file__).resolve().parents[1] / "trading_agent" / "ui" / "index.html").read_text(encoding="utf-8")
    assert "<th>Exchange</th>" in html and "d.exchange" in html


def test_settings_switch_saves_and_turns_bse_off(settings, tmp_path):
    from trading_agent.ui import App
    settings.market = "in"
    env = tmp_path / ".env"
    app = App(settings, dotenv=env, data=NSEClient(session=FakeSession({}), pause=0))
    app.data.bse = object()
    app.update_settings({"bse_deals": False})
    assert app.data.bse is None and "BSE_DEALS=false" in env.read_text(encoding="utf-8")
    assert settings.bse_deals is False


# =================================================================================================
# Fix round 1
# =================================================================================================
EMPTY = (FX / "bse_empty_day.csv").read_text(encoding="utf-8")
TUESDAY_MORNING = datetime(2026, 10, 13, 10, 0, tzinfo=IST)
MONDAY = date(2026, 10, 12)


def test_real_empty_day_csv_parses_to_nothing_without_a_layout_error():
    assert parse_deals_csv(EMPTY, "bulk") == []


def test_an_older_empty_day_is_cached_as_empty(tmp_path):
    s = BseSession(bulk=EMPTY, block=EMPTY)
    day = date(2026, 10, 5)  # a Monday a week back
    assert client(s, tmp_path).deals(day, day) == []
    assert (tmp_path / "bse_deals" / "2026-10-05.json").exists()
    n = len(s.calls)
    assert client(s, tmp_path).deals(day, day) == [] and len(s.calls) == n


def test_empty_yesterday_is_asked_for_once_an_hour_not_every_call(tmp_path):
    s = BseSession(bulk=EMPTY, block=EMPTY)
    c = client(s, tmp_path, now=TUESDAY_MORNING)
    assert c.deals(MONDAY, MONDAY) == [] and c.last_error is None
    n = len(s.calls)
    assert n == 6
    c.deals(MONDAY, MONDAY)
    c.deals(MONDAY, MONDAY)
    assert len(s.calls) == n                          # a quiet day costs one round per hour
    assert not (tmp_path / "bse_deals" / "2026-10-12.json").exists()   # and is never final on disk
    c.t[0] += 3601
    c.deals(MONDAY, MONDAY)
    assert len(s.calls) == 2 * n


class Closed:
    def holiday(self, d):
        return "Diwali" if d == MONDAY else None


def test_exchange_holiday_and_weekend_make_no_request(tmp_path):
    s = BseSession()
    c = client(s, tmp_path, now=TUESDAY_MORNING, holidays=Closed())
    assert c.deals(MONDAY, MONDAY) == []
    assert c.deals(date(2026, 10, 10), date(2026, 10, 11)) == []   # Saturday, Sunday
    assert s.calls == []


def test_resolver_is_exact_only_never_fuzzy():
    groww = ("exchange,exchange_token,trading_symbol,name,segment,series,isin\n"
             "BSE,500875,ITC,ITC Ltd,CASH,A,INE154A01025\n"
             "NSE,1,ITC,ITC Limited,CASH,EQ,INE154A01025\n"
             "BSE,500777,CLASH,Clash Textiles Ltd,CASH,A,INE000A00001\n"      # same ID as an NSE stock ...
             "NSE,2,CLASH,Clash Of Titans Limited,CASH,EQ,INE000B00002\n"     # ... but another company
             "BSE,500999,FOOBSE,Foo Industries Ltd,CASH,A,INE000C00003\n"
             "NSE,3,FOOIND,Foo Industries Limited,CASH,EQ,INE000D00004\n"     # exact company-name match
             "BSE,544954,ROOPA,Roopa Industries,CASH,A,INE0ROOPA001\n")

    class N:
        def _text(self, url, name):
            return groww

        def search(self, *a, **k):
            raise AssertionError("fuzzy search must not be used")

    r = ScripResolver(N())
    assert r("500875", "WHATEVER") == "ITC"            # scrip code -> ISIN -> NSE symbol
    assert r("500777", "CLASH") is None                # same ID, different ISIN: not the same company
    assert r("500999", "FOOBSE") == "FOOIND"           # exact normalised company name
    assert r("544954", "ROOPA") is None                # BSE only
    assert r("999999", "ITC") is None                  # unknown code: an ID alone proves nothing


def test_client_names_are_capped_and_stripped_of_control_characters():
    text = ("Deal Date,Security Code,Company,Client Name,Deal Type,Quantity,Price\r\n"
            f"09/10/2026,1,ABC,\"EVIL\x07\x1b[31m NAME {'X' * 300}\",P,1,2\r\n")
    c = parse_deals_csv(text)[0]["client"]
    assert len(c) <= 120 and "\x07" not in c and "\x1b" not in c and c.startswith("EVIL")


def test_warning_key_is_day_plus_error_class_only(caplog):
    bse._warned.clear()
    bse.warn_once_per_day(BSELayoutError("one thing"), date(2026, 10, 12))
    bse.warn_once_per_day(BSELayoutError("another detail"), date(2026, 10, 12))
    assert len([r for r in caplog.records if "BSE deals unavailable" in r.message]) == 1
    bse.warn_once_per_day(BSELayoutError("next day"), date(2026, 10, 13))
    assert len([r for r in caplog.records if "BSE deals unavailable" in r.message]) == 2


def test_requests_are_split_into_at_most_30_day_ranges(tmp_path):
    assert bse.MAX_SPAN_DAYS == 30
    s = BseSession()
    client(s, tmp_path).deals(date(2026, 8, 3), date(2026, 10, 9))
    submits = [c[2]["data"] for c in s.posts() if P + "btnSubmit" in c[2]["data"]]
    spans = {(d[P + "txtDate"], d[P + "txtToDate"]) for d in submits}
    assert len(spans) == 3 and ("03/08/2026", "01/09/2026") in spans


def test_evening_digest_deals_row_shows_bse():
    from trading_agent.digest_render import _deals_block
    sec = {"deals": [{"ticker": "ITC", "exchange": "BSE", "transaction": "Purchase", "size": "1 sh", "who": [],
                      "investor": "X", "reported": "2026-10-08"},
                     {"ticker": "ITC", "exchange": "NSE", "transaction": "Purchase", "size": "1 sh", "who": [],
                      "investor": "X", "reported": "2026-10-08"}], "total": 2, "since": "2026-10-08", "following": ["X"]}
    blk = _deals_block(sec, "Deals")
    rows = (blk.get("table") or blk.get("kv") or blk)["rows"]
    assert [r[0] for r in rows] == ["ITC (BSE)", "ITC"]


# -- BSE-only tickers are information, never orders ----------------------------------------------
def test_bo_ticker_is_refused_by_paper_broker_and_practice_buy(settings, tmp_path):
    from trading_agent.broker import BSE_ONLY_MESSAGE, LocalPaperBroker
    b = LocalPaperBroker(tmp_path / "pb.json", starting_cash=100_000, price_fn=lambda s: 100.0)
    for side in ("buy", "sell"):
        with pytest.raises(ValueError, match="BSE-only stock, no NSE listing"):
            b.submit_order("544954.BO", side, notional=1000)
    assert BSE_ONLY_MESSAGE.endswith("not tradable here") and b.account().cash == 100_000
    from trading_agent.ui import App
    settings.market = "in"
    app = App(settings, broker=b, demo_trades=[], dotenv=settings.state_dir / ".env")
    with pytest.raises(ValueError, match="BSE-only"):
        app.paper_order("544954.BO", "buy", notional=1000)   # the practice (dashboard) buy


def test_bo_ticker_is_refused_by_groww_order_and_gtt():
    from trading_agent.groww import GrowwBroker
    g = GrowwBroker.__new__(GrowwBroker)    # refused before any state or network is touched
    with pytest.raises(ValueError, match="BSE-only"):
        g.submit_order("544954.BO", "buy", qty=1)
    with pytest.raises(ValueError, match="BSE-only"):
        g.create_gtt_stop("544954.BO", 1, 10.0, 9.9)


def test_agent_tools_refuse_a_bo_ticker(settings, sample_rows):
    from .test_agent import FakeRunner, _make
    from trading_agent.runner import check
    trades, broker, notifier = _make(settings, sample_rows, auto_trade=True)
    script = [("send_recommendation", {"action": "buy", "ticker": "544954.BO", "headline": "h",
                                       "rationale": "r", "confidence": "high", "suggested_notional_usd": 1000}),
              ("place_paper_order", {"symbol": "544954.BO", "side": "buy", "notional_usd": 1000})]
    holder = {}

    def factory(**kw):
        holder["r"] = FakeRunner(script, **kw)
        return holder["r"]

    result = check(settings, trades=trades, broker=broker, notifier=notifier, runner_factory=factory)
    log = holder["r"].tool_log
    assert "BSE-only stock" in log[0][1]["error"] and "BSE-only stock" in log[1][1]["error"]
    assert result.orders == [] and result.recommendations == []
    from trading_agent.agent import MARKET_NOTES
    assert ".BO" in MARKET_NOTES["in"] and "never order it" in MARKET_NOTES["in"]

