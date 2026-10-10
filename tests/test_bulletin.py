"""The evening market bulletin: indicators against hand-computed fixtures, candle rules, wording, global/commodity
sections, the concept library, charts, inline email images and the evening layout. Fakes only, no network."""
import math
import re
import struct
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from trading_agent import bulletin, charts, concepts, digest, digest_render, digest_rules, digest_writer
from trading_agent.broker import LocalPaperBroker
from trading_agent.digest import DigestContext, evening_report
from trading_agent.digest_schedule import build_digest, send_digest
from trading_agent.groww import IST
from trading_agent.notify import Notifier

from .conftest import FakeSession
from .test_digest import News, Prices, bars as close_bars, ctx_for, holding, portfolio, s  # noqa: F401  (s is a fixture)

EVE = datetime(2026, 10, 12, 15, 50, tzinfo=IST)   # a Monday, after the close
TODAY = EVE.date()


# ---------- bar builders ----------
def ohlc(h, lo, c, o=None, d="2026-10-01"):
    return {"open": c if o is None else o, "high": h, "low": lo, "close": c, "date": d, "ts": d + "T09:15:00+05:30", "volume": 1}


def weekdays_back(end: date, n: int) -> list[date]:
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out[::-1]


def daily_bars(n=80, end=TODAY, base=22000.0):
    out, prev = [], base
    for i, d in enumerate(weekdays_back(end, n)):
        c = base + 12 * i + 120 * math.sin(i / 3)
        o = prev + 5
        out.append({"date": d.isoformat(), "ts": f"{d.isoformat()}T09:15:00+05:30", "open": o, "high": max(o, c) + 40,
                    "low": min(o, c) - 40, "close": c, "volume": 1e6})
        prev = c
    return out


def bars_at(day, start_hm, step_min, count, base=22400.0, wave=30.0):
    h, m = start_hm
    t0 = datetime(day.year, day.month, day.day, h, m, tzinfo=IST)
    out = []
    for k in range(count):
        t = t0 + timedelta(minutes=step_min * k)
        c = base + wave * math.sin(k / 2.5) + 2 * k
        o = base + wave * math.sin((k - 1) / 2.5) + 2 * (k - 1)
        out.append({"ts": t.isoformat(timespec="seconds"), "date": day.isoformat(), "open": o, "high": max(o, c) + 8,
                    "low": min(o, c) - 8, "close": c, "volume": 1000})
    return out


def sessions(end=TODAY, days=5, step=15, count=25):
    out = []
    for d in weekdays_back(end, days):
        out += bars_at(d, (9, 15), step, count if step == 15 else 7)
    return out


class Src:
    """A price source with close-only history, daily OHLC and intraday bars, like YahooPrices."""

    def __init__(self, hist=None, daily=None, intra=None):
        self.hist, self.daily, self.intra = hist or {}, daily, intra or {}

    def history(self, symbol, range_="1y"):
        if symbol not in self.hist:
            raise LookupError(symbol)
        return self.hist[symbol]

    def history_ohlc(self, symbol, range_="1y"):
        if self.daily is None:
            raise LookupError("no daily")
        return self.daily

    def history_intraday(self, symbol, interval="15m", range_="5d"):
        if interval not in self.intra:
            raise LookupError(interval)
        return self.intra[interval]


def world_hist(drop=()):
    names = [x[0] for x in digest.WORLD_INDICES] + [x[0] for x in digest.WORLD_FUTURES] + ["^VIX", "^NSEI"] + [c[0] for c in bulletin.COMMODITIES]
    return {n: close_bars(100 + (i % 7) * 10, n=260, step=0.001, end=date(2026, 10, 12)) for i, n in enumerate(names) if n not in drop}


def full_src():
    return Src(world_hist(), daily_bars(), {"15m": sessions(), "1h": sessions(days=22, step=60)})


# ---------- indicators: hand-computed ----------
def test_ema_seeds_with_the_simple_average_and_then_smooths():
    assert bulletin.ema([1, 2, 3, 4, 5], 3) == [None, None, 2.0, 3.0, 4.0]       # k = 0.5
    assert bulletin.ema([1, 2], 3) == [None, None] and bulletin.ema([], 3) == []


def test_rsi_wilder_by_hand():
    r = bulletin.rsi([10, 11, 12, 11, 12, 13], 3)
    assert r[:3] == [None] * 3
    assert r[3] == pytest.approx(66.6667, abs=1e-3)      # avg gain 2/3, avg loss 1/3
    assert r[4] == pytest.approx(77.7778, abs=1e-3)      # (2/3*2+1)/3 = 7/9 ; (1/3*2)/3 = 2/9
    assert r[5] == pytest.approx(85.1852, abs=1e-3)
    assert bulletin.rsi([1, 2, 3, 4, 5], 3)[-1] == 100.0 and bulletin.rsi([5, 5, 5, 5, 5], 3)[-1] == 50.0
    assert bulletin.rsi([1, 2], 14) == [None, None]


def test_wilder_adx_and_di_by_hand():
    bars = [ohlc(10, 8, 9), ohlc(11, 9, 10.5), ohlc(12, 10, 11.5), ohlc(11.5, 9.5, 10), ohlc(11, 9, 9.5)]
    a = bulletin.wilder_adx(bars, 2)
    assert a["plus_di"][:2] == [None, None] and a["adx"][:3] == [None] * 3
    assert (a["plus_di"][2], a["minus_di"][2]) == (50.0, 0.0)                       # DX 100
    assert (a["plus_di"][3], a["minus_di"][3]) == (25.0, 12.5)                      # DX 33.33
    assert (a["plus_di"][4], a["minus_di"][4]) == (12.5, 18.75)                     # DX 20
    assert a["adx"][3] == pytest.approx(66.6667, abs=1e-3)                          # mean of the first two DX
    assert a["adx"][4] == pytest.approx(43.3333, abs=1e-3)                          # (66.67*1 + 20)/2
    short = bulletin.wilder_adx(bars[:2], 2)
    assert short["adx"] == [None, None] and short["plus_di"] == [None, None]


def test_pivot_points():
    p = bulletin.pivot_points(ohlc(22600, 22300, 22500))
    assert p["P"] == pytest.approx(22466.667, abs=1e-3) and p["R1"] == pytest.approx(22633.333, abs=1e-3)
    assert p["S1"] == pytest.approx(22333.333, abs=1e-3) and p["R2"] == pytest.approx(22766.667, abs=1e-3)
    assert p["S2"] == pytest.approx(22166.667, abs=1e-3)


def mk(highs, drop=1.0):
    return [dict(ohlc(h, h - drop, h - drop / 2, d=f"2026-09-{i + 1:02d}"), date=f"2026-09-{i + 1:02d}") for i, h in enumerate(highs)]


def test_swing_points_ties_and_short_history():
    sw = bulletin.swing_points(mk([1, 2, 3, 9, 9, 3, 2, 1, 1, 1]))
    assert [h["index"] for h in sw["highs"]] == [3, 4]                    # equal neighbours both count
    assert bulletin.swing_points(mk([1, 2, 3, 4, 5])) == {"highs": [], "lows": []}   # too short for any +-3 swing
    assert bulletin.nearest_levels(mk([1, 2, 3]), 2.0) == {"resistance": None, "support": None}
    # lookback: only the last 60 bars are searched
    long = mk([5] * 5 + [50] + [5] * 70)
    assert 50 not in [h["price"] for h in bulletin.swing_points(long, lookback=60)["highs"]]        # the 50 is 70 bars back
    assert 50 in [h["price"] for h in bulletin.swing_points(long, lookback=80)["highs"]]


def test_nearest_levels_skip_levels_closer_than_0_3_percent():
    highs = [8, 9, 10, 10.02, 9, 8, 7, 8, 9, 10.5, 9, 8, 7]
    lv = bulletin.nearest_levels(mk(highs), 10.0)
    assert lv["resistance"]["price"] == 10.5 and lv["resistance"]["date"] == "2026-09-10"   # 10.02 is only 0.2% away
    assert lv["support"]["price"] == 6.0 and lv["support"]["date"] == "2026-09-07"
    lv2 = bulletin.nearest_levels(mk(highs), 10.0, min_distance=0.001)
    assert lv2["resistance"]["price"] == 10.02                                               # the rule is the threshold


# ---------- candle patterns ----------
@pytest.mark.parametrize("bar,prev,expect", [
    (dict(open=100, high=110, low=100, close=110), None, ["bullish marubozu"]),
    (dict(open=110, high=110, low=100, close=100), None, ["bearish marubozu"]),
    (dict(open=100, high=105, low=95, close=100.5), None, ["doji"]),
    (dict(open=103, high=105, low=95, close=105), None, ["hammer"]),
    (dict(open=97, high=105, low=95, close=95), None, ["shooting star"]),
    (dict(open=99, high=108, low=98, close=107), dict(open=105, high=106, low=99, close=100), ["bullish engulfing"]),
    (dict(open=106, high=107, low=97, close=98), dict(open=100, high=106, low=99, close=105), ["bearish engulfing"]),
    (dict(open=100, high=108, low=96, close=104), None, []),
    (dict(open=100, high=100, low=100, close=100), None, []),
])
def test_candle_pattern_rules(bar, prev, expect):
    assert bulletin.classify_candle(bar, prev) == expect


def test_marubozu_and_doji_thresholds_are_90_and_10_percent():
    assert bulletin.classify_candle(dict(open=100, high=110, low=100, close=109)) == ["bullish marubozu"]    # exactly 90%
    assert bulletin.classify_candle(dict(open=100, high=110, low=100, close=108.9)) != ["bullish marubozu"]
    assert bulletin.classify_candle(dict(open=100, high=110, low=100, close=101)) == ["doji"]                # exactly 10%
    assert bulletin.classify_candle(dict(open=100, high=110, low=100, close=101.1)) != ["doji"]


# ---------- 4-hour resampling ----------
def h1(day, hh, mm, o, h, lo, c):
    return {"ts": f"{day}T{hh:02d}:{mm:02d}:00+05:30", "open": o, "high": h, "low": lo, "close": c, "volume": 10}


def test_four_hour_candles_split_at_13_15_and_survive_a_holiday_gap():
    mon = [h1("2026-10-05", 9, 15, 100, 103, 99, 102), h1("2026-10-05", 10, 15, 102, 106, 101, 105),
           h1("2026-10-05", 11, 15, 105, 107, 104, 104), h1("2026-10-05", 12, 15, 104, 105, 102, 103),
           h1("2026-10-05", 13, 15, 103, 104, 100, 101), h1("2026-10-05", 14, 15, 101, 102, 98, 99),
           h1("2026-10-05", 15, 15, 99, 101, 97, 100),
           h1("2026-10-05", 16, 15, 1, 999, 0, 5), h1("2026-10-05", 8, 0, 1, 999, 0, 5)]       # outside the session
    wed = [h1("2026-10-07", 9, 15, 100, 101, 99, 100.5), h1("2026-10-07", 10, 15, 100.5, 102, 100, 101)]   # Tuesday is a holiday
    c = bulletin.resample_4h(wed + mon)                                                          # unsorted input
    assert [(x["date"], x["bucket"], x["bars"]) for x in c] == [("2026-10-05", "morning", 4), ("2026-10-05", "afternoon", 3),
                                                                 ("2026-10-07", "morning", 2)]
    m, a, w = c
    assert (m["open"], m["high"], m["low"], m["close"]) == (100, 107, 99, 103)
    assert (a["open"], a["high"], a["low"], a["close"]) == (103, 104, 97, 100)
    assert m["ts"].startswith("2026-10-05T09:15") and a["ts"].startswith("2026-10-05T13:15")
    assert (w["open"], w["close"]) == (100, 101)
    assert bulletin.resample_4h([]) == []


# ---------- wording ----------
def test_adx_text_bands_and_direction():
    assert bulletin.adx_text(35, 15, 30) == "Daily ADX 35: the downtrend is strong (−DI above +DI)."
    assert bulletin.adx_text(15, 20, 18) == "Daily ADX 15: weak or no trend (+DI above −DI)."
    assert bulletin.adx_text(22, 25, 15) == "Daily ADX 22: an uptrend is developing (+DI above −DI)."
    assert bulletin.adx_text(45, 30, 10) == "Daily ADX 45: the uptrend is very strong (+DI above −DI)."
    assert [bulletin.adx_band(x) for x in (19.4, 19.5, 24.4, 24.5, 40.4, 40.5)] == ["weak", "developing", "developing", "strong", "strong", "very strong"]
    assert bulletin.adx_text(None, 1, 1) is None


def test_gap_up_gap_down_and_flat_open_wording():
    up = bulletin.analyse_nifty([ohlc(22300, 22150, 22204, d="2026-10-09"), ohlc(22500, 22270, 22492.65, o=22285, d="2026-10-12")], None, None)
    assert up["lines"]["headline"] == "Nifty opened with a gap up of 81 points and closed +288.65 (+1.30%) at 22,492.65."
    down = bulletin.analyse_nifty([ohlc(22300, 22150, 22200, d="2026-10-09"), ohlc(22210, 22000, 22050, o=22160, d="2026-10-12")], None, None)
    assert down["lines"]["headline"] == "Nifty opened with a gap down of 40 points and closed −150.00 (−0.68%) at 22,050.00."
    flat = bulletin.analyse_nifty([ohlc(22300, 22150, 22200, d="2026-10-09"), ohlc(22260, 22190, 22250, o=22205, d="2026-10-12")], None, None)
    assert flat["lines"]["headline"].startswith("Nifty opened near the previous close and closed +50.00 (+0.23%)")
    with pytest.raises(ValueError):
        bulletin.analyse_nifty([ohlc(1, 1, 1)], None, None)


def test_ema_share_wording():
    bars = [{"close": 11}] * 7 + [{"close": 9}] * 2
    sh = bulletin.ema_share(bars, [10.0] * 9)
    assert sh == {"bars": 9, "above_pct": 78, "below_pct": 22}
    assert bulletin.ema_text(sh) == "It traded above its 21 EMA for most of the session (78% of 15-min bars)."
    assert bulletin.ema_text(bulletin.ema_share(bars[::-1], [10.0] * 9)) == bulletin.ema_text(sh)
    assert bulletin.ema_text({"bars": 9, "above_pct": 22, "below_pct": 78}) == "It traded below its 21 EMA for most of the session (78% of 15-min bars)."
    assert "both sides" in bulletin.ema_text({"bars": 10, "above_pct": 50, "below_pct": 50})
    assert bulletin.ema_share(bars, [None] * 9) is None and bulletin.ema_text(None) is None


def test_rsi_and_level_and_candle_text():
    assert bulletin.rsi_text(55) == "Daily RSI(14) is 55: between 30 and 70."
    assert "above 70" in bulletin.rsi_text(72) and "below 30" in bulletin.rsi_text(25)
    lv = {"resistance": {"price": 22800.0, "date": "2026-10-02"}, "support": {"price": 22200.0, "date": "2026-10-08"}}
    assert bulletin.levels_text(lv, 22480.4) == "Watch levels: resistance 22,800 (swing high 2 Oct), support 22,200 (swing low 8 Oct); pivot 22,480."
    assert "no swing high at least 0.3% above" in bulletin.levels_text({"resistance": None, "support": lv["support"]}, None)
    assert bulletin.candle_text(["bullish marubozu"], ohlc(1, 0, 1)) == "Daily candle: bullish marubozu."
    assert "no named pattern" in bulletin.candle_text([], dict(open=100, high=108, low=96, close=104))


# ---------- the whole Nifty analysis ----------
def test_analyse_nifty_with_intraday_and_four_hour_data():
    daily = daily_bars()
    r = bulletin.analyse_nifty(daily, sessions(), sessions(step=60))
    assert r["session"] == TODAY.isoformat() and r["adx"] is not None and 0 <= r["rsi"] <= 100
    L = r["lines"]
    assert {"headline", "ema", "range", "four_hour", "levels", "pivots", "adx", "rsi", "candle"} <= set(L)
    assert r["intraday"]["bars"] == 25 and r["intraday"]["ema_share"]["bars"] == 25                  # EMA is warm from the earlier sessions
    ci = r["chart_input"]
    assert len(ci["bars15"]) == 25 and len(ci["ema21"]) == 25 and ci["prev_close"] == r["prev_close"]
    assert ci["bars4h"] and len(ci["adx4h"]["adx"]) == len(ci["bars4h"])
    assert {lv["label"] for lv in ci["levels"]} >= {"Pivot"}
    assert [c["bucket"] for c in r["four_hour"]["candles"]] == ["morning", "afternoon"]
    assert L["four_hour"].startswith("4-hour candles: morning ") and ", afternoon " in L["four_hour"]


def test_analyse_nifty_degrades_without_intraday_or_enough_history():
    r = bulletin.analyse_nifty(daily_bars(10), None, [])
    assert "ema" not in r["lines"] and "four_hour" not in r["lines"] and "adx" not in r["lines"] and r["adx"] is None
    assert any("15-minute" in n for n in r["notes"]) and any("ADX" in n for n in r["notes"]) and r["chart_input"].get("bars15") is None
    stale = bulletin.analyse_nifty(daily_bars(), sessions(end=TODAY - timedelta(days=7)), None)       # intraday of another week
    assert "ema" not in stale["lines"] and any("No 15-minute bars" in n for n in stale["notes"])


# ---------- global markets: the "why" is a real headline or nothing ----------
def hl(title, hours_ago=2, sentiment=None, confidence=None, source="ET"):
    pub = (EVE - timedelta(hours=hours_ago)).isoformat()
    return {"id": title, "title": title, "source": source, "published": pub, "sentiment": sentiment, "confidence": confidence}


def test_why_headline_only_from_real_recent_headlines():
    assert bulletin.why_headline("Nikkei", [], EVE) is None
    heads = [hl("Nikkei slips as exporters fall"), hl("Oil rises"), hl("Nikkei old news", hours_ago=30), hl("Dowdy shares drift")]
    assert bulletin.why_headline("Nikkei", heads, EVE)["title"] == "Nikkei slips as exporters fall"
    assert bulletin.why_headline("Dow", heads, EVE) is None                       # "Dowdy" is not the Dow; "Nikkei" is another market
    assert bulletin.why_headline("Hang Seng", heads, EVE) is None
    assert bulletin.why_headline("Unknown index", heads, EVE) is None
    tagged = [hl("Wall Street ends higher", 1), hl("S&P 500 posts a record on chip rally", 5, "positive", "high", "Mint")]
    why = bulletin.why_headline("S&P 500", tagged, EVE)
    assert why == {"title": "S&P 500 posts a record on chip rally", "source": "Mint", "tagged": True}   # tagged beats newer
    long = bulletin.why_headline("Nasdaq", [hl("Nasdaq " + "x" * 300)], EVE)
    assert len(long["title"]) <= 110


class NewsWithMarket(News):
    def __init__(self, heads):
        super().__init__()
        self.heads = heads

    def market_headlines(self, hours=24):
        return self.heads


def test_global_markets_lines_and_reasons(s):
    ctx = ctx_for(s, world_prices=full_src(), now=lambda: EVE, news=NewsWithMarket([hl("Nikkei slips as exporters fall")]))
    g = bulletin._global(ctx)
    rows = {r["market"]: r for r in g["markets"]}
    assert {"S&P 500", "Nasdaq", "Dow", "Nikkei", "KOSPI", "VIX"} <= set(rows) and "Nifty" not in rows
    assert rows["Nikkei"]["why"]["title"] == "Nikkei slips as exporters fall" and rows["S&P 500"]["why"] is None and rows["VIX"]["why"] is None
    assert re.fullmatch(r"Nikkei [\d,]+\.\d\d, [+−][\d.]+% on the day, trend (UP|DOWN|mixed)\.", rows["Nikkei"]["line"]), rows["Nikkei"]["line"]
    none = bulletin._global(ctx_for(s, world_prices=full_src(), now=lambda: EVE, news=News()))      # a news source with no market feed
    assert all(r["why"] is None for r in none["markets"])
    assert "unavailable" in bulletin._global(ctx_for(s, now=lambda: EVE))


# ---------- commodities ----------
def test_commodity_reading_templates():
    assert bulletin.commodity_reading("Gold", 0.83, 2.1, "above", "UP") == "Gold rose 0.83% on the day and is 2.10% higher over 5 sessions; above its 50-day average; trend UP."
    assert bulletin.commodity_reading("Brent crude", -1.5, -3.25, "below", "DOWN") == \
        "Brent crude fell 1.50% on the day and is 3.25% lower over 5 sessions; below its 50-day average; trend DOWN."
    assert bulletin.commodity_reading("Silver", 0.04, None, None, "n/a") == "Silver was flat on the day (+0.04%); trend n/a."
    assert "was flat" in bulletin.commodity_reading("Silver", 0.04, 0.0, "above", "mixed")


def test_commodities_corner_rows_and_missing_prices(s):
    ctx = ctx_for(s, world_prices=Src(world_hist(drop=("SI=F", "NG=F"))), now=lambda: EVE)
    c = bulletin._commodities(ctx)
    assert [r["name"] for r in c["rows"]] == ["Gold", "Crude oil (WTI)", "Brent crude"] and c["skipped"] == 2
    gold = c["rows"][0]
    assert set(gold) >= {"last", "d1_pct", "d5_pct", "trend", "reading", "unit"} and gold["reading"].startswith("Gold ")
    assert "unavailable" in bulletin._commodities(ctx_for(s, world_prices=Src({}), now=lambda: EVE))


# ---------- the concept library ----------
ADVICE = re.compile(r"\b(you should|should (buy|sell)|buy now|sell now|buy this|sell this|recommend\w*|guarantee\w*|will (rise|fall|double)|sure[- ]shot)\b", re.I)


def test_concept_library_is_complete_neutral_and_not_advice():
    assert 28 <= len(concepts.CONCEPTS) <= 40 and len(concepts.WEEKLY) >= 6
    for title, text, uses in concepts.CONCEPTS + concepts.WEEKLY:
        assert title.strip() and len(text) > 60 and uses.strip(), title
        assert not ADVICE.search(text + " " + uses), title
    assert len({t for t, _x, _u in concepts.CONCEPTS}) == len(concepts.CONCEPTS)
    topics = " ".join(t.lower() for t, _x, _u in concepts.CONCEPTS)
    for needle in ("support", "ema", "adx", "rsi", "atr", "stop-loss", "gtt", "200-day", "momentum", "drawdown", "bulk", "promoter", "pcr",
                   "vix", "rupee", "t+1", "stcg", "diversification", "rebalancing", "slippage", "circuit", "pivot", "candlestick", "gap",
                   "delivery", "dividend", "results", "index funds", "risk-off"):
        assert needle in topics, needle
    pcr = next(x for t, x, _u in concepts.CONCEPTS if t.startswith("PCR"))
    assert "does not trade them" in pcr and "lose their whole value" in pcr


def test_concept_rotation_is_deterministic_and_fridays_are_the_weekly_concept():
    mon = date(2026, 10, 12)
    assert concepts.concept_for(mon) == concepts.concept_for(mon) and concepts.concept_for(mon)["kind"] == "day"
    week = [concepts.concept_for(mon + timedelta(days=i)) for i in range(5)]
    assert [w["kind"] for w in week] == ["day"] * 4 + ["week"] and week[4]["label"] == "Concept of the week"
    assert len({w["title"] for w in week[:4]}) == 4                                    # one new concept per trading day
    n0 = concepts.trading_day_number(mon)
    assert concepts.trading_day_number(mon + timedelta(days=1)) == n0 + 1 and concepts.trading_day_number(mon + timedelta(days=7)) == n0 + 4
    assert concepts.CONCEPTS[n0 % len(concepts.CONCEPTS)][0] == week[0]["title"]
    # a full cycle visits every entry
    seen = {concepts.concept_for(mon + timedelta(days=d))["title"] for d in range(0, 7 * len(concepts.CONCEPTS)) if (mon + timedelta(days=d)).weekday() < 4}
    assert seen == {t for t, _x, _u in concepts.CONCEPTS}
    assert concepts.concept_for(mon + timedelta(days=7 * 3 + 4))["title"] != week[4]["title"]       # the weekly one rotates too
    assert len(week[4]["text"]) > 400


# ---------- charts ----------
def png_size(data):
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", data[16:24])


def test_charts_are_pngs_of_the_right_size_and_under_the_limit():
    r = bulletin.analyse_nifty(daily_bars(), sessions(), sessions(step=60))
    ci = r["chart_input"]
    a = charts.nifty_intraday_png(ci["bars15"], ci["ema21"], ci["levels"], ci["prev_close"])
    b = charts.nifty_4h_png(ci["bars4h"], ci["adx4h"])
    for img in (a, b):
        assert png_size(img) == (1280, 720) and len(img) <= 120 * 1024
    assert charts.nifty_intraday_png(ci["bars15"], ci["ema21"], ci["levels"], ci["prev_close"]) == a        # deterministic


def test_charts_do_not_crash_on_missing_or_thin_data():
    assert charts.nifty_intraday_png(None, None, None, None) is None and charts.nifty_intraday_png([], [], [], 1) is None
    assert charts.nifty_4h_png(None, None) is None and charts.nifty_4h_png([], {}) is None
    one = [{"ts": "2026-10-12T09:15:00+05:30", "open": 100, "high": 101, "low": 99, "close": 100.5}]
    assert png_size(charts.nifty_intraday_png(one, [None], [{"label": "Pivot", "value": 100.0}, {"label": "Far", "value": 5000.0}], 100.2)) == (1280, 720)
    assert png_size(charts.nifty_intraday_png(one, None, None, None)) == (1280, 720)
    assert png_size(charts.nifty_4h_png(one, None)) == (1280, 720)
    assert png_size(charts.nifty_4h_png(one, {"adx": [None], "plus_di": [None], "minus_di": [None]})) == (1280, 720)
    assert png_size(charts.nifty_4h_png(one, {"adx": [30.0], "plus_di": [20.0], "minus_di": [10.0]})) == (1280, 720)
    assert charts.bulletin_images({}) == [] and charts.bulletin_images({"nifty": {"unavailable": "x"}}) == []
    assert charts.bulletin_images({"nifty": {"chart_input": {}}}) == []


# ---------- inline images through Resend ----------
def test_notifier_sends_inline_images_as_content_id_attachments():
    sess = FakeSession({("POST", "api.resend.com"): {"id": "e1"}})
    n = Notifier(resend_api_key="re_fake", email_to="me@example.com", session=sess)
    out = n.send("subj", "plain", html='<img src="cid:nifty15">', images=[{"cid": "nifty15", "filename": "n.png", "content": b"\x89PNGdata"}])
    assert "email" in out
    body = sess.calls[0][2]["json"]
    att = body["attachments"]
    assert len(att) == 1 and att[0]["content_id"] == "nifty15" and att[0]["filename"] == "n.png" and att[0]["content_type"] == "image/png"
    import base64
    assert base64.b64decode(att[0]["content"]) == b"\x89PNGdata" and body["html"] == '<img src="cid:nifty15">'
    n.send("subj", "plain", html="<p>x</p>")
    assert "attachments" not in sess.calls[1][2]["json"]


def test_resend_refusing_inline_images_falls_back_to_the_plain_email_once(caplog):
    import requests
    from .conftest import Seq
    sess = FakeSession({("POST", "api.resend.com"): Seq(requests.HTTPError("422", response=SimpleNamespace(status_code=422)), {"id": "e2"})})
    n = Notifier(resend_api_key="re_fake", email_to="me@example.com", session=sess)
    imgs = [{"cid": "nifty15", "filename": "n.png", "content": b"x"}]
    import trading_agent.notify as notify_mod
    notify_mod._WARNED_INLINE = False
    with caplog.at_level("WARNING"):
        out = n.send("subj", "plain", html='<p>a</p><img src="cid:nifty15" alt="c"><p>b</p>', images=imgs)
    assert "email" in out and len(sess.calls) == 2
    second = sess.calls[1][2]["json"]
    assert "attachments" not in second and "cid:" not in second["html"] and second["html"] == "<p>a</p><p>b</p>" and second["text"] == "plain"
    assert sum("inline images" in r.getMessage() for r in caplog.records) == 1
    # a failure with no images is still just a failed email, never an exception
    sess2 = FakeSession({("POST", "api.resend.com"): requests.ConnectionError("down")})
    assert Notifier(resend_api_key="k", email_to="m@e.c", session=sess2).send("s", "b", html="<p>x</p>") == ["console"]


def test_webhook_gets_text_only_even_with_images():
    sess = FakeSession({("POST", "hook.example"): {}})
    n = Notifier(webhook_url="https://hook.example/x", session=sess)
    n.send("subj", "plain text", html="<img src=\"cid:a\">", images=[{"cid": "a", "filename": "a.png", "content": b"x"}])
    assert set(sess.calls[0][2]["json"]) == {"text"}


def test_send_digest_passes_images_and_old_notifiers_still_work():
    class Old:
        channels = ["console", "email"]

        def __init__(self):
            self.got = []

        def send(self, subject, body, html=None):
            self.got.append(html)
            return ["console", "email"]

    class New(Old):
        def send(self, subject, body, html=None, images=None):
            self.got.append((html, images))
            return ["console", "email"]
    mail = {"subject": "s", "text": "t", "html": '<p>x</p><img src="cid:nifty15" alt="a">', "images": [{"cid": "nifty15", "filename": "f", "content": b"1"}]}
    old, new = Old(), New()
    send_digest(old, mail)
    send_digest(new, mail)
    assert old.got == ["<p>x</p>"] and new.got[0][1] == mail["images"]


# ---------- the evening email ----------
TITLES = ["YOUR GROWW PORTFOLIO", "PRACTICE ACCOUNT", "MARKET BULLETIN: NIFTY 50", "GLOBAL MARKETS", "COMMODITIES CORNER",
          "CONCEPT OF THE DAY", "NEWS FOR YOUR STOCKS TODAY", "TODAY'S DEALS BY FOLLOWED INVESTORS"]


def evening_ctx(s, **kw):
    practice = LocalPaperBroker(s.state_dir / "pb.json", starting_cash=100_000, price_fn=lambda x: 100.0)
    base = dict(world_prices=full_src(), now=lambda: EVE, practice=practice, prices=Prices({"AAA": close_bars(110, n=30, end=TODAY)}),
                groww=lambda: portfolio([holding("AAA", 10, 100.0, 110.0)]), news=NewsWithMarket([hl("Nikkei slips as exporters fall")]))
    base.update(kw)
    return ctx_for(s, **base)


def test_evening_email_has_the_bulletin_sections_in_order(s):
    s.digest_writer = "rules"
    mail = build_digest("evening", evening_ctx(s))
    text = mail["text"]
    pos = [text.index(t) for t in TITLES]
    assert pos == sorted(pos), TITLES
    assert [i["cid"] for i in mail["images"]] == ["nifty15", "nifty4h"] and all(i["content"][:4] == b"\x89PNG" for i in mail["images"])
    html = mail["html"]
    assert html.index("Nifty opened") < html.index('src="cid:nifty15"') < html.index("21 EMA") < html.index('src="cid:nifty4h"') < html.index("4-hour candles:")
    assert 'src="cid:nifty4h"' in html and html.count("<img") == 2 and "alt=" in html
    assert "[Chart: Nifty 50 15-minute candles" in text and "[Chart: Nifty 50 4-hour candles" in text
    # chart 1 comes after the headline and before the EMA text; chart 2 after the EMA text and before the 4-hour text
    order = [text.index(x) for x in ("Nifty opened", "[Chart: Nifty 50 15-minute", "21 EMA", "[Chart: Nifty 50 4-hour", "4-hour candles:",
                                      "Watch levels:", "Pivot points", "Daily RSI(14)", "Daily candle:")]
    assert text.index("ADX ", text.index("Pivot points")) < text.index("Daily RSI(14)")
    assert order == sorted(order), order
    assert "Readings of past prices and fixed rules, not a forecast" in text
    assert "Where you see it:" in text and "Nikkei slips as exporters fall (ET)" in text
    assert mail["subject"].startswith("Close")                                         # the subject is unchanged
    for bad in ("open interest", "straddle", "option chain"):   # no options data in the bulletin
        assert bad not in text.lower()


def test_charts_off_sends_the_bulletin_as_text_only(s):
    s.digest_writer = "rules"
    s.digest_charts = False
    mail = build_digest("evening", evening_ctx(s))
    assert mail["images"] == [] and "<img" not in mail["html"] and "[Chart:" not in mail["text"]
    assert "MARKET BULLETIN: NIFTY 50" in mail["text"] and "Watch levels:" in mail["text"]


def test_bulletin_off_leaves_the_evening_email_as_before(s):
    s.digest_writer = "rules"
    s.digest_bulletin = False
    mail = build_digest("evening", evening_ctx(s))
    assert "bulletin" not in mail["data"] and "MARKET BULLETIN" not in mail["text"] and mail["images"] == [] and "GLOBAL MARKETS" not in mail["text"]


def test_each_bulletin_section_degrades_on_its_own(s):
    s.digest_writer = "rules"
    # no 15-minute / 1-hour bars: the Nifty text goes on without charts
    src = Src(world_hist(), daily_bars(), {})
    mail = build_digest("evening", evening_ctx(s, world_prices=src))
    t = mail["text"]
    assert mail["images"] == [] and "Nifty opened" in t and "Watch levels:" in t and "GLOBAL MARKETS" in t and "Note: 15-minute bars are not available." in t
    # no daily OHLC: Nifty says unavailable, the rest stays
    mail = build_digest("evening", evening_ctx(s, world_prices=Src(world_hist())))
    t = mail["text"]
    assert "MARKET BULLETIN: NIFTY 50\nUnavailable:" in t and "COMMODITIES CORNER" in t and "CONCEPT OF THE DAY" in t and mail["images"] == []
    # nothing at all: three sections unavailable, the concept and the portfolio sections remain
    mail = build_digest("evening", evening_ctx(s, world_prices=None))
    t = mail["text"]
    assert t.count("Unavailable:") >= 3 and "CONCEPT OF THE DAY" in t and "YOUR GROWW PORTFOLIO" in t
    # a crashing source costs only its own section
    class Boom(Src):
        def history(self, symbol, range_="1y"):
            raise RuntimeError("boom")
    mail = build_digest("evening", evening_ctx(s, world_prices=Boom(daily=daily_bars(), intra={"15m": sessions(), "1h": sessions(step=60)})))
    assert "Watch levels:" in mail["text"] and "GLOBAL MARKETS\nUnavailable:" in mail["text"]


def test_html_of_the_bulletin_is_escaped_and_the_preview_inlines_the_pictures(s):
    s.digest_writer = "rules"
    heads = [hl('Nikkei <script>alert(1)</script> & "co"')]
    mail = build_digest("evening", evening_ctx(s, news=NewsWithMarket(heads)))
    assert "<script" not in mail["html"] and "&lt;script&gt;" in mail["html"]
    preview = digest_render.inline_data_urls(mail["html"], mail["images"])
    assert "cid:" not in preview and preview.count("data:image/png;base64,") == 2
    assert digest_render.strip_cid_images(mail["html"]).count("<img") == 0


# ---------- the summary sees the bulletin facts ----------
def test_summary_facts_and_validator_accept_bulletin_numbers_and_reject_invented_ones(s):
    data = digest.evening_report(evening_ctx(s))
    facts = digest_writer.summary_facts("evening", data)
    nf = facts["bulletin"]["nifty"]
    assert {"close", "change_pct", "adx", "rsi", "support", "resistance", "pivot", "candle"} <= set(nf) and "chart_input" not in str(facts)
    close, pct = nf["close"], nf["change_pct"]
    good = f"Nifty closed at {close:,.2f}, {'up' if pct > 0 else 'down'} {abs(pct):.2f}%, with an ADX of {nf['adx']:.0f}."
    assert digest_writer.validate_summary(good, facts)[0], digest_writer.validate_summary(good, facts)
    bad = f"Nifty closed at {close + 1234.5:,.2f}."
    ok, why = digest_writer.validate_summary(bad, facts)
    assert not ok and "not in the data" in why
    assert not digest_writer.validate_summary("Nifty will rise to 23,000 tomorrow.", facts)[0]
    assert "bulletin" in digest_writer.build_prompt("evening", data) and "never a forecast" in digest_writer.build_prompt("evening", data)
    rules = digest_rules.rules_summary("evening", data)
    assert f"Nifty closed at {digest.num(close, 2)}" in rules


# ---------- settings ----------
def test_settings_keys_for_the_bulletin_are_validated_and_written(s):
    from trading_agent.ui import App
    app = App(s, broker=LocalPaperBroker(s.state_dir / "pb3.json", starting_cash=1000, price_fn=lambda x: 1.0), dotenv=s.state_dir / ".env3")
    out = app.update_settings({"digest_bulletin": False, "digest_charts": "false"})
    assert out == {"DIGEST_BULLETIN": "false", "DIGEST_CHARTS": "false"}
    assert s.digest_bulletin is False and s.digest_charts is False
    env = (s.state_dir / ".env3").read_text()
    assert "DIGEST_BULLETIN=false" in env and "DIGEST_CHARTS=false" in env
    snap = app.snapshot()["settings"]
    assert snap["digest_bulletin"] is False and snap["digest_charts"] is False
    for bad in (["x"], {"a": 1}):
        with pytest.raises(ValueError):
            app.update_settings({"digest_bulletin": bad})
    assert app.update_settings({"digest_bulletin": True}) == {"DIGEST_BULLETIN": "true"} and s.digest_bulletin is True


def test_load_settings_reads_the_bulletin_switches(monkeypatch):
    from trading_agent.config import load_settings
    st = load_settings(None)
    assert st.digest_bulletin is True and st.digest_charts is True
    monkeypatch.setenv("DIGEST_BULLETIN", "false")
    monkeypatch.setenv("DIGEST_CHARTS", "0")
    st = load_settings(None)
    assert st.digest_bulletin is False and st.digest_charts is False


def test_settings_page_has_the_bulletin_switches():
    from pathlib import Path
    html = (Path(__file__).resolve().parents[1] / "trading_agent" / "ui" / "index.html").read_text(encoding="utf-8")
    for needle in ('id="f-dg-bul"', 'id="f-dg-chart"', "body.digest_bulletin", "body.digest_charts"):
        assert needle in html, needle


def test_preview_in_the_dashboard_shows_the_charts_as_data_urls(s, monkeypatch):
    from trading_agent import ui
    s.digest_writer = "rules"
    app = ui.App(s, broker=LocalPaperBroker(s.state_dir / "pb4.json", starting_cash=1000, price_fn=lambda x: 1.0))
    ctx = evening_ctx(s)
    monkeypatch.setattr("trading_agent.digest.make_context", lambda *a, **k: ctx)
    monkeypatch.setattr(ui.App, "practice_broker", property(lambda self: None))
    out = app.digest_preview("evening")
    assert out["html"].count("data:image/png;base64,") == 2 and "cid:" not in out["html"]


# ---------- the price source ----------
def test_yahoo_prices_intraday_and_ohlc_parse_and_cache(tmp_path):
    from trading_agent.prices import YahooPrices
    t0 = int(datetime(2026, 10, 12, 3, 45, tzinfo=__import__("datetime").timezone.utc).timestamp())   # 09:15 IST
    payload = {"chart": {"result": [{"timestamp": [t0, t0 + 900, t0 + 1800],
                                     "indicators": {"quote": [{"open": [1.0, None, 3.0], "high": [2.0, 2.0, 4.0], "low": [0.5, 0.5, 2.5],
                                                               "close": [1.5, 1.5, 3.5], "volume": [10, 20, None]}]}}]}}
    sess = FakeSession({("GET", "chart/%5ENSEI"): payload, ("GET", "chart/^NSEI"): payload})
    yp = YahooPrices(suffix="", session=sess, cache_dir=tmp_path)
    got = yp.history_intraday("^NSEI", "15m", "5d")
    assert [b["ts"] for b in got] == ["2026-10-12T09:15:00+05:30", "2026-10-12T09:45:00+05:30"] and got[0]["date"] == "2026-10-12"
    assert got[1]["volume"] == 0.0 and got[0]["open"] == 1.0
    params = sess.calls[0][2]["params"]
    assert params == {"range": "5d", "interval": "15m"}
    assert yp.history_intraday("^NSEI", "15m", "5d") == got and len(sess.calls) == 1            # served from the cache
    daily = yp.history_ohlc("^NSEI", "1y")
    assert sess.calls[1][2]["params"] == {"range": "1y", "interval": "1d"} and len(daily) == 1   # both bars fall on one date: the last wins
    with pytest.raises(LookupError):
        YahooPrices(suffix="", session=FakeSession({("GET", "chart"): {"chart": {"result": None}}})).history_intraday("^NSEI")


# ===================== fix round 1 =====================
def _http_error(status):
    import requests
    return requests.HTTPError(f"HTTP {status}", response=SimpleNamespace(status_code=status))


def _imgs():
    return [{"cid": "nifty15", "filename": "n.png", "content": b"x"}]


HTML_WITH_IMG = '<p>a</p><img src="cid:nifty15" alt="c"><p>b</p>'
TEXT_WITH_CHART = "head\n[Chart: Nifty chart]\ntail\n"


@pytest.mark.parametrize("status", [400, 422])
def test_inline_image_refusal_400_422_resends_once_without_pictures_with_a_new_idempotency_key(status):
    from .conftest import Seq
    sess = FakeSession({("POST", "api.resend.com"): Seq(_http_error(status), {"id": "e"})})
    out = Notifier(resend_api_key="re_fake", email_to="me@example.com", session=sess).send("s", TEXT_WITH_CHART, html=HTML_WITH_IMG, images=_imgs())
    assert "email" in out and len(sess.calls) == 2
    import hashlib
    assert "Idempotency-Key" not in sess.calls[0][2]["headers"]          # no logical id given: no key, as before
    sess2 = FakeSession({("POST", "api.resend.com"): Seq(_http_error(status), {"id": "e"})})
    Notifier(resend_api_key="re_fake", email_to="me@example.com", session=sess2).send(
        "s", TEXT_WITH_CHART, html=HTML_WITH_IMG, images=_imgs(), idempotency_key="evening:2026-10-12")
    k1, k2 = (c[2]["headers"]["Idempotency-Key"] for c in sess2.calls)
    assert k1 == hashlib.sha256(b"evening:2026-10-12:img").hexdigest() and k2 == hashlib.sha256(b"evening:2026-10-12:plain").hexdigest() and k1 != k2
    second = sess.calls[1][2]["json"]
    assert "attachments" not in second and "cid:" not in second["html"] and "[Chart:" not in second["text"] and second["text"] == "head\ntail\n"


@pytest.mark.parametrize("exc", ["timeout", "connection", 401, 429, 500, 503])
def test_other_failures_are_never_resent(exc):
    import requests
    err = {"timeout": requests.Timeout("slow"), "connection": requests.ConnectionError("down")}.get(exc) or _http_error(exc)
    sess = FakeSession({("POST", "api.resend.com"): err})
    out = Notifier(resend_api_key="re_fake", email_to="me@example.com", session=sess).send("s", "b", html=HTML_WITH_IMG, images=_imgs())
    assert out == ["console"] and len(sess.calls) == 1                     # logged, not delivered, and no second attempt


def test_the_same_logical_send_has_the_same_key_and_img_and_plain_differ():
    import hashlib
    sess = FakeSession({("POST", "api.resend.com"): {"id": "e"}})
    n = Notifier(resend_api_key="re_fake", email_to="me@example.com", session=sess)
    n.send("s", "b", html=HTML_WITH_IMG, images=_imgs(), idempotency_key="evening:2026-10-12")
    n.send("s", "b", html=HTML_WITH_IMG, images=_imgs(), idempotency_key="evening:2026-10-12")      # the scheduler's retry
    n.send("s", "b", html="<p>x</p>", idempotency_key="evening:2026-10-12")
    n.send("s", "b", html=HTML_WITH_IMG, images=_imgs(), idempotency_key="morning:2026-10-12")
    n.send("s", "alert")                                                                         # an alert email: no key
    keys = [c[2]["headers"].get("Idempotency-Key") for c in sess.calls]
    assert keys[0] == keys[1] == hashlib.sha256(b"evening:2026-10-12:img").hexdigest()
    assert keys[2] == hashlib.sha256(b"evening:2026-10-12:plain").hexdigest() != keys[0]
    assert keys[3] != keys[0] and keys[4] is None


def test_the_scheduler_names_the_send_by_kind_and_day_so_a_retry_reuses_the_key(s):
    got = []

    class Spy:
        channels = ["console", "email"]

        def send(self, subject, body, html=None, images=None, idempotency_key=None):
            got.append(idempotency_key)
            return ["console", "email"]
    mail = {"subject": "s", "text": "t", "html": "<p>x</p>", "images": []}
    send_digest(Spy(), mail, "evening", "2026-10-12")
    send_digest(Spy(), mail, "evening", "2026-10-12")
    send_digest(Spy(), mail)
    assert got == ["evening:2026-10-12", "evening:2026-10-12", None]


def test_send_digest_decides_by_signature_and_does_not_swallow_a_real_type_error():
    class Old:
        def __init__(self):
            self.got = []

        def send(self, subject, body, html=None):
            self.got.append((body, html))
            return ["console", "email"]

    class Bad:
        def send(self, subject, body, html=None, images=None):
            raise TypeError("a bug inside send")
    mail = {"subject": "s", "text": TEXT_WITH_CHART, "html": HTML_WITH_IMG, "images": _imgs()}
    old = Old()
    send_digest(old, mail)
    assert old.got == [("head\ntail\n", "<p>a</p><p>b</p>")]                 # pictures and mentions of them dropped
    with pytest.raises(TypeError):
        send_digest(Bad(), mail)


def test_unfinished_or_stale_session_is_never_called_closed(s):
    s.digest_writer = "rules"
    before = datetime(2026, 10, 12, 11, 0, tzinfo=IST)           # Monday, the market is open; the daily bar of today is partial
    data = digest.evening_report(evening_ctx(s, now=lambda: before))
    assert data["stale_close"]["date"] == "2026-10-09"
    nf = data["bulletin"]["nifty"]
    assert nf["session"] == "2026-10-09" and nf["pivots"]
    mail = build_digest("evening", evening_ctx(s, now=lambda: before))
    assert "Figures are for the session of 2026-10-09" in mail["text"] and "12 Oct session" not in mail["text"]
    sat = datetime(2026, 10, 10, 18, 0, tzinfo=IST)               # a weekend: bars after the last close are dropped too
    assert digest.evening_report(evening_ctx(s, now=lambda: sat))["bulletin"]["nifty"]["session"] <= "2026-10-09"
    assert digest.evening_report(evening_ctx(s))["bulletin"]["nifty"]["session"] == "2026-10-12"   # after the close
    bars = [{"date": "2026-10-09"}, {"date": "2026-10-12"}]
    assert bulletin.completed_bars(bars, before, None) == bars[:1] and bulletin.completed_bars(bars, EVE, None) == bars


def test_yahoo_daily_ohlc_keeps_the_last_row_of_a_duplicated_date(tmp_path):
    from datetime import timezone
    from trading_agent.prices import YahooPrices
    t0 = int(datetime(2026, 10, 12, 3, 45, tzinfo=timezone.utc).timestamp())
    payload = {"chart": {"result": [{"timestamp": [t0 - 86400, t0, t0 + 3600],
                                     "indicators": {"quote": [{"open": [1, 2, 2.5], "high": [2, 3, 3.5], "low": [0.5, 1.5, 2], "close": [1.5, 2.5, 3.0],
                                                               "volume": [1, 2, 3]}]}}]}}
    yp = YahooPrices(suffix="", session=FakeSession({("GET", "chart"): payload}), cache_dir=tmp_path)
    got = yp.history_ohlc("^NSEI")
    assert [b["date"] for b in got] == ["2026-10-11", "2026-10-12"] and got[-1]["close"] == 3.0


def test_bare_sp_does_not_match_sp_bse_sensex_headlines():
    heads = [hl("S&P BSE Sensex ends 300 points higher"), hl("S&P Global PMI slips")]
    assert bulletin.why_headline("S&P 500", heads, EVE) is None
    assert bulletin.why_headline("S&P 500", heads + [hl("Wall Street closes lower")], EVE)["title"] == "Wall Street closes lower"
    assert bulletin.why_headline("S&P 500", [hl("S&P 500 gains")], EVE)["title"] == "S&P 500 gains"


def test_charts_are_phone_sized_with_big_text_and_do_not_touch_global_matplotlib_state():
    import matplotlib
    before = dict(matplotlib.rcParams)
    r = bulletin.analyse_nifty(daily_bars(), sessions(), sessions(days=22, step=60))["chart_input"]
    a = charts.nifty_intraday_png(r["bars15"], r["ema21"], r["levels"], r["prev_close"])
    assert png_size(a) == (1280, 720) and len(a) <= 120 * 1024
    assert charts.TICK_PT >= 11 and charts.TITLE_PT <= 13 and charts.DPI == 200
    assert dict(matplotlib.rcParams) == before
    from pathlib import Path
    src = Path(charts.__file__).read_text(encoding="utf-8")
    assert "import matplotlib.pyplot" not in src and "force=True" not in src and "rcParams" not in src.replace("global rcParams", "")


def test_charts_over_the_size_limit_are_requantized_or_dropped(monkeypatch):
    r = bulletin.analyse_nifty(daily_bars(), sessions(), sessions(days=22, step=60))["chart_input"]
    monkeypatch.setattr(charts, "MAX_BYTES", 9000)
    small = charts.nifty_intraday_png(r["bars15"], r["ema21"], r["levels"], r["prev_close"])
    assert small is None or len(small) <= 9000
    monkeypatch.setattr(charts, "MAX_BYTES", 10)
    assert charts.nifty_intraday_png(r["bars15"], r["ema21"], r["levels"], r["prev_close"]) is None
    assert charts.bulletin_images({"nifty": {"chart_input": r}}) == []


def test_two_charts_can_be_drawn_at_the_same_time():
    import threading
    r = bulletin.analyse_nifty(daily_bars(), sessions(), sessions(days=22, step=60))["chart_input"]
    out, errs = [], []

    def work(kind):
        try:
            out.append(charts.nifty_intraday_png(r["bars15"], r["ema21"], r["levels"], r["prev_close"]) if kind == 0
                       else charts.nifty_4h_png(r["bars4h"], r["adx4h"]))
        except Exception as e:  # noqa: BLE001
            errs.append(e)
    ts = [threading.Thread(target=work, args=(k % 2,)) for k in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs and len(out) == 4 and all(png_size(x) == (1280, 720) for x in out)


def test_concept_rotation_counts_monday_to_thursday_from_a_fixed_epoch_and_ignores_holidays():
    epoch = date(2026, 1, 5)
    assert concepts._EPOCH == epoch and epoch.weekday() == 0
    assert concepts.trading_day_number(epoch) == 0 and concepts.trading_day_number(date(2026, 1, 8)) == 3
    assert concepts.trading_day_number(date(2026, 1, 12)) == 4                                   # Fridays do not count
    mon, tue, wed = date(2026, 10, 12), date(2026, 10, 13), date(2026, 10, 14)
    assert concepts.concept_for(wed) == concepts.concept_for(wed) and concepts.concept_for(wed)["title"] != concepts.concept_for(tue)["title"]
    n = concepts.trading_day_number(wed)
    assert concepts.CONCEPTS[n % len(concepts.CONCEPTS)][0] == concepts.concept_for(wed)["title"]
    fri = date(2026, 10, 16)
    wk = concepts.concept_for(fri)
    assert wk["kind"] == "week" and concepts.WEEKLY[fri.isocalendar()[1] % len(concepts.WEEKLY)][0] == wk["title"]
    assert concepts.concept_for(fri + timedelta(days=7))["title"] != wk["title"]


@pytest.mark.parametrize("size", [28, 29, 30, 31, 35])
def test_every_entry_is_reachable_for_any_library_size(size):
    lib = [(f"T{i}", "x" * 80, "u") for i in range(size)]
    weekly = [(f"W{i}", "y" * 80, "u") for i in range(8)]
    days = [date(2026, 1, 5) + timedelta(days=k) for k in range(0, 7 * size * 2)]
    assert {concepts.concept_for(d, lib, weekly)["title"] for d in days if d.weekday() < 4} == {t for t, _x, _u in lib}
    assert {concepts.concept_for(d, lib, weekly)["title"] for d in days if d.weekday() == 4} == {t for t, _x, _u in weekly}


def test_the_scheduler_sends_no_email_on_an_exchange_holiday(s):
    from trading_agent.digest_schedule import DigestScheduler

    class Cal:
        def is_trading_day(self, d):
            return d != date(2026, 10, 13)
    sc = DigestScheduler(s, lambda: None, SimpleNamespace(channels=["console", "email"], send=lambda *a, **k: ["console", "email"]),
                         holidays=Cal())
    assert sc.due(datetime(2026, 10, 13, 16, 0, tzinfo=IST)) == [] and "evening" in sc.due(datetime(2026, 10, 14, 16, 0, tzinfo=IST))


def test_bar_cut_off_uses_indian_time_even_for_a_utc_clock():
    from datetime import timezone
    bars = [{"date": "2026-10-09"}, {"date": "2026-10-12"}]
    # 23:30 UTC on Sunday 11 Oct is 05:00 IST on Monday 12 Oct: before the close, so Monday's bar is partial
    assert bulletin.completed_bars(bars, datetime(2026, 10, 11, 23, 30, tzinfo=timezone.utc), None) == bars[:1]
    # 10:30 UTC on Monday is 16:00 IST: after the close, Monday's bar is complete
    assert bulletin.completed_bars(bars, datetime(2026, 10, 12, 10, 30, tzinfo=timezone.utc), None) == bars


def test_the_legend_text_is_11_point():
    from pathlib import Path
    assert "fontsize=LABEL_PT - 1" not in Path(charts.__file__).read_text(encoding="utf-8") and charts.LABEL_PT == 11


def test_adx_band_uses_the_rounded_value_and_40_is_strong():
    assert [bulletin.adx_band(x) for x in (19.49, 19.5, 24.49, 24.5, 40.0, 40.49, 40.5)] == \
        ["weak", "developing", "developing", "strong", "strong", "strong", "very strong"]
    assert bulletin.adx_text(40, 20, 10) == "Daily ADX 40: the uptrend is strong (+DI above −DI)."
    assert bulletin.adx_text(40.6, 20, 10).startswith("Daily ADX 41: the uptrend is very strong")
    assert bulletin.adx_text(30, 10, 20, "4-hour ADX").startswith("4-hour ADX 30: the downtrend is strong")


def test_opposite_daily_and_four_hour_directions_get_one_fixed_sentence(monkeypatch):
    def fake(bars, n=14):
        up = len(bars) > 40                                       # the daily list is longer than the 4-hour list
        m = len(bars)
        return {"adx": [30.0] * m, "plus_di": [25.0 if up else 10.0] * m, "minus_di": [10.0 if up else 25.0] * m}
    monkeypatch.setattr(bulletin, "wilder_adx", fake)
    L = bulletin.analyse_nifty(daily_bars(), sessions(), sessions(days=10, step=60))["lines"]
    assert L["adx"].startswith("Daily ADX 30: the uptrend") and L["four_hour_adx"].startswith("4-hour ADX 30: the downtrend")
    assert L["four_hour_adx"].endswith("The 4-hour and daily trends point in different directions.")
    monkeypatch.setattr(bulletin, "wilder_adx", lambda bars, n=14: {"adx": [30.0] * len(bars), "plus_di": [25.0] * len(bars), "minus_di": [10.0] * len(bars)})
    L = bulletin.analyse_nifty(daily_bars(), sessions(), sessions(days=10, step=60))["lines"]
    assert "different directions" not in L["four_hour_adx"]


def test_four_hour_bars_aligned_to_nine_oclock_are_kept_in_the_morning_candle():
    bars = [h1("2026-10-05", 9, 0, 100, 101, 99, 100.5), h1("2026-10-05", 10, 0, 100.5, 102, 100, 101),
            h1("2026-10-05", 11, 0, 101, 103, 100, 102), h1("2026-10-05", 12, 0, 102, 103, 101, 102.5),
            h1("2026-10-05", 13, 15, 102.5, 104, 102, 103), h1("2026-10-05", 14, 15, 103, 105, 102, 104),
            h1("2026-10-05", 8, 0, 1, 500, 0, 5)]                  # 08:00-09:00 ends before the open: dropped
    c = bulletin.resample_4h(bars)
    assert [(x["bucket"], x["bars"]) for x in c] == [("morning", 4), ("afternoon", 2)]
    assert c[0]["open"] == 100 and c[0]["high"] == 103


def test_pivot_line_says_for_the_next_session():
    r = bulletin.analyse_nifty(daily_bars(), None, None)
    assert ", for the next session: P " in r["lines"]["pivots"]


def test_non_boolean_strings_for_switches_are_rejected(s):
    from trading_agent.ui import App
    app = App(s, broker=LocalPaperBroker(s.state_dir / "pb5.json", starting_cash=1000, price_fn=lambda x: 1.0), dotenv=s.state_dir / ".env5")
    for bad in ("maybe", "", "ture", "2", 2, None, 1.5):
        with pytest.raises(ValueError):
            app.update_settings({"digest_charts": bad})
    assert s.digest_charts is True                                     # nothing changed
    for word, expect in (("yes", "true"), ("No", "false"), ("on", "true"), ("OFF", "false"), ("1", "true"), ("0", "false"), (True, "true"), (0, "false")):
        assert app.update_settings({"digest_bulletin": word}) == {"DIGEST_BULLETIN": expect}
    with pytest.raises(ValueError):
        app.update_settings({"auto_trade": "sure"})
