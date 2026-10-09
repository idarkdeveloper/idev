import pytest

from trading_agent.broker import LocalPaperBroker
from trading_agent.costs import FlatCosts, IndianDeliveryCosts, cost_model_for
from trading_agent.risk import atr, check_stops, position_size, trailing_stop


def test_indian_delivery_cost_numbers():
    c = IndianDeliveryCosts()
    buy = c.breakdown("buy", 10_000)
    assert buy["brokerage"] == 10 and buy["stt"] == 10 and buy["dp_charge"] == 0
    assert buy["stamp_duty"] == pytest.approx(1.5)
    sell = c.breakdown("sell", 10_000)
    assert sell["dp_charge"] == 20 and sell["stamp_duty"] == 0
    rt = c.round_trip(10_000)
    assert 69 < rt["charges_bps"] < 70          # ~69.4 bps verified by hand
    assert 29 < c.round_trip(100_000)["charges_bps"] < 30
    assert c.brokerage(1000) == 5 and c.brokerage(50_000) == 20   # min and cap
    assert rt["total_bps"] == pytest.approx(rt["charges_bps"] + 2 * c.slippage_bps, abs=0.01)
    assert isinstance(cost_model_for("in"), IndianDeliveryCosts) and isinstance(cost_model_for("us"), FlatCosts)


def test_simulator_deducts_fees_and_tracks_high_water(tmp_path):
    b = LocalPaperBroker(tmp_path / "pb.json", starting_cash=100_000, currency="INR",
                         whole_shares=True, cost_model=IndianDeliveryCosts())
    b.set_price("SENCO", 100)
    o = b.submit_order("SENCO", "buy", notional=10_000)
    assert o["qty"] == 100 and o["fees"] > 20
    assert b.account().cash == pytest.approx(100_000 - 10_000 - o["fees"], abs=0.01)
    b.set_price("SENCO", 130); b.positions()
    b.set_price("SENCO", 120)
    assert b.positions()[0].high_water == 130
    s = b.submit_order("SENCO", "sell", qty=100)
    assert s["fees"] > o["fees"]  # DP charge on the sell side
    assert b.performance()["fees_paid"] == pytest.approx(o["fees"] + s["fees"], abs=0.02)  # per-order rounding


def _bars(closes, rng=2.0):
    return [{"date": f"d{i}", "close": c, "adj_close": c, "high": c + rng, "low": c - rng, "volume": 1}
            for i, c in enumerate(closes)]


def test_atr_and_position_size():
    a = atr(_bars([100.0] * 30, rng=2.0))
    assert a == pytest.approx(4.0)  # high-low each day
    assert atr(_bars([100.0] * 5)) is None
    r = position_size(500_000, 340.0, 12.0)
    assert r["qty"] == 147 and r["notional"] == 147 * 340 and r["stop"] == 304.0  # capped at 10% of equity
    r2 = position_size(500_000, 1000.0, 100.0)  # very volatile: 1% risk / 200 per share = 25 shares
    assert r2["qty"] == 25
    assert position_size(500_000, 100.0, None)["notional"] <= 25_000  # no ATR: half the cap
    assert position_size(0, 100.0, 1.0)["qty"] == 0


def test_trailing_stop_and_check():
    assert trailing_stop(100, 2.0) == 94.0            # 3x ATR tighter than 15%
    assert trailing_stop(100, 10.0) == 85.0           # 15% floor when ATR is wide
    assert trailing_stop(100, None) == 85.0

    class P:
        def __init__(self, s, q, px, hw, avg):
            self.symbol, self.qty, self.current_price, self.high_water, self.avg_entry_price = s, q, px, hw, avg
    hits = check_stops([P("A", 10, 80.0, 100.0, 90.0), P("B", 5, 99.0, 100.0, 95.0)],
                       lambda s: _bars([100.0] * 30, rng=1.0))
    assert [h["symbol"] for h in hits] == ["A"] and hits[0]["stop"] == 94.0
