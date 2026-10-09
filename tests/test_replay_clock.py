import pytest

from trading_agent.replay.clock import (EARLIEST_START, ClockedPrices, FutureDataError, ReplayClock,
                                        add_months)
from .replay_fakes import FakeSource, market, path


def test_clock_only_moves_forward():
    c = ReplayClock("2021-03-01")
    c.advance_to("2021-03-05")
    assert c.today == "2021-03-05"
    with pytest.raises(ValueError):
        c.advance_to("2021-03-04")
    c.check("2021-03-05")
    with pytest.raises(FutureDataError):
        c.check("2021-03-06")


def test_history_and_price_stop_at_the_clock():
    p = ClockedPrices(market(), ReplayClock("2021-03-03"))
    bars = p.history("A", "10y")
    assert bars[-1]["date"] == "2021-03-03" and all(b["date"] <= "2021-03-03" for b in bars)
    assert p.latest_price("A") == bars[-1]["adj_close"]
    assert len(p.history("A", "1y")) == 252
    with pytest.raises(FutureDataError):
        p.price_on("A", "2021-03-04")
    assert p.price_on("A", "2021-03-01") == p.history("A", "10y")[-3]["adj_close"]


def test_weekend_clock_uses_fridays_close():
    p = ClockedPrices(market(), ReplayClock("2021-03-07"))  # a Sunday
    assert p.history("A")[-1]["date"] == "2021-03-05"


def test_not_listed_yet_and_delisted():
    p = ClockedPrices(market(), ReplayClock("2021-03-03"))
    with pytest.raises(LookupError, match="2022-06-01"):
        p.latest_price("NEWCO")
    p.clock.advance_to("2023-01-02")
    assert p.last_trade_date("GONE") == "2022-03-31"
    assert p.first_trade_date("NEWCO") == "2022-06-01"


def test_field_close_vs_adj_close():
    bars = path(100, 0.0)
    for b in bars:
        if b["date"] < "2021-06-15":
            b["adj_close"] = 90.0
    src = FakeSource({"X": bars}, {"X": [{"date": "2021-06-15", "amount": 10.0}, {"date": "2022-06-15", "amount": 10.0}]})
    clock = ReplayClock("2021-06-01")
    assert ClockedPrices(src, clock).latest_price("X") == 90.0
    assert ClockedPrices(src, clock, field="close").latest_price("X") == 100.0
    clock.advance_to("2021-07-01")
    assert ClockedPrices(src, clock).dividends("X") == [{"date": "2021-06-15", "amount": 10.0}]


def test_calendar_and_source_called_once_per_symbol():
    src = market()
    p = ClockedPrices(src, ReplayClock("2021-03-01"))
    assert p.calendar("^NSEI", "2021-03-01", "2021-03-08") == ["2021-03-02", "2021-03-03", "2021-03-04",
                                                              "2021-03-05", "2021-03-08"]
    p.history("A"); p.history("A", "1y"); p.latest_price("A")
    assert src.calls.count(("history", "A")) == 1


def test_add_months_clamps_to_month_end():
    assert add_months("2021-01-31", 1) == "2021-02-28"
    assert add_months("2021-11-15", 3) == "2022-02-15"
    assert EARLIEST_START == "2021-01-04"


def test_yahoo_dividends_parsed_from_chart_events(tmp_path):
    from trading_agent.prices import YahooPrices
    from .conftest import FakeSession

    payload = {"chart": {"result": [{"events": {"dividends": {
        "1": {"amount": 3.6, "date": 1687405500}, "0": {"amount": 5.1, "date": 1655264700}}}}]}}
    p = YahooPrices(session=FakeSession({("GET", "TATASTEEL.NS"): payload}), cache_dir=tmp_path)
    assert p.dividends("TATASTEEL") == [{"date": "2022-06-15", "amount": 5.1}, {"date": "2023-06-22", "amount": 3.6}]


def test_dividend_source_error_propagates_and_is_not_cached():
    from .replay_fakes import FakeSource

    class Flaky(FakeSource):
        fail = True

        def dividends(self, symbol, range_="10y"):
            if self.fail:
                raise RuntimeError("HTTP 429")
            return super().dividends(symbol, range_)

    src = Flaky({"X": path(100, 0.0)}, {"X": [{"date": "2021-06-15", "amount": 10.0}]})
    p = ClockedPrices(src, ReplayClock("2021-07-01"))
    with pytest.raises(RuntimeError, match="HTTP 429"):
        p.dividends("X")
    src.fail = False
    assert p.dividends("X") == [{"date": "2021-06-15", "amount": 10.0}]
