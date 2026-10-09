from trading_agent.instruments import CompanyNames, nse_then_bse
from .conftest import FakeSession

NSE_LIST = ("SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING\n"
            "VAML,Vedanta Aluminium Metal Limited,EQ,01-JAN-2026\n"
            "TCS,Tata Consultancy Services Limited,EQ,25-AUG-2004\n")
GROWW_LIST = ("exchange,exchange_token,trading_symbol,groww_symbol,name,instrument_type,segment,series,isin\n"
              "BSE,1,13PCL31,x,Prachay Capital Limited Mar'31,EQ,CASH,F,INE0IID07785\n"
              "BSE,2,NSE,x,NSE,EQ,CASH,A,INE721I01024\n"
              "NSE,3,VAML,x,Vedanta Alumin Metal,EQ,CASH,EQ,INE1CDF01017\n")


def test_names_from_nse_list_then_groww_for_bse_only(tmp_path):
    sess = FakeSession({("GET", "EQUITY_L.csv"): NSE_LIST, ("GET", "instrument.csv"): GROWW_LIST})
    n = CompanyNames(tmp_path, session=sess).lookup(["vaml", "TCS", "13PCL31", "NSE", "NOPE"])
    assert n["VAML"] == {"name": "Vedanta Aluminium Metal Limited", "exchange": "NSE"}  # official, not abbreviated
    assert n["13PCL31"] == {"name": "Prachay Capital Limited Mar'31", "exchange": "BSE"}
    assert n["NSE"] == {"name": "National Stock Exchange of India Limited", "exchange": "BSE"}
    assert n["NOPE"] == {"name": None, "exchange": None}
    calls = len(sess.calls)
    again = CompanyNames(tmp_path, session=FakeSession({})).lookup(["TCS", "NSE"])  # both lists cached on disk
    assert again["TCS"]["name"].startswith("Tata") and again["NSE"]["exchange"] == "BSE" and calls == 2


def test_groww_list_only_fetched_when_needed(tmp_path):
    sess = FakeSession({("GET", "EQUITY_L.csv"): NSE_LIST, ("GET", "instrument.csv"): GROWW_LIST})
    CompanyNames(tmp_path, session=sess).lookup(["TCS", "VAML"])
    assert not any("instrument.csv" in u for _, u, _ in sess.calls)
    assert CompanyNames(tmp_path / "x", session=FakeSession({})).lookup(["TCS"]) == {"TCS": {"name": None, "exchange": None}}


def test_price_falls_back_to_bse():
    class P:
        def __init__(self, table):
            self.table = table

        def latest_price(self, s):
            if s not in self.table:
                raise LookupError(s)
            return self.table[s]

    price = nse_then_bse(P({"TCS": 2156.0}), P({"NSE": 1730.9, "TCS": 2150.0}))
    assert price("TCS") == 2156.0 and price("NSE") == 1730.9
    try:
        price("13PCL31")
        assert False, "expected no price"
    except LookupError:
        pass
