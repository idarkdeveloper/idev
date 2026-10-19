"""Bond prices from BSE's daily debt bhavcopy: parsing, the last traded close with its date, caching, and the
Groww portfolio rows. No network: fake sessions only."""
import io
import zipfile
from datetime import date

from trading_agent import runner
from trading_agent.bse_bonds import BondPrices, parse_bhavcopy

FGROUP = ("Security_cd,sc_name,Open Price,High Price,Low Price,Close Price,Total Traded Volume,Number of Trades ,"
          "Total Turnover,ISIN No.,FACE VALUE,COUP0N (%),Maturity Date\r\n"
          "941121,1225PCL29   ,1000.00,1000.00,1000.00,1000.00,19.00,2,19000.00,INE0IID07801,1000.00,12.25\r   ,\r\n"
          "940955,13PCL31     ,1012.00,1012.00,1012.00,1012.00,7.00,4,7084.00,INE0IID07785,1000.00,13   ,\r\n")
ICDM = ("Security Code,Issuer Name,Coupon (%),Maturity Date,LTP,Weighted Average Price,Weighted Average Yield,"
        "Turnover (Rs. Lakh),ISIN No.,Face Value\n"
        "INE0IID07827,PRACHAY CAPITAL LIMITED,12.7500,22/06/2031,99.9430,99.9400,13.5000,5.09,INE0IID07827,1000.00\n"
        "1300PCL31,PRACHAY CAPITAL LIMITED,13.0000,16/03/2031,99.8730,99.0000,14.1200,4.80,INE0IID07785,1000.00\n")


def bhav(fgroup=FGROUP, icdm=ICDM):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("fgroup16102026.csv", fgroup)
        z.writestr("icdm16102026.csv", icdm)
        z.writestr("wdm16102026.csv", "Scrip Code,Security Code\n400536,667GOI50\n")
    return buf.getvalue()


class Resp:
    def __init__(self, status, content=b""):
        self.status_code, self.content = status, content


class Sess:
    """Answers by the ddmmyyyy in the URL; records every request."""

    def __init__(self, days):
        self.days, self.asked = days, []

    def get(self, url, **kw):
        stamp = url.rsplit("DEBTBHAVCOPY", 1)[1][:8]
        self.asked.append(stamp)
        a = self.days.get(stamp, Resp(404))
        if isinstance(a, BaseException):
            raise a
        return a


def test_parse_keys_by_code_short_name_and_isin_and_survives_stray_carriage_returns():
    q = parse_bhavcopy(bhav())
    assert q["941121"]["price"] == q["1225PCL29"]["price"] == q["INE0IID07801"]["price"] == 1000.0
    assert q["13PCL31"]["price"] == 1012.0
    assert q["INE0IID07785"]["price"] == 1012.0          # the cash-segment close wins over the reporting LTP
    assert q["INE0IID07827"]["price"] == 999.43          # reporting platform: LTP is a percent of face value
    assert "667GOI50" not in q                           # government securities are not read


def test_the_last_traded_close_is_found_newest_first_with_its_date(tmp_path):
    # Mon 19 Oct: today's file not out yet; Fri 16 Oct has it; the weekend is never asked for
    s = Sess({"16102026": Resp(200, bhav())})
    b = BondPrices(tmp_path, session=s, today=lambda: date(2026, 10, 19))
    assert b.lookup("1225pcl29") == {"price": 1000.0, "date": "2026-10-16", "source": "BSE close", "face": 1000.0}
    assert s.asked == ["19102026", "16102026"]
    # cached on disk: a second reader asks only for today again (not out yet, so not remembered as empty)
    s2 = Sess({})
    assert BondPrices(tmp_path, session=s2, today=lambda: date(2026, 10, 19)).lookup("13PCL31")["price"] == 1012.0
    assert s2.asked == ["19102026"]


def test_a_bond_that_did_not_trade_in_the_lookback_has_no_price(tmp_path):
    s = Sess({"16102026": Resp(200, bhav())})
    b = BondPrices(tmp_path, session=s, today=lambda: date(2026, 10, 19), lookback_days=10)
    assert b.lookup("NOPE29") is None
    assert "17102026" not in s.asked and "18102026" not in s.asked


def test_holidays_are_remembered_but_failures_and_unreadable_files_are_not(tmp_path):
    days = {"15102026": Resp(200, b"<html>no file</html>"),     # a past day BSE answers with a page: a holiday
            "14102026": OSError("network down"),                # no answer now: not cached
            "13102026": Resp(503)}                              # a server error: not cached
    s = Sess(days)
    b = BondPrices(tmp_path, session=s, today=lambda: date(2026, 10, 16), lookback_days=3)
    assert b.lookup("1225PCL29") is None
    cached = sorted(p.name for p in (tmp_path / "bse_debt").iterdir())
    assert cached == ["2026-10-15.json"]                       # 16 Oct is today (404 now: not yet out)
    assert "network down" in (b.last_error or "") or "503" in (b.last_error or "")


def test_portfolio_rows_price_only_unpriced_bonds_and_carry_the_date(monkeypatch, settings):
    class Fake:
        asked = []

        def lookup(self, symbol, isin=None):
            self.asked.append(symbol)
            return {"price": 1012.0, "date": "2026-10-16", "source": "BSE close"} if symbol == "13PCL31" else None

    fake = Fake()
    monkeypatch.setattr(runner, "make_bond_prices", lambda s: fake)
    entries = [{"symbol": "13PCL31", "kind": "bond", "qty": 10, "avg_price": 1000.0, "price": None},
               {"symbol": "OLDBOND", "kind": "bond", "qty": 5, "avg_price": 1000.0, "price": None},
               {"symbol": "TCS", "kind": "equity", "qty": 1, "avg_price": 3000.0, "price": None},
               {"symbol": "PRICED", "kind": "bond", "qty": 1, "avg_price": 900.0, "price": 950.0}]
    runner._price_bonds(settings, entries)
    out = runner._assemble(entries, "now")
    rows = {r["symbol"]: r for r in out["holdings"]}
    assert rows["13PCL31"]["price"] == 1012.0 and rows["13PCL31"]["price_date"] == "2026-10-16"
    assert rows["13PCL31"]["value"] == 10120.0 and rows["13PCL31"]["pl"] == 120.0
    assert "price_date" not in rows["PRICED"] and rows["PRICED"]["price"] == 950.0
    assert sorted(out["unpriced"]) == ["OLDBOND", "TCS"]
    assert fake.asked == ["13PCL31", "OLDBOND"]                # only bonds without a price are looked up
