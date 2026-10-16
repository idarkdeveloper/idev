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
    assert r["pbo"] is None or 0 <= r["pbo"]["pbo"] <= 1
    assert "Probability of backtest overfitting" in text


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
    assert r["pbo"] is not None and r["pbo"]["variants"] >= 7 and "Probability of backtest overfitting" in text
    assert rs.prior_trials(None) == 0


# --------------------------------------------------------------------------- #
# robustness: next-day fills, cost stress, the 25% floor
# --------------------------------------------------------------------------- #
def test_next_day_fills_trade_one_session_after_the_decision():
    m = market({"A": [100.0 + i for i in range(60)]})
    same = simulate(m, Rules(top=1, liquid_rate=0.0), 0, 59, ranker=fixed("A"))
    late = simulate(m, Rules(top=1, liquid_rate=0.0, lag=1), 0, 59, ranker=fixed("A"))
    assert same["exposure"][0] > 0.99 and late["exposure"][0] == 0.0 and late["exposure"][1] > 0.99
    assert late["curve"][-1] < same["curve"][-1]            # a rising stock bought a day later costs more
    seen = []
    simulate(m, Rules(top=1, liquid_rate=0.0, lag=1), 0, 59,
             ranker=lambda mk, t: seen.append(t) or ["A"])
    assert seen and seen[0] == 0                             # ranked on the decision day, not the fill day


def test_cost_multiplier_scales_every_charge():
    m = market({"A": [100.0] * 80, "B": [100.0] * 80})
    flip = lambda mk, t: ["A"] if mk.dates[t][5:7] in ("01", "03") else ["B"]   # noqa: E731 - trades each month
    one = simulate(m, Rules(top=1, liquid_rate=0.0), 0, 79, cost_model=IndianDeliveryCosts(), ranker=flip)
    two = simulate(m, Rules(top=1, liquid_rate=0.0, cost_mult=2.0), 0, 79, cost_model=IndianDeliveryCosts(),
                   ranker=flip)
    assert one["costs"] > 0 and two["costs"] == pytest.approx(2 * one["costs"], rel=0.02)
    assert Rules(cost_mult=2.0, lag=1, floor=0.25).label() == "floor25%, fills +1 day, costs x2"


def test_floor_sells_after_a_25_percent_fall_and_waits_for_the_next_rebalance():
    crash = [100.0] * 5 + [100 - 3 * k for k in range(1, 15)] + [58.0] * 21
    m = market({"A": crash})
    end = len(crash) - 1
    kept = simulate(m, Rules(top=1, liquid_rate=0.0), 0, end, ranker=fixed("A"))
    floor = simulate(m, Rules(top=1, liquid_rate=0.0, floor=0.25), 0, end, ranker=fixed("A"))
    assert kept["events"]["floors"] == 0 and floor["events"]["floors"] == 1
    sold = floor["trades"][0]
    assert sold["reason"] == "floor" and -0.30 < sold["return"] <= -0.25
    assert floor["curve"][-1] > kept["curve"][-1]
    late = simulate(m, Rules(top=1, liquid_rate=0.0, floor=0.25, lag=1), 0, end, ranker=fixed("A"))
    assert late["trades"][0]["exit"] > sold["exit"]          # decided on the close, sold the next session


def test_robustness_end_to_end(synthetic):
    from trading_agent import research_studies as rs
    synthetic.missing = ["GONE1", "GONE2"]
    r = rs.robustness(synthetic, cost_model=IndianDeliveryCosts(), extra_trials=4)
    res = r["results"]
    assert list(res) == [name for name, _ in rs.ROBUSTNESS]
    assert res["costs x2"]["costs"] > res["adopted (+ 25% floor)"]["costs"] * 1.8
    assert res["costs x2"]["total_return"] < res["adopted (+ 25% floor)"]["total_return"]
    assert res["adopted (+ 25% floor)"]["skill"]["trials"] == len(rs.ROBUSTNESS) + 4
    assert r["missing"] == ["GONE1", "GONE2"] and r["members"] == len(synthetic.raw) + 2
    assert r["holds"] == all(x["excess"] > 0 for x in res.values())
    text = rs.format_robustness(r)
    assert "next-day fills + costs x2" in text and "Survivorship: 2 of" in text and "GONE1" in text
    assert "Verdict:" in text and "holdout stays sealed" in text


def test_research_robustness_command(synthetic, tmp_path, monkeypatch, capsys):
    import json as _json
    from types import SimpleNamespace

    from trading_agent import cli
    settings = SimpleNamespace(state_dir=tmp_path, paper_starting_cash=500_000.0)
    monkeypatch.setattr(cli, "_settings", lambda a: settings)
    monkeypatch.setattr(cli, "_research_market", lambda a, s: synthetic)
    args = SimpleNamespace(universe="NIFTYMIDCAP150", years=5, until="2025-10-01", json=False)
    assert cli.cmd_research_robustness(args) == 0
    out = capsys.readouterr().out
    assert "Robustness of the adopted rules" in out and "next-day fills + costs x2" in out and "Verdict:" in out
    log = [_json.loads(x) for x in (tmp_path / "research" / "trials.jsonl").read_text().splitlines()]
    assert len(log) == 6 and all(x["study"] == "robustness" for x in log)
