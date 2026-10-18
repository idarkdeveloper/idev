"""Pre-registered batch 3 and the circuit-lock audit. Synthetic prices and a fake NSE only; no network."""
import json
from datetime import date
from types import SimpleNamespace

import pytest

from trading_agent import research_batch3 as b3
from trading_agent import research_circuits as rc
from trading_agent import research_leakage as rl
from trading_agent.costs import cost_model_for
from trading_agent.research_sim import Market

from .test_research_leakage import make
from .test_research_sim import bars, days


def tiny(paths, n=300):
    dates = days(n, date(2021, 1, 4))
    return Market(dates, {s: bars(dates, p) for s, p in paths.items()}, bars(dates, [100.0] * n),
                  lambda d: set(paths), min_turnover=0)


def test_smooth_gains_score_lower_than_a_few_jumps():
    n = 300
    steady = [100 * 1.002 ** i for i in range(n)]                       # a small gain every day
    jumpy = []
    p = 100.0
    for i in range(n):                                                   # mostly small falls, a few big jumps
        p *= 1.25 if i % 60 == 30 else 0.999
        jumpy.append(p)
    m = tiny({"STEADY": steady, "JUMPY": jumpy}, n)
    t = n - 1
    s, j = b3.information_discreteness(m, "STEADY", t), b3.information_discreteness(m, "JUMPY", t)
    assert s == pytest.approx(-1.0) and j is not None and j > 0.5
    assert b3.information_discreteness(m, "STEADY", 200) is None         # under 252 sessions of history


def test_the_smooth_ranker_reorders_only_the_top_40(monkeypatch):
    m = make()
    t = len(m.dates) - 1
    base = list(m.momentum_ranking(t))
    monkeypatch.setattr(b3, "TOP", 5)
    out = b3.smooth_ranker()(m, t)
    assert sorted(out[:5]) == sorted(base[:5]) and out[5:] == base[5:]
    ids = [b3.information_discreteness(m, s, t) for s in out[:5]]
    known = [x for x in ids if x is not None]
    assert known == sorted(known) and (None not in ids or ids.index(None) >= len(known))


DEALS = [
    {"date": "01-SEP-2021", "symbol": "S01", "clientName": "ABC MUTUAL FUND", "buySell": "BUY", "qty": "1000", "watp": "50"},
    {"date": "05-SEP-2021", "symbol": "S01", "clientName": "XYZ Life Insurance Co", "buySell": "SELL", "qty": "100",
     "watp": "50"},
    {"date": "10-SEP-2021", "symbol": "S02", "clientName": "RETAIL TRADER", "buySell": "BUY", "qty": "9999", "watp": "9"},
    {"date": "12-SEP-2021", "symbol": "S03", "clientName": "STATE PENSION PLAN", "buySell": "BUY", "qty": "", "watp": "9"},
    {"date": "30-SEP-2021", "symbol": "S04", "clientName": "GLOBAL FUND LP", "buySell": "BUY", "qty": "10", "watp": "10"},
]


def test_institutional_flows_and_the_60_day_window():
    flows, skipped = b3.institutional_flows(DEALS)
    assert set(flows) == {"S01", "S04"} and skipped == 1                # the retail client is ignored, S03 has no qty
    assert flows["S01"] == [("2021-09-01", 50_000.0), ("2021-09-05", -5_000.0)]
    assert b3.net_value(flows, "S01", "2021-10-01") == pytest.approx(45_000.0)
    assert b3.net_value(flows, "S04", "2021-09-30") == 0.0              # a deal on the decision day is not known yet
    assert b3.net_value(flows, "S01", "2021-11-05") == 0.0              # older than 60 days
    assert b3.is_institution("hdfc mutual fund") and not b3.is_institution("Rakesh Investments")


def test_the_institutional_ranker_moves_buyers_to_the_front_of_the_top_40(monkeypatch):
    m = make()
    t = len(m.dates) - 1
    base = list(m.momentum_ranking(t))
    pick = base[3]
    flows = {pick: [(m.dates[t - 5], 1_000_000.0)], base[0]: [(m.dates[t - 5], -10.0)]}
    out = b3.institutional_ranker(flows)(m, t)
    assert out[0] == pick and out[1:4] == [s for s in base[:5] if s != pick][:3]
    monkeypatch.setattr(b3, "TOP", 2)
    assert b3.institutional_ranker(flows)(m, t) == base                 # the buyer is outside the top 2: no change


def test_neither_variant_looks_ahead():
    m = make()
    flows = {s: [(d, 1e6 if i % 3 else -1e6)] for i, s in enumerate(m.raw) for d in m.dates[::40]}
    for ranker in (b3.smooth_ranker(), b3.institutional_ranker(flows)):
        r = rl.leakage_check(m, ranker=ranker, ranking_days=3, simulation_days=1, holdout_start="2099-01-01")
        assert r["checks"]["ranking"]["failures"] == [], r["checks"]["ranking"]["failures"]


class FakeNSE:
    base_url = "https://example.invalid"

    def __init__(self, fail_on=None):
        self.calls = []
        self.fail_on = fail_on

    def _ensure_warm(self, headers):
        pass

    def _get_text(self, path, params, referer):
        self.calls.append(params)
        if self.fail_on and params["from"] == self.fail_on:
            raise ConnectionError("refused")
        return ('"Date","Symbol","Security Name","Client Name","Buy / Sell","Quantity Traded",'
                '"Trade Price / Wght. Avg. Price","Remarks"\n'
                f'"{params["from"]}","S01","S one","ABC FUND","BUY","1,000","10.5","-"\n')


def test_the_deal_history_is_strict_cached_and_complete(tmp_path):
    nse = FakeNSE()
    rows = b3.fetch_deal_history(nse, tmp_path, start="2021-01-01", end="2021-06-30", sleep=lambda s: None)
    windows = b3._windows("2021-01-01", "2021-06-30")
    assert len(windows) == 3 and windows[0] == (date(2021, 1, 1), date(2021, 3, 31))
    assert len(nse.calls) == 6 and len(rows) == 6 and {r["kind"] for r in rows} == {"bulk", "block"}
    assert all(c["csv"] == "true" for c in nse.calls)
    again = FakeNSE()
    assert len(b3.fetch_deal_history(again, tmp_path, start="2021-01-01", end="2021-06-30")) == 6 and not again.calls
    broken = FakeNSE(fail_on="01-04-2021")
    with pytest.raises(b3.DealHistoryError, match="2021-04-01"):
        b3.fetch_deal_history(broken, tmp_path / "other", start="2021-01-01", end="2021-06-30", sleep=lambda s: None)
    assert sum(1 for c in broken.calls if c["from"] == "01-04-2021") == 3   # tried three times


def test_the_batch_judges_both_variants_against_the_control():
    m = make()
    r = b3.batch3(m, {}, cost_model=cost_model_for("in"), capital=100_000)
    assert set(r["results"]) == {"control", "F", "G"}
    g, c = r["results"]["G"], r["results"]["control"]
    assert g["total_return"] == pytest.approx(c["total_return"])          # no deals: G is the control's order
    for k in ("F", "G"):
        assert set(r["results"][k]["checks"]) == {"+2 points a year over the control",
                                                  "worst fall within 5 points of the control's",
                                                  "turnover at most 1.5x the control's"}
    assert "G" not in r["passing"]
    text = b3.format_batch3(r, "5 deals read")
    assert "BATCH 3, run once" in text and "weak evidence" in text and "This batch is now spent" in text


def test_the_command_needs_confirm_and_stops_unspent_without_deals(tmp_path, monkeypatch, capsys):
    from trading_agent import cli
    monkeypatch.setattr(cli, "_settings", lambda a: SimpleNamespace(state_dir=tmp_path, paper_starting_cash=100_000.0))
    args = SimpleNamespace(confirm=False, json=False)
    assert cli.cmd_research_batch3(args) == 2 and "Run again with --confirm" in capsys.readouterr().out

    def fail(*a, **k):
        raise b3.DealHistoryError("bulk deals 2020-01-01 to 2020-03-30: refused")
    monkeypatch.setattr(b3, "fetch_deal_history", fail)
    args.confirm = True
    assert cli.cmd_research_batch3(args) == 1 and "nothing is spent" in capsys.readouterr().out
    assert not (tmp_path / "research" / "once" / "Batch3.claimed").exists()

    m = make()
    monkeypatch.setattr(b3, "fetch_deal_history", lambda *a, **k: DEALS)
    seen = {}
    monkeypatch.setattr(cli, "_research_market", lambda a, s: seen.update(until=a.until, years=a.years) or m)
    assert cli.cmd_research_batch3(args) == 0
    out = capsys.readouterr().out
    assert "Claimed Batch3" in out and "BATCH 3, run once" in out and seen == {"until": "2025-10-01", "years": 5}
    rows = [json.loads(x) for x in (tmp_path / "research" / "trials.jsonl").read_text().splitlines()]
    assert [r["params"]["variant"] for r in rows] == ["control", "F", "G"]
    assert cli.cmd_research_batch3(args) == 2 and "used once only" in capsys.readouterr().out
    assert "research-batch3" in cli.HALT_ALLOWED and "research-circuits" in cli.HALT_ALLOWED


# --------------------------------------------------------------------------- #
# circuit-lock audit
# --------------------------------------------------------------------------- #
def ohlc_market(rows, n_before=70):
    """One stock: n_before ordinary sessions, then the given (open, high, low, close, volume) rows."""
    dates = days(n_before + len(rows), date(2021, 1, 4))
    seq = [(100.0, 101.0, 99.0, 100.0, 1e6)] * n_before + rows
    series = [{"date": d, "open": o, "high": h, "low": lo, "close": c, "adj_close": c, "volume": v}
              for d, (o, h, lo, c, v) in zip(dates, seq)]
    return Market(dates, {"A": series}, bars(dates, [100.0] * len(dates)), lambda d: {"A"}, min_turnover=0), dates


def test_locks_are_read_from_the_daily_bar():
    m, d = ohlc_market([(95, 95, 95, 95, 1e4), (90, 96, 89, 92, 1e6), (99, 99, 99, 99, 1e3),
                        (94, 95, 93, 94, 1e5), (95, 96, 94, 95, 1e6)])
    assert rc.lock(m, "A", d[70]) == "lower"                             # O = H = L = C below yesterday
    assert rc.lock(m, "A", d[71]) is None
    assert rc.lock(m, "A", d[72]) == "upper"
    assert rc.lock(m, "A", d[73]) == "near-lower"                        # -5% on a tenth of the usual volume
    assert rc.lock(m, "A", d[74]) is None
    assert rc.first_free_close(m, "A", d[70]) == (d[71], 92.0)
    plain = make()
    assert rc.lock(plain, "S00", plain.dates[100]) is None                # bars without open/high/low: unknown


def test_the_audit_runs_the_adopted_rules_and_states_its_limits():
    m = make()
    r = rc.circuit_audit(m, cost_model=cost_model_for("in"), capital=100_000)
    assert r["sells"] + r["buys"] > 0 and r["locked_sells"] == [] and r["unknown"] == r["sells"] + r["buys"]
    assert r["points"] == 0.0 and not r["fix_forward"]
    text = rc.format_circuit_audit(r)
    assert "not a trial" in text and "could not be checked" in text and "Within the 15-point threshold" in text


def test_a_sell_on_a_locked_day_is_repriced_at_the_first_free_close(monkeypatch):
    m, d = ohlc_market([(95, 95, 95, 95, 1e4), (90, 96, 89, 80, 1e6)])
    t = 70
    fake = {"trades": [{"symbol": "A", "exit": d[t]}], "fills": [(t - 5, "A", 10_000.0), (t, "A", 9_500.0)],
            "curve": [100_000.0, 99_000.0]}
    monkeypatch.setattr(rc, "simulate", lambda *a, **k: fake)
    import trading_agent.research_studies as rs
    monkeypatch.setattr(rs, "_first_ranked_day", lambda mk: 0)
    r = rc.circuit_audit(m, cost_model=None, capital=100_000)
    (x,) = r["locked_sells"]
    assert r["sells"] == 1 and r["buys"] == 1 and x["filled"] == d[71] and x["hard"]
    assert r["hard_points"] == pytest.approx(r["points"]) and r["never_reopened"] == 0
    assert x["cost"] == pytest.approx(9_500.0 * (80 / 95 - 1)) and r["points"] == pytest.approx(x["cost"] / 100_000)


def test_a_lock_that_never_reopens_within_20_sessions_is_counted_not_priced(monkeypatch):
    m, d = ohlc_market([(95, 95, 95, 95, 1e4)] + [(95, 95, 95, 95, 1e3)] * 25 + [(90, 96, 89, 92, 1e6)])
    assert rc.first_free_close(m, "A", d[70]) is None                    # the free close comes after 26 sessions
    fake = {"trades": [{"symbol": "A", "exit": d[70]}], "fills": [(70, "A", 9_500.0)], "curve": [1.0, 1.0]}
    monkeypatch.setattr(rc, "simulate", lambda *a, **k: fake)
    import trading_agent.research_studies as rs
    monkeypatch.setattr(rs, "_first_ranked_day", lambda mk: 0)
    r = rc.circuit_audit(m, cost_model=None, capital=100_000)
    assert r["never_reopened"] == 1 and r["cost"] == 0.0
    text = rc.format_circuit_audit(r)
    assert "no freely traded close within 20 sessions" in text and "lower bound" in text
