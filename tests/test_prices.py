import pytest

from trading_agent.groww import GrowwBroker
from trading_agent.prices import YahooPrices, chain
from .conftest import FakeSession


def _yahoo(price):
    return {"chart": {"result": [{"meta": {"regularMarketPrice": price, "currency": "INR"}}]}}


def test_symbol_mapping():
    y = YahooPrices(".NS")
    assert y.yahoo_symbol("reliance") == "RELIANCE.NS"
    assert y.yahoo_symbol("NSE_SENCO") == "SENCO.NS"
    assert y.yahoo_symbol("BTC/USD") == "BTC-USD"
    assert YahooPrices("").yahoo_symbol("NVDA") == "NVDA"


def test_latest_price_and_errors():
    sess = FakeSession({("GET", "/RELIANCE.NS"): _yahoo(1178.0),
                        ("GET", "/NOPE.NS"): {"chart": {"result": None}}})
    y = YahooPrices(".NS", session=sess)
    assert y("reliance") == 1178.0
    with pytest.raises(LookupError):
        y("NOPE")


def test_chain_falls_through():
    def bad(_):
        raise RuntimeError("down")
    assert chain(bad, lambda s: 5)("X") == 5.0
    with pytest.raises(LookupError):
        chain(bad, bad)("X")


def test_groww_uses_fallback_when_live_data_is_unavailable():
    groww_sess = FakeSession({
        ("GET", "/holdings/user"): {"status": "SUCCESS", "payload": {"holdings": [
            {"trading_symbol": "RELIANCE", "quantity": 4, "average_price": 1000.0}]}},
        ("GET", "/live-data/ltp"): {"status": "FAILURE",
                                    "error": {"code": "GA005", "message": "plan has no live data"}},
    })
    yahoo = YahooPrices(".NS", session=FakeSession({("GET", "/RELIANCE.NS"): _yahoo(1178.0)}))
    b = GrowwBroker("tok", session=groww_sess, price_fallback=yahoo)
    assert b.latest_price("RELIANCE") == 1178.0
    assert b.positions()[0].current_price == 1178.0
    with pytest.raises(RuntimeError, match="GA005"):
        GrowwBroker("tok", session=groww_sess).latest_price("RELIANCE")
