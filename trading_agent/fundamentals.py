"""Company fundamentals for the quality and value factors, from Yahoo Finance.

Yahoo's quoteSummary endpoint gives a current snapshot per NSE stock: EPS, book value
per share, debt/equity, earnings growth, P/E and price/book. Its own return-on-equity
field is usually empty for Indian stocks, so ROE is computed as trailing EPS divided by
book value per share. Snapshots are cached on disk for a day.

These are *today's* numbers only, with no history, so quality and value cannot be
backtested point-in-time here: they are an opt-in overlay on the momentum screen
(``screen --quality --value``), never part of the factor backtest or the forward test.

* Quality: z(ROE) - z(debt/equity) + 0.5 z(earnings growth); debt/equity is ignored for
  banks and other financial firms, whose balance sheets are debt by design. Floors: ROE
  must be positive, and non-financials must carry less than 2x debt to equity.
* Value: z(earnings yield = 1 / P/E) + 0.5 z(book-to-price).
"""

from __future__ import annotations

import json
import logging
import statistics
import time
from pathlib import Path
from typing import Any, Iterable

import requests

log = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
SUMMARY_URL = "https://query2.finance.yahoo.com/v10/finance/quoteSummary/{symbol}"
CRUMB_URL = "https://query1.finance.yahoo.com/v1/test/getcrumb"
MODULES = "financialData,defaultKeyStatistics,summaryDetail,quoteType"
MAX_DEBT_TO_EQUITY = 2.0
FINANCIAL_WORDS = ("bank", "financial", "finance", "insurance", "nbfc", "capital markets")


def is_financial(industry: str | None) -> bool:
    return any(w in (industry or "").lower() for w in FINANCIAL_WORDS)


def _raw(d: dict[str, Any], key: str) -> float | None:
    v = d.get(key)
    if isinstance(v, dict):
        v = v.get("raw")
    return float(v) if isinstance(v, (int, float)) else None


def parse_summary(result: dict[str, Any]) -> dict[str, Any]:
    """Normalise one quoteSummary result into the fields the factors use."""
    fd = result.get("financialData") or {}
    ks = result.get("defaultKeyStatistics") or {}
    sd = result.get("summaryDetail") or {}
    eps, bvps = _raw(ks, "trailingEps"), _raw(ks, "bookValue")
    roe = eps / bvps if eps is not None and bvps and bvps > 0 else _raw(fd, "returnOnEquity")
    de = _raw(fd, "debtToEquity")  # Yahoo reports a percentage: 75.0 means 0.75x
    pe, pb = _raw(sd, "trailingPE"), _raw(ks, "priceToBook")
    qt = (result.get("quoteType") or {}).get("quoteType")
    return {"roe": roe, "debt_to_equity": de / 100 if de is not None else None,
            "earnings_growth": _raw(fd, "earningsGrowth"), "profit_margin": _raw(fd, "profitMargins"),
            "pe": pe, "pb": pb, "eps": eps, "book_value": bvps,
            "market_cap": _raw(sd, "marketCap"), "dividend_yield": _raw(sd, "dividendYield"),
            "week52_low": _raw(sd, "fiftyTwoWeekLow"), "week52_high": _raw(sd, "fiftyTwoWeekHigh"),
            "quote_type": qt if isinstance(qt, str) else None,
            "earnings_yield": 1 / pe if pe and pe > 0 else None,
            "book_to_price": 1 / pb if pb and pb > 0 else None}


class YahooFundamentals:
    """``get(symbol)`` returns parse_summary() fields, or {"error": ...}."""

    def __init__(self, cache_dir: Path | None = None, *, session: requests.Session | None = None,
                 suffix: str = ".NS", cache_ttl: float = 20 * 3600, timeout: float = 20.0, pause: float = 0.3,
                 sleep: Any = time.sleep):
        self.cache_dir = Path(cache_dir) / "fundamentals" if cache_dir else None
        self.session = session or requests.Session()
        self.suffix = suffix
        self.cache_ttl = cache_ttl
        self.timeout = timeout
        self.pause = pause
        self.sleep = sleep
        self._crumb: str | None = None
        self._calls = 0

    def _cache_path(self, symbol: str) -> Path | None:
        return self.cache_dir / f"{symbol.upper()}.json" if self.cache_dir else None

    def _get_crumb(self, refresh: bool = False) -> str:
        if self._crumb and not refresh:
            return self._crumb
        try:
            self.session.get("https://fc.yahoo.com", headers={"User-Agent": UA}, timeout=self.timeout)
        except requests.RequestException:
            pass  # it answers 404 but sets the cookie the crumb needs
        r = self.session.get(CRUMB_URL, headers={"User-Agent": UA}, timeout=self.timeout)
        r.raise_for_status()
        crumb = r.text.strip()
        if not crumb or "<" in crumb or len(crumb) > 64:
            raise LookupError("Yahoo did not issue a crumb")
        self._crumb = crumb
        return crumb

    def _fetch(self, symbol: str) -> dict[str, Any]:
        ysym = symbol.upper() if "." in symbol else f"{symbol.upper()}{self.suffix}"
        for attempt in (0, 1):
            if self._calls and self.pause:
                self.sleep(self.pause)
            self._calls += 1
            r = self.session.get(SUMMARY_URL.format(symbol=ysym), headers={"User-Agent": UA},
                                 params={"modules": MODULES, "crumb": self._get_crumb(refresh=attempt == 1)},
                                 timeout=self.timeout)
            if r.status_code in (401, 403) and attempt == 0:
                continue  # stale crumb: get a new one once
            r.raise_for_status()
            res = ((r.json().get("quoteSummary") or {}).get("result") or [])
            if not res:
                raise LookupError(f"Yahoo has no fundamentals for {ysym}")
            return parse_summary(res[0])
        raise LookupError(f"Yahoo refused fundamentals for {ysym}")

    def get(self, symbol: str) -> dict[str, Any]:
        path = self._cache_path(symbol)
        if path and path.exists() and time.time() - path.stat().st_mtime < self.cache_ttl:
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                pass
        try:
            out = self._fetch(symbol)
        except Exception as e:  # noqa: BLE001 - one missing stock must not stop the screen
            return {"error": f"{type(e).__name__}: {e}"}
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(out), encoding="utf-8")
        return out


def _z(values: list[float | None]) -> list[float | None]:
    xs = [v for v in values if v is not None]
    if len(xs) < 2:
        return [0.0 if v is not None else None for v in values]
    mu, sd = statistics.fmean(xs), statistics.pstdev(xs)
    return [((v - mu) / sd if sd else 0.0) if v is not None else None for v in values]


def apply_fundamentals(rows: list[dict[str, Any]], funds: dict[str, dict[str, Any]], *,
                       quality: float = 1.0, value: float = 0.0,
                       industries: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Add quality / value to already-scored screen rows, re-rank and re-apply floors.

    Only rows still ``eligible`` are scored (z-scores across that set). A row without
    fundamentals keeps its momentum score but is marked ``no_fundamentals``.
    """
    industries = industries or {}
    elig = [r for r in rows if r.get("eligible")]
    for r in elig:
        f = funds.get(r["symbol"]) or {}
        fin = is_financial(industries.get(r["symbol"]) or r.get("industry"))
        r.update(roe=f.get("roe"), debt_to_equity=None if fin else f.get("debt_to_equity"),
                 earnings_growth=f.get("earnings_growth"), pe=f.get("pe"), pb=f.get("pb"),
                 earnings_yield=f.get("earnings_yield"), book_to_price=f.get("book_to_price"),
                 financial=fin, no_fundamentals=not f or "error" in f)
    zq_roe = _z([r.get("roe") for r in elig])
    zq_de = _z([r.get("debt_to_equity") for r in elig])
    zq_g = _z([r.get("earnings_growth") for r in elig])
    zv_ey = _z([r.get("earnings_yield") for r in elig])
    zv_bp = _z([r.get("book_to_price") for r in elig])
    for r, a, b, c, d, e in zip(elig, zq_roe, zq_de, zq_g, zv_ey, zv_bp):
        r["momentum_score"] = r["score"]
        r["quality_score"] = (a or 0) - (b or 0) + 0.5 * (c or 0)
        r["value_score"] = (d or 0) + 0.5 * (e or 0)
        r["score"] = r["momentum_score"] + quality * r["quality_score"] + value * r["value_score"]
        if quality and not r["no_fundamentals"]:
            if r.get("roe") is not None and r["roe"] <= 0:
                r["eligible"], r["excluded"] = False, "loss-making (ROE <= 0)"
            elif r.get("debt_to_equity") is not None and r["debt_to_equity"] > MAX_DEBT_TO_EQUITY:
                r["eligible"], r["excluded"] = False, f"debt above {MAX_DEBT_TO_EQUITY:g}x equity"
    rows.sort(key=lambda r: (not r.get("eligible"), -r["score"]))
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return rows


def fetch_all(provider: Any, symbols: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Sequential on purpose: Yahoo throttles bursts, and the cache makes reruns instant."""
    return {s: provider.get(s) for s in symbols}
