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
    assert n["VAML"]["name"] == "Vedanta Aluminium Metal Limited" and n["VAML"]["exchange"] == "NSE"  # official name
    assert n["13PCL31"]["name"] == "Prachay Capital Limited" and n["13PCL31"]["exchange"] == "BSE"
    assert n["NSE"]["name"] == "National Stock Exchange of India Limited" and n["NSE"]["exchange"] == "BSE"
    assert n["NOPE"]["name"] is None and n["NOPE"]["exchange"] is None
    calls = len(sess.calls)
    again = CompanyNames(tmp_path, session=FakeSession({})).lookup(["TCS", "NSE"])  # both lists cached on disk
    assert again["TCS"]["name"].startswith("Tata") and again["NSE"]["exchange"] == "BSE" and calls == 2


def test_groww_list_only_fetched_when_needed(tmp_path):
    sess = FakeSession({("GET", "EQUITY_L.csv"): NSE_LIST, ("GET", "instrument.csv"): GROWW_LIST})
    CompanyNames(tmp_path, session=sess).lookup(["TCS", "VAML"])
    assert not any("instrument.csv" in u for _, u, _ in sess.calls)
    assert CompanyNames(tmp_path / "x", session=FakeSession({})).lookup(["TCS"])["TCS"]["name"] is None


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


SEARCH_LIST = ("SYMBOL,NAME OF COMPANY\n"
               "TATASTEEL,Tata Steel Limited\nTATAMOTORS,Tata Motors Limited\nTCS,Tata Consultancy Services Limited\n"
               "INFY,Infosys Limited\nSBIN,State Bank of India\nHDFCBANK,HDFC Bank Limited\nHDFCLIFE,HDFC Life Insurance Company Limited\n")


def names_with(tmp_path):
    return CompanyNames(tmp_path, session=FakeSession({("GET", "EQUITY_L.csv"): SEARCH_LIST}))


def test_search_by_name_or_ticker(tmp_path):
    n = names_with(tmp_path)
    assert n.search("tata steel")[0]["symbol"] == "TATASTEEL"
    assert [h["symbol"] for h in n.search("tata")][:3] == ["TCS", "TATASTEEL", "TATAMOTORS"] or \
        {h["symbol"] for h in n.search("tata")} >= {"TATASTEEL", "TATAMOTORS", "TCS"}
    assert n.search("TCS")[0]["symbol"] == "TCS"
    assert n.search("infosys")[0]["symbol"] == "INFY"
    assert n.search("state bank")[0]["symbol"] == "SBIN"
    assert n.search("hdfc bank")[0]["symbol"] == "HDFCBANK"
    assert n.search("x") == [] and n.search("zzzz") == []


def test_resolve_keeps_tickers_and_maps_names(tmp_path):
    n = names_with(tmp_path)
    assert n.resolve("tcs") == ("TCS", "Tata Consultancy Services Limited")
    assert n.resolve("Tata Steel") == ("TATASTEEL", "Tata Steel Limited")
    assert n.resolve("INFOSYS") == ("INFY", "Infosys Limited")
    assert n.resolve("SENCO") == ("SENCO", None)  # unknown: kept as typed, in capitals


def test_bonds_are_tagged_with_their_maturity(tmp_path):
    sess = FakeSession({("GET", "EQUITY_L.csv"): NSE_LIST, ("GET", "instrument.csv"): GROWW_LIST})
    n = CompanyNames(tmp_path, session=sess).lookup(["13PCL31", "NSE", "VAML"])
    assert n["13PCL31"] == {"name": "Prachay Capital Limited", "exchange": "BSE", "kind": "bond", "maturity": "Mar 2031"}
    assert n["NSE"]["kind"] == "equity" and n["VAML"]["kind"] == "equity"
    from trading_agent.instruments import is_debt
    assert is_debt("BSE", "F") and is_debt("NSE", "N3") and is_debt("NSE", "GB") and not is_debt("NSE", "EQ")
