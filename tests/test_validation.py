import math

import pytest

from trading_agent.factor_backtest import validate_factor_backtest
from trading_agent.validation import (deflated_sharpe, expected_max_sharpe, moments, monte_carlo, period_returns,
                                      probabilistic_sharpe, sharpe, walk_forward)

from .test_signal_lab import World
from trading_agent.costs import IndianDeliveryCosts
from trading_agent.signal_lab import run_signal_lab


def test_period_returns_skips_none_and_nonpositive():
    assert period_returns([100, 110, None, 121, 0]) == pytest.approx([0.1, 0.1])
    assert period_returns([1.0]) == []


def test_sharpe_and_moments():
    assert sharpe([0.01, 0.01]) is None
    assert sharpe([0.01, 0.01, 0.01]) is None  # zero stdev
    assert sharpe([0.1, -0.1, 0.1, -0.1]) == pytest.approx(0.0)
    assert sharpe([0.02, 0.0, 0.04]) == pytest.approx(1.2247, abs=1e-3)
    sk, ku = moments([-1.0, 1.0, -1.0, 1.0])
    assert sk == pytest.approx(0) and ku == pytest.approx(1.0)


def test_probabilistic_sharpe_required_value():
    assert probabilistic_sharpe(0.1, 100, 0.0, 3.0) == pytest.approx(0.8395, abs=5e-4)
    assert probabilistic_sharpe(0.0, 100, 0.0, 3.0) == pytest.approx(0.5)


def test_expected_max_sharpe():
    assert expected_max_sharpe(1, 0.04) == 0
    assert expected_max_sharpe(10, 0.0) == 0
    a, b = expected_max_sharpe(5, 0.04), expected_max_sharpe(50, 0.04)
    assert 0 < a < b


def test_deflated_sharpe_one_trial_equals_psr_and_more_trials_lower():
    rets = [0.03, -0.01, 0.02, 0.04, -0.02, 0.01, 0.03, 0.0, 0.02, -0.01, 0.03, 0.01]
    s = sharpe(rets)
    sk, ku = moments(rets)
    one = deflated_sharpe(rets, 1, [s])
    assert one["probability"] == pytest.approx(probabilistic_sharpe(s, len(rets), sk, ku))
    spread = [s, s - 0.4, s + 0.3, s - 0.2]
    many = deflated_sharpe(rets, 4, spread)
    assert many["probability"] <= one["probability"]
    assert set(many) == {"probability", "sharpe", "threshold", "trials", "periods"}
    assert deflated_sharpe([0.01, 0.01, 0.01], 3, [])["probability"] is None


def test_monte_carlo():
    assert monte_carlo([0.01] * 5) is None
    c = monte_carlo([0.02] * 12)
    assert c["final_return"]["p5"] == c["final_return"]["p50"] == c["final_return"]["p95"]
    assert c["loss_probability"] == 0
    rets = [0.05, -0.04, 0.02, -0.06, 0.03, 0.01, -0.02, 0.04, -0.01, 0.02]
    a, b = monte_carlo(rets, n_paths=500, seed=3), monte_carlo(rets, n_paths=500, seed=3)
    assert a == b and a["paths"] == 500 and a["block"] == 3
    assert all(v <= 0 for v in a["max_drawdown"].values())
    assert a["max_drawdown"]["p5"] <= a["max_drawdown"]["p50"] <= a["max_drawdown"]["p95"]
    assert a["final_return"]["p5"] <= a["final_return"]["p95"]


def _dates():
    return [f"{y}-{m:02d}-02" for y in (2021, 2022, 2023, 2024) for m in (1, 4, 7, 10)] + ["2025-01-02"]


def _curve(yearly):
    """Curve over _dates() growing by the given yearly return, spread evenly over each year's 4 steps."""
    out, v = [100.0], 100.0
    for g in yearly:
        for w in (1.01, 1 / 1.01, 1.01, 1 / 1.01):  # wiggle that cancels over the year
            v *= (1 + g) ** 0.25 * w
            out.append(v)
    return out[:len(_dates())]


def test_walk_forward_picks_best_of_earlier_years():
    dates = _dates()
    runs = {10: _curve([0.30, 0.00, 0.10, 0.10]), 30: _curve([0.00, 0.20, 0.05, 0.05])}
    fund = _curve([0.10, 0.10, 0.10, 0.10])
    wf = walk_forward(runs, dates, fund)
    assert wf["candidates"] == [10, 30]
    assert [y["year"] for y in wf["years"]] == [2022, 2023, 2024]
    # 2022: only year 1 known -> top 10; 2023: 1.30*1.0=1.30 vs 1.0*1.2=1.2 -> still 10; 2024: 10: 1.43 vs 30: 1.26
    assert [y["chosen_top"] for y in wf["years"]] == [10, 10, 10]
    assert [y["return"] for y in wf["years"]] == pytest.approx([0.0, 0.10, 0.10], abs=1e-6)
    assert wf["oos_return"] == pytest.approx(1.1 * 1.1 - 1, abs=1e-6)
    assert wf["fund_return"] == pytest.approx(1.1 ** 3 - 1, abs=1e-6)
    assert wf["beat_years"] == 0
    # switching: top 30 best in year 1 and 2 combined
    runs2 = {10: _curve([0.00, 0.30, 0.0, 0.0]), 30: _curve([0.10, 0.10, 0.5, 0.0])}
    wf2 = walk_forward(runs2, dates, None)
    assert [y["chosen_top"] for y in wf2["years"]] == [30, 10, 30]  # 2024: 30's 1.1*1.1*1.5 beats 10's 1.3
    assert wf2["fund_return"] is None and wf2["years"][0]["fund_return"] is None


def test_walk_forward_tie_prefers_larger_top():
    dates = _dates()
    runs = {10: _curve([0.1, 0.1, 0.1, 0.1]), 20: _curve([0.1, 0.1, 0.1, 0.1])}
    wf = walk_forward(runs, dates, None)
    assert all(y["chosen_top"] == 20 for y in wf["years"])


def _result(top, yearly):
    c = _curve(yearly)
    return {"dates": _dates(), "strategy": c, "index_fund": _curve([0.05] * 4), "top": top}


def test_validate_factor_backtest_wiring_and_verdict():
    calls = []
    table = {10: [0.25, 0.20, 0.25, 0.22], 20: [0.20, 0.18, 0.2, 0.2], 30: [0.1, 0.1, 0.1, 0.1]}

    def run_fn(top):
        calls.append(top)
        return _result(top, table[top])

    base = _result(20, table[20])
    seen = []
    v = validate_factor_backtest(run_fn, 20, base=base, progress=seen.append)
    assert sorted(calls) == [10, 30]  # base run is not recomputed
    assert v["deflated_sharpe"]["trials"] == 3
    assert v["walk_forward"]["candidates"] == [10, 20, 30]
    assert v["monte_carlo"] is not None
    assert v["verdict"] in {"likely skill", "could be luck", "no edge"}
    assert v["verdict"] == "likely skill" or isinstance(v["summary"], str)
    assert v["summary"] and seen

    # losing to the fund => no edge
    bad = {10: [0.0] * 4, 20: [-0.02] * 4, 30: [0.0] * 4}
    v2 = validate_factor_backtest(lambda t: _result(t, bad[t]), 20)
    assert v2["verdict"] == "no edge"


def test_signal_lab_deflated_sharpe_on_every_trial():
    w = World(drift=True)
    r = run_signal_lab(w.universe, w, horizons=(20, 60), years=4, cost_model=IndianDeliveryCosts(), min_names=10)
    trials = [s for h in r["results"].values() for s in h.values() if s.get("spread_sharpe") is not None]
    assert trials and r["trials"] == len(trials)
    for s in trials:
        assert 0.0 <= s["deflated_sharpe"] <= 1.0
        assert "spread_skew" in s and "spread_kurt" in s


def test_format_validation_reads_in_plain_english():
    from trading_agent.factor_backtest import format_validation
    r = _result(20, [0.2, 0.2, 0.2, 0.2])
    v = validate_factor_backtest(lambda t: _result(t, [0.2, 0.2, 0.2, 0.2]), 20, base=r)
    text = format_validation(v)
    assert "Skill or luck?" in text and "Deflated Sharpe" in text and "Monte Carlo" in text and "2022" in text
