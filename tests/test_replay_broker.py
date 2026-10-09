from trading_agent.broker import LocalPaperBroker
from trading_agent.costs import cost_model_for
from trading_agent.forward import rebalance_to


def test_orders_carry_the_injected_time_and_credits_add_cash(tmp_path):
    b = LocalPaperBroker(tmp_path / "b.json", starting_cash=10_000, price_fn=lambda s: 100.0,
                         whole_shares=True, now_fn=lambda: "2021-03-01T15:30:00+05:30")
    o = b.submit_order("A", "buy", qty=10)
    assert o["filled_at"] == "2021-03-01T15:30:00+05:30"
    b.credit(55.5, "dividend A", "2021-06-15")
    assert b.account().cash == 10_000 - 1000 + 55.5
    assert b.credits() == [{"at": "2021-06-15", "amount": 55.5, "note": "dividend A"}]
    again = LocalPaperBroker(tmp_path / "b.json", price_fn=lambda s: 100.0)
    assert again.credits()[0]["amount"] == 55.5  # persisted


def test_rebalance_to_equal_weights(tmp_path):
    prices = {"A": 100.0, "B": 250.0}
    b = LocalPaperBroker(tmp_path / "b.json", starting_cash=100_000, price_fn=lambda s: prices[s],
                         whole_shares=True, cost_model=cost_model_for("in"))
    trades = rebalance_to(b, ["A", "B"], top=2, price_fn=lambda s: prices[s], cost_model=cost_model_for("in"))
    assert {t["symbol"] for t in trades} == {"A", "B"} and all("error" not in t for t in trades)
    held = {p.symbol: p.qty * p.current_price for p in b.positions()}
    assert 47_000 < held["A"] <= 50_000 and 47_000 < held["B"] <= 50_000
