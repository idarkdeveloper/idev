"""Stock screener page: one row per stock with the usual screener columns, filled in the background.

Everything here is read-only and uses what already exists: the index constituent lists (``screen.load_universe``), the
price-history cache the factor screen uses (``prices.history``), the factor screen's own momentum figures and scoring
(``momentum`` / ``screen.score_universe``), the RSI from ``bulletin`` and ATR from ``risk``, and the Yahoo fundamentals
snapshot (``fundamentals`` shaped by ``quote.build_quote``, cached for a day). Nothing here touches a broker; the
holdings and deal flags come from saved state the caller hands in.

The first load of a big universe takes minutes (history for every stock, then the fundamentals, politely paced). So
``snapshot`` always answers at once with what is cached and says how many stocks are still ``pending``; a background
thread fills the rest and the page polls until ``pending`` is 0. Filtering and sorting happen in the browser.

Percent columns (returns, change, % from the 52-week high, dividend yield, ATR %) are plain percent numbers (1.5
means 1.5%); market cap is in rupees crore; a figure that is not known yet is ``None`` ("n/a" on the page).
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from .momentum import _ret, momentum_stats
from .screen import UNIVERSES, _vol, score_universe

log = logging.getLogger(__name__)

HOLDINGS = "HOLDINGS"
DEALS = "DEALS"
DYNAMIC = (HOLDINGS, DEALS)          # universes the caller supplies (saved state), not an index list
METRICS_TTL = 30 * 60                # a stock's price figures are refreshed in the background after this
FUNDS_TTL = 24 * 3600                # fundamentals: one day
TV_REFRESH = 15 * 60                 # TradingView columns: asked for again no sooner than this (tvscreener also enforces it)
ERROR_RETRY = 5 * 60                 # a failed fetch is tried again after this
UNIVERSE_TTL = 24 * 3600
UNIVERSE_RETRY = 60
SOURCE_NOTE = "Prices and fundamentals from Yahoo Finance, delayed"
RSI_PERIOD = 14
TURNOVER_FLOOR = 1e7                 # the factor screen's liquidity floor, rupees a day


def universe_key(text: str) -> str | None:
    """'nifty 50' -> 'NIFTY50'; one of the index lists, HOLDINGS or DEALS, else None."""
    key = str(text or "").strip().upper().replace(" ", "")
    return key if key in UNIVERSES or key in DYNAMIC else None


def _pct(v: float | None) -> float | None:
    return None if v is None else round(v * 100.0, 2)


SPARK_DAYS = 60   # closing prices sent per stock for the page's 30-day sparkline and 60-day chart


def compute_metrics(bars: list[dict[str, Any]]) -> dict[str, Any]:
    """The screener's price figures from daily bars (oldest first: date, close, adj_close, volume), or {"error"}.

    Returns the factor screen's own statistics under ``stats`` (the composite score is computed from those across the
    universe) and the display figures. Returns over a week or more use ``adj_close`` (dividends included, as the factor
    screen does); the day's change uses ``close``, like the exchange. ATR is close-to-close (the cached history has no
    daily high and low), the same figure the position sizer uses."""
    if len(bars) < 2:
        return {"error": "no price history"}
    from .bulletin import rsi
    from .risk import atr
    stats = momentum_stats(bars)
    stats["vol_60d"] = _vol(bars)
    closes = [float(b["close"]) for b in bars]
    last, prev = closes[-1], closes[-2]
    vols = [float(b.get("volume") or 0.0) for b in bars]
    rel = None
    base = vols[-21:-1]
    if len(base) == 20 and vols[-1] > 0 and sum(base) > 0:
        rel = round(vols[-1] / (sum(base) / 20.0), 2)
    rsi_last = rsi(closes)[-1]
    a = atr(bars)
    return {
        "stats": stats, "as_of": bars[-1]["date"], "price": round(last, 2),
        "chg_1d": _pct(last / prev - 1.0) if prev else None,
        "ret_1w": _pct(_ret(bars, 5)), "ret_1m": _pct(stats.get("ret_1m")), "ret_6m": _pct(stats.get("ret_6m")),
        "ret_1y": _pct(stats.get("ret_12m")), "ret_12_1": _pct(stats.get("ret_12_1")),
        "volume": vols[-1], "rel_volume": rel,
        "avg_turnover_cr": round(stats["avg_turnover_60d"] / 1e7, 2),
        "pct_from_high": _pct(stats.get("pct_from_52w_high")), "above_200": stats.get("above_200dma"),
        "rsi": None if rsi_last is None else round(rsi_last, 1),
        "atr_pct": round(a / last * 100.0, 2) if a is not None and last else None,
        "spark": [round(c, 2) for c in closes[-SPARK_DAYS:]],
    }


def fundamentals_fields(symbol: str, parsed: dict[str, Any], now: float) -> dict[str, Any]:
    """P/E, market cap (rupees crore) and dividend yield (percent) from a fundamentals snapshot, via the same shaping
    the Look up quote panel uses (ETFs get no P/E; an out-of-range yield is dropped). {"error"} when Yahoo had none."""
    from .quote import build_quote
    q = build_quote(symbol, parsed, fetched_at=now)
    if q["error"]:
        return {"error": q["error"]}
    cap, dy = q.get("market_cap"), q.get("dividend_yield")
    return {"pe": q.get("pe"), "market_cap_cr": round(cap / 1e7, 1) if cap else None,
            "div_yield": None if dy is None else round(dy * 100.0, 2)}


class ScreenerService:
    """Holds the per-stock caches and runs the background fill. One per dashboard, shared by Live and Demo."""

    def __init__(self, *, prices: Any, fundamentals: Any, load_universe: Callable[[str], list[dict[str, str]]],
                 bands: Callable[[], Any] | None = None, tv: Any | None = None,
                 names: Callable[[list[str]], dict[str, str]] | None = None,
                 workers: int = 3, fund_workers: int = 2, pace: float = 0.1,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep):
        self.prices, self.fundamentals, self._load_universe = prices, fundamentals, load_universe
        self.bands, self.tv, self._names_fn = bands, tv, names
        self.workers, self.fund_workers, self.pace = workers, fund_workers, pace
        self.clock, self.sleep = clock, sleep
        self._lock = threading.Lock()
        self._metrics: dict[str, tuple[float, dict[str, Any]]] = {}
        self._funds: dict[str, tuple[float, dict[str, Any]]] = {}
        self._universes: dict[str, tuple[float, list[dict[str, str]]]] = {}
        self._universe_error: dict[str, tuple[float, str]] = {}
        self._sector: dict[str, str] = {}
        self._names: dict[str, str] = {}
        self._tv: dict[str, tuple[float, dict[str, dict[str, Any]]]] = {}
        self._tv_tried: dict[str, float] = {}
        self._running: dict[str, threading.Thread] = {}

    # -- cache bookkeeping ------------------------------------------------------
    def _fresh(self, entry: tuple[float, dict[str, Any]] | None, ttl: float) -> bool:
        if entry is None:
            return False
        at, value = entry
        return self.clock() - at < (ERROR_RETRY if "error" in value else ttl)

    def _members(self, key: str, given: list[dict[str, str]] | None) -> list[dict[str, str]] | None:
        if given is not None:
            return given
        hit = self._universes.get(key)
        return hit[1] if hit is not None else None

    # -- the answer ---------------------------------------------------------------
    def snapshot(self, key: str, *, given: list[dict[str, str]] | None = None, held: set[str] | None = None,
                 deals: dict[str, list[str]] | None = None) -> dict[str, Any]:
        """Rows for ``key`` as cached right now, plus ``pending`` (stocks still to fetch) and ``loading`` (the
        constituent list itself is not here yet). Starts or continues the background fill when anything is missing or
        stale. ``given`` is the member list of HOLDINGS / DEALS (from saved state)."""
        held, deals = held or set(), deals or {}
        members = self._members(key, given)
        err = self._universe_error.get(key)
        if members is None:
            if err is None or self.clock() - err[0] > UNIVERSE_RETRY:
                self._ensure_fill(key, None)
            return self._payload(key, [], pending=0 if (err and self.clock() - err[0] <= UNIVERSE_RETRY) else 1,
                                 loading=not (err and self.clock() - err[0] <= UNIVERSE_RETRY),
                                 error=err[1] if err else None)
        fresh_univ = key in DYNAMIC or self._fresh_universe(key)
        symbols = [m["symbol"] for m in members]
        with self._lock:
            missing_p = [s for s in symbols if s not in self._metrics]
            missing_f = [s for s in symbols if s not in self._funds]
            stale = (not fresh_univ or any(not self._fresh(self._metrics.get(s), METRICS_TTL) for s in symbols)
                     or any(not self._fresh(self._funds.get(s), FUNDS_TTL) for s in symbols))
            tv_missing = self._tv_wanted(key)
        pending = len(set(missing_p) | set(missing_f))
        if stale or tv_missing:
            self._ensure_fill(key, given)
        rows = self._rows(members, held, deals, key)
        return self._payload(key, rows, pending=pending, loading=False, error=None)

    def _fresh_universe(self, key: str) -> bool:
        hit = self._universes.get(key)
        return hit is not None and self.clock() - hit[0] < UNIVERSE_TTL

    def _tv_wanted(self, key: str) -> bool:
        """True when TradingView columns are on and not fetched for this universe yet (the module itself decides
        whether it may call: role, back-off, 15-minute cache)."""
        if self.tv is None:
            return False
        last = max(self._tv[key][0] if key in self._tv else 0.0, self._tv_tried.get(key, 0.0))
        return self.clock() - last >= TV_REFRESH and self.tv.status()["state"] == "on"

    def _payload(self, key: str, rows: list[dict[str, Any]], *, pending: int, loading: bool, error: str | None) -> dict[str, Any]:
        as_of = max((r["as_of"] for r in rows if r.get("as_of")), default=None)
        tv = self.tv.status() if self.tv is not None else {"state": "off", "until": None}
        return {"universe": key, "count": len(rows), "pending": pending, "loading": loading, "error": error,
                "as_of": as_of, "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "source": SOURCE_NOTE, "tv": tv, "rows": rows}

    def _rows(self, members: list[dict[str, str]], held: set[str], deals: dict[str, list[str]], key: str) -> list[dict[str, Any]]:
        with self._lock:
            stats = {m["symbol"]: self._metrics[m["symbol"]][1]["stats"] for m in members
                     if m["symbol"] in self._metrics and "stats" in self._metrics[m["symbol"]][1]}
            m_snap = {m["symbol"]: self._metrics[m["symbol"]][1] for m in members if m["symbol"] in self._metrics}
            f_snap = {m["symbol"]: self._funds[m["symbol"]][1] for m in members if m["symbol"] in self._funds}
            tv_snap = self._tv[key][1] if key in self._tv else {}
            sector, names = dict(self._sector), dict(self._names)
        scored = {r["symbol"]: r for r in score_universe(stats, min_turnover=TURNOVER_FLOOR)}
        book = self.bands() if self.bands is not None else None
        out = []
        for m in members:
            sym = m["symbol"]
            met, fun, sc = m_snap.get(sym) or {}, f_snap.get(sym) or {}, scored.get(sym)
            band = None
            if book is not None:
                try:
                    band = book.rule(sym)["label"]
                except Exception:  # noqa: BLE001 - the band is a nicety
                    band = None
            row: dict[str, Any] = {
                "symbol": sym, "name": m.get("name") or names.get(sym) or "",
                "industry": m.get("industry") or sector.get(sym) or "",
                "price": met.get("price"), "chg_1d": met.get("chg_1d"), "ret_1w": met.get("ret_1w"),
                "ret_1m": met.get("ret_1m"), "ret_6m": met.get("ret_6m"), "ret_1y": met.get("ret_1y"),
                "ret_12_1": met.get("ret_12_1"), "volume": met.get("volume"), "rel_volume": met.get("rel_volume"),
                "market_cap_cr": fun.get("market_cap_cr"), "pe": fun.get("pe"), "div_yield": fun.get("div_yield"),
                "pct_from_high": met.get("pct_from_high"), "above_200": met.get("above_200"),
                "rsi": met.get("rsi"), "atr_pct": met.get("atr_pct"), "avg_turnover_cr": met.get("avg_turnover_cr"),
                "mom_score": round(sc["score"], 2) if sc else None, "mom_eligible": bool(sc["eligible"]) if sc else None,
                "band": band, "held": sym in held, "deal": sym in deals, "deal_who": deals.get(sym, []),
                "as_of": met.get("as_of"), "ready": bool(met) and bool(fun), "spark": met.get("spark"),
                "error": met.get("error") if "error" in met else None,
            }
            tv = tv_snap.get(sym)
            if tv is not None:
                row.update({"tv_summary": tv.get("summary"), "tv_summary_label": tv.get("summary_label"),
                            "tv_sector": tv.get("sector"), "tv_industry": tv.get("industry"),
                            "tv_eps_growth": tv.get("eps_growth")})
            out.append(row)
        return out

    # -- the background fill ------------------------------------------------------
    def _ensure_fill(self, key: str, given: list[dict[str, str]] | None) -> None:
        with self._lock:
            t = self._running.get(key)
            if t is not None and t.is_alive():
                return
            t = threading.Thread(target=self._fill, args=(key, given), daemon=True, name=f"screener-{key}")
            self._running[key] = t
        t.start()

    def wait(self, timeout: float = 10.0) -> bool:
        """Block until every running fill has finished (tests); True when none is left running."""
        end = time.time() + timeout
        for t in list(self._running.values()):
            t.join(max(0.0, end - time.time()))
        return not any(t.is_alive() for t in self._running.values())

    def _fill(self, key: str, given: list[dict[str, str]] | None) -> None:
        try:
            members = self._members(key, given)
            if members is None or (key not in DYNAMIC and not self._fresh_universe(key)):
                try:
                    members = self._load_universe(key)
                    with self._lock:
                        self._universes[key] = (self.clock(), members)
                        self._universe_error.pop(key, None)
                        for m in members:
                            if m.get("industry"):
                                self._sector[m["symbol"]] = m["industry"]
                except Exception as e:  # noqa: BLE001 - a dead list is shown, and retried later
                    log.warning("screener universe %s failed: %s", key, e)
                    with self._lock:
                        self._universe_error[key] = (self.clock(), f"{type(e).__name__}: {e}")
                    if members is None:
                        return
            symbols = [m["symbol"] for m in members]
            if key in DYNAMIC and self._names_fn is not None:
                need = [m["symbol"] for m in members if not m.get("name") and m["symbol"] not in self._names]
                if need:
                    try:
                        got = self._names_fn(need) or {}
                        with self._lock:
                            self._names.update({s: n for s, n in got.items() if n})
                    except Exception:  # noqa: BLE001 - names are a nicety
                        pass
            todo_p = [s for s in symbols if not self._fresh(self._metrics.get(s), METRICS_TTL)]
            with ThreadPoolExecutor(max_workers=max(1, self.workers)) as ex:
                list(ex.map(self._fetch_metrics, todo_p))
            self._fill_tv(key, symbols)
            todo_f = [s for s in symbols if not self._fresh(self._funds.get(s), FUNDS_TTL)]
            with ThreadPoolExecutor(max_workers=max(1, self.fund_workers)) as ex:
                list(ex.map(self._fetch_funds, todo_f))
        except Exception:  # noqa: BLE001
            log.exception("screener fill for %s failed", key)

    def _fetch_metrics(self, symbol: str) -> None:
        if self.pace:
            self.sleep(self.pace)
        try:
            entry = compute_metrics(self.prices.history(symbol, "2y"))
        except Exception as e:  # noqa: BLE001 - one stock must not stop the rest
            entry = {"error": f"{type(e).__name__}: {e}"}
        with self._lock:
            self._metrics[symbol] = (self.clock(), entry)

    def _fetch_funds(self, symbol: str) -> None:
        if self.pace:
            self.sleep(self.pace)
        try:
            entry = fundamentals_fields(symbol, self.fundamentals.get(symbol), self.clock())
        except Exception as e:  # noqa: BLE001
            entry = {"error": f"{type(e).__name__}: {e}"}
        with self._lock:
            self._funds[symbol] = (self.clock(), entry)

    def _fill_tv(self, key: str, symbols: list[str]) -> None:
        if self.tv is None or not self._tv_wanted(key):
            return
        self._tv_tried[key] = self.clock()
        try:
            got = self.tv.fetch(key, symbols)
        except Exception:  # noqa: BLE001 - display only; never lets it break the fill
            log.exception("TradingView columns failed")
            got = None
        if got is not None:
            with self._lock:
                self._tv[key] = (self.clock(), got)


def deal_buyers(deals: Iterable[Any], today: str, days: int = 30) -> dict[str, list[str]]:
    """{ticker: [followed investor names]} for disclosed purchases dated within ``days`` of ``today`` (ISO date).
    ``deals`` are quiver.DisclosedTrade (already only the followed investors' trades)."""
    from datetime import date, timedelta
    try:
        cutoff = (date.fromisoformat(today) - timedelta(days=days)).isoformat()
    except ValueError:
        return {}
    out: dict[str, list[str]] = {}
    for t in deals:
        if t.transaction != "Purchase":
            continue
        when = (t.transaction_date or t.report_date or "")[:10]
        if when < cutoff or when > today or not t.ticker:
            continue
        who = out.setdefault(t.ticker.upper(), [])
        if t.investor and t.investor not in who:
            who.append(t.investor)
    return out
