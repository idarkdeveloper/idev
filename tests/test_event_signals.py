"""Post-results drift and who-traded signals: SUE and friends by hand, the insider cache, the IC study and the
ranker. Synthetic data only, no network."""
import math
import random
from datetime import date

import pytest

from trading_agent.event_signals import (EventData, InsiderHistory, earnings_signals, norm_pit, pead_ranker,
                                         promoter_net, signal_study)
from trading_agent.research_sim import Market

from .test_research_sim import bars, days

ENDS = ["2022-03-31", "2022-06-30", "2022-09-30", "2022-12-31", "2023-03-31", "2023-06-30", "2023-09-30",
        "2023-12-31", "2024-03-31", "2024-06-30", "2024-09-30", "2024-12-31"]


def rec(pe, eps, rev, profit=None, filed=None):
    filed = filed or f"{pe[:4]}-{int(pe[5:7]) % 12 + 1:02d}-10T18:30:00" if pe[5:7] != "12" else f"{int(pe[:4]) + 1}-01-10T18:30:00"
    return {"period_end": pe, "eps_q": eps, "revenue_q": rev, "profit_q": profit if profit is not None else eps * 10,
            "available": filed}


def test_sue_by_hand():
    eps = [1, 2, 3, 4, 2, 3, 4, 5, 3, 4, 5, 8]          # year-on-year changes: 1 x 7, then 8 - 5 = 3
    rs = [rec(e, v, 100 + i) for i, (e, v) in enumerate(zip(ENDS, eps))]
    s = earnings_signals(rs, "2025-01-20")
    diffs = [1, 1, 1, 1, 1, 1, 1, 3]
    sd = (sum((d - sum(diffs) / 8) ** 2 for d in diffs) / 8) ** 0.5
    assert s["quarter"] == "2024-12-31" and s["sue"] == pytest.approx(3 / sd)
    assert s["dsue"] is None                                              # the previous changes never varied: no SUE
    eps2 = [1, 2, 3, 4, 2, 4, 4, 6, 3, 4, 5, 9]
    s2 = earnings_signals([rec(e, v, 100) for e, v in zip(ENDS, eps2)], "2025-01-20")
    assert s2["dsue"] is not None and s2["dsue"] > 0
    assert earnings_signals(rs, "2025-06-30") is None                     # older than 95 days: stale
    assert earnings_signals(rs, "2022-01-01") is None                     # nothing filed yet


def test_a_filing_counts_only_once_usable():
    rs = [rec(e, v, 100) for e, v in zip(ENDS, [1, 2, 3, 4, 2, 3, 4, 5, 3, 4, 5, 8])]
    rs[-1]["available"] = "2025-01-10T15:30:00"            # after 15:00 on Fri 10 Jan: usable from Mon 13 Jan
    assert earnings_signals(rs, "2025-01-10")["quarter"] == "2024-09-30"
    assert earnings_signals(rs, "2025-01-13")["quarter"] == "2024-12-31"


def test_margin_acceleration_times_sales_surprise():
    rev = [100, 100, 100, 100, 110, 110, 110, 110, 121, 121, 121, 160]
    prof = [10, 10, 10, 10, 11, 11, 11, 11, 12.1, 12.1, 12.1, 24.0]
    rs = [rec(e, p / 10, r, profit=p) for e, r, p in zip(ENDS, rev, prof)]
    s = earnings_signals(rs, "2025-01-20")
    m_now = 24 / 160 - 11 / 110
    m_prev = 12.1 / 121 - 11 / 110
    assert s["margin_x_sales"] is not None and s["margin_x_sales"] > 0
    assert (m_now - m_prev) > 0


def test_norm_pit_and_promoter_net():
    row = {"symbol": "abc", "tdpTransactionType": "Buy", "secVal": "1,50,000", "personCategory": "Promoter Group",
           "date": "16-Oct-2024 19:20", "acqfromDt": "14-Oct-2024"}
    t = norm_pit(row)
    assert t == {"symbol": "ABC", "category": "Promoter Group", "side": "BUY", "value": 150000.0,
                 "seen": "2024-10-16", "traded": "2024-10-14"}
    assert norm_pit({**row, "tdpTransactionType": "Pledge"}) is None and norm_pit({**row, "secVal": "0"}) is None
    trades = [t, {**t, "side": "SELL", "value": 50000.0}, {**t, "category": "Director", "value": 9e9},
              {**t, "seen": "2024-01-01"}]
    assert promoter_net(trades, "2024-11-01") == {"ABC": 100000.0}
    assert promoter_net(trades, "2024-10-15") == {}                       # not disclosed yet


def test_insider_history_caches_past_months_and_refreshes_the_current(tmp_path):
    calls = []

    def fetch(path, params, referer):
        calls.append(params["from_date"])
        return {"data": [{"symbol": "X", "tdpTransactionType": "BUY", "secVal": "100", "personCategory": "Promoters",
                          "date": params["from_date"].replace("-", "-")}]}
    h = InsiderHistory(tmp_path, fetch, pause=0, today=lambda: date(2024, 3, 15))
    r = h.download(date(2024, 1, 1), date(2024, 3, 15))
    assert r == {"cached": 0, "fetched": 3, "failed": 0} and calls == ["01-01-2024", "01-02-2024", "01-03-2024"]
    r2 = h.download(date(2024, 1, 1), date(2024, 3, 15))
    assert r2["cached"] == 2 and r2["fetched"] == 1                       # only the current month again
    assert len(h.trades("2024-01-01", "2024-03-31")) == 3

    def broken(path, params, referer):
        raise RuntimeError("403")
    assert InsiderHistory(tmp_path / "b", broken, pause=0).download(date(2024, 1, 1), date(2024, 12, 1))["failed"] == 3


@pytest.fixture(scope="module")
def planted():
    """30 stocks; the half with a positive earnings surprise drifts up over the next months."""
    rng = random.Random(3)
    n = 900
    dates = days(n, date(2021, 1, 4))
    series, results = {}, {}
    for k in range(30):
        good = k % 2 == 0
        p, path = 100.0, []
        for i in range(n):
            p *= math.exp((0.0012 if good else 0.0) + 0.012 * rng.gauss(0, 1))
            path.append(p)
        sym = f"S{k:02d}"
        series[sym] = bars(dates, path)
        recs = []
        for y in range(2020, 2025):
            for mth, dd in ((3, 31), (6, 30), (9, 30), (12, 31)):
                pe = date(y, mth, dd)
                base = 1.0 + 0.1 * (y - 2020) * (1 if good else -1) + rng.uniform(-0.03, 0.03)
                fy, fm = (y + 1, 1) if mth == 12 else (y, mth + 1)
                recs.append({"period_end": pe.isoformat(), "eps_q": base, "profit_q": base * 10,
                             "revenue_q": 100 * base, "available": f"{fy}-{fm:02d}-10T18:00:00"})
        results[sym] = recs
    fund = bars(dates, [100 * 1.0004 ** i for i in range(n)])
    m = Market(dates, series, fund, lambda d: set(series), min_turnover=0)
    return m, EventData(m, results, [])


def test_signal_study_finds_a_planted_drift(planted):
    m, ev = planted
    r = signal_study(m, ev, 300, len(m.dates) - 1)
    assert set(r) == {"sue", "dsue", "margin_x_sales", "promo_x_mom", "promo_quiet"}
    assert r["sue"]["60"]["mean_ic"] > 0.1 and r["sue"]["60"]["months"] > 10
    assert r["promo_x_mom"]["20"]["verdict"] == "no data"                 # no insider history given


def test_pead_ranker_puts_the_surprises_first(planted):
    m, ev = planted
    t = 600
    plain, pead = m.momentum_ranking(t), pead_ranker(ev)(m, t)
    assert set(pead) == set(plain)
    top = pead[:6]
    assert sum(int(s[1:]) % 2 == 0 for s in top) >= 5
