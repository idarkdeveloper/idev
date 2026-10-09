"""Factor screen over an NSE index universe: the AQR / Dimensional playbook, retail size.

Ranks every constituent on 12-1 momentum, 6-month return, low volatility and a trend
filter, with a liquidity floor; the ranking is documented and deterministic so a
backtest can reproduce it. Quality and value (fundamentals.py) are an opt-in overlay on
today's numbers only, so they are not part of any backtest.
"""

from __future__ import annotations

import csv
import io
import logging
import math
import statistics
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Iterable

import requests

from .momentum import momentum_stats

log = logging.getLogger(__name__)

UNIVERSES = {
    "NIFTY50": "https://archives.nseindia.com/content/indices/ind_nifty50list.csv",
    "NIFTY100": "https://archives.nseindia.com/content/indices/ind_nifty100list.csv",
    "NIFTY200": "https://archives.nseindia.com/content/indices/ind_nifty200list.csv",
    "NIFTY500": "https://archives.nseindia.com/content/indices/ind_nifty500list.csv",
    "NIFTYMIDCAP150": "https://archives.nseindia.com/content/indices/ind_niftymidcap150list.csv",
    "NIFTYSMALLCAP250": "https://archives.nseindia.com/content/indices/ind_niftysmallcap250list.csv",
}
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"}


def load_universe(name: str = "NIFTY200", session: requests.Session | None = None) -> list[dict[str, str]]:
    """[{symbol, name, industry}] from NSE's published constituent CSV."""
    url = UNIVERSES.get(name.upper().replace(" ", ""))
    if not url:
        raise ValueError(f"unknown universe {name}; choose from {', '.join(UNIVERSES)}")
    resp = (session or requests.Session()).get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    rows = list(csv.DictReader(io.StringIO(resp.content.decode("utf-8-sig"))))
    out = []
    for r in rows:
        sym = (r.get("Symbol") or "").strip()
        if sym and not sym.upper().startswith("DUMMY"):  # placeholder for a demerged company not yet listed
            out.append({"symbol": sym, "name": (r.get("Company Name") or "").strip(),
                        "industry": (r.get("Industry") or "").strip()})
    return out


def _vol(bars: list[dict[str, Any]], n: int = 60) -> float | None:
    closes = [b["adj_close"] for b in bars[-(n + 1):]]
    if len(closes) < n + 1:
        return None
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1]]
    return statistics.pstdev(rets) * math.sqrt(252) if len(rets) > 1 else None


def _z(values: list[float | None]) -> list[float | None]:
    xs = [v for v in values if v is not None]
    if len(xs) < 2:
        return [0.0 if v is not None else None for v in values]
    mu, sd = statistics.fmean(xs), statistics.pstdev(xs)
    return [((v - mu) / sd if sd else 0.0) if v is not None else None for v in values]


def score_universe(stats_by_symbol: dict[str, dict[str, Any]], *, min_turnover: float = 1e7,
                   require_above_200dma: bool = True) -> list[dict[str, Any]]:
    """Composite = z(12-1 momentum) + 0.5 z(6m) - 0.5 z(60d vol); filtered by trend and liquidity."""
    rows = []
    for sym, s in stats_by_symbol.items():
        if "error" in s or s.get("ret_12_1") is None or s.get("ret_6m") is None:
            continue
        rows.append({"symbol": sym, "ret_12_1": s["ret_12_1"], "ret_6m": s["ret_6m"], "ret_1m": s.get("ret_1m"),
                     "vol_60d": s.get("vol_60d"), "above_200dma": s.get("above_200dma"),
                     "avg_turnover_60d": s.get("avg_turnover_60d"), "pct_from_52w_high": s.get("pct_from_52w_high"),
                     "last_close": s.get("last_close"), "verdict": s.get("verdict")})
    if not rows:
        return []
    z_mom = _z([r["ret_12_1"] for r in rows])
    z_6m = _z([r["ret_6m"] for r in rows])
    z_vol = _z([r["vol_60d"] for r in rows])
    for r, a, b, c in zip(rows, z_mom, z_6m, z_vol):
        r["score"] = (a or 0) + 0.5 * (b or 0) - 0.5 * (c or 0)
        r["eligible"] = (r["avg_turnover_60d"] or 0) >= min_turnover and \
            (r["above_200dma"] is True or not require_above_200dma)
    rows.sort(key=lambda r: r["score"], reverse=True)
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return rows


def run_screen(universe: Iterable[dict[str, str]], prices: Any, *, top: int = 20, workers: int = 8,
               min_turnover: float = 1e7, require_above_200dma: bool = True,
               fundamentals: Any | None = None, quality: float = 0.0, value: float = 0.0) -> dict[str, Any]:
    """``prices.history(symbol, '2y')`` per constituent, in parallel; returns ranked rows.

    With a ``fundamentals`` provider and a ``quality`` and/or ``value`` weight, the
    eligible names are re-ranked with those factors too (see fundamentals.py). Off by
    default, so the backtested screen is unchanged.
    """
    members = list(universe)
    names = {m["symbol"]: m for m in members}

    def one(sym: str) -> tuple[str, dict[str, Any]]:
        try:
            bars = prices.history(sym, "2y")
            s = momentum_stats(bars)
            s["vol_60d"] = _vol(bars)
            return sym, s
        except Exception as e:  # noqa: BLE001
            return sym, {"error": f"{type(e).__name__}: {e}"}

    with ThreadPoolExecutor(max_workers=workers) as ex:
        stats = dict(ex.map(one, [m["symbol"] for m in members]))
    rows = score_universe(stats, min_turnover=min_turnover, require_above_200dma=require_above_200dma)
    for r in rows:
        r["name"] = names.get(r["symbol"], {}).get("name", "")
        r["industry"] = names.get(r["symbol"], {}).get("industry", "")
    overlay = None
    if fundamentals is not None and (quality or value):
        from .fundamentals import apply_fundamentals, fetch_all
        funds = fetch_all(fundamentals, [r["symbol"] for r in rows if r["eligible"]])
        apply_fundamentals(rows, funds, quality=quality, value=value)
        overlay = {"quality": quality, "value": value, "source": "Yahoo Finance snapshot",
                   "missing": sum(1 for f in funds.values() if "error" in f),
                   "excluded": sum(1 for r in rows if r.get("excluded"))}
    eligible = [r for r in rows if r["eligible"]]
    return {"universe_size": len(members), "scored": len(rows), "eligible": len(eligible),
            "errors": sum(1 for s in stats.values() if "error" in s),
            "top": eligible[:top], "all": rows, "fundamentals": overlay}


def format_screen(result: dict[str, Any], top: int = 20) -> str:
    pct = lambda v: "  n/a " if v is None else f"{v*100:+6.1f}%"  # noqa: E731
    lines = [f"Screen: {result['scored']}/{result['universe_size']} scored, {result['eligible']} eligible "
             f"(above 200dma, liquid), {result['errors']} errors",
             f"{'#':>3} {'symbol':<12} {'12-1':>7} {'6m':>7} {'1m':>7} {'vol':>6} {'52w':>7}  score  name"]
    fx = result.get("fundamentals")
    if fx:
        lines[0] += (f"\nWith fundamentals (quality x{fx['quality']:g}, value x{fx['value']:g}, {fx['source']}): "
                     f"{fx['excluded']} excluded, {fx['missing']} without data. Not backtested: today's numbers only.")
        lines[1] += "    ROE   D/E    P/E"
    for r in result["top"][:top]:
        line = (f"{r['rank']:>3} {r['symbol']:<12} {pct(r['ret_12_1'])} {pct(r['ret_6m'])} {pct(r['ret_1m'])} "
                f"{(r['vol_60d'] or 0)*100:5.0f}% {pct(r['pct_from_52w_high'])} {r['score']:+6.2f}  {r['name'][:28]:<28}")
        if fx:
            de = "  fin" if r.get("financial") else ("  n/a" if r.get("debt_to_equity") is None else f"{r['debt_to_equity']:5.2f}")
            pe = f"{r['pe']:6.1f}" if r.get("pe") else "   n/a"
            line += f"  {pct(r.get('roe'))} {de} {pe}"
        lines.append(line)
    return "\n".join(lines)
