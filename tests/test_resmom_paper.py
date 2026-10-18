"""Residual momentum's (variant E's) paper account. Synthetic prices and fakes only; no network."""
import json
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from trading_agent import resmom_paper as rp
from trading_agent.config import parse_resmom_paper_start
from trading_agent.costs import cost_model_for
from trading_agent.forward import RULES
from trading_agent.timezones import IST

from .test_forward import Clock


def screen_result(eligible, excluded=()):
    rows = [{"symbol": s, "eligible": True} for s in eligible] + [{"symbol": s, "eligible": False} for s in excluded]
    return {"top": [{"symbol": s} for s in eligible[:20]], "all": rows, "eligible": len(eligible),
            "bands_applied": True, "filters_applied": True}


def test_the_start_date_setting():
    assert parse_resmom_paper_start(None) == "2026-10-19" and parse_resmom_paper_start("") == "2026-10-19"
    assert parse_resmom_paper_start("off") is None and parse_resmom_paper_start("2026-12-01") == "2026-12-01"
    with pytest.raises(ValueError):
        parse_resmom_paper_start("soon")


def test_reorder_keeps_the_screens_names_in_residual_order():
    result = screen_result(["A", "B", "C", "D"], excluded=["X"])
    out = rp.reorder(result, ["X", "D", "b", "A"], top=2)
    assert [r["symbol"] for r in out["top"]] == ["D", "B"]               # residual order, top 2
    eligible = [r["symbol"] for r in out["all"] if r["eligible"]]
    assert eligible == ["D", "B", "A"]                                   # X stays out: the screen's filters exclude it
    assert out["unranked"] == ["C"] and out["eligible"] == 3             # too little history: left unranked
    assert out["bands_applied"] and out["filters_applied"]               # the forward test's filters are recorded


def account(tmp_path, prices, clock):
    return rp.ResidualPaper(tmp_path, universe="NIFTYMIDCAP150", capital=100_000,
                            price_fn=lambda s: prices[s.upper()], cost_model=cost_model_for("in"), now=clock)


def test_the_account_follows_the_research_rules_in_its_own_files(tmp_path):
    names = [f"S{i:02d}" for i in range(25)]
    prices = {"MID150BEES": 200.0, **{s: 100.0 for s in names}}
    clock = Clock(2026, 11, 2, 16, 30)
    a = account(tmp_path, prices, clock)
    ranked = list(reversed(names))                                       # residual order differs from the screen's
    s = a.run(lambda: rp.reorder(screen_result(names), ranked))
    assert set(s["last_picks"]) == set(ranked[:20]) and "S00" not in s["last_picks"][:4]
    assert s["rules"]["buffer"] == RULES["buffer"] and s["rules"]["since"] == "2026-11-02"
    assert len(s["holdings"]) == 20
    d = tmp_path / "forward"
    assert (d / "resmom_niftymidcap150.json").exists() and (d / "resmom_niftymidcap150_broker.json").exists()
    assert not (d / "niftymidcap150.json").exists()                     # the adopted rules' forward test is untouched
    assert json.loads((d / "resmom_niftymidcap150.json").read_text())["variant"].startswith("E: residual momentum")
    clock.t = datetime(2026, 12, 1, 16, 30, tzinfo=IST)                 # next month: the buffer keeps rank <= 35
    s2 = a.run(lambda: rp.reorder(screen_result(names), names))         # order flips: old holdings now rank 6-25
    assert s2["rebalanced"]["kept"] and len(s2["rebalanced"]["kept"]) == 20


def test_the_comparison_uses_the_control_over_the_same_days(tmp_path):
    d = tmp_path / "forward"
    d.mkdir()
    hist = [{"date": "2026-10-30", "equity": 110_000.0}, {"date": "2026-11-02", "equity": 120_000.0},
            {"date": "2026-12-01", "equity": 126_000.0}]
    (d / "niftymidcap150.json").write_text(json.dumps({"history": hist}))
    c = rp.control_over(tmp_path, "NIFTYMIDCAP150", "2026-11-02T16:30:00+05:30")
    assert c["from"] == "2026-11-02" and c["strategy_return"] == pytest.approx(0.05)
    assert rp.control_over(tmp_path, "NIFTYMIDCAP150", None) is None
    assert rp.control_over(tmp_path, "NIFTYMIDCAP150", "2026-01-01") is None   # no point that early
    s = {"universe": "NIFTYMIDCAP150", "top": 20, "benchmark": "MID150BEES", "started": "2026-11-02",
         "strategy_return": 0.08, "benchmark_return": 0.03, "gap": 0.05, "strategy_max_drawdown": -0.02,
         "days": 21, "charges_paid": 900.0, "rebalanced": None}
    text = rp.format_resmom_paper(s, c)
    assert "E minus the adopted rules: +3.00%" in text and "Practice money only" in text and "Too early" in text


def settings_for(tmp_path, start="2026-11-02"):
    return SimpleNamespace(state_dir=tmp_path, resmom_paper_start=start, forward_universe="NIFTYMIDCAP150",
                           paper_starting_cash=100_000.0)


def test_the_scheduled_run_waits_for_its_start_and_never_raises(tmp_path, monkeypatch):
    prices = SimpleNamespace(latest_price=lambda s: 100.0)
    assert "starts 2026-11-02" in rp.run_resmom_paper_due(settings_for(tmp_path), prices, None, today=date(2026, 10, 30))
    assert "off" in rp.run_resmom_paper_due(settings_for(tmp_path, None), prices, None, today=date(2026, 11, 2))
    from trading_agent import forward
    monkeypatch.setattr(forward, "_now", lambda: datetime(2026, 11, 2, 16, 30, tzinfo=IST))

    def broken():
        raise RuntimeError("no prices")
    text = rp.run_resmom_paper_due(settings_for(tmp_path), prices, None, today=date(2026, 11, 2),
                                   ranking_fn=broken, screen_fn=lambda: screen_result(["A"]))
    assert "failed: RuntimeError: no prices" in text


def test_the_scheduled_run_trades_and_reports(tmp_path, monkeypatch):
    names = [f"S{i:02d}" for i in range(22)]
    prices = SimpleNamespace(latest_price=lambda s: 200.0 if s.upper() == "MID150BEES" else 100.0)
    from trading_agent import forward
    monkeypatch.setattr(forward, "_now", lambda: datetime(2026, 11, 2, 16, 30, tzinfo=IST))
    text = rp.run_resmom_paper_due(settings_for(tmp_path), prices, None, today=date(2026, 11, 2),
                                   ranking_fn=lambda: names[::-1], screen_fn=lambda: screen_result(names))
    assert "Residual momentum paper account (variant E" in text and "Top by residual momentum: S21, S20" in text
    assert "nothing due" in rp.run_resmom_paper_due(settings_for(tmp_path), prices, None, today=date(2026, 11, 2),
                                                    ranking_fn=lambda: names, screen_fn=lambda: screen_result(names))


def test_the_kill_switch_stops_it(tmp_path, monkeypatch):
    from trading_agent import killswitch
    monkeypatch.setattr(killswitch, "reason", lambda state_dir, what: f"Halted: refusing to {what}.")
    text = rp.run_resmom_paper_due(settings_for(tmp_path), SimpleNamespace(latest_price=lambda s: 1.0), None,
                                   today=date(2026, 11, 2))
    assert text.startswith("Halted") and "residual momentum" in text


def test_the_ranking_is_the_preregistered_one_on_the_index_fund(tmp_path, monkeypatch):
    from trading_agent import index_history, research_sim, screen
    from trading_agent.research_backward import residual_momentum_ranker

    from .test_research_leakage import make
    m = make(n=420)
    seen = {}
    monkeypatch.setattr(screen, "load_universe", lambda u: [{"symbol": s} for s in m.raw])
    monkeypatch.setattr(index_history, "point_in_time", lambda u, members, state_dir: None)

    def load(members, prices, **kw):
        seen.update(kw)
        return m
    monkeypatch.setattr(research_sim, "load_market", load)
    out = rp._residual_ranking(SimpleNamespace(state_dir=tmp_path), None, "NIFTYMIDCAP150")
    assert seen["regime"] == "MID150BEES" and seen["until"] is None
    assert out == residual_momentum_ranker()(m, len(m.dates) - 1) and out
