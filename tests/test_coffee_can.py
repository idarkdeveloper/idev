"""Research C: the Coffee Can screen. Synthetic filings and prices only, no network."""
from datetime import date

import pytest

from trading_agent.coffee_can import (UnionMembership, annual_rows, coffee_can_ranker, is_financial, qualifies,
                                      reason_kind)
from trading_agent.fundamentals_history import ResultsHistory, parse_results_xbrl
from trading_agent.research_sim import Market, Rules, simulate

from .test_fundamentals_history import xbrl
from .test_research_sim import bars, days


def fy(year, revenue, pbt, finance, equity, debt, filed=None):
    return {"period_end": f"{year}-03-31", "revenue_ytd": revenue, "pbt_ytd": pbt, "finance_ytd": finance,
            "equity": equity, "borrowings": debt, "available": filed or f"{year}-05-20T18:00:00"}


GROWER = [fy(2019 + k, 100 * 1.2 ** k, 30 * 1.2 ** k, 2, 120 * 1.15 ** k, 20) for k in range(6)]


def test_roce_and_growth_by_hand():
    rows = annual_rows(GROWER, "2024-06-01")
    assert [r["fy"] for r in rows] == [f"{y}-03-31" for y in range(2019, 2025)]
    assert rows[0]["growth"] is None and rows[1]["growth"] == pytest.approx(0.2)
    assert rows[0]["roce"] == pytest.approx((30 + 2) / (120 + 20))
    assert annual_rows(GROWER, "2024-05-20")[-1]["fy"] == "2023-03-31"     # filed at 18:00: usable the next day


def test_qualifies_needs_every_year():
    assert qualifies(annual_rows(GROWER, "2024-06-01"), 4) == (True, "passes every year")
    assert qualifies(annual_rows(GROWER, "2022-06-01"), 4)[0] is False      # only 3 years of growth then
    slow = [dict(r) for r in GROWER]
    slow[3]["revenue_ytd"] = slow[2]["revenue_ytd"] * 1.05
    ok, why = qualifies(annual_rows(slow, "2024-06-01"), 4)
    assert not ok and "revenue growth" in why and "FY2022" in why
    weak = [dict(r, pbt_ytd=5.0) for r in GROWER]
    ok, why = qualifies(annual_rows(weak, "2024-06-01"), 4)
    assert not ok and "ROCE" in why
    gap = [r for r in GROWER if not r["period_end"].startswith("2021")]    # a missing year breaks the chain
    assert annual_rows(gap, "2024-06-01")[2]["growth"] is None


def test_a_rolling_window_ignores_older_years():
    covid = [dict(r) for r in GROWER]
    covid[1]["revenue_ytd"] = covid[0]["revenue_ytd"] * 0.9          # FY2020 fell, every later year grew 20%+
    for k in range(2, 6):
        covid[k]["revenue_ytd"] = covid[1]["revenue_ytd"] * 1.2 ** (k - 1)
    rows = annual_rows(covid, "2024-06-01")
    assert qualifies(rows, 4)[0] is False and "FY2020" in qualifies(rows, 4)[1]   # every year seen: the fall counts
    assert qualifies(rows, 4, window=4) == (True, "passes every year")            # the latest 4 years only
    assert qualifies(annual_rows(covid, "2023-06-01"), 4, window=4)[0] is False    # FY2020 still inside the window


def test_reasons_are_counted_without_the_numbers():
    assert reason_kind("revenue growth -12% in FY2021") == "revenue growth ≤10% in FY2021"
    assert reason_kind("ROCE 9% in FY2020") == "ROCE ≤15% in FY2020"
    assert reason_kind("ROCE n/a in FY2022") == "ROCE n/a in FY2022"
    assert reason_kind("only 2 year(s) of growth") == "only 2 year(s) of growth"


def test_financials_are_left_out():
    assert is_financial("Financial Services") and is_financial("Banks") and not is_financial("Capital Goods")


def test_ranker_and_a_yearly_portfolio():
    n = 1500   # weekdays: Jan 2019 to Oct 2024
    dates = days(n, date(2019, 1, 1))
    series = {s: bars(dates, [100 * (1.0006 if s == "GOOD" else 1.0002) ** i for i in range(n)])
              for s in ("GOOD", "BANK", "SLOW")}
    m = Market(dates, series, bars(dates, [100 * 1.0003 ** i for i in range(n)]), lambda d: set(series),
               min_turnover=0)
    slow = [dict(r, revenue_ytd=100.0) for r in GROWER]
    annual = {"GOOD": GROWER, "BANK": GROWER, "SLOW": slow}
    log, reasons = {}, {}
    rank = coffee_can_ranker(annual, {"BANK": "Financial Services"}, min_years=4, min_turnover=0, log=log,
                             reasons=reasons)
    july = next(t for t, d in enumerate(dates) if d >= "2024-07-01")
    assert rank(m, july) == ["GOOD"] and log[dates[july]] == ["GOOD"]
    assert reasons[dates[july]] == {"bank or finance company (left out)": 1, "revenue growth ≤10% in FY2020": 1}
    r = simulate(m, Rules(top=40, weights="equal_picks", rebalance="yearly", rebalance_month=7, liquid_rate=0.0),
                 july, n - 1, ranker=rank)
    assert r["exposure"][5] == pytest.approx(1.0)                         # one name, the whole portfolio
    assert m.rebalance_days(0, 400, "yearly", 7)[1:] == [t for t, d in enumerate(dates[:401])
                                                         if d[5:7] == "07" and dates[t - 1][5:7] == "06"]


def test_union_membership():
    class M:
        def __init__(self, s):
            self.s = set(s)

        def members_on(self, d):
            return self.s

        def ever_members(self, d):
            return self.s | {"OLD"}
    u = UnionMembership([M({"A"}), M({"B"}), None])
    assert u.members_on("2024-01-01") == {"A", "B"} and u.ever_members("2020-01-01") == {"A", "B", "OLD"}


def test_xbrl_full_year_fields_and_the_annual_reader(tmp_path):
    x = xbrl([("RevenueFromOperations", "FourD", "4000"), ("ProfitBeforeTax", "FourD", "900"),
              ("FinanceCosts", "FourD", "50"), ("ProfitLossForPeriod", "OneD", "200")])
    r = parse_results_xbrl(x, date(2024, 3, 31))
    assert (r["revenue_ytd"], r["pbt_ytd"], r["finance_ytd"]) == (4000.0, 900.0, 50.0)

    class Sess:
        def __init__(self):
            self.urls = []

        def get(self, url, **kw):
            self.urls.append(url)
            return type("R", (), {"content": x.encode(), "raise_for_status": lambda s: None})()
    h = ResultsHistory(tmp_path, session=Sess(), pause=0)
    h.filings = lambda sym: [  # type: ignore[method-assign]
        {"period_end": "2024-03-31", "available": "2024-05-20T18:00:00", "consolidated": False, "xbrl": "s.xml"},
        {"period_end": "2024-03-31", "available": "2024-05-21T18:00:00", "consolidated": True, "xbrl": "c.xml"},
        {"period_end": "2024-06-30", "available": "2024-08-10T18:00:00", "consolidated": True, "xbrl": "q.xml"}]
    out = h.annual("X")
    assert len(out) == 1 and out[0]["consolidated"] is True and out[0]["pbt_ytd"] == 900.0
    assert h.session.urls == ["c.xml"] and (tmp_path / "nse_results" / "filings_v3" / "c.json").exists()
