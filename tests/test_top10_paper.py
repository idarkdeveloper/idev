"""Variant B's (top 10) paper account. Fakes only; no network."""
import json
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from trading_agent import top10_paper as tp
from trading_agent.config import parse_top10_paper_start
from trading_agent.costs import cost_model_for
from trading_agent.timezones import IST

from .test_forward import Clock


def screen_result(eligible):
    rows = [{"symbol": s, "eligible": True} for s in eligible]
    return {"top": [{"symbol": s} for s in eligible[:10]], "all": rows, "eligible": len(eligible),
            "bands_applied": True, "filters_applied": True}


def test_the_start_date_setting_and_bs_preregistered_rules():
    assert parse_top10_paper_start(None) == "2026-10-19" and parse_top10_paper_start("off") is None
    assert parse_top10_paper_start("2026-12-01") == "2026-12-01"
    with pytest.raises(ValueError):
        parse_top10_paper_start("soon")
    from trading_agent.research_backward import variants
    (b,) = [v for v in variants({}) if v[0] == "B"]
    rules = b[2]
    assert tp.TOP == rules.top == 10
    assert (tp.RULES_B["buffer"], tp.RULES_B["tax_aware_floor"], tp.RULES_B["floor"]) == \
           (rules.buffer, rules.tax_aware_floor, rules.floor)


def test_the_account_holds_ten_with_bs_buffer_in_its_own_files(tmp_path):
    names = [f"S{i:02d}" for i in range(25)]
    prices = {"MID150BEES": 200.0, **{s: 100.0 for s in names}}
    clock = Clock(2026, 11, 2, 16, 30)
    a = tp.Top10Paper(tmp_path, universe="NIFTYMIDCAP150", capital=100_000, price_fn=lambda s: prices[s.upper()],
                      cost_model=cost_model_for("in"), now=clock)
    s = a.run(lambda: screen_result(names))
    assert s["last_picks"] == names[:10] and len(s["holdings"]) == 10 and s["rules"]["buffer"] == 18
    d = tmp_path / "forward"
    assert (d / "top10_niftymidcap150.json").exists() and not (d / "niftymidcap150.json").exists()
    assert json.loads((d / "top10_niftymidcap150.json").read_text())["variant"].startswith("B: top 10")
    clock.t = datetime(2026, 12, 1, 16, 30, tzinfo=IST)
    shifted = names[8:] + names[:8]                       # old holdings now rank 18-25 (S00-S07) and 1-2 (S08, S09)
    s2 = a.run(lambda: screen_result(shifted))
    kept = set(s2["rebalanced"]["kept"])
    assert {"S08", "S09"} <= kept and "S07" not in kept   # S07 ranks 25: beyond 18 (not a profitable aged holding)


def settings_for(tmp_path, start="2026-11-02"):
    return SimpleNamespace(state_dir=tmp_path, top10_paper_start=start, forward_universe="NIFTYMIDCAP150",
                           paper_starting_cash=100_000.0)


def test_the_scheduled_run_waits_trades_reports_and_never_raises(tmp_path, monkeypatch):
    names = [f"S{i:02d}" for i in range(15)]
    prices = SimpleNamespace(latest_price=lambda s: 200.0 if s.upper() == "MID150BEES" else 100.0)
    assert "starts 2026-11-02" in tp.run_top10_paper_due(settings_for(tmp_path), prices, None, today=date(2026, 10, 30))
    assert "off" in tp.run_top10_paper_due(settings_for(tmp_path, None), prices, None, today=date(2026, 11, 2))
    from trading_agent import forward
    monkeypatch.setattr(forward, "_now", lambda: datetime(2026, 11, 2, 16, 30, tzinfo=IST))

    def broken():
        raise RuntimeError("no prices")
    assert "failed: RuntimeError: no prices" in tp.run_top10_paper_due(settings_for(tmp_path), prices, None,
                                                                       today=date(2026, 11, 2), screen_fn=broken)
    text = tp.run_top10_paper_due(settings_for(tmp_path), prices, None, today=date(2026, 11, 2),
                                  screen_fn=lambda: screen_result(names))
    assert "Top-10 paper account (variant B" in text and "Holding: S00, S01" in text and "Practice money only" in text
    assert "nothing due" in tp.run_top10_paper_due(settings_for(tmp_path), prices, None, today=date(2026, 11, 2),
                                                   screen_fn=lambda: screen_result(names))


def test_the_kill_switch_stops_it_and_the_watch_runs_it(tmp_path, monkeypatch):
    from trading_agent import killswitch
    monkeypatch.setattr(killswitch, "reason", lambda state_dir, what: f"Halted: refusing to {what}.")
    text = tp.run_top10_paper_due(settings_for(tmp_path), SimpleNamespace(latest_price=lambda s: 1.0), None,
                                  today=date(2026, 11, 2))
    assert text.startswith("Halted") and "top-10" in text
    import inspect

    from trading_agent import cli
    assert "run_top10_paper_due" in inspect.getsource(cli)
    assert cli.build_parser().parse_args(["top10-paper", "--run"]).run is True


def test_the_report_compares_with_the_adopted_rules_over_the_same_days():
    s = {"universe": "NIFTYMIDCAP150", "top": 10, "benchmark": "MID150BEES", "started": "2026-10-19",
         "strategy_return": 0.06, "benchmark_return": 0.02, "gap": 0.04, "strategy_max_drawdown": -0.03,
         "days": 30, "charges_paid": 500.0, "rebalanced": None}
    text = tp.format_top10_paper(s, {"from": "2026-10-19", "to": "2026-11-30", "strategy_return": 0.05})
    assert "B minus the adopted rules: +1.00%" in text and "Too early" in text
