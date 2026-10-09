from trading_agent.screen import format_screen, load_universe, run_screen, score_universe
from .conftest import FakeSession


class Prices:
    """UP trends up with low vol, FLAT is flat, DOWN trends down, ILLIQ is up but illiquid."""

    def history(self, sym, range_="2y"):
        n = 300
        out = []
        for i in range(n):
            if sym == "UP":
                px = 100 + i * 0.3
            elif sym == "DOWN":
                px = 200 - i * 0.3
            elif sym == "ILLIQ":
                px = 100 + i * 0.4
            elif sym == "BAD":
                raise RuntimeError("no data")
            else:
                px = 100.0 + (i % 7) * 0.5
            vol = 10 if sym == "ILLIQ" else 1e6
            out.append({"date": f"d{i:03d}", "close": px, "adj_close": px, "volume": vol})
        return out


def test_screen_ranks_and_filters():
    uni = [{"symbol": s, "name": s.title(), "industry": "x"} for s in ("UP", "FLAT", "DOWN", "ILLIQ", "BAD")]
    r = run_screen(uni, Prices(), top=5, workers=2)
    assert r["universe_size"] == 5 and r["scored"] == 4 and r["errors"] == 1
    top = [x["symbol"] for x in r["top"]]
    assert top[0] == "UP" and "DOWN" not in top and "ILLIQ" not in top  # trend and liquidity filters
    ranks = {x["symbol"]: x["rank"] for x in r["all"]}
    assert ranks["ILLIQ"] < ranks["DOWN"] and ranks["UP"] < ranks["DOWN"]
    text = format_screen(r)
    assert "UP" in text and "eligible" in text


def test_score_universe_handles_missing():
    assert score_universe({"A": {"error": "x"}}) == []
    rows = score_universe({"A": {"ret_12_1": 0.1, "ret_6m": 0.05, "vol_60d": 0.2, "above_200dma": True,
                                 "avg_turnover_60d": 1e9}}, min_turnover=0)
    assert rows[0]["rank"] == 1 and rows[0]["eligible"] is True


def test_load_universe_parses_csv():
    csv = '﻿Company Name,Industry,Symbol,Series,ISIN Code\r\nABB India Ltd.,Capital Goods,ABB,EQ,X\r\n,,,,\r\n'
    sess = FakeSession({("GET", "ind_nifty50list.csv"): csv})
    u = load_universe("nifty50", session=sess)
    assert u == [{"symbol": "ABB", "name": "ABB India Ltd.", "industry": "Capital Goods"}]
