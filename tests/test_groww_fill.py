"""Groww daily candles as the fallback for a daily bar Yahoo has not posted yet. Fakes only: no Yahoo, no Groww."""
from datetime import date, datetime

import pytest
import requests

from trading_agent import groww_fill as gf
from trading_agent.timezones import IST

TUE_0900 = datetime(2026, 10, 13, 9, 0, tzinfo=IST)
SESSIONS = [date(2026, 10, 7), date(2026, 10, 8), date(2026, 10, 9), date(2026, 10, 12)]   # completed by Tue 09:00
YAHOO = [{"date": "2026-10-08", "close": 100.0, "adj_close": 99.0, "volume": 1.0},
         {"date": "2026-10-09", "close": 101.0, "adj_close": 100.0, "volume": 1.0}]          # Monday's bar missing


class Client:
    def __init__(self, error=None):
        self.calls, self.error = [], error

    def daily_candles(self, symbol, start, end):
        self.calls.append((symbol, start, end))
        if self.error:
            raise self.error
        return [{"date": "2026-10-12", "open": 101.5, "high": 104.0, "low": 100.5, "close": 103.25, "volume": 5000.0}]


def fill(tmp_path, client=None, window=True, t=None, now=TUE_0900):
    clock = t or [0.0]
    c = client if client is not None else Client()
    f = gf.GrowwFill(lambda: c, lambda n: window, lambda n: SESSIONS, cache_dir=tmp_path / "cache",
                     clock=lambda: now, monotonic=lambda: clock[0], sleep=lambda s: None)
    f.client, f.t = c, clock
    return f


def test_symbols_covered():
    assert gf.groww_symbol("TCS.NS") == "TCS" and gf.groww_symbol("^NSEI") == "NIFTY" and gf.groww_symbol("M&M.NS") == "M&M"
    for no in ("543287.BO", "^GSPC", "AAPL", "BTC-USD", ".NS", "USDINR=X"):
        assert gf.groww_symbol(no) is None, no


def test_parse_candles_seconds_and_milliseconds():
    rows = gf.parse_candles({"candles": [[1791776700, 1, 2, 0.5, 1.5, 10], [1791863100000, 2, 3, 1, 2.5, None],
                                         [1791949500, None, 1, 1, 1, 1], "junk", [1]]})
    assert rows == [{"date": "2026-10-12", "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 10.0},
                    {"date": "2026-10-13", "open": 2.0, "high": 3.0, "low": 1.0, "close": 2.5, "volume": 0.0}]
    assert gf.parse_candles(None) == [] and gf.parse_candles({"candles": "x"}) == []


def test_a_missing_session_is_filled_once_and_marked(tmp_path):
    f = fill(tmp_path)
    out = f.fill("RELIANCE.NS", YAHOO)
    assert out[:2] == YAHOO and out[2] == {"date": "2026-10-12", "close": 103.25, "adj_close": 103.25, "volume": 5000.0,
                                           "source": "groww"}
    assert f.client.calls == [("RELIANCE", date(2026, 10, 12), date(2026, 10, 12))]
    assert f.fill("RELIANCE.NS", YAHOO)[-1]["source"] == "groww" and len(f.client.calls) == 1        # from the cache
    again = fill(tmp_path, client=Client(error=AssertionError("not asked")))                         # a new process
    assert again.fill("RELIANCE.NS", YAHOO)[-1]["close"] == 103.25
    ohlc = f.fill("RELIANCE.NS", [{**b, "open": 1, "high": 1, "low": 1, "ts": "x"} for b in YAHOO], ohlc=True)
    assert ohlc[-1] == {"ts": "2026-10-12T15:30:00+05:30", "date": "2026-10-12", "open": 101.5, "high": 104.0, "low": 100.5,
                        "close": 103.25, "volume": 5000.0, "source": "groww"}


def test_nothing_to_do_or_not_allowed(tmp_path):
    full = YAHOO + [{"date": "2026-10-12", "close": 102.0, "adj_close": 102.0, "volume": 1.0}]
    f = fill(tmp_path)
    assert f.fill("RELIANCE.NS", full) == full and f.client.calls == []                             # Yahoo is up to date
    assert f.fill("543287.BO", YAHOO) == YAHOO and f.fill("RELIANCE.NS", []) == []                  # not covered / empty
    old = [{"date": "2026-09-01", "close": 1.0, "adj_close": 1.0, "volume": 1.0}]
    assert f.fill("RELIANCE.NS", old) == old and f.client.calls == []                               # too long a gap
    closed = fill(tmp_path / "w", window=False)
    assert closed.fill("RELIANCE.NS", YAHOO) == YAHOO and closed.client.calls == []                  # outside the window
    none = gf.GrowwFill(lambda: None, lambda n: True, lambda n: SESSIONS, clock=lambda: TUE_0900)
    assert none.fill("RELIANCE.NS", YAHOO) == YAHOO                                                  # no saved token


def test_failures_back_off_and_a_refusal_stops_the_day(tmp_path):
    f = fill(tmp_path, client=Client(error=requests.ConnectionError("down")))
    assert f.fill("TCS.NS", YAHOO) == YAHOO and len(f.client.calls) == 1
    f.fill("TCS.NS", YAHOO)
    assert len(f.client.calls) == 1                                                                 # not again for 30 min
    f.t[0] += gf.RETRY_S
    f.fill("TCS.NS", YAHOO)
    assert len(f.client.calls) == 2
    resp = requests.Response()
    resp.status_code = 403
    g = fill(tmp_path / "r", client=Client(error=requests.HTTPError("plan", response=resp)))
    g.fill("TCS.NS", YAHOO)
    g.t[0] += gf.RETRY_S * 10
    g.fill("INFY.NS", YAHOO)
    assert len(g.client.calls) == 1 and "plan" in g.last_error            # one refusal: no Groww fill again that day
    nxt = fill(tmp_path / "r2", client=g.client, now=datetime(2026, 10, 14, 9, 0, tzinfo=IST))
    nxt._refused_on = g._refused_on
    nxt.fill("INFY.NS", YAHOO)
    assert len(g.client.calls) == 2                                       # the next day it is tried again


def test_yahoo_prices_pass_their_daily_series_through_the_fill(tmp_path):
    from trading_agent.prices import YahooPrices

    class Sess:
        def __init__(self):
            self.n = 0

        def get(self, url, headers=None, params=None, timeout=None):
            self.n += 1
            ts = [1791517500]   # 9 Oct 2026 09:15 IST
            body = {"chart": {"result": [{"timestamp": ts[:1], "indicators": {
                "quote": [{"open": [100.0], "high": [102.0], "low": [99.0], "close": [101.0], "volume": [1]}],
                "adjclose": [{"adjclose": [101.0]}]}}]}}
            return type("R", (), {"status_code": 200, "raise_for_status": lambda s: None, "json": lambda s: body})()
    p = YahooPrices(suffix=".NS", session=Sess(), cache_dir=tmp_path / "cache")
    p.fill = fill(tmp_path)
    hist = p.history("RELIANCE", "1mo")
    assert [b["date"] for b in hist] == ["2026-10-09", "2026-10-12"] and hist[-1]["source"] == "groww"
    ohlc = p.history_ohlc("RELIANCE", "1mo")
    assert ohlc[-1]["date"] == "2026-10-12" and ohlc[-1]["open"] == 101.5
    p.fill = None
    assert [b["date"] for b in p.history("RELIANCE", "1mo")] == ["2026-10-09"]


class Resp:
    def __init__(self, status, body):
        self.status_code, self.content, self.text = status, body.encode(), body

    def json(self):
        import json
        return json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}", response=self)


class Groww:
    """A fake Groww: ``answers`` maps a URL path to (status, body); every request is recorded."""

    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw.get("params")))
        path = url.split("api.groww.in", 1)[1]
        return Resp(*self.answers.get(path, (404, "404 page not found")))


def read_only(inner):
    from trading_agent.groww import GrowwBroker
    from trading_agent.integration import ReadOnlyGroww, ReadOnlySession
    return ReadOnlyGroww(GrowwBroker("tok", live_orders=False, session=ReadOnlySession(inner)))


NEW = '{"status":"SUCCESS","payload":{"candles":[["2026-10-12T09:15:00",10,12,9,11,500,null]],"closing_price":11}}'


def test_groww_daily_candles_is_one_get_to_the_current_endpoint():
    from trading_agent.integration import OrderBlocked
    inner = Groww({"/v1/historical/candles": (200, NEW)})
    g = read_only(inner)
    assert g.daily_candles("nifty", date(2026, 10, 12), date(2026, 10, 12)) == [
        {"date": "2026-10-12", "open": 10.0, "high": 12.0, "low": 9.0, "close": 11.0, "volume": 500.0}]
    assert len(inner.calls) == 1
    method, url, params = inner.calls[0]
    assert method == "GET" and url == "https://api.groww.in/v1/historical/candles"     # one /v1, not /v1/v1
    assert params == {"exchange": "NSE", "segment": "CASH", "groww_symbol": "NSE-NIFTY",
                      "start_time": "2026-10-12 09:15:00", "end_time": "2026-10-12 15:30:00", "candle_interval": "1day"}
    with pytest.raises(OrderBlocked):
        g.submit_order("TCS", "buy", qty=1)


def test_falls_back_to_the_old_endpoint_when_the_new_one_is_not_found():
    old = '{"status":"SUCCESS","payload":{"candles":[[1791776700,10,12,9,11,500]]}}'
    inner = Groww({"/v1/historical/candle/range": (200, old)})       # the new path answers a plain-text 404
    assert [b["close"] for b in read_only(inner).daily_candles("TCS", date(2026, 10, 12), date(2026, 10, 12))] == [11.0]
    assert [c[1] for c in inner.calls] == ["https://api.groww.in/v1/historical/candles",
                                          "https://api.groww.in/v1/historical/candle/range"]
    assert inner.calls[1][2] == {"exchange": "NSE", "segment": "CASH", "trading_symbol": "TCS",
                                 "start_time": "2026-10-12 09:15:00", "end_time": "2026-10-12 15:30:00",
                                 "interval_in_minutes": 1440}


def test_a_refusal_is_not_retried_on_the_old_endpoint_and_stops_the_fill(tmp_path):
    inner = Groww({"/v1/historical/candles": (403, "forbidden")})      # plain text, not JSON
    g = read_only(inner)
    with pytest.raises(requests.HTTPError) as e:
        g.daily_candles("TCS", date(2026, 10, 12), date(2026, 10, 12))
    assert e.value.response.status_code == 403 and len(inner.calls) == 1
    f = fill(tmp_path, client=g)
    assert f.fill("TCS.NS", YAHOO) == YAHOO and f._refused_on == TUE_0900.date()


def test_a_404_on_both_paths_is_an_http_error_not_a_json_error():
    with pytest.raises(requests.HTTPError):
        read_only(Groww({})).daily_candles("TCS", date(2026, 10, 12), date(2026, 10, 12))


def test_parse_candles_with_timestamp_strings():
    rows = gf.parse_candles({"candles": [["2026-10-12T09:15:00", 1, 2, 0.5, 1.5, 10, None],
                                         ["2026-10-13 00:00:00", 2, 3, 1, 2.5, None, None],
                                         ["not a time", 1, 1, 1, 1, 1], ["1791949500", 3, 3, 3, 3, 3]]})
    assert [r["date"] for r in rows] == ["2026-10-12", "2026-10-13", "2026-10-14"]
    assert rows[0] == {"date": "2026-10-12", "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 10.0}


def test_make_fill_only_with_groww_in_india(settings):
    settings.market, settings.broker = "in", "local"
    assert gf.make_fill(settings) is None
    settings.broker = "groww"
    assert isinstance(gf.make_fill(settings), gf.GrowwFill)
    settings.market = "us"
    assert gf.make_fill(settings) is None


def test_the_pre_market_check_says_when_a_bar_came_from_groww(settings):
    from tests.test_integration import LAST, by_name, good

    class Filled:
        def history(self, symbol, range_="1mo"):
            last = {"date": LAST, "close": 2.0}
            if symbol == "^NSEI":
                last["source"] = "groww"
            return [{"date": "2026-10-08", "close": 1.0}, last]
    settings.market = "in"
    r = good(settings, prices=Filled())
    step = by_name(r)["Yahoo prices"]
    assert r["ok"] is True and step["ok"] is True
    assert f"^NSEI's {LAST} bar is from Groww (Yahoo has not posted it yet)" in step["detail"]
