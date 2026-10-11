"""Candle-chart payload for the dashboard's "Look up a stock" card. Pure computation: daily OHLC bars in, the series
Lightweight Charts draws out. Indicators are computed over the FULL history and only then sliced to the range, so
they are already warmed up at the chart's left edge. Nothing here touches the network or an order."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from .bulletin import ema, rsi

RANGES = {"1M": 30, "3M": 91, "6M": 182, "1Y": 365, "2Y": 730, "5Y": 1826}   # calendar days back from the last bar
INTRADAY = {"1D": ("5m", "5d"), "1W": ("30m", "5d")}   # range -> (Yahoo bar size, Yahoo range fetched)
ALL = "ALL"                                              # every daily bar Yahoo has
VALID_RANGES = [*INTRADAY, *RANGES, ALL]
IST_OFFSET_SECONDS = 19_800                              # +05:30
DEFAULT_RANGE = "1Y"
BB_PERIOD, BB_WIDTH = 20, 2.0
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9


def sma(values: list[float], n: int) -> list[float | None]:
    """Simple moving average over the last ``n`` values; None until ``n`` values exist."""
    out: list[float | None] = [None] * len(values)
    if n < 1:
        return out
    run = 0.0
    for i, v in enumerate(values):
        run += v
        if i >= n:
            run -= values[i - n]
        if i >= n - 1:
            out[i] = run / n
    return out


def bollinger(closes: list[float], n: int = BB_PERIOD, width: float = BB_WIDTH) -> dict[str, list[float | None]]:
    """Bollinger bands: the n-period SMA, plus and minus ``width`` population standard deviations."""
    mid = sma(closes, n)
    upper: list[float | None] = [None] * len(closes)
    lower: list[float | None] = [None] * len(closes)
    for i, m in enumerate(mid):
        if m is None:
            continue
        window = closes[i - n + 1:i + 1]
        sd = (sum((x - m) ** 2 for x in window) / n) ** 0.5
        upper[i], lower[i] = m + width * sd, m - width * sd
    return {"upper": upper, "mid": mid, "lower": lower}


def macd(closes: list[float], fast: int = MACD_FAST, slow: int = MACD_SLOW,
         signal: int = MACD_SIGNAL) -> dict[str, list[float | None]]:
    """MACD = EMA(fast) - EMA(slow); signal = EMA of the MACD line (seeded once ``signal`` MACD values exist);
    histogram = MACD - signal."""
    f, s = ema(closes, fast), ema(closes, slow)
    line: list[float | None] = [(a - b) if a is not None and b is not None else None for a, b in zip(f, s)]
    first = next((i for i, v in enumerate(line) if v is not None), None)
    sig: list[float | None] = [None] * len(closes)
    if first is not None:
        tail = ema([v for v in line[first:] if v is not None], signal)
        sig[first:] = tail
    hist: list[float | None] = [(m - g) if m is not None and g is not None else None for m, g in zip(line, sig)]
    return {"macd": line, "signal": sig, "hist": hist}


def parse_range(text: str | None) -> str | None:
    """The range key (case-insensitive; None means the default), or None when it is not one of RANGES."""
    key = (text or DEFAULT_RANGE).strip().upper()
    return key if key in VALID_RANGES else None


def _series(dates: list[str], values: list[float | None], start: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i in range(start, len(dates)):
        v = values[i]
        if v is not None:
            out.append({"time": dates[i], "value": round(v, 2)})
    return out


def build_candles(ticker: str, range_key: str, bars: list[dict[str, Any]],
                  position: dict[str, Any] | None = None) -> dict[str, Any]:
    """The /api/candles payload. ``bars`` are daily {date, open, high, low, close, volume}, oldest first."""
    clean = [b for b in bars if None not in (b.get("open"), b.get("high"), b.get("low"), b.get("close"))]
    clean.sort(key=lambda b: str(b["date"]))
    out: dict[str, Any] = {"ticker": ticker, "range": range_key, "bars": [], "position": position, "intraday": False}
    for k in ("ema20", "ema50", "ma200", "bb_upper", "bb_mid", "bb_lower", "rsi14", "macd", "macd_signal",
              "macd_hist"):
        out[k] = []
    if not clean:
        out["error"] = "price history unavailable"
        return out
    dates = [str(b["date"])[:10] for b in clean]
    closes = [float(b["close"]) for b in clean]
    if range_key == ALL:
        start = 0
    else:
        cutoff = (date.fromisoformat(dates[-1]) - timedelta(days=RANGES[range_key])).isoformat()
        start = next((i for i, d in enumerate(dates) if d >= cutoff), len(dates) - 1)
    bb, mc = bollinger(closes), macd(closes)
    out["bars"] = [{"time": dates[i], "open": round(float(clean[i]["open"]), 2),
                    "high": round(float(clean[i]["high"]), 2), "low": round(float(clean[i]["low"]), 2),
                    "close": round(closes[i], 2), "volume": int(clean[i].get("volume") or 0)}
                   for i in range(start, len(clean))]
    out["ema20"], out["ema50"] = _series(dates, ema(closes, 20), start), _series(dates, ema(closes, 50), start)
    out["ma200"] = _series(dates, sma(closes, 200), start)
    out["bb_upper"] = _series(dates, bb["upper"], start)
    out["bb_mid"] = _series(dates, bb["mid"], start)
    out["bb_lower"] = _series(dates, bb["lower"], start)
    out["rsi14"] = _series(dates, rsi(closes, 14), start)
    out["macd"], out["macd_signal"] = _series(dates, mc["macd"], start), _series(dates, mc["signal"], start)
    out["macd_hist"] = _series(dates, mc["hist"], start)
    return out


def ist_chart_time(ts: str) -> int:
    """An ISO timestamp with an offset ("2026-10-09T09:15:00+05:30") -> epoch seconds SHIFTED to IST: true UTC epoch plus
    19,800 s. Lightweight Charts draws intraday times as UTC, so after the shift 09:15 IST shows as 09:15."""
    return int(datetime.fromisoformat(ts).timestamp()) + IST_OFFSET_SECONDS


def build_intraday(ticker: str, range_key: str, bars: list[dict[str, Any]],
                   position: dict[str, Any] | None = None, prev_close: float | None = None) -> dict[str, Any]:
    """The /api/candles payload for 1D (5-minute bars of the latest session) and 1W (30-minute bars of the last five
    sessions). ``bars`` are Yahoo intraday bars {ts, date, open, high, low, close, volume}, oldest first. Times are
    IST-shifted epoch seconds (see ist_chart_time). No indicators: they need daily history. ``prev_close`` (the
    previous session's close) is drawn as a dashed line on 1D; when the fetch holds the previous session it wins."""
    interval, _ = INTRADAY[range_key]
    out: dict[str, Any] = {"ticker": ticker, "range": range_key, "bars": [], "position": position, "intraday": True,
                           "interval": interval, "prev_close": None, "session": None}
    for k in ("ema20", "ema50", "ma200", "bb_upper", "bb_mid", "bb_lower", "rsi14", "macd", "macd_signal", "macd_hist"):
        out[k] = []
    clean = [b for b in bars if b.get("ts") and None not in (b.get("open"), b.get("high"), b.get("low"), b.get("close"))]
    clean.sort(key=lambda b: str(b["ts"]))
    if not clean:
        out["error"] = "intraday price history unavailable"
        return out
    days = sorted({str(b["date"]) for b in clean})
    last_day = days[-1]
    earlier = [b for b in clean if str(b["date"]) < last_day]
    shown = [b for b in clean if str(b["date"]) == last_day] if range_key == "1D" else clean
    out["session"] = last_day
    out["prev_close"] = round(float(earlier[-1]["close"]), 2) if earlier else (None if prev_close is None else round(float(prev_close), 2))
    seen: set[int] = set()
    for b in shown:
        t = ist_chart_time(str(b["ts"]))
        if t in seen:
            continue
        seen.add(t)
        out["bars"].append({"time": t, "open": round(float(b["open"]), 2), "high": round(float(b["high"]), 2),
                            "low": round(float(b["low"]), 2), "close": round(float(b["close"]), 2),
                            "volume": int(b.get("volume") or 0)})
    return out
