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


# --- Yahoo's broken BSE daily series (18 Oct 2026): daily bars rebuilt from hourly bars -------------------------------

class _ChartSess:
    """A fake Yahoo chart API: one answer per interval, and every request's params recorded."""

    def __init__(self, by_interval):
        self.by_interval, self.params = by_interval, []

    def get(self, url, params=None, **kw):
        self.params.append(dict(params or {}))
        return _Resp(self.by_interval[params["interval"]], 200)


def _chart(rows):
    """rows: (epoch seconds, open, high, low, close, volume)."""
    ts, o, h, lo, c, v = (list(x) for x in zip(*rows))
    return {"chart": {"result": [{"meta": {"regularMarketPrice": c[-1]}, "timestamp": ts,
                                  "indicators": {"quote": [{"open": o, "high": h, "low": lo, "close": c, "volume": v}],
                                                 "adjclose": [{"adjclose": c}]}}]}}


D15, D16 = 1792036800, 1792123200          # 2026-10-15 and 2026-10-16, 04:00 UTC (09:30 IST)
ONE_DAY = _chart([(D16, 176.45, 178.15, 174.9, 175.6, 965045)])
HOURLY = _chart([(D15, 174.2, 175.0, 173.1, 174.8, 100), (D15 + 3600, 174.8, 176.9, 174.5, 176.45, 200),
                 (D16, 176.45, 178.15, 176.0, 177.0, 300), (D16 + 3600, 177.0, 177.2, 174.9, 175.6, 400)])


def test_daily_from_hourly_keeps_first_open_extremes_last_close_and_summed_volume():
    from trading_agent.prices import daily_from_hourly
    bars = YahooPrices(".BO", session=_ChartSess({"60m": HOURLY}))._ohlc("TATASTEEL.BO", "2y", "60m", 0)
    days = daily_from_hourly(bars)
    assert [(d["date"], d["open"], d["high"], d["low"], d["close"], d["volume"]) for d in days] == [
        ("2026-10-15", 174.2, 176.9, 173.1, 176.45, 300.0), ("2026-10-16", 176.45, 178.15, 174.9, 175.6, 700.0)]
    assert all(d["from_hourly"] for d in days)


def test_a_one_bar_daily_answer_is_rebuilt_from_hourly_bars_for_the_chart_and_the_close_series():
    sess = _ChartSess({"1d": ONE_DAY, "60m": HOURLY})
    y = YahooPrices(".BO", session=sess)
    y.clock = lambda: D16 + 86400 * 2
    ohlc = y.history_ohlc("TATASTEEL", "5y")
    assert [b["date"] for b in ohlc] == ["2026-10-15", "2026-10-16"] and ohlc[0]["from_hourly"]
    closes = y.history("TATASTEEL", "1y")
    assert [(b["date"], b["close"]) for b in closes] == [("2026-10-15", 176.45), ("2026-10-16", 175.6)]
    assert not any(b.get("provisional") for b in closes)
    assert {"range": "2y", "interval": "60m"} in sess.params


def test_todays_hourly_built_bar_is_provisional_so_it_is_never_archived():
    y = YahooPrices(".BO", session=_ChartSess({"1d": ONE_DAY, "60m": HOURLY}))
    y.clock = lambda: D16 + 3 * 3600
    closes = y.history("TATASTEEL", "1y")
    assert closes[-1]["provisional"] and not closes[0].get("provisional")


def test_a_one_bar_answer_is_normal_for_short_ranges_and_a_working_daily_series_is_kept():
    sess = _ChartSess({"1d": ONE_DAY, "60m": HOURLY})
    assert len(YahooPrices(".BO", session=sess).history_ohlc("TATASTEEL", "5d")) == 1
    assert not any(p["interval"] == "60m" for p in sess.params)
    daily = _chart([(D15, 1, 2, 0.5, 1.5, 10), (D16, 1.5, 2, 1, 1.8, 10)])
    sess = _ChartSess({"1d": daily, "60m": HOURLY})
    assert [b["close"] for b in YahooPrices(".NS", session=sess).history_ohlc("TATASTEEL", "5y")] == [1.5, 1.8]
    assert not any(p["interval"] == "60m" for p in sess.params)


def test_the_all_range_of_ohlc_asks_by_dates_because_range_max_serves_monthly_bars(tmp_path):
    daily = _chart([(D15, 1, 2, 0.5, 1.5, 10), (D16, 1.5, 2, 1, 1.8, 10)])
    sess = _ChartSess({"1d": daily})
    YahooPrices(".NS", session=sess, cache_dir=tmp_path).history_ohlc("TATASTEEL", "max")
    assert sess.params[0]["period1"] == 0 and "range" not in sess.params[0]
    assert (tmp_path / "yahoo_TATASTEEL.NS_maxd_1d_ohlc.json").exists()
