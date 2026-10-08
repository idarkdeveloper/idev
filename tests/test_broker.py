import pytest

from trading_agent.broker import AlpacaPaperBroker, LocalPaperBroker
from .conftest import FakeSession


def test_local_paper_broker_round_trip(tmp_path):
    b = LocalPaperBroker(tmp_path / "pb.json", starting_cash=1000)
    b.set_price("NVDA", 100)
    order = b.submit_order("NVDA", "buy", notional=500)
    assert order["status"] == "filled" and order["qty"] == 5
    assert b.account().cash == 500
    b.set_price("NVDA", 120)
    assert b.account().equity == 1100
    assert b.positions()[0].unrealized_pl == pytest.approx(100)
    b.submit_order("NVDA", "sell", qty=5)
    assert b.positions() == [] and b.account().cash == 1100
    # persisted
    b2 = LocalPaperBroker(tmp_path / "pb.json")
    assert b2.performance()["pnl"] == 100 and b2.performance()["orders"] == 2


def test_local_broker_guards(tmp_path):
    b = LocalPaperBroker(tmp_path / "pb.json", starting_cash=100)
    b.set_price("AAPL", 50)
    with pytest.raises(ValueError):
        b.submit_order("AAPL", "buy", notional=500)
    with pytest.raises(ValueError):
        b.submit_order("AAPL", "sell", qty=1)
    with pytest.raises(LookupError):
        b.latest_price("ZZZZ")


def test_alpaca_refuses_live_endpoint():
    with pytest.raises(ValueError):
        AlpacaPaperBroker("k", "s", base_url="https://api.alpaca.markets")


def test_alpaca_paper_requests():
    sess = FakeSession({
        ("GET", "/v2/account"): {"cash": "80000", "equity": "80500", "currency": "USD"},
        ("GET", "/v2/positions"): [{"symbol": "NVDA", "qty": "2", "avg_entry_price": "150",
                                    "current_price": "180"}],
        ("GET", "/v2/stocks/NVDA/trades/latest"): {"trade": {"p": 181.5}},
        ("GET", "/v1beta3/crypto/us/latest/trades"): {"trades": {"BTC/USD": {"p": 121000}}},
        ("POST", "/v2/orders"): {"id": "o1", "status": "accepted"},
    })
    b = AlpacaPaperBroker("key", "sec", session=sess)
    assert b.account().equity == 80500
    assert b.positions()[0].unrealized_pl == 60
    assert b.latest_price("NVDA") == 181.5
    assert b.latest_price("BTC/USD") == 121000
    order = b.submit_order("nvda", "buy", notional=1000)
    assert order["id"] == "o1"
    method, url, kw = sess.calls[-1]
    assert kw["json"] == {"symbol": "NVDA", "side": "buy", "type": "market",
                          "time_in_force": "day", "notional": "1000"}
    assert kw["headers"]["APCA-API-KEY-ID"] == "key"
