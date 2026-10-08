import json
from pathlib import Path

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
