"""BSE bulk/block deals: form parsing, CSV parsing, symbol mapping, cache, keys, wiring. Fakes only, no network."""
import hashlib
import json
from datetime import date, datetime
from pathlib import Path

import pytest
import requests

from trading_agent import bse
from trading_agent.bse import (BSEClient, BSELayoutError, IST, ScripResolver, build_post, parse_deals_csv,
                               parse_form, to_trade)
from trading_agent.nse import NSEClient, _dedupe, _norm_deal
from trading_agent.quiver import DisclosedTrade
from .conftest import FakeSession

FX = Path(__file__).resolve().parents[1] / "trading_agent" / "fixtures"
FORM = (FX / "bse_deals_form.html").read_text(encoding="utf-8")
CSV = (FX / "bse_deals_sample.csv").read_text(encoding="utf-8")
MONDAY_MORNING = datetime(2026, 10, 12, 10, 0, tzinfo=IST)
MONDAY_EVENING = datetime(2026, 10, 12, 17, 0, tzinfo=IST)
URL = "bseindia.com/markets/equity/EQReports/BulknBlockDeals.aspx"


class Resp:
    def __init__(self, body, status=200):
        self.content = body.encode("utf-8") if isinstance(body, str) else body
        self.status_code = status

    @property
    def text(self):
        return self.content.decode("utf-8")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class BseSession:
    """GET answers the form page; POST answers the CSV. ``pages``/``csvs`` may differ by host."""

    def __init__(self, page=FORM, csv_text=CSV, fail=None):
        self.page, self.csv_text, self.fail = page, csv_text, fail
        self.calls = []

    def _answer(self, what, url):
        if self.fail and self.fail in url:
            raise requests.ConnectionError("down")
        if isinstance(what, dict):
            what = next((v for k, v in what.items() if k in url), "")
        return Resp(what)

    def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        return self._answer(self.page, url)

    def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        if kw["data"].get("ctl00$ContentPlaceHolder1$rblDealType") == "block" and self.csv_text == CSV:
            return Resp(CSV.splitlines()[0] + "\n")  # no block deals in this sample: header only
        return self._answer(self.csv_text, url)

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


# -- the form ------------------------------------------------------------------------------------
def test_form_parsing_collects_hidden_fields_and_controls():
    form = parse_form(FORM)
    data = form.base_data()
    assert data["__VIEWSTATE"] == "VS-PLACEHOLDER==" and data["__EVENTVALIDATION"] == "EV-PLACEHOLDER=="
    assert data["__VIEWSTATEGENERATOR"] == "1A2B3C4D" and data["ctl00$hdnFoo"] == "bar"
    assert form.find("rblDT") == "ctl00$ContentPlaceHolder1$rblDT"
    assert form.date_inputs() == ["ctl00$ContentPlaceHolder1$txtDate", "ctl00$ContentPlaceHolder1$txtToDate"]
    assert form.kind_control() == ("ctl00$ContentPlaceHolder1$rblDealType", {"bulk": "bulk", "block": "block"})
    assert "ctl00$ContentPlaceHolder1$btnSubmit" not in data  # buttons are only sent when pressed


def test_postback_for_history_has_dates_mode_all_markets_and_download():
    form = parse_form(FORM)
    d = build_post(form, bse.MODE_HISTORY, date(2026, 10, 1), date(2026, 10, 9), "block")
    assert d["ctl00$ContentPlaceHolder1$rblDT"] == "1"
    assert d["ctl00$ContentPlaceHolder1$chkAllMarket"] == "on"
    assert d["ctl00$ContentPlaceHolder1$txtDate"] == "01/10/2026"
    assert d["ctl00$ContentPlaceHolder1$txtToDate"] == "09/10/2026"
    assert d["ctl00$ContentPlaceHolder1$rblDealType"] == "block"
    assert d["ctl00$ContentPlaceHolder1$btnDownload"] == "Download"
    assert d["__VIEWSTATE"] == "VS-PLACEHOLDER==" and d["__EVENTTARGET"] == ""


def test_missing_hidden_field_is_a_clear_error():
    broken = FORM.replace('name="__VIEWSTATE"', 'name="__SOMETHING"')
    with pytest.raises(BSELayoutError, match="__VIEWSTATE"):
        parse_form(broken)


def test_javascript_app_shell_is_a_layout_error():
    with pytest.raises(BSELayoutError, match="layout changed"):
        parse_form("<html><body><app-root></app-root></body></html>")


# -- the CSV -------------------------------------------------------------------------------------
def test_csv_parsing_from_the_sample():
    rows = parse_deals_csv(CSV, "bulk")
    assert len(rows) == 7
    r = rows[0]
    assert r == {"date": "2026-10-09", "code": "500001", "name": "SAMPLE INDUSTRIES LTD",
                 "client": "ASHISH KACHOLIA", "side": "B", "qty": "150000", "price": "245.60", "kind": "bulk"}
    assert rows[3]["side"] == "S" and rows[4]["qty"] == "325000"
    t = to_trade(r, "SAMPLEIND", "SAMPLEIND")
    assert (t.exchange, t.source, t.transaction, t.transaction_date, t.investor) == (
        "BSE", "bulk", "Purchase", "2026-10-09", "ASHISH KACHOLIA")
    assert t.size == "150000 sh @ ₹245.60"
    assert to_trade(rows[3], "X").transaction == "Sale"


def test_csv_with_other_dates_and_words_still_parses():
    text = ("Date,Scrip Code,Scrip Name,Client Name,Buy/Sell,Qty,Trade Price\r\n"
            "09-Oct-2026,500002,ABC LTD,Some Client,Buy,\"1,000\",12.5\r\n")
    r = parse_deals_csv(text)[0]
    assert r["date"] == "2026-10-09" and r["qty"] == "1000" and r["side"] == "Buy"


def test_unexpected_csv_columns_are_a_layout_error():
    with pytest.raises(BSELayoutError):
        parse_deals_csv("Foo,Bar\r\n1,2\r\n")


# -- symbol mapping ------------------------------------------------------------------------------
def test_symbol_mapping_hit_and_miss(tmp_path):
    mapping = {"500001": "SAMPLEIND"}
    c = client(BseSession(), tmp_path, resolver=lambda code, name: mapping.get(code))
    deals = c.deals(date(2026, 10, 2), date(2026, 10, 9), investors=["Ashish Kacholia"])
    by_code = {t.raw["bse_code"]: t for t in deals}
    assert by_code["500001"].ticker == "SAMPLEIND" and by_code["500001"].raw["nse_symbol"] == "SAMPLEIND"
    assert by_code["500001"].raw["bse_code"] == "500001" and by_code["500001"].exchange == "BSE"
    miss = by_code["543210"]
    assert miss.ticker == "543210.BO" and miss.raw["bse_name"] == "DEMO CHEMICALS LIMITED"
    assert miss.raw["nse_symbol"] is None


GROWW = ("exchange,exchange_token,trading_symbol,name,segment,series,isin\n"
         "BSE,500001,SAMPLEIND,Sample Industries Ltd,CASH,A,INE000A01011\n"
         "NSE,1111,SAMPLEIND,Sample Industries Limited,CASH,EQ,INE000A01011\n")


class FakeNames:
    def _text(self, url, name):
        return GROWW

    def search(self, query, limit=1):
        if query.upper().startswith("EXAMPLE PACKAGING"):
            return [{"symbol": "EXAMPKG", "name": query, "score": 95}]
        return [{"symbol": "WRONG", "name": query, "score": 60}]


def test_scrip_resolver_by_isin_then_exact_name_else_none():
    r = ScripResolver(FakeNames())
    assert r("500001", "SAMPLE INDUSTRIES LTD") == "SAMPLEIND"      # scrip code -> ISIN -> NSE symbol
    assert r("539999", "EXAMPLE PACKAGING LTD") == "EXAMPKG"        # no ISIN hit, exact name match
    assert r("543210", "DEMO CHEMICALS LIMITED") is None            # only a weak name match: not guessed


# -- cache and politeness ------------------------------------------------------------------------
def test_past_days_are_cached_and_not_fetched_again(tmp_path):
    s = BseSession()
    c = client(s, tmp_path)
    first = c.deals(date(2026, 10, 2), date(2026, 10, 9))
    assert len(first) == 7
    n = len(s.calls)
    assert n >= 2 and len(s.posts()) == 2          # one GET + a POST each for bulk and block
    c2 = client(s, tmp_path)                        # a new client, same cache dir
    again = c2.deals(date(2026, 10, 2), date(2026, 10, 9))
    assert len(s.calls) == n and len(again) == 7   # no new requests
    assert (tmp_path / "bse_deals" / "2026-10-09.json").exists()


def test_requests_are_at_least_two_seconds_apart(tmp_path):
    c = client(BseSession(), tmp_path)
    c.deals(date(2026, 10, 2), date(2026, 10, 9))
    assert c.requests_made == 3 and len(c.sleeps) >= 2 and all(s >= 2.0 - 1e-9 for s in c.sleeps[:2])


def test_today_is_only_read_after_the_close(tmp_path):
    s = BseSession()
    before = client(s, tmp_path, now=MONDAY_MORNING)
    before.deals(date(2026, 10, 12), date(2026, 10, 12))
    assert s.calls == []                            # nothing published yet: no request at all
    after = client(s, tmp_path, now=MONDAY_EVENING)
    after.deals(date(2026, 10, 12), date(2026, 10, 12))
    assert s.posts() and s.posts()[0][2]["data"]["ctl00$ContentPlaceHolder1$rblDT"] == "0"
    assert not (tmp_path / "bse_deals" / "2026-10-12.json").exists()   # today is never final


def test_falls_back_to_the_beta_host_when_www_serves_no_form(tmp_path):
    s = BseSession(page={"www.bseindia": "<html><app-root></app-root></html>", "beta.bseindia": FORM})
    deals = client(s, tmp_path).deals(date(2026, 10, 2), date(2026, 10, 9))
    assert len(deals) == 7
    assert any("beta.bseindia.com" in c[1] for c in s.calls) and s.posts()[0][1].startswith("https://beta.")


def test_failure_does_not_raise_and_is_not_retried_at_once(tmp_path):
    s = BseSession(fail="bseindia")
    c = client(s, tmp_path)
    assert c.deals(date(2026, 10, 2), date(2026, 10, 9)) == []
    assert c.last_error
    n = len(s.calls)
    c.deals(date(2026, 10, 2), date(2026, 10, 9))
    assert len(s.calls) == n                         # backing off
    c.t[0] += 3600
    c.deals(date(2026, 10, 2), date(2026, 10, 9))
    assert len(s.calls) > n


def test_download_that_is_not_a_csv_is_reported_not_crashing(tmp_path):
    c = client(BseSession(csv_text="<html><body>Error</body></html>"), tmp_path)
    assert c.deals(date(2026, 10, 2), date(2026, 10, 9)) == []
    assert "CSV" in (c.last_error or "")


# -- keys and dedupe -----------------------------------------------------------------------------
def _old_key(t):
    material = json.dumps([t.source, t.investor, t.ticker, t.transaction, t.transaction_date, t.report_date, t.size],
                          sort_keys=True)
    return hashlib.sha1(material.encode()).hexdigest()[:16]


def test_nse_keys_are_unchanged_and_bse_keys_are_distinct():
    nse = _norm_deal({"buySell": "BUY", "clientName": "ASHISH KACHOLIA", "date": "09-Oct-2026",
                      "symbol": "SAMPLEIND", "qty": "150000", "watp": "245.60"}, "bulk")
    assert nse.exchange == "NSE" and nse.key == _old_key(nse)
    b = to_trade(parse_deals_csv(CSV)[0], "SAMPLEIND", "SAMPLEIND")
    assert b.size == "150000 sh @ ₹245.60" == nse.size and b.ticker == nse.ticker
    assert b.key != nse.key
    assert len(_dedupe([nse, b])) == 2               # two real trades on two exchanges
    assert len(_dedupe([nse, nse])) == 1
    assert "on BSE" in b.summary() and "on BSE" not in nse.summary()


# -- NSEClient wiring ----------------------------------------------------------------------------
NSE_CSV = ('"Date ","Symbol ","Security Name ","Client Name ","Buy / Sell ","Quantity Traded ",'
           '"Trade Price / Wght. Avg. Price ","Remarks "\r\n'
           '"09-OCT-2026","SAMPLEIND","Sample Industries","ASHISH KACHOLIA","BUY","1,50,000","245.60","-"\r\n')


def nse_with(bse_client):
    sess = FakeSession({("GET", "bulk-block-short-deals"): NSE_CSV})
    n = NSEClient(session=sess, pause=0)
    n.bse = bse_client
    return n, sess


def test_nse_client_adds_bse_deals_with_both_exchanges(tmp_path, monkeypatch):
    monkeypatch.setattr("trading_agent.nse.date", type("D", (date,), {"today": staticmethod(lambda: date(2026, 10, 12))}))
    n, _ = nse_with(client(BseSession(), tmp_path, resolver=lambda c, nm: {"500001": "SAMPLEIND"}.get(c)))
    got = n.trades_for_investors(["Ashish Kacholia"], "deals", days=10)
    pairs = {(t.exchange, t.ticker) for t in got}
    assert ("NSE", "SAMPLEIND") in pairs and ("BSE", "SAMPLEIND") in pairs and ("BSE", "543210.BO") in pairs
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
    assert [t.exchange for t in n.trades_for_investors(["Ashish Kacholia"], "deals", days=10)] == ["NSE"]
    assert s.calls == []


def test_bse_failure_leaves_nse_deals_and_warns_once(tmp_path, caplog):
    class Boom:
        def deals(self, *a, **k):
            raise bse.BSELayoutError("BSE deals page layout changed: missing __VIEWSTATE")
    bse._warned.clear()
    n, _ = nse_with(Boom())
    for _ in range(3):
        got = n.trades_for_investors(["Ashish Kacholia"], "deals", days=10)
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
    nse = _norm_deal({"buySell": "BUY", "clientName": "ASHISH KACHOLIA", "date": "09-Oct-2026",
                      "symbol": "SAMPLEIND", "qty": "1", "watp": "2"}, "bulk")
    b = to_trade(parse_deals_csv(CSV)[0], "SAMPLEIND", "SAMPLEIND")
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
