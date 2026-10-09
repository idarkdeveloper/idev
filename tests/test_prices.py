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


class _Resp:
    def __init__(self, payload, status):
        self.payload, self.status_code = payload, status

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"HTTP {self.status_code}")


class _Sess:
    def __init__(self, payload, status):
        self.r = _Resp(payload, status)

    def get(self, url, **kw):
        return self.r


@pytest.mark.parametrize("method", ["history", "dividends"])
def test_yahoo_4xx_and_null_result_are_lookup_errors_but_429_and_5xx_are_not(method):
    import requests
    ok_shape = {"chart": {"result": None, "error": {"code": "Not Found"}}}
    for status in (404, 400):
        with pytest.raises(LookupError, match=f"HTTP {status}"):
            getattr(YahooPrices(".NS", session=_Sess({}, status)), method)("HDFC")
    with pytest.raises(LookupError):
        getattr(YahooPrices(".NS", session=_Sess(ok_shape, 200)), method)("HDFC")
    for status in (429, 503):
        with pytest.raises(requests.HTTPError) as ei:
            getattr(YahooPrices(".NS", session=_Sess({}, status)), method)("HDFC")
        assert not isinstance(ei.value, LookupError)
