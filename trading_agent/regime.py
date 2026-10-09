"""Global market context: a one-line regime flag for position sizing, not direction.

US overnight moves, Nikkei, India VIX, the rupee and crude predict how Nifty *opens*,
which is priced in the first minute. What survives for a slow retail agent is the regime:
in a global drawdown Indian small caps fall harder, so the agent should size down or
refuse new buys. Everything here comes from Yahoo Finance for free.
"""

from __future__ import annotations

import math
import statistics
import time
from typing import Any

SYMBOLS = {
    "nifty50": "^NSEI",
    "sp500": "^GSPC",
    "nasdaq_fut": "NQ=F",
    "nikkei": "^N225",
    "india_vix": "^INDIAVIX",
    "usdinr": "USDINR=X",
    "brent": "BZ=F",
}
LABELS = {"nifty50": "Nifty 50", "sp500": "S&P 500", "nasdaq_fut": "Nasdaq fut", "nikkei": "Nikkei",
          "india_vix": "India VIX", "usdinr": "USD/INR", "brent": "Brent"}


def _ret(bars: list[dict[str, Any]], n: int) -> float | None:
    if len(bars) <= n:
        return None
    a, b = bars[-1 - n]["close"], bars[-1]["close"]
    return b / a - 1.0 if a else None


def _realised_vol(bars: list[dict[str, Any]], n: int = 20) -> float | None:
    closes = [b["close"] for b in bars[-(n + 1):]]
    if len(closes) < n + 1:
        return None
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1]]
    return statistics.pstdev(rets) * math.sqrt(252) if len(rets) > 1 else None


def compute_regime(series: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """``series`` maps the keys of SYMBOLS to daily bars (oldest first). Pure function."""
    out: dict[str, Any] = {"markets": {}, "signals": [], "score": 0}
    score = 0

    def mark(key: str) -> dict[str, Any]:
        bars = series.get(key) or []
        m = {"label": LABELS[key], "last": bars[-1]["close"] if bars else None,
             "as_of": bars[-1]["date"] if bars else None,
             "ret_1d": _ret(bars, 1), "ret_5d": _ret(bars, 5), "ret_20d": _ret(bars, 20)}
        out["markets"][key] = m
        return m

    nifty = mark("nifty50")
    nbars = series.get("nifty50") or []
    if len(nbars) >= 200:
        ma = sum(b["close"] for b in nbars[-200:]) / 200
        nifty["above_200dma"] = nbars[-1]["close"] > ma
        nifty["pct_vs_200dma"] = nbars[-1]["close"] / ma - 1.0
    else:
        nifty["above_200dma"] = None
        nifty["pct_vs_200dma"] = None
    nifty["realised_vol_20d"] = _realised_vol(nbars)
    if len(nbars) >= 200:
        ma50 = sum(b["close"] for b in nbars[-50:]) / 50
        ma200 = nifty.get("pct_vs_200dma")
        nifty["ma50_above_ma200"] = ma50 > (nbars[-1]["close"] / (1 + ma200)) if ma200 is not None else None
        # Trend: price vs 200dma and 50dma vs 200dma (the classic CTA / golden-cross filter).
        above = nifty["above_200dma"]
        nifty["trend"] = ("up" if above and nifty["ma50_above_ma200"] else
                          "down" if (not above and not nifty["ma50_above_ma200"]) else "mixed")
    else:
        nifty["ma50_above_ma200"] = None
        nifty["trend"] = None

    sp, nq, nk = mark("sp500"), mark("nasdaq_fut"), mark("nikkei")
    vix, inr, oil = mark("india_vix"), mark("usdinr"), mark("brent")

    def sig(text: str, delta: int) -> None:
        nonlocal score
        out["signals"].append(text)
        score += delta

    if nifty["above_200dma"] is True:
        sig("Nifty above 200-day MA", +1)
    elif nifty["above_200dma"] is False:
        sig("Nifty below 200-day MA", -1)
    if nifty["ret_20d"] is not None and nifty["ret_20d"] < -0.05:
        sig(f"Nifty {nifty['ret_20d']*100:+.1f}% over 20 days", -1)
    if sp["ret_1d"] is not None and sp["ret_1d"] < -0.015:
        sig(f"S&P 500 {sp['ret_1d']*100:+.1f}% overnight", -1)
    elif sp["ret_5d"] is not None:
        sig(f"S&P 500 {sp['ret_5d']*100:+.1f}% over 5 days", +1 if sp["ret_5d"] > 0 else (-1 if sp["ret_5d"] < -0.03 else 0))
    if nq["ret_1d"] is not None and nq["ret_1d"] < -0.02:
        sig(f"Nasdaq futures {nq['ret_1d']*100:+.1f}%", -1)
    if nk["ret_5d"] is not None and nk["ret_5d"] < -0.03:
        sig(f"Nikkei {nk['ret_5d']*100:+.1f}% over 5 days", -1)
    if vix["last"] is not None:
        if vix["last"] > 25:
            sig(f"India VIX {vix['last']:.1f} (stress)", -2)
        elif vix["last"] > 20:
            sig(f"India VIX {vix['last']:.1f} (elevated)", -1)
        elif vix["last"] < 15:
            sig(f"India VIX {vix['last']:.1f} (calm)", +1)
    if inr["ret_5d"] is not None and inr["ret_5d"] > 0.01:
        sig(f"Rupee weaker {inr['ret_5d']*100:+.1f}% in 5 days", -1)
    if oil["ret_5d"] is not None and oil["ret_5d"] > 0.08:
        sig(f"Brent {oil['ret_5d']*100:+.1f}% in 5 days", -1)

    if nifty.get("trend") == "down":
        sig("Nifty in a downtrend (50-day MA below 200-day MA, price below both)", -1)
    out["score"] = score
    out["trend"] = nifty.get("trend")
    out["regime"] = "risk_off" if score <= -2 else "risk_on" if score >= 2 else "neutral"
    out["guidance"] = {
        "risk_off": "No new buys; existing positions at most half size; prefer watch over buy.",
        "neutral": "Normal sizing; require strong momentum for new buys.",
        "risk_on": "Normal sizing; neutral-momentum buys acceptable with high-quality counterparties.",
    }[out["regime"]]
    out["summary"] = regime_summary(out)
    return out


def regime_summary(r: dict[str, Any]) -> str:
    m = r["markets"]
    pct = lambda v: "n/a" if v is None else f"{v*100:+.1f}%"  # noqa: E731
    parts = [f"{r['regime'].replace('_', '-')} (score {r['score']:+d}, trend {r.get('trend') or 'n/a'})"]
    if m.get("nifty50", {}).get("last"):
        n = m["nifty50"]
        parts.append(f"Nifty {n['last']:,.0f} {pct(n['ret_1d'])} 1d, {pct(n['ret_20d'])} 20d, "
                     f"{'above' if n['above_200dma'] else 'below' if n['above_200dma'] is False else '?'} 200dma")
    for k in ("sp500", "nasdaq_fut", "nikkei"):
        if m.get(k, {}).get("last"):
            parts.append(f"{LABELS[k]} {pct(m[k]['ret_1d'])} 1d / {pct(m[k]['ret_5d'])} 5d")
    if m.get("india_vix", {}).get("last"):
        parts.append(f"VIX {m['india_vix']['last']:.1f}")
    if m.get("usdinr", {}).get("last"):
        parts.append(f"USD/INR {m['usdinr']['last']:.2f} ({pct(m['usdinr']['ret_5d'])} 5d)")
    if m.get("brent", {}).get("last"):
        parts.append(f"Brent {m['brent']['last']:.0f} ({pct(m['brent']['ret_5d'])} 5d)")
    return "; ".join(parts)


class GlobalContext:
    """Fetches the series through a ``history(symbol, range_)`` source; cached 15 minutes."""

    def __init__(self, source: Any, ttl: float = 900.0):
        self.source = source
        self.ttl = ttl
        self._cached: dict[str, Any] | None = None
        self._at = 0.0

    def fetch(self, force: bool = False) -> dict[str, Any]:
        if self._cached and not force and time.time() - self._at < self.ttl:
            return self._cached
        series: dict[str, list[dict[str, Any]]] = {}
        errors: dict[str, str] = {}
        for key, sym in SYMBOLS.items():
            try:
                series[key] = self.source.history(sym, "1y" if key == "nifty50" else "3mo")
            except Exception as e:  # noqa: BLE001
                errors[key] = f"{type(e).__name__}: {e}"
        r = compute_regime(series)
        r["errors"] = errors
        self._cached, self._at = r, time.time()
        return r
