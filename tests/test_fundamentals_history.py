from datetime import date, timedelta

import pytest

from trading_agent.factor_backtest import run_factor_backtest
from trading_agent.fundamentals_history import (ResultsHistory, parse_results_xbrl, point_in_time,
                                                with_price)
from .conftest import FakeSession
from .test_scorecard_factor import Prices


def xbrl(facts, contexts=""):
    body = "".join(f'<in-capmkt:{t} contextRef="{c}" unitRef="INR" decimals="-5">{v}</in-capmkt:{t}>'
                   for t, c, v in facts)
    return ('<?xml version="1.0" encoding="UTF-8"?><xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" '
            f'xmlns:in-capmkt="http://www.sebi.gov.in/xbrl/capmkt">{contexts}{body}</xbrli:xbrl>')


# FourD declared with the quarter's dates (as in some 2023 filings) and OneD not declared at all
# (as in 2021 filings): the parser must go by the ids.
COMPANY = xbrl([("ProfitLossForPeriod", "OneD", "1167000000"), ("ProfitLossForPeriod", "FourD", "6938000000"),
                ("ProfitOrLossAttributableToOwnersOfParent", "OneD", "1148000000"),
                ("ProfitOrLossAttributableToOwnersOfParent", "FourD", "6800000000"),
                ("RevenueFromOperations", "OneD", "22000000000"),
                ("PaidUpValueOfEquityShareCapital", "OneD", "611000000"), ("FaceValueOfEquityShareCapital", "OneD", "10"),
                ("EquityAttributableToOwnersOfParent", "OneI", "30825000000"), ("Equity", "OneI", "31000000000"),
                ("BorrowingsCurrent", "OneI", "3000000000"), ("BorrowingsNoncurrent", "OneI", "382000000"),
                ("ProfitLossForPeriod", "TwoD", "999")],  # a comparative period: ignored
               contexts='<xbrli:context id="FourD"><xbrli:period><xbrli:startDate>2023-01-01</xbrli:startDate>'
                        '<xbrli:endDate>2023-03-31</xbrli:endDate></xbrli:period></xbrli:context>')
BANK = xbrl([("ProfitLossAfterTaxesMinorityInterestAndShareOfProfitLossOfAssociates", "OneD", "20448800000"),
             ("ProfitLossAfterTaxesMinorityInterestAndShareOfProfitLossOfAssociates", "FourD", "70168600000"),
             ("Capital", "OneI", "76915600000"), ("ReservesAndSurplus", "OneI", "255330800000"),
             ("Borrowings", "OneI", "352335800000"), ("Deposits", "OneI", "3505382400000"),
             ("PaidUpValueOfEquityShareCapital", "FourD", "76915500000"), ("FaceValueOfEquityShareCapital", "FourD", "10")])


def test_parse_by_context_id_not_declared_dates():
    r = parse_results_xbrl(COMPANY, date(2023, 3, 31))
    assert r["profit_q"] == 1148e6 and r["profit_ytd"] == 6800e6  # owners of the parent first
    assert r["revenue_q"] == 22e9 and r["equity"] == 30825e6 and r["borrowings"] == 3382e6
    assert r["shares"] == 61.1e6


def test_parse_bank_and_first_quarter():
    b = parse_results_xbrl(BANK, date(2026, 3, 31))
    assert b["profit_q"] == 20448.8e6 and b["profit_ytd"] == 70168.6e6
    assert b["equity"] == 76915.6e6 + 255330.8e6 and b["borrowings"] == 352335.8e6
    assert round(b["shares"]) == 7691550000
    q1 = parse_results_xbrl(xbrl([("ProfitLossForPeriod", "OneD", "500")]), date(2025, 6, 30))
    assert q1["profit_ytd"] == 500  # Q1: the year to date is the quarter
    with pytest.raises(ValueError):
        parse_results_xbrl('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><x>&a;</x>', date(2025, 6, 30))


def quarters(sym_profit_q, start=date(2020, 6, 30), n=21, equity=1000.0, borrow=100.0, shares=10.0, lag=30):
    """Quarterly records with year-to-date profit, balance sheets in Sep and Mar."""
    out, ytd = [], 0.0
    d = start
    for i in range(n):
        q = sym_profit_q(i)
        ytd = q if d.month == 6 else ytd + q
        rec = {"period_end": d.isoformat(), "profit_q": q, "profit_ytd": ytd, "shares": shares,
               "equity": equity if d.month in (3, 9) else None, "borrowings": borrow if d.month in (3, 9) else None,
               "available": (d + timedelta(days=lag)).isoformat() + "T18:00:00", "consolidated": True}
        out.append(rec)
        m = d.month + 3
        d = date(d.year + (m > 12), (m - 1) % 12 + 1, 1)
        d = date(d.year + (d.month == 12), d.month % 12 + 1, 1) - timedelta(days=1)
    return out


def test_point_in_time_ttm_roe_growth_and_cutoff():
    recs = quarters(lambda i: 10.0 + i)  # profit grows every quarter
    m = point_in_time(recs, "2023-11-15")  # Sep 2023 filed 30 Oct 2023
    assert m["latest_quarter"] == "2023-09-30"
    # TTM to Sep 2023 = quarters Dec 22..Sep 23 = i 10..13 -> 20+21+22+23
    assert m["ttm_profit"] == 86.0 and m["roe"] == 0.086 and m["debt_to_equity"] == 0.1
    # a year earlier: i 6..9 -> 16+17+18+19 = 70
    assert abs(m["earnings_growth"] - (86 / 70 - 1)) < 1e-12
    assert point_in_time(recs, "2023-10-29")["latest_quarter"] == "2023-06-30"  # not broadcast yet
    assert point_in_time(recs, "2020-07-01") is None
    assert point_in_time(recs[:4], "2024-06-01")["stale"] is True  # last result far in the past
    v = with_price(m, 50.0)  # market cap 500
    assert v["earnings_yield"] == 86 / 500 and v["book_to_price"] == 2.0 and abs(v["pe"] - 500 / 86) < 1e-12
    assert "error" in with_price(point_in_time(recs[:4], "2024-06-01"), 50.0)


OLD_LIST = [{"toDate": "31-Mar-2023", "broadCastDate": "27-Apr-2023 14:42:12", "consolidated": "Consolidated",
             "xbrl": "https://nsearchives.nseindia.com/corporate/xbrl/INDAS_C.xml"},
            {"toDate": "31-Mar-2023", "broadCastDate": "27-Apr-2023 14:40:00", "consolidated": "Non-Consolidated",
             "xbrl": "https://nsearchives.nseindia.com/corporate/xbrl/INDAS_S.xml"},
            {"toDate": "31-Dec-2015", "broadCastDate": "28-Jan-2016 10:00:00", "consolidated": "Consolidated",
             "xbrl": "-"}]
NEW_LIST = {"data": [{"qe_Date": "31-MAR-2026", "broadcast_Date": "20-Apr-2026 07:23:32", "consolidated": "Consolidated",
                      "xbrl": "https://nsearchives.nseindia.com/corporate/xbrl/INTEGRATED_FILING_BANKING_1.xml"}]}


def test_results_history_lists_both_feeds_prefers_consolidated_and_caches(tmp_path):
    sess = FakeSession({("GET", "corporates-financial-results"): OLD_LIST,
                        ("GET", "integrated-filing-results"): NEW_LIST,
                        ("GET", "INDAS_C.xml"): COMPANY, ("GET", "INDAS_S.xml"): "<x/>",
                        ("GET", "INTEGRATED_FILING_BANKING_1.xml"): BANK})
    h = ResultsHistory(tmp_path, session=sess, sleep=lambda s: None)
    recs = h.history("coforge")
    assert [r["period_end"] for r in recs] == ["2023-03-31", "2026-03-31"]
    assert recs[0]["consolidated"] and recs[0]["available"] == "2023-04-27T14:42:12"
    assert not any("INDAS_S" in u for _, u, _ in sess.calls)  # standalone skipped when consolidated exists
    assert h.downloads == 2
    again = ResultsHistory(tmp_path, session=FakeSession({}), max_new_downloads=0)
    assert [r["profit_ytd"] for r in again.history("COFORGE")] == [6800e6, 70168.6e6]  # all from cache
    empty = ResultsHistory(tmp_path / "other", session=sess, max_new_downloads=0)
    assert empty.history("COFORGE") == []  # nothing cached and no download budget


class TwoRisers(Prices):
    def history(self, sym, range_="2y"):
        return super().history("UP" if sym == "UP2" else sym, range_)


class FakeFundamentals:
    def __init__(self):
        self.good = quarters(lambda i: 10.0 + i, start=date(2019, 6, 30), n=26)
        self.loss = quarters(lambda i: -5.0, start=date(2019, 6, 30), n=26)

    def history(self, sym):
        return {"UP": self.good, "UP2": self.loss}.get(sym, [])


def test_backtest_drops_loss_makers_point_in_time():
    uni = [{"symbol": s, "industry": "Industrials"} for s in ("UP", "UP2", "FLAT")]
    plain = run_factor_backtest(uni, TwoRisers(900), top=2, years=2, min_turnover=0, workers=2)
    assert any("UP2" in p["picks"] for p in plain["picks"]) and plain["fundamentals"] is None
    q = run_factor_backtest(uni, TwoRisers(900), top=2, years=2, min_turnover=0, workers=2,
                            fundamentals=FakeFundamentals(), quality=1.0)
    assert all("UP2" not in p["picks"] and "UP" in p["picks"] for p in q["picks"])
    assert q["fundamentals"]["quality"] == 1.0 and q["fundamentals"]["avg_coverage"] > 0.5
    assert "share count" in q["fundamentals"]["note"]


def test_stalled_downloads_are_retried_later_not_cached(tmp_path):
    import requests
    listing = [{"toDate": f"{d}-2023", "broadCastDate": f"01-{n}-2024 10:00:00", "consolidated": "Consolidated",
                "xbrl": f"https://nsearchives.nseindia.com/corporate/xbrl/F{i}.xml"}
               for i, (d, n) in enumerate((("31-Mar", "May"), ("30-Jun", "Aug"), ("30-Sep", "Nov"), ("31-Dec", "Feb")))]
    sess = FakeSession({("GET", "corporates-financial-results"): listing, ("GET", "integrated-filing-results"): {"data": []},
                        ("GET", ".xml"): requests.Timeout("read timed out")})
    sleeps = []
    h = ResultsHistory(tmp_path, session=sess, sleep=sleeps.append)
    h.session = sess  # keep the fake after the back-off swaps in a fresh connection
    orig = requests.Session
    requests.Session = lambda: sess
    try:
        assert h.history("X") == []
    finally:
        requests.Session = orig
    assert h.refused == 2 and 60 in sleeps
    assert len([u for _, u, _ in sess.calls if u.endswith(".xml")]) == 2  # stopped after the second stall
    assert not (tmp_path / "nse_results" / "filings_v2").exists()  # nothing cached: retried next run
    ok = ResultsHistory(tmp_path, session=FakeSession({("GET", "corporates-financial-results"): listing[:1],
                                                       ("GET", "integrated-filing-results"): {"data": []},
                                                       ("GET", ".xml"): COMPANY}), sleep=lambda s: None)
    assert len(ok.history("X")) == 4  # the next run downloads them (listing reused from cache)
