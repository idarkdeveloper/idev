"""Candle payload for the lookup chart: indicators against hand-computed references, slicing, warm-up, ranges."""
import pytest

from trading_agent.candles import RANGES, bollinger, build_candles, macd, parse_range, sma
from trading_agent.bulletin import ema, rsi


def approx(a, b):
    assert a == pytest.approx(b, abs=1e-9)


def test_ema_is_alpha_two_over_n_plus_one_seeded_with_the_simple_average():
    out = ema([1, 2, 3, 4, 5], 3)    # seed (1+2+3)/3 = 2, then alpha 0.5: 3, 4
    assert out[:2] == [None, None]
    assert out[2:] == [2.0, 3.0, 4.0]


def test_sma():
    assert sma([1, 2, 3, 4], 2) == [None, 1.5, 2.5, 3.5]


def test_bollinger_uses_population_standard_deviation():
    b = bollinger([1, 2, 3, 4], n=3, width=2.0)
    sd = (2 / 3) ** 0.5    # window [1,2,3]: mean 2, population variance 2/3
    assert b["upper"][:2] == [None, None]
    approx(b["mid"][2], 2.0)
    approx(b["upper"][2], 2 + 2 * sd)
    approx(b["lower"][2], 2 - 2 * sd)
    approx(b["mid"][3], 3.0)


def test_rsi_is_wilder_smoothed():
    out = rsi([1, 2, 1, 2, 3], 2)    # gains [1,0,1,1], losses [0,1,0,0]
    assert out[:2] == [None, None]
    approx(out[2], 50.0)             # avg gain 0.5, avg loss 0.5
    approx(out[3], 75.0)             # 0.75 / 0.25
    approx(out[4], 87.5)             # 0.875 / 0.125


def test_macd_line_signal_and_histogram():
    m = macd([1, 2, 3, 4, 5, 7], fast=2, slow=3, signal=2)
    assert m["macd"][:2] == [None, None]
    for i in (2, 3, 4):
        approx(m["macd"][i], 0.5)
    approx(m["macd"][5], 6.1666666666666667 - 5.5)
    assert m["signal"][:3] == [None, None, None]     # needs 2 MACD values: first signal at index 3
    approx(m["signal"][3], 0.5)
    approx(m["signal"][4], 0.5)
    approx(m["signal"][5], (6.1666666666666667 - 5.5) * 2 / 3 + 0.5 / 3)
    approx(m["hist"][3], 0.0)
    approx(m["hist"][5], m["macd"][5] - m["signal"][5])


def _bars(n, start="2024-01-01"):
    from datetime import date, timedelta
    d, out, px = date.fromisoformat(start), [], 100.0
    while len(out) < n:
        if d.weekday() < 5:
            px += 0.5 if len(out) % 3 else -0.7
            out.append({"date": d.isoformat(), "open": px - 0.2, "high": px + 1, "low": px - 1, "close": px, "volume": 1000 + len(out)})
        d += timedelta(days=1)
    return out


def test_payload_shape_and_rounding():
    bars = _bars(300)
    bars[-1]["close"] = 123.456
    p = build_candles("TCS", "1Y", bars, None)
    assert set(p) >= {"ticker", "range", "bars", "ema20", "ema50", "ma200", "bb_upper", "bb_mid", "bb_lower", "rsi14",
                      "macd", "macd_signal", "macd_hist", "position"}
    assert p["position"] is None and "error" not in p
    last = p["bars"][-1]
    assert set(last) == {"time", "open", "high", "low", "close", "volume"} and last["close"] == 123.46
    assert len(last["time"]) == 10 and last["time"][4] == "-"      # a YYYY-MM-DD date string, not epoch seconds
    assert p["ema20"][-1]["time"] == last["time"] and set(p["ema20"][-1]) == {"time", "value"}


def test_range_slices_bars_and_indicators_are_warm_at_the_left_edge():
    bars = _bars(400)
    p = build_candles("X", "3M", bars)
    assert 55 <= len(p["bars"]) <= 70 and p["bars"][0]["time"] >= "2024-01-01"
    first = p["bars"][0]["time"]
    # computed over the full history, so the first visible bar already has every indicator, even the 200-day average
    for k in ("ema20", "ema50", "ma200", "bb_mid", "rsi14", "macd", "macd_signal", "macd_hist"):
        assert p[k][0]["time"] == first, k
    # and the values equal those of the full-range payload at the same dates
    full = build_candles("X", "5Y", bars)
    assert {(x["time"], x["value"]) for x in p["ma200"]} <= {(x["time"], x["value"]) for x in full["ma200"]}


def test_undefined_points_are_left_out_and_short_history_is_not_an_error():
    p = build_candles("X", "5Y", _bars(30))
    assert len(p["bars"]) == 30 and len(p["ema20"]) == 11 and p["ema50"] == [] and p["ma200"] == []


def test_ranges_and_empty_history():
    assert parse_range(None) == "1Y" and parse_range("5y") == "5Y"
    assert parse_range("10Y") is None and parse_range("") == "1Y"
    assert set(RANGES) == {"1M", "3M", "6M", "1Y", "2Y", "5Y"}
    e = build_candles("X", "1Y", [])
    assert e["error"] and e["bars"] == []


def test_chart_colours_use_real_nocturne_tokens_and_both_theme_signals():
    import re
    from pathlib import Path
    ui = Path(__file__).resolve().parent.parent / "trading_agent" / "ui"
    js = (ui / "common.js").read_text(encoding="utf-8")
    css = (ui / "nocturne.css").read_text(encoding="utf-8")
    block = js[js.index("function stockColors"):js.index("const fmtVol")]
    tokens = set(re.findall(r'"(--color-[a-z0-9-]+)"', block))
    assert tokens >= {"--color-surface", "--color-muted", "--color-rule", "--color-divider", "--color-profit", "--color-loss"}
    for t in tokens:
        assert re.search(re.escape(t) + r"\s*:", css), t + " is not defined in nocturne.css"
    assert "MutationObserver" in js and 'attributeFilter: ["data-theme"]' in js
    assert "(prefers-color-scheme: dark)" in js and "chart.remove()" in js


def test_position_is_passed_through():
    pos = {"cost": 101.0, "stop": 95.5}
    assert build_candles("X", "1M", _bars(40), pos)["position"] == pos
