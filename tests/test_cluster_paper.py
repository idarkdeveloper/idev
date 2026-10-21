"""The cluster-buying paper account. Fakes only; no network."""
import json
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from trading_agent import cluster_paper as cp
from trading_agent.config import parse_cluster_paper_start
from trading_agent.costs import cost_model_for
from trading_agent.timezones import IST

from .test_forward import Clock


def buy(sym, person, day, reported, cat="Promoter Group", mode="Market Purchase", value=1e6, side="buy"):
    return {"symbol": sym, "person": person, "category": cat, "mode": mode, "instrument": "Equity", "side": side,
            "value": value, "qty": 100.0, "date": day, "reported": reported, "company": f"{sym} Ltd"}


def test_the_plan_numbers_and_start_setting():
    assert (cp.MIN_INSIDERS, cp.MIN_TRADE_DATES, cp.WINDOW_DAYS, cp.SLOTS, cp.SLOT_WEIGHT, cp.HOLD_DAYS, cp.FLOOR,
            cp.BENCHMARK) == (3, 2, 30, 20, 0.05, 182, 0.25, "HDFCSML250")
    text = open("docs/research/cluster-buying-paper-account-2026-10-21.md", encoding="utf-8").read()
    for needle in ("**3 different buyers**", "**2 different trade dates**", "**5% of the account's value**",
                   "**20 names**", "**6 months (182 days) or more", "**25% or more below", "HDFCSML250", "22 Oct 2026"):
        assert needle in text
    assert parse_cluster_paper_start(None) == "2026-10-22" and parse_cluster_paper_start("off") is None
    with pytest.raises(ValueError):
        parse_cluster_paper_start("soon")


def test_a_cluster_needs_three_insiders_two_dates_open_market_and_the_30_day_disclosure_window():
    day = "2026-11-02"
    good = [buy("AAA", "Ram  Kumar", "2026-10-10", "2026-10-12"), buy("AAA", "SITA DEVI", "2026-10-11", "2026-10-13"),
            buy("AAA", "ram kumar", "2026-10-15", "2026-10-16"), buy("AAA", "Gopal Rao", "2026-10-15", "2026-10-16",
                                                                      cat="Director")]
    block = [buy("FAM", f"Member {i}", "2026-10-20", "2026-10-21") for i in range(8)]          # one day only
    two = [buy("TWO", "A", "2026-10-01", "2026-10-05"), buy("TWO", "B", "2026-10-02", "2026-10-06")]
    noise = [buy("NOI", "A", "2026-10-01", "2026-10-05", mode="Inter-se-Transfer"),
             buy("NOI", "B", "2026-10-02", "2026-10-06", cat="Designated Person"),
             buy("NOI", "C", "2026-10-03", "2026-10-07", side="sell"),
             buy("NOI", "D", "2026-10-04", "2026-10-08", cat="Immediate Relative")]
    old = [buy("OLD", p, d, "2026-09-20") for p, d in (("A", "2026-09-10"), ("B", "2026-09-11"), ("C", "2026-09-12"))]
    late = [buy("LAT", p, d, "2026-11-03") for p, d in (("A", "2026-10-30"), ("B", "2026-10-31"), ("C", "2026-11-01"))]
    found = cp.clusters(good + block + two + noise + old + late, day)
    assert [c["symbol"] for c in found] == ["AAA"]                 # names normalised: Ram Kumar counted once
    assert found[0]["insiders"] == 3 and found[0]["dates"] == 3
    rel = [buy("REL", "A", "2026-10-01", "2026-10-05", cat="Immediate Relative"),
           buy("REL", "B", "2026-10-02", "2026-10-06", cat="Promoter"), buy("REL", "C", "2026-10-02", "2026-10-06",
                                                                          cat="KMP")]
    assert [c["symbol"] for c in cp.clusters(rel, day)] == ["REL"]


def _cluster(sym, value=1e6, reported="2026-10-20"):
    return [buy(sym, p, d, reported, value=value) for p, d in (("A", "2026-10-10"), ("B", "2026-10-11"), ("C", "2026-10-11"))]


def _account(tmp_path, clock, prices):
    return cp.ClusterPaper(tmp_path, universe="NIFTYMIDCAP150", capital=200_000,
                           price_fn=lambda s: prices[s.upper()], cost_model=cost_model_for("in"), now=clock)


def screen_ok(*bad):
    return lambda syms: {"all": [{"symbol": s, "eligible": s not in bad} for s in syms]}


def test_monthly_buys_filtered_clusters_in_five_percent_slots_and_sells_after_six_months(tmp_path):
    prices = {"HDFCSML250": 100.0, "AAA": 100.0, "BBB": 50.0, "CCC": 20.0}
    clock = Clock(2026, 11, 2, 16, 30)
    rows = _cluster("AAA", 3e6) + _cluster("BBB", 2e6) + _cluster("CCC", 1e6)
    s = _account(tmp_path, clock, prices).run(rows, screen_ok("CCC"))
    held = {h["symbol"]: h for h in s["holdings"]}
    assert set(held) == {"AAA", "BBB"} and s["rebalanced"]["passed"] == ["AAA", "BBB"]
    assert all(0.04 < h["weight"] < 0.051 for h in held.values()) and 0.08 < s["invested"] < 0.11
    data = json.loads((tmp_path / "forward" / "cluster_niftymidcap150.json").read_text())
    assert data["benchmark"] == "HDFCSML250" and data["bought"]["AAA"] == "2026-11-02" and data["variant"].startswith("Cluster")
    clock.t = datetime(2026, 12, 1, 16, 30, tzinfo=IST)                 # AAA again a cluster: not bought twice
    assert cp.clusters(rows, "2026-12-01") == []                        # the October filings are outside 30 days
    rows = _cluster("AAA", 3e6, "2026-11-25") + _cluster("CCC", 1e6, "2026-11-25")
    s = _account(tmp_path, clock, prices).run(rows, screen_ok())
    assert s["rebalanced"]["bought"][0]["symbol"] == "CCC" and len(s["holdings"]) == 3
    clock.t = datetime(2027, 5, 3, 16, 30, tzinfo=IST)                  # 182+ days after 2 Nov
    s = _account(tmp_path, clock, prices).run([], screen_ok())
    assert {x["symbol"] for x in s["rebalanced"]["sold"]} == {"AAA", "BBB"} and [h["symbol"] for h in s["holdings"]] == ["CCC"]


def test_the_floor_sells_a_fallen_holding_any_day(tmp_path):
    prices = {"HDFCSML250": 100.0, "AAA": 100.0}
    clock = Clock(2026, 11, 2, 16, 30)
    _account(tmp_path, clock, prices).run(_cluster("AAA"), screen_ok())
    prices["AAA"] = 70.0
    clock.t = datetime(2026, 11, 10, 16, 30, tzinfo=IST)
    s = _account(tmp_path, clock, prices).run([], screen_ok())
    assert s["holdings"] == [] and s["floor_sales"][0]["symbol"] == "AAA"


def settings_for(tmp_path, start="2026-11-02"):
    return SimpleNamespace(state_dir=tmp_path, cluster_paper_start=start, forward_universe="NIFTYMIDCAP150",
                           paper_starting_cash=200_000.0)


def test_the_scheduled_run_waits_reports_and_never_raises(tmp_path, monkeypatch):
    prices = SimpleNamespace(latest_price=lambda s: 100.0)
    assert "starts 2026-11-02" in cp.run_cluster_paper_due(settings_for(tmp_path), prices, None, today=date(2026, 10, 30))
    assert "off" in cp.run_cluster_paper_due(settings_for(tmp_path, None), prices, None, today=date(2026, 11, 2))
    from trading_agent import forward
    monkeypatch.setattr(forward, "_now", lambda: datetime(2026, 11, 2, 16, 30, tzinfo=IST))

    def broken():
        raise RuntimeError("no insider book")
    assert "failed: RuntimeError: no insider book" in cp.run_cluster_paper_due(
        settings_for(tmp_path), prices, None, today=date(2026, 11, 2), rows_fn=broken)
    text = cp.run_cluster_paper_due(settings_for(tmp_path), prices, None, today=date(2026, 11, 2),
                                    rows_fn=lambda: _cluster("AAA"), screen_fn=screen_ok())
    assert "Cluster-buying paper account" in text and "bought AAA" in text and "Practice money only" in text


def test_the_kill_switch_stops_it_and_the_watch_and_page_run_it(tmp_path, monkeypatch):
    from trading_agent import killswitch
    monkeypatch.setattr(killswitch, "reason", lambda state_dir, what: f"Halted: refusing to {what}.")
    text = cp.run_cluster_paper_due(settings_for(tmp_path), SimpleNamespace(latest_price=lambda s: 1.0), None,
                                    today=date(2026, 11, 2))
    assert text.startswith("Halted") and "cluster" in text
    import inspect

    from trading_agent import cli, ui
    assert "run_cluster_paper_due" in inspect.getsource(cli)
    assert cli.build_parser().parse_args(["cluster-paper", "--run"]).run is True
    assert '("cluster", "cluster_"' in inspect.getsource(ui)
