"""The leakage test: cut the market at a decision day and re-decide. Synthetic prices only; no network."""
import math
import random
from datetime import date
from types import SimpleNamespace

import pytest

from trading_agent import research_leakage as rl
from trading_agent.research_sim import Market, ema

from .test_research_sim import bars, days


def make(n=560, k=24, seed=5, cls=Market):
    rng = random.Random(seed)
    dates = days(n, date(2021, 1, 4))
    series = {}
    for i in range(k):
        p, path = 100.0, []
        drift, vol = rng.uniform(-0.0003, 0.0015), rng.uniform(0.01, 0.02)
        for _ in range(n):
            p *= math.exp(drift + vol * rng.gauss(0, 1))
            path.append(p)
        series[f"S{i:02d}"] = bars(dates, path)
    fund = [100 * 1.0004 ** i * (1 + 0.03 * math.sin(i / 40)) for i in range(n)]
    return cls(dates, series, bars(dates, fund), lambda d: set(series), min_turnover=0,
               flows={d: float(j % 5 - 2) for j, d in enumerate(dates)})


@pytest.fixture(scope="module")
def clean():
    return make()


def quick(m, **kw):
    kw.setdefault("ranking_days", 2)
    kw.setdefault("simulation_days", 1)
    return rl.leakage_check(m, holdout_start="2099-01-01", **kw)


def test_the_cut_market_holds_nothing_after_the_decision_day(clean):
    cut = rl.truncate(clean, 400)
    day = clean.dates[400]
    assert cut.dates == clean.dates[:401] and cut.dates[-1] == day
    assert all(b["date"] <= day for s in cut.raw.values() for b in s) and max(cut.flows) == day
    assert cut.close["S00"] == clean.close["S00"][:401] and cut.fund_close == clean.fund_close[:401]
    k = rl.truncate(clean, 400, rescale={"S00": 2.0})
    assert k.adj["S00"][400] == pytest.approx(2 * clean.adj["S00"][400]) and k.close["S00"][400] == clean.close["S00"][400]


def test_the_real_simulator_passes(clean):
    r = quick(clean)
    assert r["ok"] and r["failures"] == 0
    assert set(r["checks"]) == {"holdout sealed", "ranking", "ranking scores", "back-adjustment", "indicators",
                                "simulation, same-close fills", "simulation, next-day fills"}
    assert r["checks"]["ranking"]["checked"] == 2 and r["checks"]["simulation, next-day fills"]["checked"] == 1
    text = rl.format_leakage(r)
    assert "PASSED: no decision used later data" in text and "Index membership is today's list" in text
    assert "Not covered: event signals" in text


def test_a_ranker_that_reads_tomorrows_close_is_caught(clean):
    def peeks(m, t):     # ranks by the return over the next five sessions
        return sorted(m.close, key=lambda s: -(m.close[s][t + 5] / m.close[s][t]))
    r = quick(clean, ranker=peeks)
    assert not r["ok"]
    assert r["checks"]["ranking"]["failures"] and "after the decision day" in r["checks"]["ranking"]["failures"][0]["detail"]
    assert r["checks"]["simulation, same-close fills"]["failures"]
    assert "FAILED" in rl.format_leakage(r)


def test_a_ranker_that_reads_the_last_price_in_the_file_is_caught(clean):
    def last_price(m, t):   # no index error: it silently reads the end of whatever data it was given
        return sorted(m.close, key=lambda s: -(m.close[s][-1] / m.close[s][t]))
    r = quick(clean, ranker=last_price)
    assert r["checks"]["ranking"]["failures"] and "with later data" in r["checks"]["ranking"]["failures"][0]["detail"]


class CentredEma(Market):
    """A 50-day average shifted three sessions early: each day sees three later closes."""
    def ema50(self, sym):
        e = ema(self.close[sym], 50)
        return e[3:] + [e[-1]] * 3


def test_an_indicator_that_looks_ahead_is_caught():
    r = quick(make(cls=CentredEma))
    f = r["checks"]["indicators"]["failures"]
    assert f and "ema50" in f[0]["detail"]


class LevelRanker(Market):
    def momentum_ranking(self, t):   # ranks by adjusted price LEVEL, which later dividends rewrite
        return sorted(self.adj, key=lambda s: -(self.adj[s][t] or 0))

    def momentum_scores(self, t):
        return {}


def test_reading_adjusted_price_levels_fails_the_back_adjustment_check():
    r = quick(make(cls=LevelRanker))
    assert r["checks"]["back-adjustment"]["failures"] and r["checks"]["ranking"]["failures"] == []


def test_bars_inside_the_holdout_fail(clean):
    r = rl.leakage_check(clean, holdout_start=clean.dates[-10], ranking_days=1, simulation_days=0)
    f = r["checks"]["holdout sealed"]["failures"]
    assert not r["ok"] and f and "10 day(s)" in f[0]["detail"]


def test_point_in_time_membership_has_no_warning(clean):
    m = make()
    m.point_in_time = True
    assert quick(m, simulation_days=0)["warnings"] == []


def test_the_guard_stops_a_leaky_run_and_lets_a_clean_one_through(clean):
    assert rl.guard(clean, holdout_start="2099-01-01", ranking_days=2, simulation_days=1)["ok"]
    with pytest.raises(rl.LeakageError, match="FAILED"):
        rl.guard(make(cls=CentredEma), holdout_start="2099-01-01", ranking_days=2, simulation_days=1)


def test_research_commands_stop_on_a_leak(monkeypatch, capsys):
    from trading_agent import cli
    monkeypatch.setattr(rl, "guard", lambda m, **kw: (_ for _ in ()).throw(rl.LeakageError("Leakage test: FAILED")))
    with pytest.raises(SystemExit, match="Stopped: a decision used data published after it"):
        cli._leakage_guard(object())
    assert "FAILED" in capsys.readouterr().out


def test_research_leakage_command(clean, tmp_path, monkeypatch, capsys):
    from trading_agent import cli
    monkeypatch.setattr(cli, "_settings", lambda a: SimpleNamespace(state_dir=tmp_path))
    seen = {}

    def market(a, s):
        seen["skip"] = a.skip_leakage_guard
        return clean
    monkeypatch.setattr(cli, "_research_market", market)
    from trading_agent import research_studies
    monkeypatch.setattr(research_studies, "HOLDOUT_START", "2099-01-01")
    assert cli.cmd_research_leakage(SimpleNamespace(json=False, days=2, cuts=1)) == 0
    out = capsys.readouterr().out
    assert seen["skip"] is True and "PASSED" in out
    assert "research-leakage" in cli.HALT_ALLOWED
