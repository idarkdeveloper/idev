"""The pre-registered US and Japan replication. Synthetic prices and fake member lists only; no network."""
import json
import math
import random
from datetime import date
from types import SimpleNamespace

import pytest

from trading_agent import research_replication as rr

from .test_research_sim import bars, days

US_PAGE = """
<table class="wikitable sortable" id="constituents">
<tr><th>Symbol</th><th>Security</th><th>Sector</th></tr>
<tr><td><a>AA</a></td><td>Alcoa</td><td>Materials</td></tr>
<tr><td><a>BRK.B</a></td><td>Berkshire class B</td><td>Financials</td></tr>
<tr><td><a>NEW</a></td><td>Newcomer</td><td>Industrials</td></tr>
</table>
<table class="wikitable sortable" id="changes">
<tr><th rowspan="2">Date</th><th colspan="2">Added</th><th colspan="2">Removed</th><th rowspan="2">Reason</th></tr>
<tr><th>Ticker</th><th>Security</th><th>Ticker</th><th>Security</th></tr>
<tr><td rowspan="2">September 21, 2026</td><td>NEW</td><td>Newcomer</td><td>OLD</td><td>Oldco</td><td>Cap<sup>[3]</sup></td></tr>
<tr><td></td><td></td><td>GONE</td><td>Goneco</td><td>Acquired</td></tr>
<tr><td>March 26, 2012</td><td>AA</td><td>Alcoa</td><td>XX</td><td>Ex</td><td>Cap</td></tr>
</table>
<table class="navbox"><tr><td>not a data table</td></tr></table>
"""

JP_CSV = ("Date,Issue,Code,Sector,Component Weight (TOPIX),New Index Series Code\n"
          '20260831,"A CO.",1332,"Foods",0.03%,TOPIX Mid400\n'
          '20260831,"B CO.",1301,"Foods",0.01%,TOPIX Small 1\n'
          '20260831,"C CO.",130A,"Services",0.02%,TOPIX Mid400\n'
          '20260831,"D CO.",,"Services",0.02%,TOPIX Mid400\n')


def test_the_us_page_gives_members_and_dated_changes_with_rowspans():
    current, changes = rr.parse_us(US_PAGE)
    assert current == ["AA", "BRK-B", "NEW"]                               # Yahoo's spelling of share classes
    assert changes == [("2012-03-26", ("AA",), ("XX",)), ("2026-09-21", ("NEW",), ("GONE", "OLD"))]


def test_the_jpx_file_gives_todays_mid400():
    assert rr.parse_jp(JP_CSV) == ["130A", "1332"]


def month_calendar():
    return days(2600, date(2015, 10, 1))


def test_membership_is_point_in_time_only_when_the_history_is_complete():
    cal = month_calendar()
    current = [f"S{i:03d}" for i in range(400)]
    swap = [("2014-01-02", ("S000",), ("X000",)), ("2020-03-02", ("S001",), ("X001",))]
    m, notes = rr.us_membership(current, swap, cal)
    assert m is not None and "point in time" in notes[0]
    assert "X001" in m.members_on("2019-01-02") and "S001" not in m.members_on("2019-01-02")
    lopsided = swap + [("2021-06-01", (f"S{i:03d}" for i in range(2, 20)), ())]   # additions with no removals
    lopsided = [(d, tuple(a), tuple(r)) for d, a, r in lopsided]
    m2, notes2 = rr.us_membership(current, lopsided, cal)
    assert m2 is None and "today's 400 members" in notes2[0] and "outside 390-410" in notes2[0]
    m3, notes3 = rr.us_membership(current, [("2018-01-02", ("S000",), ("X000",))], cal)
    assert m3 is None and "starts 2018-01-02" in notes3[0]
    assert rr.us_membership(current, [], cal)[0] is None


class FakePrices:
    def __init__(self, series):
        self.series = series

    def history(self, sym, rng):
        if sym not in self.series:
            raise LookupError(sym)
        return self.series[sym]


def synthetic(n=900, k=30, seed=3):
    rng = random.Random(seed)
    dates = days(n, date(2015, 10, 1))
    series = {}
    for i in range(k):
        p, path = 100.0, []
        drift, vol = rng.uniform(-0.0002, 0.0015), rng.uniform(0.01, 0.02)
        for _ in range(n):
            p *= math.exp(drift + vol * rng.gauss(0, 1))
            path.append(p)
        series[f"S{i:02d}"] = [{**b, "volume": 1e5} for b in bars(dates, path)]
    series["IJH"] = bars(dates, [100 * 1.0003 ** i for i in range(n)])
    return dates, series


def test_the_run_uses_the_unchanged_rules_without_tax_and_the_fixed_costs():
    dates, series = synthetic()
    members = [s for s in series if s != "IJH"] + ["DEAD"]                 # one member without prices
    m, notes = rr.load_replication_market("us", FakePrices(series), members, [])
    assert m.min_turnover == 120_000.0 and not m.point_in_time and m.missing == ["DEAD"]
    assert "today's 31 members" in notes[0] and m.dates[0] >= rr.LOOKBACK_FROM and m.dates[-1] < rr.WINDOW_END
    r = rr.replicate("us", m)
    assert r["start"] >= rr.TRADING_FROM and "buffer35" in r["rules"] and r["point_in_time"] is False
    assert set(r["checks"]) == {"beats the fund", "worst fall within 10 points of the fund's"}
    assert r["passes"] == all(r["checks"].values())
    ahead, months = r["months_ahead"]
    assert 0 <= ahead <= months and months > 10
    text = rr.format_replication(r, notes)
    assert "REPLICATION, run once" in text and "S&P MidCap 400" in text and "5 bps a side" in text
    assert "Members without prices: 1 of 31 (DEAD)" in text and "This market is now spent" in text
    j = rr.MARKETS["jp"]
    assert j["benchmark"] == "1306" and j["suffix"] == ".T" and j["one_way_bps"] == 10.0


def test_a_benchmark_without_the_lookback_year_stops_before_anything_runs():
    dates, series = synthetic()
    series["IJH"] = [b for b in series["IJH"] if b["date"] >= "2016-06-01"]
    with pytest.raises(ValueError, match="no prices from 2015-10-01"):
        rr.load_replication_market("us", FakePrices(series), ["S00"], [])


def test_the_command_needs_confirm_and_runs_each_market_once(tmp_path, monkeypatch, capsys):
    from trading_agent import cli
    dates, series = synthetic()
    members = [s for s in series if s != "IJH"]
    monkeypatch.setattr(cli, "_settings", lambda a: SimpleNamespace(state_dir=tmp_path))
    monkeypatch.setattr(rr, "fetch_members", lambda market: (members * 11, []))   # enough names to pass the count
    monkeypatch.setattr(rr, "prices_for", lambda market, state_dir: FakePrices(series))
    guarded = {}
    monkeypatch.setattr(cli, "_leakage_guard", lambda mk, holdout_start=None: guarded.update(h=holdout_start))
    args = SimpleNamespace(replication_market="us", confirm=False, json=False)
    assert cli.cmd_research_replication(args) == 2 and "Run again with --confirm" in capsys.readouterr().out
    args.confirm = True
    assert cli.cmd_research_replication(args) == 0
    out = capsys.readouterr().out
    assert "Claimed Replication-US" in out and "REPLICATION, run once" in out and guarded["h"] == rr.WINDOW_END
    rows = [json.loads(x) for x in (tmp_path / "research" / "trials.jsonl").read_text().splitlines()]
    assert [r["study"] for r in rows] == ["Replication-US"]
    assert cli.cmd_research_replication(args) == 2 and "used once only" in capsys.readouterr().out
    monkeypatch.setattr(rr, "fetch_members", lambda market: (["A"], []))   # Japan: too few names read -> not spent
    assert cli.cmd_research_replication(SimpleNamespace(replication_market="jp", confirm=True, json=False)) == 1
    assert not (tmp_path / "research" / "once" / "Replication-JP.claimed").exists()


def test_the_market_flag_never_reaches_the_apps_market_setting(tmp_path, monkeypatch, capsys):
    """--market jp once reached _settings as MARKET=jp and stopped the run; it must parse and leave MARKET alone."""
    from trading_agent import cli, config
    monkeypatch.delenv("MARKET", raising=False)
    monkeypatch.setattr(config, "load_settings", lambda: SimpleNamespace(state_dir=tmp_path))
    monkeypatch.setattr(cli, "load_settings", lambda: SimpleNamespace(state_dir=tmp_path), raising=False)
    args = cli.build_parser().parse_args(["research-replication", "--market", "jp"])
    assert args.replication_market == "jp" and getattr(args, "market", None) is None
    assert cli.cmd_research_replication(args) == 2 and "TOPIX Mid400" in capsys.readouterr().out
    import os
    assert "MARKET" not in os.environ


UK_PAGE = """
<table class="wikitable sortable" id="constituents">
<tr><th>Company</th><th>Ticker</th><th>Sector[13]</th></tr>
<tr><td>3i Infrastructure</td><td>3IN</td><td>Financial Services</td></tr>
<tr><td>BT class A</td><td>BT.A</td><td>Telecoms</td></tr>
<tr><td>Newco</td><td>NEW.<sup>[4]</sup></td><td>Industrials</td></tr>
<tr><td>Blank</td><td></td><td>None</td></tr>
</table>
"""


def test_the_uk_page_gives_todays_ftse250_in_yahoo_spelling():
    assert rr.parse_uk(UK_PAGE) == ["3IN", "BT-A", "NEW"]
    assert rr.parse_uk("<table id='constituents'><tr><th>Company</th></tr></table>") == []
    u = rr.MARKETS["uk"]
    assert (u["suffix"], u["benchmark"], u["min_turnover"], u["study"]) == (".L", "MIDD", 9_500_000.0, "Replication-UK")
    assert u["plan"] == rr.UK_ADDENDUM and u["min_members"] == 240


def test_uk_costs_charge_stamp_duty_on_buys_only():
    c = rr.cost_model("uk")
    assert c.charges("buy", 10_000) == pytest.approx(55.0) and c.charges("sell", 10_000) == pytest.approx(5.0)
    assert c.round_trip_bps(1) == 60.0
    assert rr.cost_model("us").charges("buy", 10_000) == pytest.approx(5.0)
    assert rr.cost_text("uk") == "55 bps on buys, 5 bps on sells" and rr.cost_text("jp") == "10 bps a side"


def test_the_uk_run_uses_the_addendum_costs_and_the_fund_pays_no_stamp_duty():
    dates, series = synthetic()
    series["MIDD"] = series.pop("IJH")
    members = [s for s in series if s != "MIDD"]
    m, notes = rr.load_replication_market("uk", FakePrices(series), members, [])
    assert m.min_turnover == 9_500_000.0 and m.unit_jumps == [] and "Wikipedia publishes no free change history" in notes[0]
    r = rr.replicate("uk", m)
    assert r["fund_total_return"] is not None
    text = rr.format_replication(r, notes)
    assert "FTSE 250 (the United Kingdom)" in text and "55 bps on buys, 5 bps on sells" in text
    assert rr.UK_ADDENDUM in text and "iShares FTSE 250 ETF (MIDD.L)" in text
    # the same run with symmetric 55 bps costs does worse: buys and sells really are charged differently
    from trading_agent.research_studies import FINALIST
    rules, end = rr.with_rules(FINALIST, tax=False), len(m.dates) - 1
    sym = rr.simulate(m, rules, 0, end, capital=rr.CAPITAL, cost_model=rr.SideCosts(55.0, 55.0))
    asym = rr.simulate(m, rules, 0, end, capital=rr.CAPITAL, cost_model=rr.SideCosts(55.0, 5.0))
    assert sym["costs"] > asym["costs"] > 0


def test_a_pence_pound_mix_up_stops_the_uk_run_before_anything_is_spent(tmp_path, monkeypatch, capsys):
    from trading_agent import cli
    dates, series = synthetic()
    series["MIDD"] = series.pop("IJH")
    series["S00"] = [{**b, "close": b["close"] / 100} if i >= 500 else b for i, b in enumerate(series["S00"])]
    assert rr.unit_jumps(series) == ["S00"]
    members = [s for s in series if s != "MIDD"]
    monkeypatch.setattr(cli, "_settings", lambda a: SimpleNamespace(state_dir=tmp_path))
    monkeypatch.setattr(rr, "fetch_members", lambda market: (members * 9, []))
    monkeypatch.setattr(rr, "prices_for", lambda market, state_dir: FakePrices(series))
    monkeypatch.setattr(cli, "_leakage_guard", lambda mk, holdout_start=None: None)
    args = SimpleNamespace(replication_market="uk", confirm=True, json=False)
    assert cli.cmd_research_replication(args) == 1 and "unit mix-up" in capsys.readouterr().out
    assert not (tmp_path / "research" / "once" / "Replication-UK.claimed").exists()
    series["S00"] = list(series["S01"])                                       # clean data: runs once, then refused
    assert cli.cmd_research_replication(args) == 0 and "Claimed Replication-UK" in capsys.readouterr().out
    assert cli.cmd_research_replication(args) == 2 and "used once only" in capsys.readouterr().out


def test_the_uk_flag_parses():
    from trading_agent import cli
    assert cli.build_parser().parse_args(["research-replication", "--market", "uk"]).replication_market == "uk"
