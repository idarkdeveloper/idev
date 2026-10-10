"""The evening market bulletin: Nifty 50 levels and indicators, global markets, commodities, a concept of the day.

Everything is rules over past prices: the indicator functions are pure (lists in, lists out) and the wording comes
from the fixed templates below. Nothing here forecasts: levels are "watch levels" taken from earlier turning points,
and every section is a reading of what already happened. Options data (OI, PCR, straddles) is deliberately not used.

``build_bulletin(ctx, today)`` reads the data through the digest context and never raises: a part that cannot be
read becomes ``{"unavailable": reason}`` and the rest still goes out.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time as dtime, timedelta
from typing import Any

from .concepts import concept_for
from .digest import (DigestContext, _avg, _chg, _section, _world, num, num_intl, pct_text, trend_label, unavailable)
from .notify import clean_text
from .timezones import IST

NOTE = "Readings of past prices and fixed rules, not a forecast. Watch levels are earlier turning points, not targets."

# -- thresholds (also quoted in the README) -----------------------------------------------------
EMA_PERIOD = 21           # on 15-minute closes
ADX_PERIOD = 14           # Wilder, on daily bars (and on 4-hour bars for the chart panel)
RSI_PERIOD = 14           # Wilder, on daily closes
SWING_WINDOW = 3          # a swing is the extreme of +-3 bars
SWING_LOOKBACK = 60       # sessions
LEVEL_MIN_DISTANCE = 0.003  # the watch level must be at least 0.3% from the close
MARUBOZU_BODY = 0.90      # body >= 90% of the range
DOJI_BODY = 0.10          # body <= 10% of the range
SHADOW_BODY_RATIO = 2.0   # hammer / shooting star: the long shadow is at least twice the body
SHADOW_SMALL = 0.10       # ... and the opposite shadow at most 10% of the range
GAP_FLAT_PCT = 0.10       # a gap smaller than 0.1% of the previous close is "near the previous close"
SHARE_MOST = 60.0         # >= 60% of the bars on one side of the EMA is "most of the session"
SESSION_OPEN = dtime(9, 15)
SESSION_SPLIT = dtime(13, 15)
SESSION_CLOSE = dtime(15, 30)


# =============================================================================
# Indicators (pure)
# =============================================================================
def ema(values: list[float], n: int = EMA_PERIOD) -> list[float | None]:
    """Exponential moving average, k = 2/(n+1), seeded with the simple average of the first ``n`` values; None before
    that. Same length as ``values``."""
    out: list[float | None] = [None] * len(values)
    if n < 1 or len(values) < n:
        return out
    k = 2.0 / (n + 1)
    prev = sum(values[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def wilder_adx(bars: list[dict[str, Any]], n: int = ADX_PERIOD) -> dict[str, list[float | None]]:
    """Wilder's ADX with +DI and -DI from high/low/close bars. TR, +DM and -DM are smoothed as: first value = sum of
    the first ``n``, then ``prev - prev/n + current``. DX = 100*|+DI - -DI|/(+DI + -DI); the first ADX is the mean of
    the first ``n`` DX values (so it needs 2n bars), then ``(prev*(n-1) + DX)/n``. Lists are as long as ``bars``."""
    size = len(bars)
    plus: list[float | None] = [None] * size
    minus: list[float | None] = [None] * size
    adx: list[float | None] = [None] * size
    if size < n + 1:
        return {"plus_di": plus, "minus_di": minus, "adx": adx}
    tr, pdm, mdm = [0.0] * size, [0.0] * size, [0.0] * size
    for i in range(1, size):
        h, lo, pc = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
        tr[i] = max(h - lo, abs(h - pc), abs(lo - pc))
        up, down = h - bars[i - 1]["high"], bars[i - 1]["low"] - lo
        pdm[i] = up if (up > down and up > 0) else 0.0
        mdm[i] = down if (down > up and down > 0) else 0.0
    s_tr, s_p, s_m = sum(tr[1:n + 1]), sum(pdm[1:n + 1]), sum(mdm[1:n + 1])
    dx: list[float | None] = [None] * size
    for i in range(n, size):
        if i > n:
            s_tr = s_tr - s_tr / n + tr[i]
            s_p = s_p - s_p / n + pdm[i]
            s_m = s_m - s_m / n + mdm[i]
        if s_tr <= 0:
            plus[i], minus[i], dx[i] = 0.0, 0.0, 0.0
            continue
        plus[i], minus[i] = 100.0 * s_p / s_tr, 100.0 * s_m / s_tr
        total = plus[i] + minus[i]
        dx[i] = 0.0 if total == 0 else 100.0 * abs(plus[i] - minus[i]) / total
    if size >= 2 * n:
        first = 2 * n - 1
        adx[first] = sum(d for d in dx[n:first + 1] if d is not None) / n
        for i in range(first + 1, size):
            adx[i] = (adx[i - 1] * (n - 1) + dx[i]) / n
    return {"plus_di": plus, "minus_di": minus, "adx": adx}


def rsi(closes: list[float], n: int = RSI_PERIOD) -> list[float | None]:
    """Wilder's RSI: the first average gain / loss is the mean of the first ``n`` changes, then
    ``(prev*(n-1) + current)/n``. 100 when there were no losses (50 when nothing moved)."""
    out: list[float | None] = [None] * len(closes)
    if len(closes) < n + 1:
        return out
    gains = [max(closes[i] - closes[i - 1], 0.0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0.0) for i in range(1, len(closes))]
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n

    def value(g: float, lo: float) -> float:
        if lo == 0:
            return 50.0 if g == 0 else 100.0
        return 100.0 - 100.0 / (1.0 + g / lo)
    out[n] = value(ag, al)
    for i in range(n + 1, len(closes)):
        ag = (ag * (n - 1) + gains[i - 1]) / n
        al = (al * (n - 1) + losses[i - 1]) / n
        out[i] = value(ag, al)
    return out


def pivot_points(bar: dict[str, Any]) -> dict[str, float]:
    """Classic pivots from one session: P=(H+L+C)/3, R1=2P-L, S1=2P-H, R2=P+(H-L), S2=P-(H-L)."""
    h, lo, c = bar["high"], bar["low"], bar["close"]
    p = (h + lo + c) / 3.0
    return {"P": p, "R1": 2 * p - lo, "R2": p + (h - lo), "S1": 2 * p - h, "S2": p - (h - lo)}


def swing_points(bars: list[dict[str, Any]], window: int = SWING_WINDOW,
                 lookback: int = SWING_LOOKBACK) -> dict[str, list[dict[str, Any]]]:
    """Swing highs and lows among the last ``lookback`` bars: a bar whose high (low) equals the highest (lowest) of the
    ``window`` bars on each side and itself. Equal neighbours both count (ties). The last ``window`` bars cannot be
    swings yet. Each entry: {index, date, price}."""
    highs: list[dict[str, Any]] = []
    lows: list[dict[str, Any]] = []
    size = len(bars)
    for i in range(max(window, size - lookback), size - window):
        seg = bars[i - window:i + window + 1]
        if bars[i]["high"] >= max(b["high"] for b in seg):
            highs.append({"index": i, "date": bars[i].get("date"), "price": bars[i]["high"]})
        if bars[i]["low"] <= min(b["low"] for b in seg):
            lows.append({"index": i, "date": bars[i].get("date"), "price": bars[i]["low"]})
    return {"highs": highs, "lows": lows}


def nearest_levels(bars: list[dict[str, Any]], close: float, min_distance: float = LEVEL_MIN_DISTANCE) -> dict[str, Any]:
    """The nearest swing high at least ``min_distance`` above ``close`` (resistance) and the nearest swing low at
    least that far below (support), each {price, date} or None."""
    sw = swing_points(bars)
    above = [h for h in sw["highs"] if h["price"] >= close * (1 + min_distance)]
    below = [lo for lo in sw["lows"] if lo["price"] <= close * (1 - min_distance)]
    res = min(above, key=lambda h: (h["price"], -h["index"])) if above else None
    sup = max(below, key=lambda lo: (lo["price"], lo["index"])) if below else None
    return {"resistance": None if res is None else {"price": res["price"], "date": res["date"]},
            "support": None if sup is None else {"price": sup["price"], "date": sup["date"]}}


def classify_candle(bar: dict[str, Any], prev: dict[str, Any] | None = None) -> list[str]:
    """Named patterns for one candle by fixed rules (shape only, no trend context):
    marubozu: body >= 90% of the range (bullish if it closed above the open); doji: body <= 10% of the range;
    hammer: body > 10% of the range, lower shadow >= 2x the body, upper shadow <= 10% of the range;
    shooting star: the mirror image; engulfing: the body covers the previous candle's body, which had the
    opposite colour and was smaller."""
    o, h, lo, c = bar["open"], bar["high"], bar["low"], bar["close"]
    rng = h - lo
    if rng <= 0:
        return []
    body = abs(c - o)
    upper, lower = h - max(o, c), min(o, c) - lo
    out: list[str] = []
    if prev is not None:
        po, pc = prev["open"], prev["close"]
        if pc < po and c > o and o <= pc and c >= po and body > abs(po - pc):
            out.append("bullish engulfing")
        elif pc > po and c < o and o >= pc and c <= po and body > abs(po - pc):
            out.append("bearish engulfing")
    if body >= MARUBOZU_BODY * rng:
        out.append("bullish marubozu" if c > o else "bearish marubozu")
    elif body <= DOJI_BODY * rng:
        out.append("doji")
    elif lower >= SHADOW_BODY_RATIO * body and upper <= SHADOW_SMALL * rng:
        out.append("hammer")
    elif upper >= SHADOW_BODY_RATIO * body and lower <= SHADOW_SMALL * rng:
        out.append("shooting star")
    return out


def _parse_ts(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    return dt.astimezone(IST) if dt.tzinfo else dt.replace(tzinfo=IST)


def resample_4h(bars_1h: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """1-hour bars to the two NSE-session "4-hour" candles of a day: 09:15-13:15 and 13:15-15:30 (IST). A bar belongs
    to the morning candle when it ends after 09:15 (so bars aligned to 09:00 are kept) and starts before 13:15, and to
    the afternoon candle when it starts from 13:15 and before 15:30; anything else is dropped. A day with a holiday gap
    just has no candles. Each candle: {ts (bucket start), date, bucket (morning / afternoon), open, high, low, close,
    volume, bars}."""
    groups: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for b in sorted(bars_1h, key=lambda x: x["ts"]):
        dt = _parse_ts(b["ts"])
        t = dt.time()
        if dt + timedelta(hours=1) <= datetime.combine(dt.date(), SESSION_OPEN, tzinfo=IST) or t >= SESSION_CLOSE:
            continue
        groups.setdefault((dt.date().isoformat(), 0 if t < SESSION_SPLIT else 1), []).append(b)
    out = []
    for (day, bucket), grp in sorted(groups.items()):
        start = dtime(9, 15) if bucket == 0 else dtime(13, 15)
        ts = datetime.combine(date.fromisoformat(day), start, tzinfo=IST).isoformat(timespec="seconds")
        out.append({"ts": ts, "date": day, "bucket": "morning" if bucket == 0 else "afternoon",
                    "open": grp[0]["open"], "high": max(g["high"] for g in grp), "low": min(g["low"] for g in grp),
                    "close": grp[-1]["close"], "volume": sum(g.get("volume") or 0 for g in grp), "bars": len(grp)})
    return out


# =============================================================================
# Wording (fixed templates)
# =============================================================================
def sgn(v: float, d: int = 2) -> str:
    """+288.65 / −12.00 with the true minus sign; Indian grouping."""
    s = num(v, d)
    if float(s.replace(",", "")) == 0:
        return s
    return ("−" if v < 0 else "+") + s


def adx_band(adx: float) -> str:
    """The band of the ADX as shown (rounded to a whole number): below 20 weak, 20 to below 25 developing, 25 to 40
    strong (40 itself is strong), above 40 very strong."""
    r = int(adx + 0.5)
    return "weak" if r < 20 else "developing" if r < 25 else "strong" if r <= 40 else "very strong"


def adx_text(adx: float | None, plus_di: float | None, minus_di: float | None, label: str = "Daily ADX") -> str | None:
    """ADX bands: <20 weak or no trend, 20-25 developing, 25-40 strong, >40 very strong; direction from +DI / -DI.
    The timeframe is always in the label ("Daily ADX", "4-hour ADX")."""
    if adx is None or plus_di is None or minus_di is None:
        return None
    up = plus_di >= minus_di
    word = "uptrend" if up else "downtrend"
    lead = "+DI above −DI" if up else "−DI above +DI"
    band = adx_band(adx)
    a = f"{label} {int(adx + 0.5)}"
    if band == "weak":
        return f"{a}: weak or no trend ({lead})."
    if band == "developing":
        return f"{a}: {'an' if up else 'a'} {word} is developing ({lead})."
    return f"{a}: the {word} is {band} ({lead})."


def rsi_text(value: float | None) -> str | None:
    if value is None:
        return None
    zone = ("above 70, the range usually called stretched" if value >= 70
            else "below 30, the range usually called stretched" if value <= 30 else "between 30 and 70")
    return f"Daily RSI(14) is {value:.0f}: {zone}."


def gap_clause(gap: float, gap_pct: float) -> str:
    if abs(gap_pct) < GAP_FLAT_PCT:
        return "opened near the previous close"
    return f"opened with a gap {'up' if gap > 0 else 'down'} of {abs(gap):.0f} points"


def ema_share(bars: list[dict[str, Any]], ema_values: list[float | None]) -> dict[str, Any] | None:
    """Share of the 15-minute bars (that have a 21 EMA) closing above / below it."""
    pairs = [(b["close"], e) for b, e in zip(bars, ema_values) if e is not None]
    if not pairs:
        return None
    above = sum(1 for c, e in pairs if c > e)
    below = sum(1 for c, e in pairs if c < e)
    n = len(pairs)
    return {"bars": n, "above_pct": round(100.0 * above / n), "below_pct": round(100.0 * below / n)}


def ema_text(share: dict[str, Any] | None) -> str | None:
    if share is None:
        return None
    if share["above_pct"] >= SHARE_MOST:
        return f"It traded above its 21 EMA for most of the session ({share['above_pct']}% of 15-min bars)."
    if share["below_pct"] >= SHARE_MOST:
        return f"It traded below its 21 EMA for most of the session ({share['below_pct']}% of 15-min bars)."
    return (f"It spent the session on both sides of its 21 EMA ({share['above_pct']}% of 15-min bars above it, "
            f"{share['below_pct']}% below).")


def _day_month(iso: Any) -> str:
    try:
        d = date.fromisoformat(str(iso)[:10])
        return f"{d.day} {d:%b}"
    except ValueError:
        return ""


def levels_text(levels: dict[str, Any], pivot: float | None) -> str:
    parts = []
    r, s = levels.get("resistance"), levels.get("support")
    parts.append(f"resistance {num(r['price'])} (swing high {_day_month(r['date'])})" if r
                 else "no swing high at least 0.3% above the close in the last 60 sessions")
    parts.append(f"support {num(s['price'])} (swing low {_day_month(s['date'])})" if s
                 else "no swing low at least 0.3% below the close in the last 60 sessions")
    return "Watch levels: " + ", ".join(parts) + (f"; pivot {num(pivot)}." if pivot is not None else ".")


def pivots_text(piv: dict[str, float], day: str) -> str:
    return (f"Pivot points from the {_day_month(day)} session, for the next session: P {num(piv['P'])}, R1 {num(piv['R1'])}, R2 {num(piv['R2'])}, "
            f"S1 {num(piv['S1'])}, S2 {num(piv['S2'])}.")


def candle_text(names: list[str], bar: dict[str, Any]) -> str:
    if names:
        return f"Daily candle: {' and '.join(names)}."
    rng = bar["high"] - bar["low"]
    pct = 0 if rng <= 0 else abs(bar["close"] - bar["open"]) / rng * 100
    return f"Daily candle: no named pattern (body {pct:.0f}% of the day's range)."


def four_hour_text(candles: list[dict[str, Any]], all_candles: list[dict[str, Any]]) -> str | None:
    """Patterns of the last session's 4-hour candles (engulfing is judged against the candle before each)."""
    if not candles:
        return None
    bits = []
    for c in candles:
        idx = all_candles.index(c)
        names = classify_candle(c, all_candles[idx - 1] if idx > 0 else None)
        bits.append(f"{c['bucket']} {' and '.join(names) if names else 'no named pattern'}")
    return "4-hour candles: " + ", ".join(bits) + "."


# =============================================================================
# Nifty analysis (pure given the bars)
# =============================================================================
def analyse_nifty(daily: list[dict[str, Any]], bars15: list[dict[str, Any]] | None,
                  bars1h: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Everything the Nifty block says, from daily OHLC bars and (optional) 15-minute and 1-hour bars. Raises
    ValueError when the daily bars are too short to say anything."""
    daily = [b for b in daily if None not in (b.get("open"), b.get("high"), b.get("low"), b.get("close"))]
    if len(daily) < 2:
        raise ValueError("fewer than 2 daily bars")
    today_bar, prev_bar = daily[-1], daily[-2]
    close, prev_close = today_bar["close"], prev_bar["close"]
    change = close - prev_close
    change_pct = 100.0 * change / prev_close if prev_close else 0.0
    gap = today_bar["open"] - prev_close
    gap_pct = 100.0 * gap / prev_close if prev_close else 0.0
    session = str(today_bar["date"])
    notes: list[str] = []
    out: dict[str, Any] = {
        "session": session, "close": round(close, 2), "prev_close": round(prev_close, 2), "open": round(today_bar["open"], 2),
        "high": round(today_bar["high"], 2), "low": round(today_bar["low"], 2), "change": round(change, 2),
        "change_pct": round(change_pct, 2), "gap": round(gap, 2), "gap_pct": round(gap_pct, 2)}
    lines: dict[str, str | None] = {
        "headline": f"Nifty {gap_clause(gap, gap_pct)} and closed {sgn(change)} ({pct_text(change_pct, 2)}) at {num(close, 2)}."}

    # intraday: 15-minute bars of the last session, 21 EMA over the whole series (so it is warm at the open)
    chart: dict[str, Any] = {"prev_close": round(prev_close, 2)}
    out["intraday"] = None
    if bars15:
        series = sorted(bars15, key=lambda b: b["ts"])
        e = ema([b["close"] for b in series], EMA_PERIOD)
        idx = [i for i, b in enumerate(series) if _parse_ts(b["ts"]).date().isoformat() == session]
        if idx:
            day_bars = [series[i] for i in idx]
            day_ema = [e[i] for i in idx]
            share = ema_share(day_bars, day_ema)
            hi = max(day_bars, key=lambda b: b["high"])
            lo = min(day_bars, key=lambda b: b["low"])
            out["intraday"] = {
                "bars": len(day_bars), "ema21": None if day_ema[-1] is None else round(day_ema[-1], 2),
                "ema_share": share, "high": round(hi["high"], 2), "high_time": _parse_ts(hi["ts"]).strftime("%H:%M"),
                "low": round(lo["low"], 2), "low_time": _parse_ts(lo["ts"]).strftime("%H:%M")}
            lines["ema"] = ema_text(share)
            lines["range"] = (f"Day range {num(lo['low'], 2)} to {num(hi['high'], 2)}: low at "
                              f"{out['intraday']['low_time']}, high at {out['intraday']['high_time']}.")
            chart["bars15"] = [{k: round(b[k], 2) if k != "ts" else b[k] for k in ("ts", "open", "high", "low", "close")}
                               for b in day_bars]
            chart["ema21"] = [None if v is None else round(v, 2) for v in day_ema]
        else:
            notes.append(f"No 15-minute bars for {_day_month(session)}.")
    else:
        notes.append("15-minute bars are not available.")

    # daily indicators
    adx = wilder_adx(daily)
    rs = rsi([b["close"] for b in daily])
    out["adx"] = None if adx["adx"][-1] is None else round(adx["adx"][-1], 1)
    out["plus_di"] = None if adx["plus_di"][-1] is None else round(adx["plus_di"][-1], 1)
    out["minus_di"] = None if adx["minus_di"][-1] is None else round(adx["minus_di"][-1], 1)
    out["adx_band"] = None if out["adx"] is None else adx_band(out["adx"])
    out["rsi"] = None if rs[-1] is None else round(rs[-1], 1)
    lines["adx"] = adx_text(adx["adx"][-1], adx["plus_di"][-1], adx["minus_di"][-1])
    lines["rsi"] = rsi_text(rs[-1])
    if lines["adx"] is None:
        notes.append("Not enough daily history for ADX (needs 28 sessions).")

    # levels
    piv = pivot_points(today_bar)
    levels = nearest_levels(daily, close)
    out["pivots"] = {k: round(v, 2) for k, v in piv.items()}
    out["levels"] = {k: None if v is None else {"price": round(v["price"], 2), "date": v["date"]} for k, v in levels.items()}
    lines["levels"] = levels_text(levels, piv["P"])
    lines["pivots"] = pivots_text(piv, session)
    chart["levels"] = ([{"label": "Resistance", "value": round(levels["resistance"]["price"], 2)}] if levels["resistance"] else []) \
        + ([{"label": "Support", "value": round(levels["support"]["price"], 2)}] if levels["support"] else []) \
        + [{"label": "Pivot", "value": round(piv["P"], 2)}]

    # candles
    names = classify_candle(today_bar, prev_bar)
    out["candle"] = names
    lines["candle"] = candle_text(names, today_bar)

    # 4-hour candles from the 1-hour bars
    out["four_hour"] = None
    if bars1h:
        c4 = resample_4h(bars1h)
        last_day = [c for c in c4 if c["date"] == session]
        if last_day:
            a4 = wilder_adx(c4)
            lines["four_hour"] = four_hour_text(last_day, c4)
            if a4["adx"][-1] is not None:
                lines["four_hour_adx"] = adx_text(a4["adx"][-1], a4["plus_di"][-1], a4["minus_di"][-1], "4-hour ADX")
                if adx["adx"][-1] is not None and (a4["plus_di"][-1] >= a4["minus_di"][-1]) != (adx["plus_di"][-1] >= adx["minus_di"][-1]):
                    lines["four_hour_adx"] += " The 4-hour and daily trends point in different directions."
            out["four_hour"] = {"candles": [{"bucket": c["bucket"], "patterns": classify_candle(c, c4[c4.index(c) - 1] if c4.index(c) else None)}
                                            for c in last_day],
                                "adx": None if a4["adx"][-1] is None else round(a4["adx"][-1], 1)}
            keep = c4[-20:]
            off = len(c4) - len(keep)
            chart["bars4h"] = [{k: round(c[k], 2) if k != "ts" else c[k] for k in ("ts", "open", "high", "low", "close")} for c in keep]
            chart["adx4h"] = {k: [None if v is None else round(v, 1) for v in a4[k][off:]] for k in ("adx", "plus_di", "minus_di")}
        else:
            notes.append(f"No 4-hour candles for {_day_month(session)}.")
    else:
        notes.append("1-hour bars are not available for the 4-hour candles.")
    out["lines"] = {k: v for k, v in lines.items() if v}
    out["notes"] = notes
    out["chart_input"] = chart
    return out


# =============================================================================
# Global markets and commodities
# =============================================================================
# market name -> words a headline must contain (whole words, any case)
MARKET_WORDS: dict[str, tuple[str, ...]] = {
    "S&P 500": ("S&P 500", "Wall Street"), "Nasdaq": ("Nasdaq",), "Dow": ("Dow", "Dow Jones"),
    "Nikkei": ("Nikkei", "Tokyo stocks"), "Hang Seng": ("Hang Seng", "Hong Kong stocks"),
    "Shanghai": ("Shanghai Composite", "Shanghai stocks", "China stocks"), "KOSPI": ("KOSPI", "Seoul stocks"),
    "Taiwan": ("Taiex", "Taiwan stocks"), "Straits Times": ("Straits Times", "Singapore stocks"),
    "ASX 200": ("ASX", "Australian shares", "Australian stocks")}
_RANK = {"high": 3, "medium": 2, "low": 1}


def _published(item: dict[str, Any]) -> datetime | None:
    try:
        return _parse_ts(str(item.get("published")))
    except ValueError:
        return None


def why_headline(market: str, headlines: list[dict[str, Any]], now: datetime, hours: float = 24.0) -> dict[str, str] | None:
    """The best real headline from the last ``hours`` that names this market: tagged ones first (by tag confidence),
    then the newest. Returns {title, source} or None; nothing is ever made up."""
    words = MARKET_WORDS.get(market)
    if not words:
        return None
    pats = [re.compile(r"(?<![A-Za-z0-9])" + re.escape(w) + r"(?![A-Za-z0-9])", re.I) for w in words]
    cands = []
    for h in headlines:
        title = str(h.get("title") or "")
        pub = _published(h)
        if not title or pub is None or not (now - timedelta(hours=hours) <= pub <= now + timedelta(minutes=5)):
            continue
        if any(p.search(title) for p in pats):
            cands.append((1 if h.get("sentiment") else 0, _RANK.get(str(h.get("confidence")), 0), pub, h))
    if not cands:
        return None
    best = max(cands, key=lambda c: c[:3])[3]
    return {"title": clean_text(best["title"], 110), "source": clean_text(best.get("source") or "", 40),
            "tagged": bool(best.get("sentiment"))}


def _global(ctx: DigestContext) -> dict[str, Any]:
    w = _world(ctx)
    if "unavailable" in w:
        return w
    try:
        heads = list(ctx.news.market_headlines(hours=24)) if ctx.news is not None and hasattr(ctx.news, "market_headlines") else []
    except Exception:  # noqa: BLE001 - no headlines: no reasons
        heads = []
    now = ctx.now()
    rows = []
    for r in w["us"] + w["asia"] + ([w["vix"]] if w.get("vix") else []):
        row = {"market": r["index"], "close": r["close"], "d1_pct": r["d1_pct"], "d5_pct": r["d5_pct"], "trend": r["trend"],
               "why": why_headline(r["index"], heads, now) if r["index"] != "VIX" else None}
        d1 = r["d1_pct"]
        row["line"] = (f"{r['index']} {num_intl(r['close'], 2)}, {pct_text(d1, 2)} on the day, trend {r['trend']}." if d1 is not None
                       else f"{r['index']} {num_intl(r['close'], 2)}, trend {r['trend']}.")
        rows.append(row)
    if not rows:
        return unavailable("none of the world indices could be read")
    return {"markets": rows, "region_lines": w["region_lines"], "futures_line": w.get("futures_line"), "vix_line": w.get("vix_line"),
            "skipped": w["skipped"],
            "note": "Latest completed session of each market (US markets close after the Indian day). Headlines are shown only "
                    "when one names the market; they are not a stated cause."}


COMMODITIES = [("GC=F", "Gold", "$/oz"), ("SI=F", "Silver", "$/oz"), ("CL=F", "Crude oil (WTI)", "$/barrel"),
               ("BZ=F", "Brent crude", "$/barrel"), ("NG=F", "Natural gas", "$/MMBtu")]


def commodity_reading(name: str, d1: float | None, d5: float | None, vs50: str | None, trend: str) -> str:
    """One rule-based line: the day's move, the 5-session move, the side of the 50-day average and the trend label."""
    if d1 is None:
        move = f"{name} has no day move available"
    else:
        word = "rose" if d1 >= 0.05 else "fell" if d1 <= -0.05 else "was flat"
        move = f"{name} {word} {abs(d1):.2f}% on the day" if word != "was flat" else f"{name} was flat on the day ({pct_text(d1, 2)})"
    if d5 is not None:
        move += (f" and is {abs(d5):.2f}% {'higher' if d5 > 0 else 'lower'} over 5 sessions" if d5 != 0
                 else " and is unchanged over 5 sessions")
    tail = f"; {vs50} its 50-day average" if vs50 else ""
    return f"{move}{tail}; trend {trend}."


def _commodities(ctx: DigestContext) -> dict[str, Any]:
    src = ctx.world_prices or getattr(ctx.context, "source", None)
    if src is None:
        return unavailable("no price source for commodities")
    rows, skipped = [], 0
    for sym, name, unit in COMMODITIES:
        if ctx.expired():
            skipped += 1
            continue
        try:
            closes = [float(b["close"]) for b in src.history(sym, "1y") if b.get("close") is not None]
        except Exception:  # noqa: BLE001
            closes = []
        if len(closes) < 6:
            skipped += 1
            continue
        ma50 = _avg(closes, 50)
        vs50 = None if ma50 is None else ("above" if closes[-1] > ma50 else "below")
        d1, d5, trend = _chg(closes, 1), _chg(closes, 5), trend_label(closes)
        rows.append({"name": name, "unit": unit, "last": round(closes[-1], 2), "d1_pct": d1, "d5_pct": d5, "trend": trend,
                     "reading": commodity_reading(name, d1, d5, vs50, trend)})
    if not rows:
        return unavailable("none of the commodity prices could be read")
    return {"rows": rows, "skipped": skipped,
            "note": "Futures prices in dollars; trend UP means close above the 50-day average above the 200-day (DOWN the reverse). Not a forecast."}


# =============================================================================
# The whole bulletin
# =============================================================================
def _intraday(src: Any, symbol: str, interval: str, range_: str) -> list[dict[str, Any]] | None:
    fn = getattr(src, "history_intraday", None)
    if fn is None:
        return None
    try:
        return fn(symbol, interval, range_)
    except Exception:  # noqa: BLE001 - the Nifty block just goes without that chart
        return None


def completed_bars(daily: list[dict[str, Any]], now: datetime, stale: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The daily bars of finished sessions only. With ``stale`` (the email's stale_close: no trading today, or before the
    close) every bar after that last close is dropped; otherwise today's bar is dropped while it is before 15:30 IST,
    because it is still a partial session. The bulletin never calls an unfinished session closed."""
    if stale and stale.get("date"):
        return [b for b in daily if str(b.get("date")) <= str(stale["date"])]
    if now.time() < SESSION_CLOSE:
        return [b for b in daily if str(b.get("date")) < now.date().isoformat()]
    return daily


def _nifty(ctx: DigestContext, stale: dict[str, Any] | None = None) -> dict[str, Any]:
    src = ctx.world_prices or getattr(ctx.context, "source", None)
    if src is None:
        return unavailable("no price source for the Nifty bulletin")
    if not hasattr(src, "history_ohlc"):
        return unavailable("the price source has no daily open/high/low bars")
    daily = completed_bars(src.history_ohlc("^NSEI", "1y"), ctx.now(), stale)
    if ctx.expired():
        return unavailable("out of time")
    bars15 = _intraday(src, "^NSEI", "15m", "5d")
    bars1h = None if ctx.expired() else _intraday(src, "^NSEI", "1h", "1mo")
    return analyse_nifty(daily, bars15, bars1h)


def _holiday_dates(ctx: DigestContext) -> list[str]:
    """NSE holidays from the calendar, or none when it cannot say."""
    try:
        return list(ctx.calendar.days()) if ctx.calendar is not None and hasattr(ctx.calendar, "days") else []
    except Exception:  # noqa: BLE001 - no calendar: every weekday counts
        return []


def _concept(ctx: DigestContext) -> dict[str, Any]:
    return concept_for(ctx.now().date(), _holiday_dates(ctx))


def build_bulletin(ctx: DigestContext, stale: dict[str, Any] | None = None) -> dict[str, Any]:
    """The bulletin data: {"note", "nifty", "global", "commodities", "concept"}; each part may be {"unavailable"}.
    ``stale`` is the email's stale_close (figures are those of an earlier close)."""
    return {"note": NOTE,
            "nifty": _section(_nifty, "Nifty 50", ctx, stale),
            "global": _section(_global, "global markets", ctx),
            "commodities": _section(_commodities, "commodities", ctx),
            "concept": _section(_concept, "concept of the day", ctx)}
