"""The drawdown brake's paper account. Fakes only; no network."""
import json
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from trading_agent import brake_paper as bp
from trading_agent.config import parse_brake_paper_start
from trading_agent.costs import cost_model_for
from trading_agent.timezones import IST

from .test_forward import Clock


def screen_result(eligible):
    rows = [{"symbol": s, "eligible": True} for s in eligible]
    return {"top": [{"symbol": s} for s in eligible[:20]], "all": rows, "eligible": len(eligible),
            "bands_applied": True, "filters_applied": True}


UP = [100.0 + i for i in range(120)]            # the fund above its 100-day average
DOWN = UP[:-1] + [50.0]                          # last close far below it


def test_the_start_date_setting_and_the_brake_rule_match_the_simulator():
    assert parse_brake_paper_start(None) == "2026-10-20" and parse_brake_paper_start("off") is None
    with pytest.raises(ValueError):
        parse_brake_paper_start("soon")
    assert bp.brake_on(UP) is False and bp.brake_on(DOWN) is True and bp.brake_on(UP[:50]) is None
    from trading_agent.research_india import rule_sets
    from trading_agent.research_studies import FINALIST
    (brk,) = [r for k, _, r, _ in rule_sets() if k == "BRK"]
    assert brk.brake == "sma100" and bp.SMA_DAYS == 100
    assert (brk.buffer, brk.tax_aware_floor, brk.floor, brk.top) == (FINALIST.buffer, FINALIST.tax_aware_floor,
                                                                      FINALIST.floor, 20)


def _account(tmp_path, clock, names):
    prices = {"MID150BEES": 200.0, **{s: 100.0 for s in names}}
    return bp.BrakePaper(tmp_path, universe="NIFTYMIDCAP150", capital=200_000, price_fn=lambda s: prices[s.upper()],
                         cost_model=cost_model_for("in"), now=clock)


def test_brake_off_trades_like_the_adopted_rules_in_its_own_files(tmp_path):
    names = [f"S{i:02d}" for i in range(25)]
    clock = Clock(2026, 11, 2, 16, 30)
    s = _account(tmp_path, clock, names).run(lambda: screen_result(names), lambda sym: UP)
    assert not s["braked"] and s["last_picks"] == names[:20] and len(s["holdings"]) == 20 and s["rules"]["buffer"] == 35
    d = tmp_path / "forward"
    assert (d / "brake_niftymidcap150.json").exists() and not (d / "niftymidcap150.json").exists()
    assert json.loads((d / "brake_niftymidcap150.json").read_text())["variant"].startswith("BRK")


def test_brake_on_sells_everything_skips_the_rebalance_and_reenters_next_month(tmp_path):
    names = [f"S{i:02d}" for i in range(25)]
    clock = Clock(2026, 11, 2, 16, 30)
    a = _account(tmp_path, clock, names)
    a.run(lambda: screen_result(names), lambda sym: UP)
    clock.t = datetime(2026, 11, 3, 16, 30, tzinfo=IST)
    s = _account(tmp_path, clock, names).run(lambda: screen_result(names), lambda sym: DOWN)
    assert s["braked"] and s["brake_event"]["brake"] == "on" and len(s["brake_event"]["sold"]) == 20
    assert s["holdings"] == []
    clock.t = datetime(2026, 12, 1, 16, 30, tzinfo=IST)            # new month, brake still on: no buying
    s = _account(tmp_path, clock, names).run(lambda: screen_result(names), lambda sym: DOWN)
    assert s["holdings"] == [] and s["rebalanced"] is None
    assert json.loads((tmp_path / "forward" / "brake_niftymidcap150.json").read_text())["rebalances"][-1]["skipped"] == "brake on"
    clock.t = datetime(2026, 12, 15, 16, 30, tzinfo=IST)           # lifts mid-month: waits for the next rebalance
    s = _account(tmp_path, clock, names).run(lambda: screen_result(names), lambda sym: UP)
    assert not s["braked"] and s["brake_event"]["brake"] == "off" and s["holdings"] == []
    clock.t = datetime(2027, 1, 1, 16, 30, tzinfo=IST)
    s = _account(tmp_path, clock, names).run(lambda: screen_result(names), lambda sym: UP)
    assert len(s["holdings"]) == 20


def test_too_little_fund_history_leaves_the_brake_off(tmp_path):
    names = [f"S{i:02d}" for i in range(25)]
    s = _account(tmp_path, Clock(2026, 11, 2, 16, 30), names).run(lambda: screen_result(names), lambda sym: UP[:20])
    assert not s["braked"] and len(s["holdings"]) == 20


def settings_for(tmp_path, start="2026-11-02"):
    return SimpleNamespace(state_dir=tmp_path, brake_paper_start=start, forward_universe="NIFTYMIDCAP150",
                           paper_starting_cash=200_000.0)


def test_the_scheduled_run_waits_reports_and_never_raises(tmp_path, monkeypatch):
    names = [f"S{i:02d}" for i in range(25)]
    prices = SimpleNamespace(latest_price=lambda s: 200.0 if s.upper() == "MID150BEES" else 100.0)
    assert "starts 2026-11-02" in bp.run_brake_paper_due(settings_for(tmp_path), prices, None, today=date(2026, 10, 30))
    assert "off" in bp.run_brake_paper_due(settings_for(tmp_path, None), prices, None, today=date(2026, 11, 2))
    from trading_agent import forward
    monkeypatch.setattr(forward, "_now", lambda: datetime(2026, 11, 2, 16, 30, tzinfo=IST))

    def broken(sym):
        raise RuntimeError("no fund history")
    assert "failed: RuntimeError: no fund history" in bp.run_brake_paper_due(
        settings_for(tmp_path), prices, None, today=date(2026, 11, 2), screen_fn=lambda: screen_result(names),
        closes_fn=broken)
    text = bp.run_brake_paper_due(settings_for(tmp_path), prices, None, today=date(2026, 11, 2),
                                  screen_fn=lambda: screen_result(names), closes_fn=lambda sym: UP)
    assert "Brake paper account (100-day average on MID150BEES" in text and "Brake: off" in text
    assert "Practice money only" in text


def test_the_kill_switch_stops_it_and_the_watch_and_page_run_it(tmp_path, monkeypatch):
    from trading_agent import killswitch
    monkeypatch.setattr(killswitch, "reason", lambda state_dir, what: f"Halted: refusing to {what}.")
    text = bp.run_brake_paper_due(settings_for(tmp_path), SimpleNamespace(latest_price=lambda s: 1.0), None,
                                  today=date(2026, 11, 2))
    assert text.startswith("Halted") and "brake" in text
    import inspect

    from trading_agent import cli, ui
    assert "run_brake_paper_due" in inspect.getsource(cli)
    assert cli.build_parser().parse_args(["brake-paper", "--run"]).run is True
    assert any(k == "brake" and p == "brake_" for k, p, *_ in ui.DashboardHandler.PAPER_ACCOUNTS) \
        if hasattr(ui, "DashboardHandler") else "\"brake_\"" in inspect.getsource(ui)


def test_the_report_names_the_brake_state_and_compares_with_the_adopted_rules():
    s = {"universe": "NIFTYMIDCAP150", "top": 20, "benchmark": "MID150BEES", "started": "2026-10-20",
         "strategy_return": 0.0, "benchmark_return": -0.05, "gap": 0.05, "strategy_max_drawdown": -0.02,
         "days": 30, "charges_paid": 900.0, "rebalanced": None, "braked": True,
         "brake_event": {"date": "2026-11-20", "brake": "on", "sold": [{"symbol": "A"}, {"symbol": "B"}]}}
    text = bp.format_brake_paper(s, {"from": "2026-10-20", "to": "2026-11-20", "strategy_return": -0.04})
    assert "ON, in cash" in text and "sold 2 holding(s)" in text and "brake minus the adopted rules: +4.00%" in text
