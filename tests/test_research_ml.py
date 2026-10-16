"""Triple-barrier labels, overlap weights, purged cross-validation and the sizing models. Synthetic data only."""
import random
from datetime import date

import pytest

from trading_agent.research_ml import (_month_sharpe, barrier_exit, barrier_study, calibration_chart, cpcv_signal_model,
                                       cpcv_splits, evaluate_cpcv, fan_chart, fit_logistic, fit_sharpe, format_meta,
                                       meta_samples, monthly_sized, paths, predict, uniqueness, verdict)
from trading_agent.research_sim import Market

from .test_research_sim import bars, days


def mk(prices):
    n = len(prices)
    dates = days(n)
    return Market(dates, {"A": bars(dates, prices)}, bars(dates, [100.0] * n), lambda d: {"A"}, min_turnover=0)


def test_barrier_exits_by_target_stop_or_time():
    wiggle = [100.0 + (i % 2) for i in range(30)]                     # ATR(14) = 1, so R = 2.5
    up = mk(wiggle + [101 + 2 * k for k in range(1, 20)])
    ex = barrier_exit(up, "A", 29, "two_stage", target_r=3.0, max_days=60)
    assert ex["reason"] == "target" and up.close["A"][ex["t_out"]] >= 101 + 7.5
    down = mk(wiggle + [101 - 2 * k for k in range(1, 20)])
    assert barrier_exit(down, "A", 29, "two_stage")["reason"] == "stop"
    flat = mk(wiggle + [100.5] * 30)
    ex = barrier_exit(flat, "A", 29, "none", max_days=10)
    assert ex["reason"] == "time" and ex["days"] == 10
    assert barrier_exit(flat, "A", 10_000, "none") is None


def test_uniqueness_by_hand():
    s = [{"t_in": 0, "t_out": 1}, {"t_in": 1, "t_out": 2}, {"t_in": 5, "t_out": 5}]
    u = uniqueness(s)
    assert u == pytest.approx([(1 + 0.5) / 2, (0.5 + 1) / 2, 1.0])


def samples_grid(months=36, per=10, span=15, seed=0, signal=True):
    rng = random.Random(seed)
    out = []
    for k in range(months):
        t = 21 * k
        for j in range(per):
            x = rng.gauss(0, 1)
            net = (0.03 * x if signal else 0.0) + rng.gauss(0, 0.02)
            out.append({"month": k, "t": t, "t_in": t + 1, "t_out": t + 1 + span, "symbol": f"S{j}", "rank": j + 1,
                        "x": [x, rng.gauss(0, 1)], "net": net})
    return out


def test_purged_splits_and_paths():
    s = samples_grid(months=36, span=30)
    splits, by_block = cpcv_splits(s, embargo=21)
    assert len(splits) == 15 and all(len(sp["test_blocks"]) == 2 for sp in splits)
    for sp in splits:
        test_t = {s[j]["t"] for j in sp["test"]}
        lo = min(s[j]["t_in"] for j in sp["test"])
        for j in sp["train"]:
            assert s[j]["t"] not in test_t
        # nothing in train overlaps a test sample's label span
        spans = [(s[j]["t_in"], s[j]["t_out"]) for j in sp["test"]]
        for j in sp["train"]:
            assert not any(s[j]["t_in"] <= b and s[j]["t_out"] >= a for a, b in spans)
        assert lo >= 0
    route = paths(splits)
    assert len(route) == 5 and all(len(p) == 6 for p in route)
    for b in range(6):   # each block is tested by 5 different splits, one per path
        assert len({p[b] for p in route}) == 5
    assert sum(len(x) for x in by_block) == len(s)


def test_logistic_and_sharpe_fits_learn_a_real_signal():
    s = samples_grid(months=40, per=12)
    X, y = [r["x"] for r in s], [int(r["net"] > 0) for r in s]
    beta = fit_logistic(X, y, [1.0] * len(s))
    assert beta[1] > 0.5 and abs(beta[2]) < 0.3
    p = predict(beta, X)
    assert sum(pi > 0.5 for pi, yi in zip(p, y) if yi) > sum(pi > 0.5 for pi, yi in zip(p, y) if not yi)
    sized = _month_sharpe(monthly_sized(s, p))
    better = fit_sharpe(s, beta, steps=15)
    assert _month_sharpe(monthly_sized(s, predict(better, X))) >= sized - 1e-9
    assert sized > _month_sharpe(monthly_sized(s, [1.0] * len(s)))


def test_cpcv_evaluation_beats_the_simple_rules_only_with_a_signal(tmp_path):
    good = evaluate_cpcv(samples_grid(months=36, per=10), cost=0.004)
    v = verdict(good)
    assert set(good["objectives"]) == {"plain", "fee_asymmetric", "sharpe"} and good["paths"] == 5
    assert v["beats_simple"] and good["calibration"]
    text = format_meta(good, v, ["x1", "x2"])
    assert "purged splits" in text and "Verdict:" in text and "Calibration" in text
    fan_chart(good, v["best_objective"], tmp_path / "fan.png", "test")
    calibration_chart(good["calibration"], tmp_path / "cal.png", "test")
    assert (tmp_path / "fan.png").stat().st_size > 1000 and (tmp_path / "cal.png").stat().st_size > 1000
    noise = verdict(evaluate_cpcv(samples_grid(months=36, per=10, seed=5, signal=False), cost=0.004))
    assert not noise["passes"]


@pytest.fixture(scope="module")
def market30():
    import math
    rng = random.Random(11)
    n = 1100
    dates = days(n, date(2019, 6, 3))
    series = {}
    for k in range(30):
        drift, vol, p = rng.uniform(-0.0002, 0.0012), rng.uniform(0.008, 0.025), 100.0
        path = []
        for _ in range(n):
            p *= math.exp(drift + vol * rng.gauss(0, 1))
            path.append(p)
        series[f"S{k:02d}"] = bars(dates, path)
    return Market(dates, series, bars(dates, [100 * 1.0004 ** i for i in range(n)]), lambda d: set(series),
                  min_turnover=0)


def test_barrier_study_and_meta_samples_run_on_a_market(market30):
    m = market30
    r = barrier_study(m, 400, len(m.dates) - 1, rule="two_stage", cost=0.004, max_days=40)
    assert "composite" in r and r["momentum_12_1"]["months"] > 10
    assert sum(r["momentum_12_1"]["exits"].values()) > 0
    s, names = meta_samples(m, 400, len(m.dates) - 1, rule="two_stage", cost=0.004)
    assert len(s) > 100 and len(s[0]["x"]) == len(names) and names[-2:] == ["regime_above_ema50", "breadth"]
    assert all(x["t_out"] >= x["t_in"] > x["t"] for x in s)


def test_cpcv_signal_model_runs(market30):
    r = cpcv_signal_model(market30, 400, len(market30.dates) - 1, cost=0.004, horizon=20)
    assert r["splits"] == 15 and len(r["paths"]) == 5 and all(len(p) > 5 for p in r["paths"])
