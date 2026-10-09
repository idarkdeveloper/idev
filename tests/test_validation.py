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
    dates = _monthly(2021, 1, 49)
    comp = _market(48)
    edge = {10: 0.012, 20: 0.010, 30: 0.008}
    make = lambda t: _fake([c + edge[t] + e for c, e in zip(comp, _noise(48, t, 0.005))], comp, dates)  # noqa: E731
    calls, seen = [], []

    def run_fn(top):
        calls.append(top)
        return make(top)

    v = validate_factor_backtest(run_fn, 20, base=make(20), progress=seen.append, universe="NIFTYMIDCAP150")
    assert sorted(calls) == [10, 30]  # base run is not recomputed
    assert v["deflated_sharpe"]["trials"] == 3
    assert v["walk_forward"]["candidates"] == [10, 20, 30]
    assert v["monte_carlo"] is not None and len(seen) == 2
    assert v["comparison"] == {"symbol": "MID150BEES", "kind": "index fund", "note": ""}
    assert v["verdict"] == "likely skill" and v["summary"].startswith("Likely skill")
    assert v["walk_forward"]["full_years"] == 3

    # losing to the fund => no edge
    lose = lambda t: _fake([c - 0.01 + e for c, e in zip(comp, _noise(48, t, 0.005))], comp, dates)  # noqa: E731
    v2 = validate_factor_backtest(lose, 20, universe="NIFTYMIDCAP150")
    assert v2["verdict"] == "no edge"


def _monthly(start_y, start_m, n):
    out, y, m = [], start_y, start_m
    for _ in range(n):
        out.append(f"{y}-{m:02d}-02")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def _compound(rets):
    out = [100.0]
    for r in rets:
        out.append(out[-1] * (1 + r))
    return out


def _noise(n, seed, sd=0.01):
    import random
    rnd = random.Random(seed)
    xs = [rnd.gauss(0, sd) for _ in range(n)]
    mu = sum(xs) / n
    return [x - mu for x in xs]  # exactly zero mean


def _market(n, seed=1):
    return [0.01 + x for x in _noise(n, seed, 0.03)]


def _fake(strategy_rets, comp_rets, dates, fund_symbol="MID150BEES", with_fund=True):
    r = {"dates": dates, "strategy": _compound(strategy_rets), "top": 20, "benchmark_symbol": "NIFTYBEES",
         "benchmark": _compound(comp_rets), "index_fund": None, "index_fund_symbol": None}
    if with_fund:
        r["index_fund"], r["index_fund_symbol"] = _compound(comp_rets), fund_symbol
    return r


def test_index_like_strategy_gets_low_odds_and_steady_excess_high_odds():
    dates = _monthly(2021, 1, 49)
    comp = _market(48)
    like = [c + e for c, e in zip(comp, _noise(48, 5))]
    other = lambda t: _fake([c + e + (t - 20) * 2e-4 for c, e in zip(comp, _noise(48, t))], comp, dates)  # noqa: E731
    v = validate_factor_backtest(other, 20, base=_fake(like, comp, dates), universe="NIFTYMIDCAP150")
    assert v["deflated_sharpe"]["probability"] < 0.5 and v["verdict"] == "no edge"
    steady = [c + 0.01 + e for c, e in zip(comp, _noise(48, 6, 0.005))]
    v2 = validate_factor_backtest(lambda t: _fake(steady, comp, dates), 20, base=_fake(steady, comp, dates),
                                  universe="NIFTYMIDCAP150")
    assert v2["deflated_sharpe"]["probability"] > 0.95 and v2["verdict"] == "likely skill"
    assert "MID150BEES" in v2["summary"]


def test_comparison_fallback_is_named_and_never_called_skill():
    dates = _monthly(2021, 1, 49)
    comp = _market(48)
    steady = [c + 0.01 + e for c, e in zip(comp, _noise(48, 6, 0.005))]
    mk = lambda t=20: _fake(steady, comp, dates, with_fund=False)  # noqa: E731
    v = validate_factor_backtest(mk, 20, base=mk(), universe="NIFTYMIDCAP150")
    assert v["comparison"]["kind"] == "other benchmark" and v["comparison"]["symbol"] == "NIFTYBEES"
    assert v["verdict"] == "could be luck"
    assert "NIFTYBEES" in v["summary"] and "different index" in v["summary"]
    assert "MID150BEES" in v["summary"] and "didn't exist" in v["summary"]
    # NIFTYBEES is NIFTY 50's own fund
    v50 = validate_factor_backtest(mk, 20, base=mk(), universe="NIFTY50")
    assert v50["comparison"]["kind"] == "index fund" and v50["verdict"] == "likely skill"
    # a price index is labelled as such
    px = mk()
    px["benchmark_symbol"] = "^NSEI"
    assert validate_factor_backtest(mk, 20, base=px, universe="NIFTY50")["comparison"]["kind"] == "price index"


def test_no_comparison_curve_never_crashes_or_claims_skill():
    dates = _monthly(2021, 1, 49)
    comp = _market(48)
    r = _fake(comp, comp, dates, with_fund=False)
    r["benchmark"] = None
    v = validate_factor_backtest(lambda t: dict(r), 20, base=r)
    assert v["verdict"] == "could be luck" and v["comparison"]["kind"] is None
    assert v["deflated_sharpe"]["probability"] is None and v["walk_forward"]["fund_return"] is None


def test_walk_forward_missing_comparison_year_is_dropped_from_both_totals():
    dates = _monthly(2021, 1, 49)
    runs = {10: _compound([0.01] * 48), 30: _compound([0.012] * 48)}
    fund = _compound([0.005] * 48)
    fund[24] = None  # no fund price on the 2023 start date: 2022 and 2023 both lack a comparison
    wf = walk_forward(runs, dates, fund)
    y = {r["year"]: r for r in wf["years"]}
    assert y[2023]["missing"] is True and y[2022]["missing"] is True and y[2024]["missing"] is False
    assert wf["oos_return"] == pytest.approx(1.012 ** 12 - 1, rel=1e-6)  # 2024 only, in both totals
    assert wf["fund_return"] == pytest.approx(1.005 ** 12 - 1, rel=1e-6)
    assert wf["full_years"] == 1
    # a candidate with a missing return is not eligible
    bad = dict(runs)
    bad[30] = list(runs[30])
    for i in range(0, 13):
        bad[30][i] = None
    assert all(r["chosen_top"] == 10 for r in walk_forward(bad, dates, fund)["years"] if not r["missing"])


def test_stub_first_year_is_folded_and_partial_last_year_flagged():
    dates = _monthly(2020, 12, 32)  # Dec 2020 .. Jul 2023
    runs = {10: _compound([0.01] * 31)}
    wf = walk_forward(runs, dates, _compound([0.0] * 31))
    assert [y["year"] for y in wf["years"]] == [2022, 2023]  # 2020's single month is not its own year
    assert [y["partial"] for y in wf["years"]] == [False, True]
    assert wf["full_years"] == 1


def test_two_oos_years_cannot_be_likely_skill():
    dates = _monthly(2021, 1, 37)  # 2021 in-sample, 2022 and 2023 out of sample
    comp = _market(36)
    steady = [c + 0.01 + e for c, e in zip(comp, _noise(36, 6, 0.005))]
    r = _fake(steady, comp, dates)
    v = validate_factor_backtest(lambda t: dict(r), 20, base=r, universe="NIFTYMIDCAP150")
    assert v["walk_forward"]["full_years"] == 2 and v["verdict"] == "could be luck"
    assert "3 full years" in v["summary"]


def test_expected_max_sharpe_known_values():
    assert expected_max_sharpe(1000, 1) == pytest.approx(3.2551, abs=1e-3)
    assert expected_max_sharpe(10, 1) == pytest.approx(1.5746, abs=1e-3)


def test_probabilistic_sharpe_undefined_cases_are_none():
    assert probabilistic_sharpe(0.5, 1, 0.0, 3.0) is None
    assert probabilistic_sharpe(10.0, 50, 5.0, 1.0) is None  # 1 - 50 + 0 < 0


def test_monte_carlo_drawdown_matches_hand_computation():
    rets = [0.10, -0.20, 0.10, 0.10, -0.10, 0.05]
    v, peak, worst = 1.0, 1.0, 0.0
    for r in rets:
        v *= 1 + r
        peak = max(peak, v)
        worst = min(worst, v / peak - 1)
    assert worst == pytest.approx(-0.2, abs=1e-12)  # the -20% month right after the first peak
    mc = monte_carlo(rets, n_paths=50, block=len(rets))
    assert mc["max_drawdown"] == {"p5": pytest.approx(worst), "p50": pytest.approx(worst), "p95": pytest.approx(worst)}
    assert mc["final_return"]["p50"] == pytest.approx(v - 1)


def test_signal_lab_odds_are_after_charges_and_horizon_neutral():
    from trading_agent.signal_lab import _summarise, deflate_trials
    spreads = [0.02, 0.0, 0.04, 0.01, 0.03]
    s = _summarise([0.1, 0.0, 0.2, 0.1, 0.05], spreads, [0.5] * 5, [0.5] * 5, [0.5] * 5, 0.01, 12)
    assert s["spread_sharpe"] == pytest.approx(sharpe([x - 0.01 for x in spreads]))

    def fake(h, sr):
        return {"spread_sharpe": sr, "periods": 252 // h * 4, "spread_skew": 0.0, "spread_kurt": 3.0}

    # the same annual edge measured at 5 and 60 days, plus a spread of other trials
    ann = 0.9
    results = {"5": {"a": fake(5, ann / math.sqrt(252 / 5)), "b": fake(5, 0.01)},
               "60": {"a": fake(60, ann / math.sqrt(252 / 60)), "b": fake(60, 0.2)}}
    trials = deflate_trials(results)
    assert len(trials) == 4
    assert abs(results["5"]["a"]["deflated_sharpe"] - results["60"]["a"]["deflated_sharpe"]) < 0.08
    assert all(0 <= t["deflated_sharpe"] <= 1 for t in trials)


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
    dates = _monthly(2021, 1, 49)
    comp = _market(48)
    r = _fake([c + 0.01 + e for c, e in zip(comp, _noise(48, 6, 0.005))], comp, dates)
    v = validate_factor_backtest(lambda t: dict(r), 20, base=r, universe="NIFTYMIDCAP150")
    text = format_validation(v)
    assert "Skill or luck?" in text and "truly beats" in text and "Monte Carlo" in text and "2022" in text


def test_a_jan_to_dec_final_year_is_partial_and_nifty50_fund_is_exact():
    from trading_agent.validation import walk_forward
    from trading_agent.factor_backtest import _comparison

    dates = [f"{y}-{m:02d}-01" for y in (2022, 2023, 2024) for m in range(1, 13)]
    dates = dates[:-11] + ["2024-12-01"]  # the data stops on 1 Dec 2024 after starting January 2024
    dates = [d for d in dates if not d.startswith("2024")] + [f"2024-{m:02d}-01" for m in range(1, 13)]
    curve = [100 * 1.01 ** i for i in range(len(dates))]
    wf = walk_forward({10: curve, 20: curve}, dates, curve)
    last = wf["years"][-1]
    assert last["year"] == 2024 and last["partial"] is True
    assert wf["used_years"] == len(wf["years"])
    run = {"benchmark_symbol": "BANKBEES", "index_fund_symbol": None, "index_fund": None,
           "benchmark": curve, "price_index_symbol": None}
    _, label = _comparison(run, universe="NIFTY50")
    assert label["kind"] == "other benchmark"  # BANKBEES is not the NIFTY 50 fund
    run["benchmark_symbol"] = "NIFTYBEES"
    assert _comparison(run, universe="NIFTY50")[1]["kind"] == "index fund"
