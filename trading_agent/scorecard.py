"""Keep score: what each of Claude's recommendations did afterwards.

For every stored recommendation: entry at the first close after the day it was made,
then the stock's return over 5 / 20 / 60 trading days and its excess over its own
benchmark. A buy counts as right when the excess beats the round-trip cost; a sell
when the stock then lagged its benchmark. Watch and hold calls are reported, not scored.

Which benchmark: a mid-cap pick carries more market risk than a large cap, so in a rally
a mediocre mid-cap looks like alpha against the NIFTY 50. Each Indian recommendation is
therefore scored against the index fund of the index the stock belonged to ON THE DAY OF
THE RECOMMENDATION (point-in-time membership, the same data the factor backtest uses):
NIFTY 50 / NIFTY 100 member -> NIFTYBEES, Midcap 150 -> MID150BEES, Smallcap 250 ->
HDFCSML250, anything else -> NIFTYBEES. All of these are funds, so dividends are included.
"""

from __future__ import annotations

import statistics
import threading
import time
from typing import Any, Iterable

from .backtest import _forward
from .factor_backtest import INDEX_FUNDS

HORIZONS = (5, 20, 60)

NIFTY_FUND = "NIFTYBEES"
MID_FUND = INDEX_FUNDS["NIFTYMIDCAP150"]
SMALL_FUND = INDEX_FUNDS["NIFTYSMALLCAP250"]
# membership universes consulted, in priority order: (universe key, fund)
BENCHMARK_UNIVERSES = (("NIFTY50", NIFTY_FUND), ("NIFTY100", NIFTY_FUND),
                       ("NIFTYMIDCAP150", MID_FUND), ("NIFTYSMALLCAP250", SMALL_FUND))
ASSUMED_NOTE = "membership assumed (before recorded history)"
GROUP_LABEL = {NIFTY_FUND: "large caps and others", MID_FUND: "mid caps", SMALL_FUND: "small caps"}


_CACHE: dict[tuple[str, str, str], tuple[Any, float]] = {}
_CACHE_LOCK = threading.Lock()
RETRY_FAILED_AFTER = 600.0   # seconds before a universe that failed to load is tried again


def _ist_today() -> str:
    from datetime import datetime
    from .timezones import IST
    return datetime.now(IST).date().isoformat()


def clear_membership_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def build_memberships(state_dir: Any, *, progress: Any = None, use_cache: bool = False,
                      now_fn: Any = time.time, today_fn: Any = _ist_today) -> dict[str, Any]:
    """Point-in-time membership per benchmark universe. A universe that cannot be loaded
    (no network, no history yet) maps to None, which the scorer reports as 'membership unknown'.

    With use_cache (the web app) each universe is kept per (state_dir, IST date): a loaded one for the day, a failed
    one (None) only for RETRY_FAILED_AFTER seconds, so an outage is retried soon and never pinned for the day.
    The CLI builds fresh each time."""
    from .index_history import point_in_time
    from .screen import load_universe
    out: dict[str, Any] = {}
    day = today_fn()
    if use_cache:
        with _CACHE_LOCK:   # earlier days are never read again
            for k in [k for k in _CACHE if k[1] < day]:
                del _CACHE[k]
    for key, _fund in BENCHMARK_UNIVERSES:
        ck = (str(state_dir), day, key)
        if use_cache:
            with _CACHE_LOCK:
                hit = _CACHE.get(ck)
            if hit is not None and (hit[0] is not None or now_fn() - hit[1] < RETRY_FAILED_AFTER):
                out[key] = hit[0]
                continue
        try:
            current = [m["symbol"] for m in load_universe(key)]
            value = point_in_time(key, current, state_dir, progress=progress)
        except Exception as e:  # noqa: BLE001
            if progress:
                progress(f"membership for {key} unavailable: {type(e).__name__}: {e}")
            value = None
        out[key] = value
        if use_cache:
            with _CACHE_LOCK:
                _CACHE[ck] = (value, now_fn())
    return out


def pick_benchmark(ticker: str, day: str, memberships: dict[str, Any] | None) -> tuple[str, str | None]:
    """(benchmark symbol, note). The note says when membership could not be determined, or only assumed
    because the day is before the recorded history of the universe that decided it."""
    from .membership import _norm
    sym = _norm(ticker.replace("NSE_", "").replace("BSE_", ""))
    unknown = assumed = False
    for key, fund in BENCHMARK_UNIVERSES:
        m = (memberships or {}).get(key)
        if m is None:
            unknown = True
            continue
        try:
            before = day < m.known_since
            if sym in m.members_on(day):
                return fund, ASSUMED_NOTE if before else None
            assumed = assumed or before
        except Exception:  # noqa: BLE001
            unknown = True
    if unknown:
        return NIFTY_FUND, "membership unknown"
    return NIFTY_FUND, ASSUMED_NOTE if assumed else None


def _aggregate(rows: list[dict[str, Any]], horizons: tuple[int, ...]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for action in ("buy", "sell", "watch", "hold"):
        per_h = {}
        for h in horizons:
            ex = [r["excess"][str(h)] for r in rows if r["action"] == action and r["excess"].get(str(h)) is not None]
            if not ex:
                continue
            item: dict[str, Any] = {"n": len(ex), "mean_excess": statistics.fmean(ex),
                                    "median_excess": statistics.median(ex)}
            calls = [r["correct"][str(h)] for r in rows if r["action"] == action
                     and r["correct"].get(str(h)) is not None]
            if calls:
                item["hit_rate"] = sum(calls) / len(calls)
            per_h[str(h)] = item
        if per_h:
            out[action] = per_h
    return out


def score_recommendations(recs: Iterable[dict[str, Any]], prices: Any, *, horizons: Iterable[int] = HORIZONS,
                          benchmark: str = "^NSEI", cost_model: Any | None = None,
                          default_notional: float = 25_000.0,
                          memberships: dict[str, Any] | None = None) -> dict[str, Any]:
    """Score each call. With `memberships` (see build_memberships) every call is scored against
    its own index fund and `benchmark` is only the fallback for the NIFTYBEES reference;
    without it every call is scored against the single `benchmark` (the US market)."""
    horizons = tuple(horizons)
    per_universe = memberships is not None
    nifty_symbol = NIFTY_FUND if per_universe else benchmark
    histories: dict[str, Any] = {}

    def hist(sym: str) -> tuple[list[dict[str, Any]], list[str]]:
        if sym not in histories:  # every benchmark history is fetched once per call
            bars = prices.history(sym, "2y")
            histories[sym] = (bars, [b["date"] for b in bars])
        return histories[sym]

    cache: dict[str, Any] = {}
    rows: list[dict[str, Any]] = []
    for i, r in enumerate(recs):
        ticker = str(r.get("ticker") or "").upper()
        action = str(r.get("action") or "").lower()
        if not ticker or ticker == "PORTFOLIO" or action not in {"buy", "sell", "watch", "hold"}:
            continue
        day = str(r.get("at") or "")[:10]
        row: dict[str, Any] = {"index": i, "at": r.get("at"), "ticker": ticker, "action": action,
                               "confidence": r.get("confidence"), "headline": r.get("headline"),
                               "investor": r.get("investor") or "",
                               "dismissed": bool(r.get("dismissed")), "returns": {}, "excess": {},
                               "excess_nifty": {}, "correct": {}, "error": None, "benchmark": benchmark}
        try:
            bench_sym = benchmark
            if per_universe:
                bench_sym, note = pick_benchmark(ticker, day, memberships)
                if note:
                    row["benchmark_note"] = note
            row["benchmark"] = bench_sym
            if ticker not in cache:
                cache[ticker] = prices.history(ticker, "2y")
            bars = cache[ticker]
            entry_date, entry_price, rets = _forward(bars, [b["date"] for b in bars], day, horizons)
            try:
                bench, bench_dates = hist(bench_sym)
                if bench_sym != nifty_symbol and bench and bench[0]["date"] > day:
                    raise LookupError(f"{bench_sym} history starts {bench[0]['date']}, after the recommendation")
            except Exception as e:  # noqa: BLE001 - e.g. a fund with no history yet: fall back, say so
                if bench_sym == nifty_symbol:
                    raise
                why = str(e) if isinstance(e, LookupError) else f"{bench_sym} history unavailable"
                row["benchmark_note"] = f"{why}; scored against {nifty_symbol}"
                bench_sym = row["benchmark"] = nifty_symbol
                bench, bench_dates = hist(bench_sym)
            _, _, b_rets = _forward(bench, bench_dates, day, horizons)
            if not per_universe:
                n_rets = {}      # a single-benchmark run has no NIFTYBEES reference
            elif bench_sym == nifty_symbol:
                n_rets = b_rets
            else:
                try:
                    nb, nd = hist(nifty_symbol)
                    _, _, n_rets = _forward(nb, nd, day, horizons)
                except Exception:  # noqa: BLE001
                    n_rets = {}
            row["entry_date"], row["entry_price"] = entry_date, entry_price
            notional = float(r.get("suggested_notional_usd") or 0) or default_notional
            cost = (cost_model.round_trip_bps(notional) / 10_000) if cost_model is not None else 0.0
            row["cost"] = cost
            for h in horizons:
                ret, b, nb_ret = rets.get(h), b_rets.get(h), n_rets.get(h)
                row["returns"][str(h)] = ret
                ex = (ret - b) if ret is not None and b is not None else None
                row["excess"][str(h)] = ex
                row["excess_nifty"][str(h)] = (ret - nb_ret) if ret is not None and nb_ret is not None else None
                if ex is None or action in {"watch", "hold"}:
                    row["correct"][str(h)] = None
                elif action == "buy":
                    row["correct"][str(h)] = ex - cost > 0
                else:
                    row["correct"][str(h)] = ex < 0
            if entry_date is None:
                row["error"] = "no trading day after the recommendation yet"
        except Exception as e:  # noqa: BLE001
            row["error"] = f"{type(e).__name__}: {e}"
        rows.append(row)

    summary: dict[str, Any] = {"recommendations": len(rows), "horizons": list(horizons),
                               "benchmark": "own index fund" if per_universe else benchmark,
                               "per_universe": per_universe}
    summary["by_action"] = _aggregate(rows, horizons)
    by_bench: dict[str, Any] = {}
    for b in sorted({r["benchmark"] for r in rows}):
        agg = _aggregate([r for r in rows if r["benchmark"] == b], horizons)
        if agg:
            by_bench[b] = agg
    summary["by_benchmark"] = by_bench
    summary["pending"] = sum(1 for r in rows if all(v is None for v in r["excess"].values()))
    warns: list[str] = []
    for m in (memberships or {}).values():
        for w in getattr(m, "warnings", None) or []:
            if w not in warns:
                warns.append(w)
    summary["membership_warnings"] = warns
    summary["assumed_membership"] = sum(1 for r in rows if r.get("benchmark_note") == ASSUMED_NOTE)
    summary["unknown_membership"] = sum(1 for r in rows if r.get("benchmark_note") == "membership unknown")
    return {"summary": summary, "rows": rows}


def format_scorecard(result: dict[str, Any]) -> str:
    s = result["summary"]
    if s.get("per_universe"):
        vs = (f"vs its index fund ({MID_FUND} for mid caps, {SMALL_FUND} for small caps, "
              f"{NIFTY_FUND} for the rest)")
    else:
        vs = f"vs {s['benchmark']}"
    lines = [f"Scorecard: {s['recommendations']} recommendations {vs}, {s['pending']} too recent to score"]
    if not s["by_action"]:
        lines.append("  Nothing to score yet. Recommendations need at least 5 trading days of history.")

    def block(by_action: dict[str, Any], indent: str) -> None:
        for action, per_h in by_action.items():
            lines.append(f"{indent}{action}:")
            for h, r in per_h.items():
                hit = f"  right {r['hit_rate']*100:4.0f}%" if "hit_rate" in r else ""
                lines.append(f"{indent}  {h:>3}d  n={r['n']:<4} mean excess {r['mean_excess']*100:+6.2f}%  "
                             f"median {r['median_excess']*100:+6.2f}%{hit}")

    block(s["by_action"], "  ")
    if s.get("per_universe") and len(s.get("by_benchmark", {})) > 0:
        for b, by_action in s["by_benchmark"].items():
            lines.append(f"  vs {b} ({GROUP_LABEL.get(b, b)}):")
            block(by_action, "    ")
    if s.get("unknown_membership"):
        lines.append(f"  {s['unknown_membership']} call(s) scored against {NIFTY_FUND}: index membership unknown.")
    if s.get("assumed_membership"):
        lines.append(f"  {s['assumed_membership']} call(s): {ASSUMED_NOTE}.")
    for w in s.get("membership_warnings") or []:
        lines.append(f"  Note: {w}")
    return "\n".join(lines)
