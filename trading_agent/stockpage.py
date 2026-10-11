"""Data for the stock page (GET /api/stock): everything the Look up page shows beside the chart, in one read-only payload.

* Yahoo quoteSummary modules (price, summary detail, key statistics, financial data, profile, income statements) give the
  price block, the fundamentals grid, the About text and the revenue / profit bars. Yahoo fills some modules thinly for
  Indian stocks, so every part is parsed on its own and a missing value stays None (the page shows "n/a").
* The shareholding pattern comes from NSE through the existing client, and is reported as unavailable when NSE does not
  answer. Nothing is ever estimated or invented.
* Circuit limits come from the NSE price band percentage and the previous close; the face value from NSE's equity list;
  the industry P/E is the median over same-industry peers whose P/E the screener has already saved; similar stocks are
  other members of the same industry in the NSE index constituent lists.

Pure functions up top (tested with fake JSON), one small caching service at the bottom. Nothing here touches a broker.
"""

from __future__ import annotations

import logging
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Callable

from .bulletin import adx_reading, rsi, rsi_zone, wilder_adx
from .candles import macd as macd_series
from .candles import sma
from .fundamentals import _raw, parse_summary
from .quote import build_quote, group_indian

log = logging.getLogger(__name__)

YAHOO_MODULES = ("price,summaryDetail,defaultKeyStatistics,financialData,assetProfile,quoteType,"
                 "incomeStatementHistoryQuarterly,incomeStatementHistory")
SOURCE = "Yahoo Finance"
STOCK_TTL_SECONDS = 5 * 60
PARTIAL_TTL_SECONDS = 60          # a payload with a part that failed (NSE shareholding) is asked again sooner
PERIODS = 5                       # bars in the financial performance chart
MIN_INDUSTRY_PEERS = 3            # fewer known peer P/Es than this: the industry P/E is "n/a"
SIMILAR = 5
TICK = Decimal("0.05")
CRORE = 1e7
SHAREHOLDING_DOWN = "unavailable from NSE right now"


# ---------- small maths ----------
def to_crore(rupees: float | None) -> float | None:
    return None if rupees is None else rupees / CRORE


def pct_change(cur: float | None, prev: float | None) -> float | None:
    """(cur - prev) / |prev| as a fraction; None when either is missing or the base is zero."""
    if cur is None or prev is None or prev == 0:
        return None
    return (cur - prev) / abs(prev)


def cagr(end: float | None, start: float | None, years: float) -> float | None:
    """Compound annual growth from ``start`` to ``end`` over ``years``. Needs two positive numbers: growth from a loss
    (or to one) has no meaningful rate, so it is None rather than a made-up figure."""
    if end is None or start is None or years <= 0 or end <= 0 or start <= 0:
        return None
    return (end / start) ** (1 / years) - 1


def tick_round(x: float) -> float:
    """Round a price to the NSE tick of 5 paise."""
    return float((Decimal(str(round(x, 6))) / TICK).quantize(Decimal(1), rounding=ROUND_HALF_UP) * TICK)


def circuit_limits(prev_close: float | None, band: Any) -> dict[str, Any]:
    """Lower / upper circuit from the NSE price band percent and the previous close. ``band`` is an int percent, None for
    a stock with no band, or anything else (the list is missing or does not hold the stock) for unknown."""
    if isinstance(band, bool) or not (band is None or isinstance(band, (int, float))):
        return {"state": "unknown", "band": None, "lower": None, "upper": None}
    if band is None:
        return {"state": "none", "band": "no band", "lower": None, "upper": None}
    if not prev_close or prev_close <= 0 or band <= 0:
        return {"state": "unknown", "band": f"{band:g}%", "lower": None, "upper": None}
    return {"state": "band", "band": f"{band:g}%", "lower": tick_round(prev_close * (1 - band / 100)),
            "upper": tick_round(prev_close * (1 + band / 100))}


def crore_text(cr: float | None) -> str | None:
    """Crore with Indian digit grouping: 12345.6 -> '₹12,346 Cr'; under one crore keeps two decimals."""
    if cr is None:
        return None
    if abs(cr) < 1:
        return f"₹{cr:.2f} Cr"
    return f"₹{group_indian(round(cr))} Cr"


# ---------- Yahoo modules ----------
def _ts_date(v: Any) -> str | None:
    if isinstance(v, dict):
        v = v.get("raw")
    if not isinstance(v, (int, float)) or v <= 0:
        return None
    return datetime.fromtimestamp(v, tz=timezone.utc).date().isoformat()


def period_label(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%b '%y")


def parse_price(result: dict[str, Any]) -> dict[str, Any]:
    """Open, previous close, day range, volume and the last price. The ``price`` module wins; summaryDetail and
    financialData fill the gaps when Yahoo leaves it out. Change is computed from the last price and previous close."""
    pr, sd, fd = result.get("price") or {}, result.get("summaryDetail") or {}, result.get("financialData") or {}

    def first(*pairs: tuple[dict[str, Any], str]) -> float | None:
        for d, k in pairs:
            v = _raw(d, k)
            if v is not None:
                return v
        return None
    last = first((pr, "regularMarketPrice"), (fd, "currentPrice"))
    prev = first((pr, "regularMarketPreviousClose"), (sd, "regularMarketPreviousClose"), (sd, "previousClose"))
    change = last - prev if last is not None and prev else first((pr, "regularMarketChange"))
    change_pct = change / prev if change is not None and prev else first((pr, "regularMarketChangePercent"))
    vol = first((pr, "regularMarketVolume"), (sd, "volume"), (sd, "regularMarketVolume"))
    name = pr.get("longName") or pr.get("shortName")
    return {"last": last, "open": first((pr, "regularMarketOpen"), (sd, "open"), (sd, "regularMarketOpen")),
            "prev_close": prev, "day_low": first((pr, "regularMarketDayLow"), (sd, "dayLow"), (sd, "regularMarketDayLow")),
            "day_high": first((pr, "regularMarketDayHigh"), (sd, "dayHigh"), (sd, "regularMarketDayHigh")),
            "volume": None if vol is None else int(vol), "change": change, "change_pct": change_pct,
            "name": name if isinstance(name, str) and name.strip() else None}


def parse_periods(result: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    """Revenue and net profit per period in ₹ crore, oldest first, at most the last PERIODS. ``kind`` is "quarterly" or
    "yearly". Each period also carries its change against the period before it. Fewer periods than asked is normal."""
    block = result.get("incomeStatementHistoryQuarterly" if kind == "quarterly" else "incomeStatementHistory") or {}
    rows = block.get("incomeStatementHistory") if isinstance(block, dict) else None
    out: dict[str, dict[str, Any]] = {}
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        end = _ts_date(r.get("endDate"))
        rev, profit = _raw(r, "totalRevenue"), _raw(r, "netIncome")
        if end is None or (rev is None and profit is None):
            continue
        out[end] = {"end": end, "label": period_label(end), "revenue_cr": to_crore(rev), "profit_cr": to_crore(profit)}
    periods = [out[k] for k in sorted(out)][-PERIODS:]
    for i, p in enumerate(periods):
        prev = periods[i - 1] if i else None
        p["revenue_change"] = pct_change(p["revenue_cr"], prev["revenue_cr"]) if prev else None
        p["profit_change"] = pct_change(p["profit_cr"], prev["profit_cr"]) if prev else None
    return periods


def growth_table(quarterly: list[dict[str, Any]], yearly: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """1Y and 3Y CAGR for revenue and profit. 1Y is the trailing twelve months against the twelve before them when eight
    quarters are known, otherwise the latest year against the one before. 3Y CAGR needs four yearly periods."""
    out: dict[str, dict[str, Any]] = {}
    for key, field in (("revenue", "revenue_cr"), ("profit", "profit_cr")):
        y1: float | None = None
        q = [p[field] for p in quarterly]
        if len(q) >= 8 and None not in q[-8:]:
            y1 = pct_change(sum(q[-4:]), sum(q[-8:-4]))
        if y1 is None and len(yearly) >= 2:
            y1 = pct_change(yearly[-1][field], yearly[-2][field])
        y3 = cagr(yearly[-1][field], yearly[-4][field], 3) if len(yearly) >= 4 else None
        out[key] = {"y1": y1, "cagr3": y3}
    return out


def parse_profile(result: dict[str, Any]) -> dict[str, Any]:
    ap = result.get("assetProfile") or {}

    def text(k: str) -> str | None:
        v = ap.get(k)
        return v.strip() if isinstance(v, str) and v.strip() else None
    return {"summary": text("longBusinessSummary"), "sector": text("sector"), "industry": text("industry"),
            "website": text("website")}


def build_fundamentals(ticker: str, result: dict[str, Any], price: dict[str, Any], *, face_value: float | None,
                       industry_pe: float | None, fetched_at: float) -> dict[str, Any]:
    """The Fundamentals grid. Debt to equity is Yahoo's percentage turned into a ratio (90 -> 0.90) by parse_summary."""
    parsed = parse_summary(result)
    q = build_quote(ticker, parsed, fetched_at=fetched_at)
    last, book = price.get("last"), parsed.get("book_value")
    pb = parsed.get("pb")
    if pb is None and last and book and book > 0:
        pb = last / book
    cap = parsed.get("market_cap")
    cap_cr = to_crore(cap) if cap and cap > 0 else None
    roe = parsed.get("roe")
    return {"fund": q["fund"], "market_cap_cr": cap_cr, "market_cap_text": crore_text(cap_cr), "roe": roe,
            "pe": q["pe"], "eps": parsed.get("eps"), "pb": None if pb is None or pb <= 0 else round(pb, 2),
            "dividend_yield": q["dividend_yield"], "industry_pe": industry_pe, "book_value": book,
            "debt_to_equity": parsed.get("debt_to_equity"), "face_value": face_value,
            "week52_low": q["week52_low"], "week52_high": q["week52_high"]}


def industry_pe(peer_pes: list[float | None]) -> tuple[float | None, int]:
    """Median positive P/E over the peers whose P/E is known, and how many that was; None below MIN_INDUSTRY_PEERS."""
    known = [p for p in peer_pes if isinstance(p, (int, float)) and p > 0]
    if len(known) < MIN_INDUSTRY_PEERS:
        return None, len(known)
    return round(statistics.median(known), 1), len(known)


# ---------- shareholding ----------
def shareholding_payload(quarters: list[dict[str, Any]] | None, error: str | None = None) -> dict[str, Any]:
    """Quarter chips (newest first, at most five) with a bar per category. Without data: state "unavailable"."""
    if error or not quarters:
        return {"state": "unavailable", "message": SHAREHOLDING_DOWN, "quarters": []}
    out = []
    for q in quarters[:PERIODS]:
        try:
            label = datetime.strptime(str(q["date"]).title(), "%d-%b-%Y").strftime("%b '%y")
        except ValueError:
            label = str(q["date"])
        out.append({"date": q["date"], "label": label, "promoters": q.get("promoters"), "dii": q.get("dii"),
                    "public": q.get("public"), "fii": q.get("fii")})
    return {"state": "ok", "message": None, "quarters": out}


# ---------- technicals ----------
def _last(values: list[float | None]) -> float | None:
    return next((v for v in reversed(values) if v is not None), None)


def technicals(bars: list[dict[str, Any]]) -> dict[str, Any]:
    """Latest RSI(14), MACD (line, signal, histogram), ADX with +DI / -DI and the moving averages from daily bars."""
    clean = [b for b in bars if None not in (b.get("high"), b.get("low"), b.get("close"))]
    clean.sort(key=lambda b: str(b["date"]))
    if len(clean) < 30:
        return {"available": False}
    closes = [float(b["close"]) for b in clean]
    mc, adx = macd_series(closes), wilder_adx(clean)
    r = _last(rsi(closes, 14))
    a, p, m = _last(adx["adx"]), _last(adx["plus_di"]), _last(adx["minus_di"])
    ma50, ma200 = _last(sma(closes, 50)), _last(sma(closes, 200))
    return {"available": True, "as_of": str(clean[-1]["date"])[:10], "close": closes[-1],
            "rsi14": r, "rsi_zone": rsi_zone(r) if r is not None else None,
            "macd": _last(mc["macd"]), "macd_signal": _last(mc["signal"]), "macd_hist": _last(mc["hist"]),
            "adx": a, "plus_di": p, "minus_di": m, "adx_reading": adx_reading(a, p, m),
            "ma50": ma50, "ma200": ma200}


# ---------- service ----------
@dataclass
class StockSources:
    """Everything the service reads from outside, as plain callables so tests pass fakes (no network)."""
    modules: Callable[[str], dict[str, Any]]                 # Yahoo symbol -> raw quoteSummary result (raises on failure)
    shareholding: Callable[[str], list[dict[str, Any]]]      # NSE symbol -> quarters, newest first (raises on failure)
    daily_bars: Callable[[str, str], list[dict[str, Any]]]   # (ticker, exchange) -> daily OHLC bars
    universe: Callable[[], list[dict[str, str]]]             # [{symbol, name, industry}] from the index lists
    peer_pe: Callable[[str], float | None]                   # saved P/E of a peer, no request
    price_pair: Callable[[str], tuple[float, float] | None]  # (last close, previous close) of a peer
    face_value: Callable[[str], float | None]
    band: Callable[[str], Any]                               # int | None | "unknown"


class StockService:
    """``get(ticker, exchange)`` -> the /api/stock payload, cached per ticker and exchange."""

    def __init__(self, sources: StockSources, *, ttl: float = STOCK_TTL_SECONDS,
                 clock: Callable[[], float] = time.time):
        self.src, self.ttl, self.clock = sources, ttl, clock
        self._cache: dict[tuple[str, str], tuple[float, float, dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def get(self, ticker: str, exchange: str = "NSE") -> dict[str, Any]:
        base = ticker.strip().upper().split(".")[0]
        exch = "BSE" if exchange.upper() == "BSE" or ticker.strip().upper().endswith(".BO") else "NSE"
        key, now = (base, exch), self.clock()
        with self._lock:
            hit = self._cache.get(key)
        if hit is not None and now - hit[0] < hit[1]:
            return hit[2]
        out = self._build(base, exch, now)
        if not out["error"]:
            ttl = PARTIAL_TTL_SECONDS if out["shareholding"]["state"] != "ok" and not out["fund"] else self.ttl
            with self._lock:
                self._cache[key] = (now, min(ttl, self.ttl), out)
        return out

    def _similar(self, base: str, industry: str | None, members: list[dict[str, str]]) -> tuple[list[dict[str, Any]], float | None, int]:
        peers = [m for m in members if industry and m.get("industry") == industry and m["symbol"].upper() != base]
        ipe, n = industry_pe([self.src.peer_pe(m["symbol"]) for m in peers])
        picks = peers[:SIMILAR + 3]   # a few spares: a peer whose price cannot be read is skipped

        def one(m: dict[str, str]) -> dict[str, Any] | None:
            try:
                pair = self.src.price_pair(m["symbol"])
            except Exception:  # noqa: BLE001 - a peer without a price is just left out
                return None
            if not pair or not pair[0]:
                return None
            last, prev = pair
            return {"symbol": m["symbol"], "name": m.get("name") or "", "price": last,
                    "change_pct": pct_change(last, prev)}
        with ThreadPoolExecutor(max_workers=4) as ex:
            rows = [r for r in ex.map(one, picks) if r]
        return rows[:SIMILAR], ipe, n

    def _build(self, base: str, exch: str, now: float) -> dict[str, Any]:
        ysym = f"{base}.BO" if exch == "BSE" else f"{base}.NS"
        errors: dict[str, str] = {}
        out: dict[str, Any] = {"ticker": base, "exchange": exch, "yahoo_symbol": ysym, "source": SOURCE, "fetched_at": now,
                               "error": None, "fund": False}

        def guarded(name: str, fn: Callable[[], Any]) -> Any:
            try:
                return fn()
            except Exception as e:  # noqa: BLE001 - one failing part must not blank the page
                errors[name] = f"{type(e).__name__}: {e}"
                return None
        with ThreadPoolExecutor(max_workers=4) as ex:
            f_mod = ex.submit(guarded, "yahoo", lambda: self.src.modules(ysym))
            f_sh = ex.submit(guarded, "shareholding", lambda: self.src.shareholding(base))
            f_bars = ex.submit(guarded, "technicals", lambda: self.src.daily_bars(base, exch))
            f_uni = ex.submit(guarded, "similar", self.src.universe)
        result, quarters, bars, members = f_mod.result(), f_sh.result(), f_bars.result(), f_uni.result() or []
        result = result if isinstance(result, dict) else {}
        if "yahoo" in errors:
            out["error"] = errors["yahoo"]
        price = parse_price(result)
        member = next((m for m in members if m["symbol"].upper() == base), None)
        nse_industry = member.get("industry") if member else None
        similar, ipe, ipe_n = (guarded("similar", lambda: self._similar(base, nse_industry, members)) or ([], None, 0))
        fund = build_fundamentals(base, result, price, face_value=guarded("face_value", lambda: self.src.face_value(base)),
                                  industry_pe=ipe, fetched_at=now) if result else None
        quarterly, yearly = parse_periods(result, "quarterly"), parse_periods(result, "yearly")
        band = guarded("band", lambda: self.src.band(base))
        out.update(
            name=price.pop("name"), fund=bool(fund and fund["fund"]), price=price,
            week52={"low": fund["week52_low"], "high": fund["week52_high"]} if fund else {"low": None, "high": None},
            circuit=circuit_limits(price.get("prev_close"), band if band is not None or "band" not in errors else "unknown"),
            fundamentals=fund, industry_pe_n=ipe_n,
            financials={"quarterly": quarterly, "yearly": yearly, "growth": growth_table(quarterly, yearly)},
            about={**parse_profile(result), "nse_industry": nse_industry},
            shareholding=shareholding_payload(quarters, errors.get("shareholding")),
            technicals=technicals(bars or []) if bars else {"available": False},
            similar=similar, errors=errors)
        if out["fund"]:
            out["similar"], out["shareholding"] = [], shareholding_payload(None)
        return out
