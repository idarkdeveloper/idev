"""The pre-registered backward check. Synthetic prices, a fake NSE and fake Yahoo only; no network."""
import json
import math
import random
from datetime import date
from types import SimpleNamespace

import pytest

from trading_agent import research_backward as rb
from trading_agent.costs import IndianDeliveryCosts
from trading_agent.research_sim import Market, Rules, capped_picks, simulate, tax_due

from .test_research_sim import bars, days


def test_taxes_as_they_applied_then():
    assert tax_due(10_000, 500_000, "2018-03-31") == pytest.approx(1_500)        # long-term exempt before Apr 2018
    assert tax_due(10_000, 500_000, "2019-03-31") == pytest.approx(1_500 + 40_000)  # 10% above Rs 1 lakh
    assert tax_due(10_000, 500_000, "2025-03-31") == pytest.approx(2_000 + 46_875)  # 12.5% above Rs 1.25 lakh


def test_shares_held_on_31_jan_2018_are_grandfathered():
    dates = days(400, date(2017, 6, 1))
    t_gf = dates.index("2018-01-31")
    px = [100.0 + 100.0 * min(i, t_gf) / t_gf for i in range(len(dates))]     # doubles to 31 Jan 2018, then flat
    m = Market(dates, {"A": bars(dates, px)}, bars(dates, [100.0] * len(dates)), lambda d: {"A"}, min_turnover=0)
    sell_day = next(i for i, d in enumerate(dates) if d >= "2018-07-02")
    hold = lambda mk, t: ["A"] if t < sell_day else []                         # noqa: E731
    run = simulate(m, Rules(top=1, tax=True, liquid_rate=0.0), 0, len(dates) - 1, ranker=hold)
    assert run["trades"] and run["trades"][0]["exit"] >= "2018-07-02"
    # bought at 100 in Jun 2017, worth 200 on 31 Jan 2018 and sold at 200: no taxable long-term gain
    assert run["tax_by_fy"].get("FY2019", 0.0) == pytest.approx(0.0, abs=1.0)
    later = days(400, date(2018, 6, 1))                                        # the same trade bought after: taxed
    m2 = Market(later, {"A": bars(later, [100.0 + i for i in range(400)])}, bars(later, [100.0] * 400),
                lambda d: {"A"}, min_turnover=0)
    run2 = simulate(m2, Rules(top=1, tax=True, liquid_rate=0.0), 0, 399, ranker=lambda mk, t: ["A"] if t < 380 else [])
    assert sum(run2["tax_by_fy"].values()) > 0


def test_the_industry_cap_skips_to_the_next_name():
    ind = {"A": "Banks", "B": "Banks", "C": "Banks", "D": "IT", "E": "IT", "F": "Pharma"}
    ranked = ["A", "B", "C", "D", "E", "F"]
    assert capped_picks(ranked, [], 3, 3, 2, ind) == ["A", "B", "D"]          # C skipped, D takes its slot
    assert capped_picks(ranked, ["A"], 2, 3, 2, ind) == ["B", "D"]
    assert capped_picks(ranked, [], 3, 3, 99, ind) == ["A", "B", "C"]         # no cap reached: the buffer rule's picks
    assert capped_picks(["X", "Y"], [], 2, 2, 1, {}) == ["X", "Y"]            # no industry: never capped


def test_sector_relative_strength_ranks_within_industries():
    scores = {"B1": 3.0, "B2": 2.9, "B3": 2.8, "I1": 1.0, "I2": 0.5, "I3": 0.0, "Z": 0.1}
    ind = {"B1": "Banks", "B2": "Banks", "B3": "Banks", "I1": "IT", "I2": "IT", "I3": "IT"}
    fake = SimpleNamespace(momentum_scores=lambda t: scores)
    ranked = rb.sector_relative_ranker(ind)(fake, 0)
    assert set(ranked) == set(scores)
    assert ranked.index("I1") < ranked.index("B2")       # the best IT name beats a middling bank
    assert ranked[:2] in (["B1", "I1"], ["I1", "B1"])


def test_total_return_rows_fee_and_fetch(tmp_path):
    rows = [{"Date": "03 Apr 2017", "TotalReturnsIndex": "1,000.00"}, {"Date": "31 Mar 2017", "TotalReturnsIndex": "990"},
            {"Date": "bad", "TotalReturnsIndex": "1"}, {"Date": "04 Apr 2017", "TotalReturnsIndex": "-"}]
    assert rb.parse_tri_rows(rows) == [{"date": "2017-03-31", "close": 990.0}, {"date": "2017-04-03", "close": 1000.0}]
    fee = rb.with_fee([{"date": "2017-01-01", "close": 100.0}, {"date": "2018-01-01", "close": 100.0}])
    assert fee[1]["close"] == pytest.approx(100 * 0.998, rel=1e-4) and fee[0]["close"] == 100.0
    posts = []

    class S:
        def get(self, *a, **k):
            return None

        def post(self, url, headers, data, timeout):
            posts.append(json.loads(data)["cinfo"])
            return SimpleNamespace(raise_for_status=lambda: None,
                                   json=lambda: [{"Date": "02 Jan 2017", "TotalReturnsIndex": "10"},
                                                 {"Date": "01 Oct 2020", "TotalReturnsIndex": "99"}])
    got = rb.fetch_tri(tmp_path, start="2016-04-01", end="2020-10-01", session=S())
    assert len(posts) == 5 and "01-Apr-2016" in posts[0] and got == [{"date": "2017-01-02", "close": 10.0}]
    assert rb.fetch_tri(tmp_path, start="2016-04-01", end="2020-10-01", session=None) == got   # cached: no network


class FakeMembership:
    def __init__(self, names):
        self.names = set(names)

    def ever_members(self, start):
        return self.names | {"GONE"}

    def members_on(self, day):
        return self.names | ({"GONE"} if day < "2018-01-01" else set())


def backward_market(seed=9, n_stocks=32):
    rng = random.Random(seed)
    dates = [d for d in days(1200, date(2016, 4, 1)) if d < rb.WINDOW_END]
    series = {}
    for k in range(n_stocks):
        drift, vol, p = rng.uniform(0.0002, 0.0012), rng.uniform(0.008, 0.02), 100.0
        path = []
        for _ in dates:
            p *= math.exp(drift + vol * rng.gauss(0, 1))
            path.append(p)
        series[f"S{k:02d}"] = bars(dates + ["2020-10-05"], path + [path[-1]])   # one bar past the window
    bench = [{"date": d, "close": 100 * 1.0003 ** i} for i, d in enumerate(dates)]
    prices = SimpleNamespace(history=lambda sym, rng_: series[sym] if sym in series else (_ for _ in ()).throw(KeyError()))
    return rb.load_backward_market(FakeMembership(series), prices, bench), series


def test_the_backward_market_keeps_nothing_after_the_window():
    m, series = backward_market()
    assert m.dates[0] >= rb.LOOKBACK_FROM and m.dates[-1] < rb.WINDOW_END and m.start_day == rb.TRADING_FROM
    assert all(b["date"] < rb.WINDOW_END for s in m.raw.values() for b in s)
    assert m.missing == ["GONE"] and m.point_in_time is True
    assert m.fund_close[365] < 100 * 1.0003 ** 365        # the fund fee is taken off


def test_the_batch_runs_the_five_preregistered_rule_sets():
    m, series = backward_market()
    ind = {s: ("Banks", "IT", "Pharma", "Metals")[i % 4] for i, s in enumerate(sorted(series))}
    r = rb.backward_batch(m, ind, cost_model=IndianDeliveryCosts())
    assert list(r["results"]) == ["A0", "A", "B", "C", "D"] and r["start"] >= rb.TRADING_FROM
    c = r["results"]["A0"]
    assert r["control_passes"] == (c["excess"] > 0 and c["max_drawdown"] >= c["fund_max_drawdown"] - 0.10)
    for k in ("A", "B", "C", "D"):
        x = r["results"][k]
        assert set(x["checks"]) == {"beats the benchmark", "+2 points a year over the control",
                                    "worst fall within 5 points of the control's", "turnover at most 1.5x the control's"}
        assert x["passes"] == all(x["checks"].values())
    assert r["passing_variants"] == [k for k in "ABCD" if r["results"][k]["passes"]]
    text = rb.format_backward(r, ["rebuilt from 36 index changes since 2016-09-30."])
    assert "THE BACKWARD CHECK, run once" in text and "Survivorship: 1 of" in text and "This window is now spent" in text


def test_the_batch_refuses_a_window_that_reaches_the_development_years():
    m, _ = backward_market()
    m.dates[-1] = "2020-10-01"
    with pytest.raises(ValueError, match="the window must run"):
        rb.backward_batch(m, {}, cost_model=IndianDeliveryCosts())


def test_the_command_needs_confirm_and_runs_once(tmp_path, monkeypatch, capsys):
    from trading_agent import cli, index_history, membership, research_sectors, runner, screen
    m, series = backward_market()
    monkeypatch.setattr(cli, "_settings", lambda a: SimpleNamespace(state_dir=tmp_path, paper_starting_cash=500_000.0))
    monkeypatch.setattr(screen, "load_universe", lambda name: [{"symbol": s, "industry": "IT"} for s in series])
    monkeypatch.setattr(index_history, "build_history", lambda *a, **k: {"path": "x", "changes": 36, "problems": []})
    monkeypatch.setattr(membership, "membership_for", lambda *a, **k: SimpleNamespace(known_since="2016-09-30"))
    monkeypatch.setattr(rb, "fetch_tri", lambda cache: [])
    monkeypatch.setattr(rb, "load_backward_market", lambda mb, prices, tri: m)
    monkeypatch.setattr(runner, "free_prices", lambda s: None)
    guarded = {}
    monkeypatch.setattr(cli, "_leakage_guard", lambda mk, holdout_start=None: guarded.update(h=holdout_start))
    monkeypatch.setattr(research_sectors, "industry_map", lambda load: ({s: "IT" for s in series}, []))
    assert cli.cmd_research_backward(SimpleNamespace(confirm=False, tri_csv=None, json=False)) == 2
    assert "--confirm" in capsys.readouterr().out and not (tmp_path / "research").exists()
    assert cli.cmd_research_backward(SimpleNamespace(confirm=True, tri_csv=None, json=False)) == 0
    out = capsys.readouterr().out
    assert "THE BACKWARD CHECK" in out and guarded["h"] == rb.WINDOW_END
    rows = [json.loads(x) for x in (tmp_path / "research" / "trials.jsonl").read_text().splitlines()]
    assert [x["params"]["variant"] for x in rows] == ["A0", "A", "B", "C", "D"] and rows[0]["study"] == rb.STUDY
    assert cli.cmd_research_backward(SimpleNamespace(confirm=True, tri_csv=None, json=False)) == 2
    assert "already run" in capsys.readouterr().out and "research-backward" in cli.HALT_ALLOWED


def test_membership_known_too_late_is_refused(tmp_path, monkeypatch, capsys):
    from trading_agent import cli, index_history, membership, screen
    monkeypatch.setattr(cli, "_settings", lambda a: SimpleNamespace(state_dir=tmp_path, paper_starting_cash=500_000.0))
    monkeypatch.setattr(screen, "load_universe", lambda name: [{"symbol": "A", "industry": "IT"}])
    monkeypatch.setattr(index_history, "build_history", lambda *a, **k: {"path": "x", "changes": 3, "problems": []})
    monkeypatch.setattr(membership, "membership_for", lambda *a, **k: SimpleNamespace(known_since="2019-03-29"))
    assert cli.cmd_research_backward(SimpleNamespace(confirm=True, tri_csv=None, json=False)) == 1
    assert "Not running" in capsys.readouterr().out and not (tmp_path / "research").exists()


def test_preregistered_numbers_are_unchanged():
    assert (rb.TRADING_FROM, rb.WINDOW_END, rb.FUND_FEE, rb.CONTROL_FALL_MARGIN, rb.MIN_CAGR_GAIN,
            rb.VARIANT_FALL_MARGIN, rb.MAX_TURNOVER_RATIO, rb.INDUSTRY_CAP, rb.SECTOR_MIN_NAMES) == (
        "2017-04-01", "2020-10-01", 0.002, 0.10, 0.02, 0.05, 1.5, 5, 3)
    v = {vid: (r.top, r.buffer, r.tax_aware_floor, r.industry_cap) for vid, _, r, _ in rb.variants({})}
    assert v == {"A0": (20, 35, 50, None), "A": (20, 35, 50, None), "B": (10, 18, 25, None),
                 "C": (20, 35, 50, None), "D": (20, 35, 50, 5)}


def test_the_withdrawn_march_2020_review_is_ignored_except_where_nse_kept_it():
    from trading_agent.index_history import changes_for
    changes = [{"date": "2020-03-27", "index": "NIFTYMIDCAP150", "added": ["X"], "removed": ["Y"]},
               {"date": "2020-06-26", "index": "NIFTYMIDCAP150", "added": ["X"], "removed": ["Y"]},
               {"date": "2020-03-27", "index": "NIFTY50", "added": ["P"], "removed": ["Q"]}]
    assert changes_for(changes, "NIFTYMIDCAP150") == [("2020-06-26", ("X",), ("Y",))]
    assert changes_for(changes, "NIFTY50") == [("2020-03-27", ("P",), ("Q",))]
