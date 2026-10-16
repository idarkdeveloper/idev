"""The prediction model's own paper account. Synthetic prices and fakes only; no network."""
import json
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from trading_agent import model_paper as mp
from trading_agent.config import parse_model_paper_start
from trading_agent.costs import cost_model_for
from trading_agent.timezones import IST

from .test_forward import Clock
from .test_research_ml import market30  # noqa: F401 - the fixture


def test_the_start_date_setting():
    assert parse_model_paper_start(None) == "2026-10-19" and parse_model_paper_start("") == "2026-10-19"
    assert parse_model_paper_start("off") is None and parse_model_paper_start("2026-11-02") == "2026-11-02"
    with pytest.raises(ValueError):
        parse_model_paper_start("next monday")


def test_fit_once_then_size_the_top_20(market30):  # noqa: F811
    m = market30
    model = mp.fit_frozen(m, None, 0.004)
    assert model["samples"] >= 100 and model["features"][-2:] == ["regime_above_ema50", "breadth"]
    assert len(model["beta"]) == len(model["features"]) + 1
    picks = mp.score(m, None, model)
    assert 1 <= len(picks) <= 20 and [p["rank"] for p in picks] == list(range(1, len(picks) + 1))
    assert all(0 <= p["size"] <= 1 and p["weight"] == pytest.approx(p["size"] / 20, abs=1e-5) for p in picks)
    with pytest.raises(ValueError):                                    # features changed since the fit: refuse
        mp.score(m, None, {**model, "features": ["other"]})


def account(tmp_path, prices, clock):
    return mp.ModelPaper(tmp_path, universe="NIFTYMIDCAP150", capital=100_000, price_fn=lambda s: prices[s.upper()],
                         cost_model=cost_model_for("in"), now=clock)


def test_the_account_trades_to_the_model_sizes_in_its_own_files(tmp_path):
    prices = {"MID150BEES": 200.0, "A": 100.0, "B": 250.0, "C": 1000.0}
    clock = Clock(2026, 10, 19, 16, 30)
    a = account(tmp_path, prices, clock)
    picks = [{"symbol": "A", "rank": 1, "size": 0.8, "weight": 0.4}, {"symbol": "B", "rank": 2, "size": 0.4, "weight": 0.2},
             {"symbol": "C", "rank": 3, "size": 0.0, "weight": 0.0}]
    s = a.run(lambda: picks)
    r = s["rebalanced"]
    assert r["invested"] == pytest.approx(0.6) and r["sizes"]["A"] == 0.8
    held = {p["symbol"]: p["value"] for p in s["holdings"]}
    assert set(held) == {"A", "B"} and 37_000 < held["A"] <= 40_000 and 18_000 < held["B"] <= 20_000
    assert (tmp_path / "forward" / "model_niftymidcap150.json").exists()
    assert (tmp_path / "forward" / "model_niftymidcap150_broker.json").exists()
    assert not (tmp_path / "forward" / "niftymidcap150.json").exists()          # the forward test is untouched
    called = []
    clock.t = datetime(2026, 10, 20, 16, 30, tzinfo=IST)                       # same month: mark only
    s2 = a.run(lambda: called.append(1) or picks)
    assert s2["rebalanced"] is None and not called and s2["days"] == 2
    clock.t = datetime(2026, 11, 2, 16, 30, tzinfo=IST)                        # next month: resized
    s3 = a.run(lambda: [{"symbol": "C", "rank": 1, "size": 0.5, "weight": 0.5}])
    assert {p["symbol"] for p in s3["holdings"]} == {"C"}
    text = mp.format_model_paper(s3, {"strategy_return": 0.01})
    assert "frozen prediction model" in text and "C 0.50" in text and "Practice money only" in text


def settings_for(tmp_path, start="2026-10-19"):
    return SimpleNamespace(state_dir=tmp_path, model_paper_start=start, forward_universe="NIFTYMIDCAP150",
                           paper_starting_cash=100_000.0)


def test_the_scheduled_run_waits_for_its_start_and_never_raises(tmp_path, monkeypatch):
    prices = SimpleNamespace(latest_price=lambda s: 100.0)
    assert "starts 2026-10-19" in mp.run_model_paper_due(settings_for(tmp_path), prices, None, today=date(2026, 10, 16))
    assert "off" in mp.run_model_paper_due(settings_for(tmp_path, None), prices, None, today=date(2026, 10, 19))

    def broken(*a, **k):
        raise RuntimeError("no prices")
    monkeypatch.setattr(mp, "_market_and_events", broken)
    monkeypatch.setattr(mp.ModelPaper, "run", lambda self, fn: fn())          # straight to fitting
    from trading_agent import forward
    monkeypatch.setattr(forward.ForwardTest, "due", lambda self: True)
    out = mp.run_model_paper_due(settings_for(tmp_path), prices, None, today=date(2026, 10, 19))
    assert out.startswith("Model paper account failed: RuntimeError")


def test_a_saved_model_is_reused_not_refitted(tmp_path, monkeypatch):
    prices = {"MID150BEES": 200.0, "A": 100.0}
    a = account(tmp_path, prices, Clock(2026, 10, 19, 16, 30))
    a.save_model({"beta": [0.0], "features": [], "samples": 500, "from": "2020-10-01", "to": "2025-09-30"})
    assert json.loads(a.fit_path.read_text())["samples"] == 500 and a.model()["to"] == "2025-09-30"
