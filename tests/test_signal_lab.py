import math
import random

from trading_agent.costs import IndianDeliveryCosts
from trading_agent.membership import Membership
from trading_agent.signal_lab import (MODEL, _rolling_max, _verdict, format_signal_lab, rsi, run_signal_lab,
                                      signal_series, spearman)

from .test_scorecard_factor import _dates


class World:
    """Random-walk stocks; with `drift`, each stock keeps its own trend, so momentum must predict."""

    def __init__(self, n_stocks=30, n_days=1300, drift=True, seed=7):
        rnd = random.Random(seed)
        self.dates = _dates(n_days, start=(2021, 1, 4))
        self.series = {}
        for k in range(n_stocks):
            mu = (k / (n_stocks - 1) - 0.5) * 0.003 if drift else 0.0
            p, out = 100.0, []
            for _ in range(n_days):
                p *= math.exp(mu + rnd.gauss(0, 0.012))
                out.append(p)
            self.series[f"S{k:02d}"] = out
        self.series["NIFTYBEES"] = [100 * 1.0003 ** i for i in range(n_days)]

    def history(self, sym, range_="5y"):
        if sym not in self.series:
            raise LookupError(sym)
        return [{"date": d, "close": c, "adj_close": c, "volume": 1e6} for d, c in zip(self.dates, self.series[sym])]

    @property
    def universe(self):
        return [{"symbol": s} for s in self.series if s.startswith("S")]


def test_indicators():
    assert rsi([float(i) for i in range(1, 30)])[-1] == 100.0
    assert rsi([float(30 - i) for i in range(30)])[-1] < 1
    assert _rolling_max([1, 3, 2, 5, 4, 1], 3) == [None, None, 3, 5, 5, 5]
    assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == 1.0
    assert abs(spearman([1, 2, 3, 4], [4, 3, 2, 1]) + 1.0) < 1e-12
    assert spearman([1, 1, 1], [1, 2, 3]) is None
    w = World(n_stocks=2, n_days=300)
    s = signal_series(w.history("S00"))
    assert s["momentum_12_1"][251] is None and s["momentum_12_1"][299] is not None
    assert all(len(v) == 300 for v in s.values())


def test_verdicts():
    assert _verdict(3.5, 0.02, 0.008) == "predictive"
    assert _verdict(2.4, 0.02, 0.008) == "could be luck"
    assert _verdict(3.5, 0.002, 0.008) == "too small to trade"
    assert _verdict(-2.5, -0.01, 0.008) == "reversed"
    assert _verdict(0.4, 0.001, 0.008) == "no edge"
    assert _verdict(None, None, 0.008) == "not enough data"


def test_lab_finds_a_planted_trend_and_learns_it():
    w = World(drift=True)
    r = run_signal_lab(w.universe, w, horizons=(20, 60), years=4, cost_model=IndianDeliveryCosts(), min_names=10)
    mom = r["results"]["20"]["momentum_12_1"]
    assert mom["t_stat"] >= 3 and mom["verdict"] == "predictive" and mom["top_minus_bottom"] > r["round_trip_cost"]
    model = r["results"]["20"][MODEL]
    assert model["periods"] < mom["periods"] and model["t_stat"] > 2  # trained on past periods only
    assert r["model_weights"]["20"]["momentum_12_1"] > 0
    assert r["point_in_time"] is False and "Predictive after costs" in r["summary"]
    assert "12-1 momentum" in format_signal_lab(r)


def test_lab_finds_nothing_in_noise_and_respects_membership():
    w = World(drift=False, seed=11)
    m = Membership(frozenset(f"S{k:02d}" for k in range(30)), [("2020-01-01", (), ())])
    r = run_signal_lab(w.universe, w, horizons=(20,), years=4, cost_model=IndianDeliveryCosts(), min_names=10,
                       membership=m)
    verdicts = [s["verdict"] for s in r["results"]["20"].values()]
    assert "predictive" not in verdicts and r["point_in_time"] is True
    assert "20" in r["index_timing"] and r["index_timing"]["20"]["above"]["n"] > 0
