import json
from pathlib import Path

import pytest
import requests

from trading_agent.nse import NSEClient, _iso, _norm_deal, _norm_insider
from .conftest import FakeSession

FX = Path(__file__).resolve().parents[1] / "trading_agent" / "fixtures" / "nse_deals_sample.json"


def test_iso_dates():
    assert _iso("08-Oct-2026") == "2026-10-08"
    assert _iso("01-OCT-2026") == "2026-10-01"
    assert _iso("08-10-2026") == "2026-10-08"
    assert _iso(None) == ""


def test_snapshot_and_historical_rows_normalise_the_same():
    snap = {"buySell": "BUY", "clientName": "ASHISH KACHOLIA", "date": "08-Oct-2026",
            "name": "Senco Gold Limited", "qty": "450000", "remarks": "-", "symbol": "SENCO",
            "watp": "345.10"}
    hist = {"BD_DT_DATE": "08-OCT-2026", "BD_SYMBOL": "SENCO", "BD_SCRIP_NAME": "Senco Gold",
            "BD_CLIENT_NAME": "ASHISH KACHOLIA", "BD_BUY_SELL": "BUY", "BD_QTY_TRD": 450000,
            "BD_TP_WATP": 345.1, "BD_REMARKS": "-"}
    a, b = _norm_deal(snap, "bulk"), _norm_deal(hist, "bulk")
    assert a.ticker == b.ticker == "SENCO" and a.transaction == b.transaction == "Purchase"
    assert a.transaction_date == b.transaction_date == "2026-10-08"
    assert a.investor == b.investor


def test_insider_row():
    t = _norm_insider({"symbol": "INFY", "acqName": "Salil Parekh", "tdpTransactionType": "Sell",
                       "secAcq": "50000", "secVal": "95000000", "acqfromDt": "01-Oct-2026",
                       "intimDt": "03-Oct-2026"})
    assert t.source == "insider" and t.transaction == "Sale" and t.report_date == "2026-10-03"


CSV = (
    '\ufeff"Date ","Symbol ","Security Name ","Client Name ","Buy / Sell ","Quantity Traded ",'
    '"Trade Price / Wght. Avg. Price ","Remarks "\r\n'
    '"08-OCT-2026","SENCO","Senco Gold Limited","ASHISH KACHOLIA","BUY","4,50,000","345.10","-"\r\n'
    '"02-OCT-2026","ZAGGLE","Zaggle Prepaid","ASHISH KACHOLIA","BUY","1,00,000","400","-"\r\n'
    '"08-OCT-2026","PNGSREVA","PNGS Reva","SOMEONE ELSE","SELL","1","1.0","-"\r\n'
)


def test_csv_range_download_is_preferred():
    sess = FakeSession({("GET", "bulk-block-short-deals"): CSV})
    c = NSEClient(session=sess)
    rows = c.historical_deals(10, "bulk")
    assert [t.ticker for t in rows] == ["SENCO", "ZAGGLE", "PNGSREVA"]
    assert rows[0].size == "450000 sh @ ₹345.10" and rows[0].transaction_date == "2026-10-08"
    assert rows[1].transaction == "Purchase"
    assert sess.calls[-1][2]["params"]["csv"] == "true"


def test_client_filters_by_investor_and_dedupes():
    snap = json.loads(FX.read_text())
    hist_bulk = {"data": [
        {"BD_DT_DATE": "08-OCT-2026", "BD_SYMBOL": "SENCO", "BD_CLIENT_NAME": "ASHISH KACHOLIA",
         "BD_BUY_SELL": "BUY", "BD_QTY_TRD": 450000, "BD_TP_WATP": 345.1},
        {"BD_DT_DATE": "02-OCT-2026", "BD_SYMBOL": "ZAGGLE", "BD_CLIENT_NAME": "ASHISH KACHOLIA",
         "BD_BUY_SELL": "BUY", "BD_QTY_TRD": 100000, "BD_TP_WATP": 400.0},
        {"BD_DT_DATE": "08-OCT-2026", "BD_SYMBOL": "PNGSREVA", "BD_CLIENT_NAME": "SOMEONE ELSE",
         "BD_BUY_SELL": "SELL", "BD_QTY_TRD": 1, "BD_TP_WATP": 1.0},
    ]}
    sess = FakeSession({
        ("GET", "snapshot-capital-market-largedeal"): snap,
        ("GET", "bulk-block-short-deals"): hist_bulk,  # JSON fallback; CSV check fails on this
    })
    c = NSEClient(session=sess)
    today = c.large_deals()
    assert {t.source for t in today} == {"bulk", "block"}
    mine = c.trades_for_investor("kacholia")
    assert all("KACHOLIA" in t.investor for t in mine)
    assert len({t.key for t in mine}) == len(mine)  # bulk+block fetch returned same rows -> deduped
    assert mine[0].report_date >= mine[-1].report_date
    hist = c.history_for_ticker("ASHISH KACHOLIA", "zaggle")
    assert [t.ticker for t in hist] == ["ZAGGLE"]
    # browser-like headers were sent
    assert "Mozilla" in sess.calls[-1][2]["headers"]["User-Agent"]


PIT_XBRL = """<?xml version="1.0" encoding="UTF-8"?>
<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:in-bse-co="http://www.bseindia.com/xbrl/co">
  <in-bse-co:Symbol contextRef="MainI">DMART</in-bse-co:Symbol>
  <in-bse-co:NameOfTheCompany contextRef="MainI">AVENUE SUPERMARTS LIMITED</in-bse-co:NameOfTheCompany>
  <in-bse-co:DateOfFiling contextRef="MainI">2026-10-09</in-bse-co:DateOfFiling>
  <in-bse-co:DisclosureUnderRegulation contextRef="MainI">Regulation 7 (2)</in-bse-co:DisclosureUnderRegulation>
  <in-bse-co:CategoryOfPerson contextRef="Disclosure1">Promoters</in-bse-co:CategoryOfPerson>
  <in-bse-co:NameOfThePerson contextRef="Disclosure1">RADHAKISHAN DAMANI</in-bse-co:NameOfThePerson>
  <in-bse-co:SecuritiesAcquiredOrDisposedNumberOfSecurity contextRef="Disclosure1">4500</in-bse-co:SecuritiesAcquiredOrDisposedNumberOfSecurity>
  <in-bse-co:SecuritiesAcquiredOrDisposedValueOfSecurity contextRef="Disclosure1">15075000</in-bse-co:SecuritiesAcquiredOrDisposedValueOfSecurity>
  <in-bse-co:SecuritiesAcquiredOrDisposedTransactionType contextRef="Disclosure1">Buy</in-bse-co:SecuritiesAcquiredOrDisposedTransactionType>
  <in-bse-co:DateOfAllotmentAdviceOrAcquisitionOfSharesOrSaleOfSharesSpecifyFromDate contextRef="Disclosure1">2026-10-07</in-bse-co:DateOfAllotmentAdviceOrAcquisitionOfSharesOrSaleOfSharesSpecifyFromDate>
  <in-bse-co:DateOfIntimationToCompany contextRef="Disclosure1">2026-10-08</in-bse-co:DateOfIntimationToCompany>
  <in-bse-co:ModeOfAcquisitionOrDisposal contextRef="Disclosure1">Market Purchase</in-bse-co:ModeOfAcquisitionOrDisposal>
  <in-bse-co:NameOfThePerson contextRef="Disclosure2">GOPIKISHAN DAMANI</in-bse-co:NameOfThePerson>
  <in-bse-co:SecuritiesAcquiredOrDisposedNumberOfSecurity contextRef="Disclosure2">100</in-bse-co:SecuritiesAcquiredOrDisposedNumberOfSecurity>
  <in-bse-co:SecuritiesAcquiredOrDisposedTransactionType contextRef="Disclosure2">Pledge</in-bse-co:SecuritiesAcquiredOrDisposedTransactionType>
  <in-bse-co:DateOfAllotmentAdviceOrAcquisitionOfSharesOrSaleOfSharesSpecifyFromDate contextRef="Disclosure2">2026-10-06</in-bse-co:DateOfAllotmentAdviceOrAcquisitionOfSharesOrSaleOfSharesSpecifyFromDate>
</xbrli:xbrl>"""

PIT_LIST = {"data": [
    {"symbol": "DMART", "companyName": "AVENUE SUPERMARTS LIMITED", "regulation": "Regulation 7 (2)",
     "broadcastDateTime": "09-Oct-2026 19:18:02",
     "xmlFileName": "https://nsearchives.nseindia.com/corporate/xbrl/IT_1_WebXMLFile_A.xml"},
    {"symbol": "BAD", "xmlFileName": "https://nsearchives.nseindia.com/corporate/xbrl/IT_2_WebXMLFile_B.xml"},
]}


def test_parse_pit_xbrl_filing():
    from trading_agent.nse import parse_pit_xbrl
    rows = parse_pit_xbrl(PIT_XBRL, PIT_LIST["data"][0])
    assert [r.investor for r in rows] == ["RADHAKISHAN DAMANI", "GOPIKISHAN DAMANI"]
    a, b = rows
    assert a.ticker == "DMART" and a.transaction == "Purchase" and a.source == "insider"
    assert a.transaction_date == "2026-10-07" and a.report_date == "2026-10-08"
    assert a.size == "4500 sh (₹15075000)" and a.raw["category"] == "Promoters"
    assert b.transaction == "Pledge" and b.report_date == "2026-10-09"  # falls back to filing date
    with pytest.raises(ValueError):  # entity tricks are refused
        parse_pit_xbrl('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><x>&a;</x>')


def test_insider_feed_uses_xbrl_after_may_2026_and_caches(tmp_path):
    from datetime import date
    sess = FakeSession({
        ("GET", "api/corporates-pit-gg"): PIT_LIST,
        ("GET", "WebXMLFile_A.xml"): PIT_XBRL,
        ("GET", "WebXMLFile_B.xml"): "<html>not xml",
        ("GET", "api/corporates-pit"): {"data": [
            {"symbol": "INFY", "acqName": "Salil Parekh", "tdpTransactionType": "Sell", "secAcq": "10",
             "acqfromDt": "02-Apr-2026", "intimDt": "03-Apr-2026"}]},
    })
    sleeps = []
    c = NSEClient(session=sess, cache_dir=tmp_path, sleep=sleeps.append)
    rows = c.insider_trades(30, end=date(2026, 10, 9))
    assert {r.investor for r in rows} == {"RADHAKISHAN DAMANI", "GOPIKISHAN DAMANI"}
    assert not any("corporates-pit?" in u or u.endswith("corporates-pit") for _, u, _ in sess.calls)
    assert (tmp_path / "nse_pit" / "IT_1_WebXMLFile_A.json").exists()
    # cached: a second client downloads nothing but the list
    sess2 = FakeSession({("GET", "api/corporates-pit-gg"): PIT_LIST, ("GET", "WebXMLFile_B.xml"): "<html>x",
                         ("GET", "api/corporates-pit"): sess.routes[("GET", "api/corporates-pit")]})
    c2 = NSEClient(session=sess2, cache_dir=tmp_path, sleep=sleeps.append)
    assert len(c2.insider_trades(30, end=date(2026, 10, 9))) == 2
    assert not any("WebXMLFile_A" in u for _, u, _ in sess2.calls)
    # a window that starts before May 2026 also reads the old JSON feed
    both = c2.trades_for_investor("parekh", "insider", days=200)
    assert [t.ticker for t in both] == ["INFY"]
    # watched investor matching works on the new feed
    assert [t.ticker for t in c2.trades_for_investor("radhakishan", "insider")] == ["DMART"]


def test_insider_feed_backs_off_when_nse_refuses(tmp_path):
    from datetime import date
    from .conftest import FakeResponse
    files = [{"symbol": f"S{i}", "xmlFileName": f"https://nsearchives.nseindia.com/x/F{i}.xml"} for i in range(5)]

    class Denied(FakeResponse):
        def raise_for_status(self):
            raise requests.HTTPError("403 Access Denied", response=self)

    class Refusing(FakeSession):
        def request(self, method, url, **kw):
            if url.endswith(".xml"):
                self.calls.append((method, url, kw))
                return Denied("<HTML>Access Denied", 403)
            return super().request(method, url, **kw)

    sess = Refusing({("GET", "api/corporates-pit-gg"): {"data": files}})
    sleeps = []
    c = NSEClient(session=sess, cache_dir=tmp_path, sleep=sleeps.append)
    assert c.insider_trades(10, end=date(2026, 10, 9)) == []
    xml_calls = [u for _, u, _ in sess.calls if u.endswith(".xml")]
    assert len(xml_calls) == 2 and 60 in sleeps  # one back-off, then stop for this run


def test_insider_download_cap_per_run(tmp_path):
    from datetime import date
    files = [{"symbol": "DMART", "xmlFileName": f"https://nsearchives.nseindia.com/x/F{i}.xml"} for i in range(5)]
    sess = FakeSession({("GET", "api/corporates-pit-gg"): {"data": files}, ("GET", ".xml"): PIT_XBRL})
    sleeps = []
    c = NSEClient(session=sess, cache_dir=tmp_path, max_new_downloads=3, sleep=sleeps.append)
    c.insider_trades(10, end=date(2026, 10, 9))
    assert len([u for _, u, _ in sess.calls if u.endswith(".xml")]) == 3
    assert sleeps == [0.25, 0.25]  # pause between downloads, not before the first
