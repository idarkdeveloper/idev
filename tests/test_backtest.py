from trading_agent.backtest import format_summary, run_backtest
from trading_agent.quiver import DisclosedTrade


class FakePrices:
    """Stock doubles over 100 days; index flat; a ticker with no data raises."""

    def history(self, symbol, range_="2y"):
        if symbol == "NODATA":
            raise LookupError("no history")
        n = 120
        out = []
        for i in range(n):
            px = 100.0 * (1 + i / 100) if symbol != "^NSEI" else 1000.0
            out.append({"date": f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}", "close": px,
                        "adj_close": px, "volume": 1e5})
        return out


def _deal(ticker, side, date, investor="ASHISH KACHOLIA"):
    return DisclosedTrade(source="bulk", investor=investor, ticker=ticker, transaction=side,
                          transaction_date=date, report_date=date, size="1 sh", raw={})


def test_forward_returns_and_summary():
    deals = [_deal("UP", "Purchase", "2026-01-10"), _deal("UP", "Sale", "2026-01-10"),
             _deal("NODATA", "Purchase", "2026-01-10"),
             _deal("UP", "Purchase", "2026-04-25")]  # too late for a 20-day horizon
    r = run_backtest("ASHISH KACHOLIA", deals, FakePrices(), horizons=(5, 20), cost_bps=100)
    ok = [o for o in r.outcomes if not o.error]
    assert len(ok) == 3 and r.outcomes[2].error and "no history" in r.outcomes[2].error
    first = ok[0]
    assert first.entry_date == "2026-01-11"  # first close strictly after the deal date
    assert abs(first.returns[5] - (1.15 / 1.10 - 1)) < 1e-9
    assert first.excess[5] == first.returns[5]  # flat benchmark
    assert ok[2].returns[20] is None  # not enough bars after the late deal
    s = r.summary()
    buys5 = s["by_side"]["Purchase"]["5"]
    assert buys5["n"] == 2 and buys5["hit_rate"] == 1.0
    expected = ((first.returns[5] - 0.01) + (ok[2].returns[5] - 0.01)) / 2
    assert abs(buys5["mean_excess"] - expected) < 1e-9
    assert s["by_side"]["Sale"]["5"]["hit_rate"] == 0.0  # stock rose after the sale
    assert s["by_client_type"]["individual"]["n"] == 1  # only one buy reaches the 20-day horizon
    text = format_summary(s)
    assert "buys: excess return after cost" in text and "sells:" in text
    assert "individual" in text
    assert r.to_dict()["outcomes"][0]["client_type"] == "individual"
