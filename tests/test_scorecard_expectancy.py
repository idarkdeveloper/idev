"""Expectancy in R on Claude's track record: 1R = 2 x ATR(14) at the entry close, read from bars up to the entry only;
a buy's result is its excess after charges, a sell's the excess it avoided. Fakes only, no network."""
import pytest

from trading_agent.scorecard import _expectancy, format_scorecard, risk_unit, score_recommendations

DATES = [f"2026-0{1 + i // 28}-{1 + i % 28:02d}" for i in range(120)]   # 120 sorted fake trading days
START = 40                                                                # calls are made on DATES[START - 1]


def series(step_before, after):
    """Closes moving +/-step_before (alternating) before the entry, so ATR is exactly step_before, then after(k)."""
    out = []
    for i, d in enumerate(DATES):
        c = 100.0 + (step_before if i % 2 else 0.0) if i < START else after(i - START)
        out.append({"date": d, "close": c, "adj_close": c, "volume": 1.0})
    return out


class Prices:
    def __init__(self, stocks):
        self.stocks = stocks

    def history(self, sym, range_="2y"):
        if sym in self.stocks:
            return self.stocks[sym]
        return series(1.0, lambda k: 100.0)          # the index: flat after the entry


def rec(ticker, action="buy"):
    return {"ticker": ticker, "action": action, "at": DATES[START - 1] + "T10:00:00+05:30"}


def test_one_r_is_twice_the_atr_at_entry_and_never_reads_later_bars():
    bars = series(2.0, lambda k: 100.0)
    assert risk_unit(bars, DATES[START]) == pytest.approx(2 * 2.0 / 100.0)
    wild = series(2.0, lambda k: 100.0 + (50.0 if k % 2 else 0.0))       # huge swings only after the entry
    assert risk_unit(wild, DATES[START]) == pytest.approx(risk_unit(bars, DATES[START]))
    assert risk_unit(bars[:10], DATES[9]) is None and risk_unit(bars, None) is None


def test_buys_and_sells_are_measured_in_r_and_match_right_and_wrong():
    winner = series(1.0, lambda k: 100.0 + 0.1 * k)     # entry 100, +2% at 20 days: 1R = 2% -> +1R
    loser = series(1.0, lambda k: 100.0 - 0.05 * k)     # -1% at 20 days -> -0.5R
    faller = series(1.0, lambda k: 100.0 - 0.1 * k)     # a sell that fell 2% -> +1R avoided
    p = Prices({"WIN.NS": winner, "LOSE.NS": loser, "FALL.NS": faller})
    r = score_recommendations([rec("WIN.NS"), rec("LOSE.NS"), rec("FALL.NS", "sell"), rec("WIN.NS", "watch")], p)
    rows = {(x["ticker"], x["action"]): x for x in r["rows"]}
    assert rows[("WIN.NS", "buy")]["r"]["20"] == pytest.approx(1.0)
    assert rows[("LOSE.NS", "buy")]["r"]["20"] == pytest.approx(-0.5)
    assert rows[("FALL.NS", "sell")]["r"]["20"] == pytest.approx(1.0)
    assert rows[("WIN.NS", "watch")]["r"]["20"] is None                     # watch and hold are not scored
    for x in r["rows"]:                                                     # above 0R exactly when counted right
        for h, v in x["r"].items():
            if v is not None:
                assert (v > 0) == x["correct"][h]
    buy20 = r["summary"]["by_action"]["buy"]["20"]
    assert buy20["expectancy_r"] == pytest.approx(0.25) and buy20["n_r"] == 2
    assert buy20["avg_win_r"] == pytest.approx(1.0) and buy20["avg_loss_r"] == pytest.approx(-0.5)
    assert buy20["hit_rate"] == 0.5                                         # 50% right, yet +0.25R a call


def test_charges_come_off_a_buy_in_r():
    class Cost:
        def round_trip_bps(self, notional):
            return 50.0                                                     # 0.5% = 0.25R when 1R is 2%
    p = Prices({"WIN.NS": series(1.0, lambda k: 100.0 + 0.1 * k)})
    row = score_recommendations([rec("WIN.NS")], p, cost_model=Cost())["rows"][0]
    assert row["r"]["20"] == pytest.approx(0.75)


def test_no_r_without_enough_history_before_the_entry():
    short = series(1.0, lambda k: 100.0 + 0.1 * k)[START - 5:]
    row = score_recommendations([rec("NEW.NS")], Prices({"NEW.NS": short}))["rows"][0]
    assert row["risk"] is None and all(v is None for v in row["r"].values())
    assert row["excess"]["20"] is not None                                  # still scored vs the index


def test_expectancy_summary_and_text():
    assert _expectancy([]) == {}
    e = _expectancy([2.0, -1.0, -1.0, 2.0, -1.0])
    assert e == {"n_r": 5, "expectancy_r": pytest.approx(0.2), "avg_win_r": 2.0, "avg_loss_r": -1.0}
    p = Prices({"WIN.NS": series(1.0, lambda k: 100.0 + 0.1 * k)})
    text = format_scorecard(score_recommendations([rec("WIN.NS")], p))
    assert "expectancy +1.00R (win +1.00R)" in text and "1R = 2 x ATR(14) at entry" in text
