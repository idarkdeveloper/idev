from trading_agent.momentum import MomentumScreen, momentum_stats, momentum_summary, momentum_verdict


def _bars(prices):
    return [{"date": f"d{i:04d}", "close": p, "adj_close": p, "volume": 1000} for i, p in enumerate(prices)]


def test_uptrend_is_strong():
    bars = _bars([100 + i * 0.5 for i in range(300)])
    s = momentum_stats(bars)
    assert s["ret_6m"] > 0 and s["ret_12_1"] > 0 and s["above_200dma"] is True
    assert s["verdict"] == "strong"
    assert "strong momentum" in momentum_summary(s)
    assert s["avg_turnover_60d"] > 0 and s["pct_from_52w_high"] <= 0


def test_downtrend_is_weak_and_short_history_insufficient():
    bars = _bars([300 - i * 0.5 for i in range(300)])
    assert momentum_stats(bars)["verdict"] == "weak"
    short = momentum_stats(_bars([10, 11, 12]))
    assert short["verdict"] == "insufficient" and short["ret_6m"] is None
    assert momentum_stats([]) == {"error": "no price history"}


def test_verdict_rules_mixed():
    assert momentum_verdict({"ret_6m": 0.1, "ret_12_1": -0.1, "above_200dma": True}) == "neutral"
    assert momentum_verdict({"ret_6m": -0.1, "ret_12_1": -0.2, "above_200dma": True}) == "weak"


def test_screen_wraps_errors():
    class Bad:
        def history(self, s, r):
            raise RuntimeError("offline")
    assert "offline" in MomentumScreen(Bad()).stats("X")["error"]
