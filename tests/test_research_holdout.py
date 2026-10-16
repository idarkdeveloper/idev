"""Opening the sealed holdout once for the adopted rules. Synthetic prices and fakes only; no network."""
import json
import math
import random
from datetime import date
from types import SimpleNamespace

import pytest

from trading_agent import cli
from trading_agent import research_studies as rs
from trading_agent.costs import IndianDeliveryCosts
from trading_agent.research_sim import Market

from .test_research_sim import bars, days


def holdout_market(seed=3):
    """About 16 months before 1 Oct 2025 (the ranking's lookback) and a year after it."""
    rng = random.Random(seed)
    dates = days(600, date(2024, 6, 3))
    series = {}
    for i in range(40):
        p, path = 100.0, []
        drift, vol = rng.uniform(0.0008, 0.002), rng.uniform(0.006, 0.012)
        for _ in dates:
            p *= math.exp(drift + vol * rng.gauss(0, 1))
            path.append(p)
        series[f"S{i:02d}"] = bars(dates, path)
    m = Market(dates, series, bars(dates, [100 * 1.0003 ** i for i in range(600)]), lambda d: set(series),
               min_turnover=0)
    m.point_in_time = True
    return m


def test_the_holdout_runs_the_adopted_rules_on_the_sealed_months_only():
    m = holdout_market()
    m.start_day = rs.HOLDOUT_START
    r = rs.holdout(m, cost_model=IndianDeliveryCosts())
    assert r["start"] >= "2025-10-01" and r["end"] == m.dates[-1]
    a = r["results"]["adopted"]
    assert set(r["results"]) == {"adopted", "next-day fills + costs x2"} and a["months"] >= 10
    assert r["beats_fund"] == (a["excess"] > 0)
    assert r["fall_ok"] == (a["max_drawdown"] >= a["fund_max_drawdown"] - 0.10)
    assert r["passes"] == (r["beats_fund"] and r["fall_ok"])
    text = rs.format_holdout(r)
    assert "THE SEALED HOLDOUT, opened once" in text and "set on 16 Oct 2026 before the run" in text
    assert ("PASSED" in text) == r["passes"] and "cannot be used again" in text


def test_a_window_that_starts_before_the_holdout_is_refused():
    with pytest.raises(ValueError, match="before the holdout"):
        rs.holdout(holdout_market(), cost_model=IndianDeliveryCosts())   # start_day not moved to the holdout


def test_the_command_needs_confirm_runs_once_and_logs_first(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_settings", lambda a: SimpleNamespace(state_dir=tmp_path, paper_starting_cash=500_000.0))
    seen = {}

    def market(a, s):
        seen.update(until=a.until, years=a.years, opening=a.opening_holdout)
        return holdout_market()
    monkeypatch.setattr(cli, "_research_market", market)
    args = SimpleNamespace(json=False, confirm=False, until="2025-10-01", years=5)
    assert cli.cmd_research_holdout(args) == 2 and not seen
    assert "--confirm" in capsys.readouterr().out
    assert cli.cmd_research_holdout(SimpleNamespace(json=False, confirm=True, until="2025-10-01", years=5)) == 0
    assert seen == {"until": None, "years": 2, "opening": True}
    assert "THE SEALED HOLDOUT" in capsys.readouterr().out
    rows = [json.loads(x) for x in (tmp_path / "research" / "trials.jsonl").read_text().splitlines()]
    assert [r["study"] for r in rows] == ["Adopted-holdout"] * 2 and rows[0]["summary"]["passes"] in (True, False)
    assert cli.cmd_research_holdout(SimpleNamespace(json=False, confirm=True, until=None, years=2)) == 2
    assert "already run" in capsys.readouterr().out
    assert "research-holdout" in cli.HALT_ALLOWED


def test_the_leakage_guard_still_runs_but_allows_the_sealed_months(monkeypatch):
    from trading_agent import research_leakage as rl
    got = {}
    monkeypatch.setattr(rl, "guard", lambda m, **kw: got.update(kw) or {"ok": True})
    cli._leakage_guard(object(), holdout_start="9999-12-31")
    assert got["holdout_start"] == "9999-12-31"
    cli._leakage_guard(object())
    assert got["holdout_start"] is None                     # every other run keeps the holdout check


def test_the_claim_is_exclusive_and_survives_a_crashed_run(tmp_path, monkeypatch, capsys):
    from trading_agent.gap_diagnosis import claim_once, once_marker, trial_logged
    path = claim_once(tmp_path, rs.HOLDOUT_STUDY)
    assert path == once_marker(tmp_path, rs.HOLDOUT_STUDY) and json.loads(path.read_text())["study"] == rs.HOLDOUT_STUDY
    with pytest.raises(FileExistsError):                  # a second run started at the same time cannot claim it
        claim_once(tmp_path, rs.HOLDOUT_STUDY)
    assert not trial_logged(tmp_path, rs.HOLDOUT_STUDY)   # the first run "crashed": no result in the log
    monkeypatch.setattr(cli, "_settings", lambda a: SimpleNamespace(state_dir=tmp_path, paper_starting_cash=500_000.0))
    assert cli.cmd_research_holdout(SimpleNamespace(json=False, confirm=True, until=None, years=2)) == 2
    out = capsys.readouterr().out
    assert "already claimed" in out and "only you can decide to delete the marker" in out


def test_the_trial_log_is_read_as_json(tmp_path):
    from trading_agent.gap_diagnosis import trial_logged
    log = tmp_path / "research" / "trials.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text('not json\n{"study":"Adopted-holdout","params":{}}\n')   # no space after the colon
    assert trial_logged(tmp_path, "Adopted-holdout") and not trial_logged(tmp_path, "Backward-batch")


def test_a_failed_download_does_not_spend_the_holdout(tmp_path, monkeypatch):
    from trading_agent.gap_diagnosis import once_marker
    monkeypatch.setattr(cli, "_settings", lambda a: SimpleNamespace(state_dir=tmp_path, paper_starting_cash=500_000.0))
    monkeypatch.setattr(cli, "_research_market", lambda a, s: (_ for _ in ()).throw(OSError("Yahoo refused")))
    with pytest.raises(OSError):
        cli.cmd_research_holdout(SimpleNamespace(json=False, confirm=True, until="2025-10-01", years=5))
    assert not once_marker(tmp_path, rs.HOLDOUT_STUDY).exists()
