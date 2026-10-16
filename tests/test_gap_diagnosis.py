"""Research A: the split of the screen's gap to its index fund into cash timing, selection and charges.
Fake prices only, no network."""
import json

import pytest

from trading_agent.costs import IndianDeliveryCosts
from trading_agent.factor_backtest import run_factor_backtest
from trading_agent.gap_diagnosis import _paths, _split, diagnose, format_diagnosis, log_trial

from .test_scorecard_factor import Prices


def run(until=None, cost=True, prices=None):
    uni = [{"symbol": s} for s in ("UP", "DOWN", "FLAT")]
    return run_factor_backtest(uni, prices or Prices(900), top=2, years=2, min_turnover=0, workers=2,
                               cost_model=IndianDeliveryCosts() if cost else None, capital=500_000,
                               index_fund="NIFTYBEES", until=until)


def test_the_three_parts_add_up_to_the_gap_and_match_the_backtest():
    r = run()
    d = diagnose(r)
    z = d["cash_at_zero"]
    assert d["reconciles_to_backtest"] is True
    assert z["timing"] + z["selection"] + z["charges"] == pytest.approx(z["gap"])
    assert z["strategy"] == pytest.approx(r["strategy"][-1] / r["strategy"][0] - 1)
    assert z["index_fund"] == pytest.approx(r["index_fund"][-1] / r["index_fund"][0] - 1)
    assert z["charges"] < 0 and z["selection"] > 0          # UP beats the fund; charges always cost
    assert z["timing"] < 0                                   # one slot of two sits in cash while the fund rises
    lq = d["cash_in_liquid_fund"]
    assert lq["timing"] > z["timing"] and lq["timing"] + lq["selection"] + lq["charges"] == pytest.approx(lq["gap"])
    assert 0.49 < d["avg_invested"] < 1 and d["months_fully_invested"] < d["window"]["months"]   # FLAT qualifies some months
    assert d["annual_turnover"] is not None and d["holding_months"]["still_open"] >= 1
    assert "selection added value" in d["verdict"]


def test_without_charges_the_charges_part_is_zero():
    d = diagnose(run(cost=False))
    assert d["cash_at_zero"]["charges"] == pytest.approx(0.0, abs=1e-12)


def test_until_keeps_the_holdout_sealed():
    p = Prices(900)
    cut = p.dates[700]
    r = run(until=cut, prices=p)
    assert r["dates"][-1] < cut and r["until"] == cut
    assert r["dates"][-1] == p.dates[699]                    # the final mark is the last day before the cut
    assert diagnose(r)["window"]["end"] == p.dates[699]


def test_paths_by_hand():
    months = [{"start": "2025-01-01", "end": "2025-02-01", "invested": 0.5, "picks_ret": 0.10, "cost_frac": 0.01},
              {"start": "2025-02-01", "end": "2025-03-01", "invested": 1.0, "picks_ret": -0.05, "cost_frac": 0.0}]
    s = _split(_paths(months, [0.04, -0.02], 0.0))
    fund, timing = 1.04 * 0.98, (1 + 0.5 * 0.04) * 0.98
    gross, net = (1 + 0.05) * 0.95, 0.99 * 1.05 * 0.95
    assert s["timing"] == pytest.approx(timing - fund) and s["selection"] == pytest.approx(gross - timing)
    assert s["charges"] == pytest.approx(net - gross) and s["gap"] == pytest.approx(net - fund)


def test_negative_selection_puts_post_results_drift_first_and_the_text_reads():
    class Bad(Prices):
        def history(self, sym, range_="2y"):
            bars = super().history(sym, range_)
            if sym == "NIFTYBEES":   # a fund far stronger than any pick
                return [{**b, "close": 100 * 1.003 ** i, "adj_close": 100 * 1.003 ** i} for i, b in enumerate(bars)]
            return bars
    d = diagnose(run(prices=Bad(900)))
    assert d["cash_at_zero"]["selection"] < 0 and "post-results drift is researched before" in d["verdict"]
    text = format_diagnosis(d)
    assert "stock selection" in text and "cash timing" in text and "charges" in text and "Verdict:" in text


def test_no_index_fund_is_an_error_and_trials_are_logged(tmp_path):
    r = run()
    r["index_fund"] = None
    with pytest.raises(ValueError):
        diagnose(r)
    path = log_trial(tmp_path, "A-gap", {"top": 20}, {"selection": 0.01})
    log_trial(tmp_path, "A-gap", {"top": 30}, {"selection": -0.01})
    rows = [json.loads(x) for x in path.read_text().splitlines()]
    assert [x["params"]["top"] for x in rows] == [20, 30] and rows[0]["study"] == "A-gap"


def test_charges_never_leave_cash_negative_and_rounding_does_not_break_the_check():
    r = run()
    assert all(m["invested"] <= 1.0 + 1e-9 for m in r["diagnostics"]["months"])   # no borrowing to pay charges
    # a strategy that grew 7x with rounded monthly figures still reconciles; a real 0.1% mismatch does not
    months = [{"start": f"2020-{i + 1:02d}-01", "end": f"2020-{i + 2:02d}-01", "invested": 0.999999,
               "picks_ret": 0.215, "cost_frac": 0.00012345} for i in range(10)]
    fund = [100.0 * 1.01 ** i for i in range(11)]
    exact = _paths(months, [fund[i + 1] / fund[i] - 1 for i in range(10)], 0.0)["net"]
    curve = [round(100_000 * v, 2) for v in exact]
    ok = diagnose({"index_fund": fund, "diagnostics": {"months": months}, "strategy": curve})
    assert curve[-1] / curve[0] > 6 and ok["reconciles_to_backtest"] is True
    curve[-1] *= 1.001
    bad = diagnose({"index_fund": fund, "diagnostics": {"months": months}, "strategy": curve})
    assert bad["reconciles_to_backtest"] is False
