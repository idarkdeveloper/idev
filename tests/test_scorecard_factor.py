import pytest

from trading_agent.costs import IndianDeliveryCosts
from trading_agent.factor_backtest import _month_starts, _yahoo_range, format_factor_backtest, run_factor_backtest
from trading_agent.scorecard import format_scorecard, score_recommendations
from trading_agent.state import State


def _dates(n, start=(2022, 1, 3)):
    import datetime as dt
    d = dt.date(*start); out = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += dt.timedelta(days=1)
    return out


class Prices:
    """UP rises steadily, DOWN falls, FLAT is flat, index rises slowly."""

    def __init__(self, n=900):
        self.dates = _dates(n)

    def history(self, sym, range_="2y"):
        out = []
        for i, d in enumerate(self.dates):
            if sym == "UP":
                px = 100 * (1.0015 ** i)
            elif sym == "DOWN":
                px = 100 * (0.999 ** i)
            elif sym == "^NSEI":
                px = 1000 * (1.0003 ** i)
            elif sym == "NONE":
                raise RuntimeError("no data")
            else:
                px = 100.0 + (i % 5) * 0.2
            out.append({"date": d, "close": px, "adj_close": px, "volume": 1e6})
        return out


def test_scorecard_scores_buys_and_sells():
    p = Prices(400)
    d0 = p.dates[200]
    recs = [
        {"at": d0 + "T12:00:00+00:00", "action": "buy", "ticker": "UP", "confidence": "high", "suggested_notional_usd": 25000},
        {"at": d0 + "T12:00:00+00:00", "action": "sell", "ticker": "DOWN"},
        {"at": d0 + "T12:00:00+00:00", "action": "watch", "ticker": "FLAT"},
        {"at": p.dates[-2] + "T12:00:00+00:00", "action": "buy", "ticker": "UP"},  # too recent
        {"at": d0 + "T12:00:00+00:00", "action": "hold", "ticker": "PORTFOLIO"},  # skipped
        {"at": d0 + "T12:00:00+00:00", "action": "buy", "ticker": "NONE"},  # data error
    ]
    r = score_recommendations(recs, p, cost_model=IndianDeliveryCosts())
    s = r["summary"]
    assert s["recommendations"] == 5 and s["pending"] >= 1
    buy60 = s["by_action"]["buy"]["60"]
    assert buy60["n"] == 1 and buy60["hit_rate"] == 1.0 and buy60["mean_excess"] > 0
    assert s["by_action"]["sell"]["60"]["hit_rate"] == 1.0          # DOWN lagged the index
    assert "hit_rate" not in s["by_action"]["watch"]["60"]           # watch is reported, not scored
    err = [x for x in r["rows"] if x["ticker"] == "NONE"][0]
    assert "no data" in err["error"]
    assert "buy:" in format_scorecard(r) and "right" in format_scorecard(r)


def test_scorecard_empty():
    r = score_recommendations([], Prices(300))
    assert r["summary"]["recommendations"] == 0 and "Nothing to score" in format_scorecard(r)


def test_factor_backtest_beats_index_and_pays_costs():
    uni = [{"symbol": s} for s in ("UP", "DOWN", "FLAT", "NONE")]
    r = run_factor_backtest(uni, Prices(900), top=2, years=2, cost_model=IndianDeliveryCosts(), capital=500_000,
                            min_turnover=0, workers=2)
    assert r["with_history"] == 3 and r["months"] >= 12
    assert len(r["dates"]) == len(r["strategy"]) == len(r["benchmark"]) == len(r["equal_weight"])
    assert r["strategy"][0] == 500_000 and r["costs_paid"] > 0 and r["trades"] > 0
    # the rising stock is always held, the falling one never (trend filter); empty slots stay cash
    assert all("UP" in p["picks"] and "DOWN" not in p["picks"] for p in r["picks"])
    assert r["stats"]["strategy"]["total_return"] > r["stats"]["benchmark"]["total_return"]
    assert 1.0 <= r["avg_names_held"] <= 2.0
    text = format_factor_backtest(r)
    assert "Strategy" in text and "Caveat" in text


def test_factor_backtest_helpers_and_short_history():
    assert _yahoo_range(1) == "2y" and _yahoo_range(4) == "5y" and _yahoo_range(9) == "10y" and _yahoo_range(20) == "max"
    assert _month_starts(["2026-01-02", "2026-01-05", "2026-02-02"]) == ["2026-01-02", "2026-02-02"]
    with pytest.raises(ValueError):
        run_factor_backtest([{"symbol": "UP"}], Prices(280), top=1, years=1)


def test_equity_history_throttles(tmp_path):
    st = State(tmp_path / "s.json")
    assert st.record_equity(100, 50, 1) is True
    assert st.record_equity(101, 50, 1) is False      # within 10 minutes: replaces the last point
    assert len(st.data["equity_history"]) == 1 and st.data["equity_history"][0]["equity"] == 101
    assert st.record_equity(102, 50, 1, min_gap_s=0) is True
    assert len(st.data["equity_history"]) == 2
