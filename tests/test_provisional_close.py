"""Yahoo's empty last close: for hours after an Indian close the chart API posts the day's last row with
``close: null`` while ``meta.regularMarketPrice`` holds the day's last price. The reader serves that price as a
``provisional`` close once the session is over, never archives it, and the pre-market check says so. Fakes only."""
from datetime import datetime

from trading_agent.price_archive import PriceArchive
from trading_agent.prices import YahooPrices
from trading_agent.timezones import IST


def ist(d, h=9, m=15):
    return int(datetime(2026, 10, d, h, m, tzinfo=IST).timestamp())


def chart(close_last=None, market_day=15, high_last=23050.0):
    """Two daily rows (14 and 15 Oct); the 15th has open/high/low and the given close."""
    return {"chart": {"result": [{
        "meta": {"regularMarketPrice": 22997.35, "regularMarketTime": ist(market_day, 15, 31), "gmtoffset": 19800,
                 "regularMarketDayHigh": 23060.0, "regularMarketDayLow": 22900.0,
                 "currentTradingPeriod": {"regular": {"start": ist(16), "end": ist(16, 15, 30)}}},   # the next session
        "timestamp": [ist(14), ist(15)],
        "indicators": {"quote": [{"open": [22800.0, 22950.0], "high": [22900.0, high_last], "low": [22700.0, 22910.0],
                                  "close": [22850.0, close_last], "volume": [100, 200]}],
                       "adjclose": [{"adjclose": [22850.0, close_last]}]}}]}}


class Sess:
    def __init__(self, body):
        self.body = body

    def get(self, url, headers=None, params=None, timeout=None):
        body = self.body
        return type("R", (), {"status_code": 200, "raise_for_status": lambda s: None, "json": lambda s: body})()


def prices(body, now, tmp_path=None, archive=None):
    p = YahooPrices(session=Sess(body), archive=archive)
    p.clock = lambda: datetime(2026, 10, *now, tzinfo=IST).timestamp()
    return p


def test_an_empty_close_after_the_session_is_the_last_price_marked_provisional():
    bars = prices(chart(), (16, 4, 17)).history("^NSEI", "1mo")
    assert bars[-1] == {"date": "2026-10-15", "close": 22997.35, "adj_close": 22997.35, "volume": 200.0,
                        "provisional": True}
    assert bars[0]["date"] == "2026-10-14" and "provisional" not in bars[0]
    ohlc = prices(chart(), (16, 4, 17)).history_ohlc("^NSEI", "1mo")
    assert ohlc[-1]["date"] == "2026-10-15" and ohlc[-1]["provisional"] is True
    assert (ohlc[-1]["open"], ohlc[-1]["high"], ohlc[-1]["low"], ohlc[-1]["close"]) == (22950.0, 23050.0, 22910.0, 22997.35)
    no_high = prices(chart(high_last=None), (16, 4, 17)).history_ohlc("^NSEI", "1mo")[-1]
    assert no_high["high"] == 23060.0                                         # the meta's day high fills a gap


def test_no_provisional_close_mid_session_or_from_another_day():
    assert [b["date"] for b in prices(chart(), (15, 14, 0)).history("^NSEI", "1mo")] == ["2026-10-14"]   # still trading
    assert [b["date"] for b in prices(chart(), (15, 15, 30)).history("^NSEI", "1mo")] == ["2026-10-14", "2026-10-15"]
    stale = chart(market_day=14)                                              # the quote is from the day before
    assert [b["date"] for b in prices(stale, (16, 4, 17)).history("^NSEI", "1mo")] == ["2026-10-14"]
    settled = prices(chart(close_last=23001.1), (16, 4, 17)).history("^NSEI", "1mo")[-1]
    assert settled["close"] == 23001.1 and "provisional" not in settled       # Yahoo's own close always wins


def test_a_provisional_close_is_served_but_never_archived(tmp_path):
    arch = PriceArchive(tmp_path / "archive.sqlite", now_fn=lambda: datetime(2026, 10, 16, 4, 17, tzinfo=IST))
    bars = prices(chart(), (16, 4, 17), archive=arch).history("^NSEI", "1mo")
    assert [b["date"] for b in bars] == ["2026-10-14", "2026-10-15"] and bars[-1]["provisional"] is True
    assert [b["date"] for b in arch.bars("^NSEI")] == ["2026-10-14"]
    later = prices(chart(close_last=23001.1), (16, 9, 0), archive=arch).history("^NSEI", "1mo")
    assert later[-1]["close"] == 23001.1 and "provisional" not in later[-1]   # the settled bar replaces it
    assert arch.bars("^NSEI")[-1]["close"] == 23001.1


def test_the_pre_market_check_passes_and_says_the_close_is_provisional(settings):
    from tests.test_integration import LAST, by_name, good

    class Provisional:
        def history(self, symbol, range_="1mo"):
            last = {"date": LAST, "close": 2.0}
            if symbol == "^NSEI":
                last["provisional"] = True
            return [{"date": "2026-10-08", "close": 1.0}, last]
    settings.market = "in"
    r = good(settings, prices=Provisional())
    step = by_name(r)["Yahoo prices"]
    assert r["ok"] is True and step["ok"] is True
    assert f"^NSEI's {LAST} close is Yahoo's last price (the settled bar is not posted yet)" in step["detail"]
