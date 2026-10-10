"""Fix round 2 for the blind test: resume after a crash, partial verdicts, net-value verdict text, IST dates."""
import json
import os
import re
import time

import pytest

from trading_agent.replay import blind
from trading_agent.replay.blind import Case, compare_case, latest_verdict, run_cli, stance_of, verdict

from .test_replay_blind import (TICKER, Fake, News, _args, _res, _setup, source)


def _view(action, conf="high"):
    return stance_of({"recommendations": [{"action": action, "ticker": "X", "confidence": conf}]}, "X")


def _case(u1, m1, u2, m2):
    return compare_case(Case("t", "d", "X"), _view(u1), _view(m1), None, _view(u2), _view(m2))


class Dies4(Fake):
    def create(self, **kw):
        if len(self.calls) == 4:
            raise RuntimeError("API down")
        return super().create(**kw)


def _crash_first_run(tmp_path):
    settings, uni = _setup(tmp_path)
    with pytest.raises(RuntimeError):
        run_cli(_args(yes=True), settings, source=source(), news_client=News(), universe=uni, client=Dies4(),
                out=lambda *_: None, today="2026-10-09")
    return settings, uni


def test_resume_skips_finished_cases_and_bills_only_the_rest(tmp_path):
    settings, uni = _crash_first_run(tmp_path)
    lines, client = [], Fake()
    rc = run_cli(_args(), settings, source=source(), news_client=News(), universe=uni, client=client,
                 out=lines.append, today="2026-10-09")
    assert rc == 1 and client.calls == [] and "4 Claude calls" in "\n".join(lines)   # estimate: the remaining case only
    lines.clear()
    rc = run_cli(_args(yes=True), settings, source=source(), news_client=News(), universe=uni, client=client,
                 out=lines.append, today="2026-10-09")
    text = "\n".join(lines)
    assert rc == 0 and len(client.calls) == 4
    assert re.search(r"resuming .*blind_test_2026-10-09\.json: 1 of 2 cases already done", text)
    files = sorted((settings.state_dir / "research").glob("*.json"))
    assert [f.name for f in files] == ["blind_test_2026-10-09.json"]
    rep = json.loads(files[0].read_text())
    assert rep["complete"] is True and len(rep["results"]) == 2 and rep["summary"]["cases"] == 2
    again = Fake()                              # a finished run is not resumed
    run_cli(_args(yes=True), settings, source=source(), news_client=News(), universe=uni, client=again,
            out=lambda *_: None, today="2026-10-09")
    assert len(again.calls) == 8 and (settings.state_dir / "research" / "blind_test_2026-10-09_2.json").exists()


def test_fresh_and_a_different_case_list_do_not_resume(tmp_path):
    settings, uni = _crash_first_run(tmp_path)
    client = Fake()
    run_cli(_args(yes=True, fresh=True), settings, source=source(), news_client=News(), universe=uni, client=client,
            out=lambda *_: None, today="2026-10-09")
    assert len(client.calls) == 8
    settings2, uni2 = _crash_first_run(tmp_path / "b")
    other = Fake()
    run_cli(_args(yes=True, case=["2022-03-15:" + TICKER]), settings2, source=source(), news_client=News(),
            universe=uni2, client=other, out=lambda *_: None, today="2026-10-09")
    assert len(other.calls) == 4                                        # its own single case, not the 2-case run


def test_latest_verdict_skips_incomplete_files_but_labels_a_lone_partial(tmp_path):
    settings, uni = _crash_first_run(tmp_path)
    v = latest_verdict(settings.state_dir)
    assert v["partial"] is True and v["verdict"].endswith("(partial, 1 of 2 cases)")
    run_cli(_args(yes=True, fresh=True), settings, source=source(), news_client=News(), universe=uni, client=Fake(),
            out=lambda *_: None, today="2026-10-09")
    full = latest_verdict(settings.state_dir)
    assert full["partial"] is False and "partial" not in full["verdict"]
    newest = settings.state_dir / "research" / "blind_test_2026-10-10.json"      # newer but unfinished
    newest.write_text(json.dumps({"complete": False, "planned": [1, 2, 3], "model": "m",
                                  "summary": {"verdict": "no sign of hindsight", "cases": 1}}))
    os.utime(newest, (time.time() + 50, time.time() + 50))
    assert latest_verdict(settings.state_dir)["file"] == full["file"]


def test_verdict_text_describes_noise_as_an_expected_average():
    noisy = [_case("buy", "buy", "hold", "hold")] * 5       # no identity change; each pair flips under noise
    v = verdict(noisy)
    assert v["verdict"] == "noise explains all of the difference; no sign of hindsight"
    assert "about 1.00 changes per case from chance alone" in v["thresholds"]
    assert v["net_flips"] == -5 and not v["possible_hindsight"]
    assert verdict([_res(("buy", "high"), ("buy", "high"))] * 5)["verdict"] == \
        "noise explains all of the difference; no sign of hindsight"          # net 0 and 0
    small = [_res(("buy", "medium"), ("buy", "low"))] * 2 + [_res(("buy", "high"), ("buy", "high"))] * 3
    assert verdict(small)["verdict"] == "no sign of hindsight"                # real but below the thresholds


def test_possible_hindsight_count_uses_net_values():
    clean = _case("buy", "hold", "buy", "hold")             # identity flip, no noise
    shaky = _case("buy", "hold", "hold", "hold")            # identity flip, half a noise flip
    none = _case("buy", "buy", "buy", "buy")
    v = verdict([clean] * 2 + [shaky] * 2 + [none])
    assert v["identity_flips"] == 4 and v["noise_flips"] == 1.0 and v["net_flips"] == 3.0
    assert v["possible_hindsight"] and v["verdict"] == "possible hindsight: 3 of 5 cases"   # not the raw 4
    gap = [compare_case(Case("t", "d", "X"), _view("buy", "high"), _view("buy", "low"), None,
                        _view("buy", "high"), _view("buy", "low"))] * 5
    g = verdict(gap)
    assert g["possible_hindsight"] and g["verdict"] == "possible hindsight: 5 of 5 cases"


def test_page_text_never_prints_more_noise_than_changes():
    js = open(os.path.join(os.path.dirname(blind.__file__), "..", "ui", "replay.js"), encoding="utf-8").read()
    assert "fewer stance changes than chance alone would give" in js and "from noise" not in js


def test_new_report_path_uses_the_ist_date(tmp_path, monkeypatch):
    monkeypatch.setattr(blind, "_ist_today", lambda: "2031-01-02")
    assert blind.new_report_path(tmp_path).name == "blind_test_2031-01-02.json"


def test_membership_cache_evicts_earlier_days(monkeypatch, tmp_path):
    from trading_agent import index_history, scorecard, screen
    scorecard.clear_membership_cache()
    monkeypatch.setattr(screen, "load_universe", lambda k: [{"symbol": "X"}])
    monkeypatch.setattr(index_history, "point_in_time", lambda k, c, sd, progress=None: object())
    scorecard.build_memberships(tmp_path, use_cache=True, today_fn=lambda: "2026-10-09")
    assert {k[1] for k in scorecard._CACHE} == {"2026-10-09"}
    scorecard.build_memberships(tmp_path, use_cache=True, today_fn=lambda: "2026-10-10")
    assert {k[1] for k in scorecard._CACHE} == {"2026-10-10"}
    scorecard.clear_membership_cache()
