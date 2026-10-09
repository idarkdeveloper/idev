from trading_agent.regime import GlobalContext, SYMBOLS, compute_regime


def _bars(prices):
    return [{"date": f"2026-01-{i+1:02d}", "close": p, "adj_close": p, "volume": 0} for i, p in enumerate(prices)]


def _series(nifty, sp, vix, nq=None, nk=None, inr=None, oil=None):
    flat = [100.0] * 30
    return {"nifty50": _bars(nifty), "sp500": _bars(sp), "india_vix": _bars(vix),
            "nasdaq_fut": _bars(nq or flat), "nikkei": _bars(nk or flat),
            "usdinr": _bars(inr or flat), "brent": _bars(oil or flat)}


def test_risk_on():
    nifty = [100 + i * 0.1 for i in range(260)]  # above 200dma
    sp = [100.0] * 25 + [101, 102, 103, 104, 105]
    r = compute_regime(_series(nifty, sp, [12.0] * 30))
    assert r["regime"] == "risk_on" and r["score"] >= 2
    assert r["markets"]["nifty50"]["above_200dma"] is True
    assert "Nifty above 200-day MA" in r["signals"] and "calm" in " ".join(r["signals"])
    assert "risk-on" in r["summary"] and "Guidance" not in r["summary"]


def test_risk_off():
    nifty = [200 - i * 0.3 for i in range(260)]  # below 200dma, falling fast
    sp = [100.0] * 29 + [97.0]  # -3% overnight
    r = compute_regime(_series(nifty, sp, [27.0] * 30))
    assert r["regime"] == "risk_off" and r["score"] <= -2
    assert any("stress" in s for s in r["signals"]) and any("overnight" in s for s in r["signals"])
    assert "No new buys" in r["guidance"]


def test_short_history_is_neutral_not_crash():
    r = compute_regime({k: [] for k in SYMBOLS})
    assert r["regime"] == "neutral" and r["markets"]["nifty50"]["last"] is None


def test_context_caches_and_tolerates_errors():
    calls = []

    class Src:
        def history(self, sym, range_):
            calls.append(sym)
            if sym == "BZ=F":
                raise RuntimeError("down")
            return _bars([100.0] * 30)
    g = GlobalContext(Src(), ttl=1000)
    r1 = g.fetch(); r2 = g.fetch()
    assert r1 is r2 and "brent" in r1["errors"] and len(calls) == len(SYMBOLS)


def test_trend_state():
    up = compute_regime(_series([100 + i * 0.2 for i in range(300)], [100.0] * 30, [12.0] * 30))
    assert up["trend"] == "up" and "trend up" in up["summary"]
    down = compute_regime(_series([300 - i * 0.5 for i in range(300)], [100.0] * 30, [12.0] * 30))
    assert down["trend"] == "down" and any("downtrend" in s for s in down["signals"])
