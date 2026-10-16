"""The daily research simulator and the B1-B4 studies. Synthetic prices only, no network."""
import math
import random
from datetime import date, timedelta

import pytest

from trading_agent.costs import IndianDeliveryCosts
from trading_agent.research_sim import (Market, Rules, atr_close, ema, fy_of, inverse_vol_weights, simulate, sma,
                                        stats, summarize, tax_due)


def days(n, start=date(2019, 1, 1)):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def bars(dates, prices):
    return [{"date": d, "close": p, "adj_close": p, "volume": 1e6} for d, p in zip(dates, prices)]


def market(series, fund=None, n=None, members=None):
    n = n or len(next(iter(series.values())))
    dates = days(n)
    fund = fund or [100.0] * n
    return Market(dates, {s: bars(dates, p) for s, p in series.items()}, bars(dates, fund),
                  lambda d: set(members or series))


def fixed(*names):
    return lambda m, t: list(names)


# --------------------------------------------------------------------------- #
# indicators and helpers
# --------------------------------------------------------------------------- #
def test_indicators():
    assert sma([1, 2, 3, 4], 2) == [None, 1.5, 2.5, 3.5]
    e = ema([10.0] * 5, 3)
    assert e[:2] == [None, None] and e[2:] == [10.0, 10.0, 10.0]
    assert atr_close([10, 11, 10, 11, 10], 2)[-1] == 1.0
    assert fy_of("2025-03-31") == "FY2025" and fy_of("2025-04-01") == "FY2026"


def test_tax_rates_change_on_23_july_2024():
    assert tax_due(100_000, 0, "2024-07-22") == pytest.approx(15_000)
    assert tax_due(100_000, 0, "2024-07-23") == pytest.approx(20_000)
    assert tax_due(0, 225_000, "2025-01-01") == pytest.approx(0.125 * 100_000)     # ₹1.25 lakh exempt
    assert tax_due(-50_000, 225_000, "2025-01-01") == pytest.approx(0.125 * 50_000)  # ST loss set off first
    assert tax_due(-10, -10, "2025-01-01") == 0


def test_inverse_vol_weights_stay_within_bounds():
    vols = {f"S{i}": 0.1 + 0.05 * i for i in range(20)}
    w = inverse_vol_weights(vols, 0.025, 0.08)
    assert sum(w.values()) == pytest.approx(1.0)
    assert all(0.025 - 1e-9 <= x <= 0.08 + 1e-9 for x in w.values())
    assert w["S0"] > w["S19"]
    assert sum(inverse_vol_weights({"A": 0.1, "B": 0.1}, 0.025, 0.08).values()) == pytest.approx(0.16)  # capped


# --------------------------------------------------------------------------- #
# the simulation
# --------------------------------------------------------------------------- #
def test_flat_prices_no_costs_no_interest_keep_the_capital():
    m = market({"A": [100.0] * 300, "B": [100.0] * 300})
    r = simulate(m, Rules(top=2, liquid_rate=0.0), 0, 299, ranker=fixed("A", "B"))
    assert r["curve"][0] == pytest.approx(500_000) and r["curve"][-1] == pytest.approx(500_000)
    assert r["exposure"][-1] == pytest.approx(1.0)


def test_cash_earns_the_liquid_rate_and_charges_are_paid():
    m = market({"A": [100.0] * 300})
    r = simulate(m, Rules(top=1), 0, 299, ranker=fixed())   # never invested
    years = (date.fromisoformat(m.dates[-1]) - date.fromisoformat(m.dates[0])).days / 365
    assert r["curve"][-1] == pytest.approx(500_000 * 1.065 ** years, rel=1e-6)
    paid = simulate(m, Rules(top=1, liquid_rate=0.0), 0, 299, cost_model=IndianDeliveryCosts(), ranker=fixed("A"))
    assert paid["costs"] > 0 and paid["curve"][-1] < 500_000


def test_stop_rules():
    up_then_crash = [100 + (i % 2) for i in range(60)] + [100 - 2 * k for k in range(1, 40)]
    m = market({"A": up_then_crash})
    end = len(up_then_crash) - 1
    for rule in ("atr3", "atr5", "ema50", "two_stage"):
        r = simulate(m, Rules(top=1, stop=rule, liquid_rate=0.0), 0, end, ranker=fixed("A"))
        assert r["events"]["stops"] >= 1, rule
    # the 20-day grace: a fall inside the first 20 days does not stop it
    early = [100.0, 101.0] * 10 + [90.0] * 5 + [101.0, 102.0] * 25   # falls, then recovers by day 20
    m2 = market({"A": early})
    g = simulate(m2, Rules(top=1, stop="grace20", liquid_rate=0.0), 15, len(early) - 1, ranker=fixed("A"))
    a3 = simulate(m2, Rules(top=1, stop="atr3", liquid_rate=0.0), 15, len(early) - 1, ranker=fixed("A"))
    assert a3["events"]["stops"] >= 1 and g["events"]["stops"] == 0
    # a stopped stock is not bought back before the next rebalance
    assert all(tr["reason"] == "stop" for tr in a3["trades"])


def test_two_stage_moves_to_break_even():
    rise_then_fall = [100 + (i % 2) for i in range(30)] + [100 + 2 * k for k in range(1, 15)] + [101.5] * 10
    m = market({"A": rise_then_fall})
    r = simulate(m, Rules(top=1, stop="two_stage", liquid_rate=0.0), 29, len(rise_then_fall) - 1, ranker=fixed("A"))
    assert r["events"]["stops"] == 1 and r["trades"][0]["return"] >= 0          # sold at or above break-even


def test_brake_trims_and_restores():
    n = 400
    fund = [100 + 0.2 * i for i in range(250)] + [150 - 0.5 * k for k in range(1, 81)] + [110 + 0.6 * k for k in range(70)]
    m = market({"A": [100.0] * n}, fund=fund)
    r = simulate(m, Rules(top=1, brake="ema", liquid_rate=0.0), 0, n - 1, ranker=fixed("A"))
    lows = min(r["exposure"][260:340])
    assert lows < 0.6 and r["events"]["brake_trades"] >= 2 and max(r["exposure"][:240]) > 0.99
    s100 = simulate(m, Rules(top=1, brake="sma100", liquid_rate=0.0), 0, n - 1, ranker=fixed("A"))
    assert min(s100["exposure"][260:340]) == pytest.approx(0.0, abs=1e-9)


def test_rank_buffer_keeps_holdings_and_trades_less():
    rng = random.Random(1)
    names = [f"S{i}" for i in range(6)]
    series = {s: [100.0 + rng.uniform(-1, 1) for _ in range(300)] for s in names}
    m = market(series)

    def shuffling(mk, t):   # the order changes every month but S0..S3 stay in the top 4
        order = names[:4] if (t // 20) % 2 == 0 else list(reversed(names[:4]))
        return order + names[4:]
    plain = simulate(m, Rules(top=2, liquid_rate=0.0), 0, 299, cost_model=IndianDeliveryCosts(), ranker=shuffling)
    buf = simulate(m, Rules(top=2, buffer=4, liquid_rate=0.0), 0, 299, cost_model=IndianDeliveryCosts(),
                   ranker=shuffling)
    assert buf["traded"] < plain["traded"] and buf["costs"] < plain["costs"]


def test_tax_is_paid_at_the_end_of_each_financial_year():
    n = 600
    m = market({"A": [100 + 0.2 * i for i in range(n)], "B": [100.0] * n})

    def alternate(mk, t):
        return ["A"] if mk.dates[t][5:7] in ("01", "03", "05", "07", "09", "11") else ["B"]
    pre = simulate(m, Rules(top=1, liquid_rate=0.0), 0, n - 1, ranker=alternate)
    post = simulate(m, Rules(top=1, liquid_rate=0.0, tax=True), 0, n - 1, ranker=alternate)
    assert post["tax_paid"] > 0 and post["curve"][-1] < pre["curve"][-1] and len(post["tax_by_fy"]) >= 2


def test_summary_numbers():
    m = market({"A": [100 * 1.001 ** i for i in range(300)]}, fund=[100 * 1.0005 ** i for i in range(300)])
    r = simulate(m, Rules(top=1, liquid_rate=0.0), 0, 299, ranker=fixed("A"))
    s = summarize(r, m, 0, 299)
    assert s["excess"] > 0 and s["max_drawdown"] == pytest.approx(0.0) and s["avg_invested"] == pytest.approx(1, abs=1e-6)
    assert stats([100, 50, 100], ["2020-01-01", "2020-06-01", "2021-01-01"])["max_drawdown"] == pytest.approx(-0.5)


# --------------------------------------------------------------------------- #
# the studies end to end on a synthetic 30-stock market
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def synthetic():
    rng = random.Random(7)
    n = 1500
    dates = days(n, date(2019, 6, 3))
    series = {}
    for k in range(30):
        drift, vol, p = rng.uniform(-0.0002, 0.0012), rng.uniform(0.008, 0.025), 100.0
        path = []
        for _ in range(n):
            p *= math.exp(drift + vol * rng.gauss(0, 1))
            path.append(p)
        series[f"S{k:02d}"] = bars(dates, path)
    fp, fund = 100.0, []
    for i in range(n):
        fp *= math.exp(0.0004 + 0.01 * rng.gauss(0, 1) - (0.004 if 700 < i < 760 else 0))
        fund.append(fp)
    return Market(dates, series, bars(dates, fund), lambda d: set(series), min_turnover=0)


def test_stop_race_end_to_end(synthetic, monkeypatch):
    from trading_agent import research_studies as rs
    monkeypatch.setattr(rs, "WINDOW_STARTS", ["2021-01-01", "2021-09-01", "2022-05-01"])
    r = rs.stop_race(synthetic, cost_model=IndianDeliveryCosts(), extra_trials=3)
    assert set(r["rules"]) == set(rs.STOP_SET) and len(r["windows"]) == 3
    assert r["rules"]["none"]["median_window_excess"] is not None
    assert r["rules"]["none"]["median_stops_per_window"] == 0 and r["rules"]["atr3"]["median_stops_per_window"] > 0
    assert r["rules"]["atr5"]["skill"]["trials"] == 6 + 3
    for rule in ("atr5", "two_stage", "grace20", "ema50"):
        assert set(r["rules"][rule]["checks"]) == {"holds 60+ days", "worst fall no worse than 3xATR",
                                                   "clears the skill bar"}
    text = rs.format_stop_race(r)
    assert "Recommendation:" in text and "two_stage" in text and "close-to-close ATR" in text


def test_portfolio_controls_end_to_end(synthetic):
    from trading_agent import research_studies as rs
    r = rs.portfolio_controls(synthetic, cost_model=IndianDeliveryCosts())
    res = r["results"]
    assert {"baseline", "buffer35", "buffer35_tax_aware", "brake_ema", "brake_sma100", "brake_ema_breadth",
            "inverse_vol"} <= set(res)
    assert "brake_ema_flows" not in res and "not testable" in r["notes"]["brake_ema_flows"]
    assert res["buffer35"]["turnover"] < res["baseline"]["turnover"]
    assert res["brake_ema"]["avg_invested"] < res["baseline"]["avg_invested"]
    assert all(x["tax_paid"] >= 0 for x in res.values())
    text = rs.format_controls(r)
    assert "baseline" in text and "inverse_vol" in text and "after Indian capital-gains tax" in text
    assert rs.prior_trials(None) == 0
