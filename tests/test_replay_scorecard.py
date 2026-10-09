import pytest

from trading_agent.replay.engine import step
from trading_agent.replay.scorecard import end_trial, what_happened_next
from trading_agent.replay.trial import Trial
from .replay_fakes import FakeUniverse, market, top_by_6m


def make(tmp_path):
    return Trial.create(tmp_path / "replay", name="t", start="2021-03-15", cash=100_000,
                        universe="NIFTYMIDCAP150", top=3, dividends="reinvest", source=market(),
                        universe_obj=FakeUniverse(), screen_fn=top_by_6m, today="2026-10-09")


def test_scorecard_per_portfolio(tmp_path):
    t = make(tmp_path)
    t.order("A", "buy", qty=50)
    t.order("E", "buy", qty=20)
    step(t, "2022-03-15", today="2026-10-09")
    s = end_trial(t)
    assert t.data["ended"] == "2022-03-15" and t.data["scorecard"] == s
    you = s["you"]
    assert you["final"] == t.data["equity"][-1]["you"]
    assert you["return"] == pytest.approx(you["final"] / 100_000 - 1)
    assert you["best"]["symbol"] == "A" and you["worst"]["symbol"] == "E"
    assert you["hit_rate"] == 0.5 and you["trades"] == 2 and you["charges"] > 0
    assert s["agent"]["trades"] >= 3 and s["nifty"]["trades"] == 1
    assert s["agent"]["max_drawdown"] <= 0


def test_ended_trial_is_read_only(tmp_path):
    t = make(tmp_path)
    end_trial(t)
    with pytest.raises(ValueError, match="ended"):
        t.order("A", "buy", qty=1)


def test_what_happened_next_only_after_the_end(tmp_path):
    t = make(tmp_path)
    src = market()
    with pytest.raises(ValueError, match="End the replay"):
        what_happened_next(t, src, "2026-10-09")
    t.order("A", "buy", qty=10)
    end_trial(t)
    n = what_happened_next(t, src, "2021-04-15")
    assert n["dates"][0] == "2021-03-15" and n["dates"][-1] == "2021-04-15"
    a0 = [b for b in src.bars["A"] if b["date"] == "2021-04-15"][0]["adj_close"]
    assert n["you"][-1] == pytest.approx(t.you.account().cash + 10 * a0, abs=0.01)
