"""Saved portfolio value, one line per trading day (equity_history.py), and the Portfolio page's value chart."""
import json
from pathlib import Path
from types import SimpleNamespace

from trading_agent import equity_history as eh
from trading_agent.ui import App

UI = Path(__file__).resolve().parent.parent / "trading_agent" / "ui"


def test_one_line_per_day_and_the_last_write_of_a_day_wins(tmp_path):
    assert eh.read_days(tmp_path) == []
    eh.record_day(tmp_path, "2026-10-19", 186000.5, 200716)
    eh.record_day(tmp_path, "2026-10-16", 190000, None)
    eh.record_day(tmp_path, "2026-10-19", 186160, 200716)   # the close replaces the earlier figure
    days = eh.read_days(tmp_path)
    assert [d["day"] for d in days] == ["2026-10-16", "2026-10-19"]
    assert days[1] == {"day": "2026-10-19", "value": 186160.0, "invested": 200716.0} and days[0]["invested"] is None


def test_a_broken_file_is_an_empty_history_and_the_history_is_capped(tmp_path):
    (tmp_path / eh.FILE).write_text("{not json", encoding="utf-8")
    assert eh.read_days(tmp_path) == []
    for i in range(eh.KEEP_DAYS + 5):
        eh.record_day(tmp_path, f"{2000 + i // 365}-{1 + (i % 365) // 31:02d}-{1 + i % 28:02d}-{i:05d}", 1.0, None)   # unique, sortable keys
    assert len(json.loads((tmp_path / eh.FILE).read_text())["days"]) == eh.KEEP_DAYS


def test_the_app_serves_the_saved_days(tmp_path):
    eh.record_day(tmp_path, "2026-10-19", 186160, 200716)
    app = SimpleNamespace(settings=SimpleNamespace(state_dir=tmp_path))
    assert App.equity_history(app)["days"][0]["value"] == 186160.0   # type: ignore[arg-type]


def test_the_page_draws_the_value_chart():
    html = (UI / "index.html").read_text(encoding="utf-8")
    for needle in ('id="mp-eq"', "/api/equity-history", "function renderValueChart"):
        assert needle in html, needle
    css = (UI / "nocturne.css").read_text(encoding="utf-8")
    assert "#mp-eq" in css


def test_the_value_chart_function_has_its_own_name():
    """index.html already has a practice-account renderEquity(); a second one with that name silently replaced it
    (hoisting) and the Portfolio chart stayed empty."""
    html = (UI / "index.html").read_text(encoding="utf-8")
    assert html.count("function renderEquity(") == 1


def test_the_forward_factor_screen_lives_on_the_paper_accounts_tab():
    html = (UI / "index.html").read_text(encoding="utf-8")
    paper = html[html.index('id="ws-paper"'):html.index('id="ws-tools"')]
    assert 'id="fw-card"' in paper and 'id="fw-card"' not in html[html.index('id="ws-overview"'):html.index('id="ws-deals"')]
    assert 'forward: "paper"' in html and '<a href="#forward">' in html
