"""Signal lab: do common algo-trading signals actually predict Indian stocks?

Every popular rule an algo can follow (momentum, moving-average crosses, RSI, MACD,
Bollinger bands, volume surges, ...) is a bet that some number computed from past
prices tells you which stocks will do better next. This module measures that claim
instead of assuming it:

* On a fixed calendar (every `h` trading days, so periods don't overlap) each signal
  ranks that day's index members, using only data up to that close.
* The position is entered at the *next* close and held `h` days, so short-term signals
  don't get credit for the bid-ask bounce on the signal day.
* Score: rank correlation with the following excess return over NIFTY 50 (the
  information coefficient, IC), its t-statistic, how often it was right, and the gap
  between the top and bottom fifth after a round trip of Indian delivery charges.
* A walk-forward model learns a weighting of all signals from past periods only
  (ridge regression, retrained every period), the simplest honest version of "let the
  machine find the pattern".
* A market-timing test asks whether NIFTY being above its 200-day average predicted the
  index's own next move.

Verdicts: testing a dozen signals at three horizons means about one in twenty will look
significant at t >= 2 by luck alone, so "predictive" needs t >= 3 (the bar Harvey, Liu
and Zhu proposed for newly tested factors) and a top-minus-bottom gap larger than the
trading cost. Between 2 and 3 it is "could be luck"; significant but smaller than the
cost is "too small to trade"; t <= -2 is "reversed"; everything else is "no edge".
"""

from __future__ import annotations

import math
import statistics
from bisect import bisect_right
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from typing import Any, Callable, Iterable

from .membership import Membership

Series = list[float | None]

SIGNALS: dict[str, dict[str, str]] = {
    "momentum_12_1": {"label": "12-1 momentum", "family": "trend",
                      "about": "Return from 12 months to 1 month ago; the classic momentum factor."},
    "return_6m": {"label": "6-month return", "family": "trend", "about": "Return over the last 6 months."},
    "trend_200dma": {"label": "Above 200-day average", "family": "trend",
                     "about": "How far the price is above its 200-day moving average."},
    "golden_cross": {"label": "50/200-day cross", "family": "trend",
                     "about": "50-day average relative to the 200-day; a golden cross is when it turns positive."},
    "macd": {"label": "MACD histogram", "family": "trend",
             "about": "MACD (12, 26) minus its 9-day signal line, as a share of price."},
    "near_52w_high": {"label": "Near 52-week high", "family": "trend",
                      "about": "Closeness to the 52-week high; stocks near highs tend to keep going."},
    "reversal_1m": {"label": "1-month reversal", "family": "mean reversion",
                    "about": "Last month's losers bounce: minus the 1-month return."},
    "rsi_oversold": {"label": "RSI oversold", "family": "mean reversion",
                     "about": "Minus the 14-day RSI, so oversold stocks rank highest."},
    "bollinger_reversion": {"label": "Bollinger reversion", "family": "mean reversion",
                            "about": "Below the lower 20-day band ranks highest."},
    "low_volatility": {"label": "Low volatility", "family": "risk",
                       "about": "Minus 60-day volatility; calmer stocks rank highest."},
    "volume_surge": {"label": "Volume surge", "family": "attention",
                     "about": "20-day average volume relative to the 120-day."},
    "composite": {"label": "Factor screen score", "family": "combined",
                  "about": "The screen's ranking: momentum + half 6-month return − half volatility."},
}
MODEL = "walk_forward_model"
MODEL_INFO = {"label": "Walk-forward model", "family": "machine learning",
              "about": "Ridge regression on all signals, retrained each period on past periods only."}
FEATURES = [k for k in SIGNALS if k != "composite"]


# ----------------------------------------------------------------------------- indicators

def _rolling_mean(x: list[float], n: int) -> Series:
    out: Series = [None] * len(x)
    s = 0.0
    for i, v in enumerate(x):
        s += v
        if i >= n:
            s -= x[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def _rolling_std(x: list[float], n: int) -> Series:
    out: Series = [None] * len(x)
    s = s2 = 0.0
    for i, v in enumerate(x):
        s += v
        s2 += v * v
        if i >= n:
            s -= x[i - n]
            s2 -= x[i - n] ** 2
        if i >= n - 1:
            out[i] = math.sqrt(max(s2 / n - (s / n) ** 2, 0.0))
    return out


def _rolling_max(x: list[float], n: int) -> Series:
    out: Series = [None] * len(x)
    q: deque[int] = deque()
    for i, v in enumerate(x):
        while q and x[q[-1]] <= v:
            q.pop()
        q.append(i)
        if q[0] <= i - n:
            q.popleft()
        if i >= n - 1:
            out[i] = x[q[0]]
    return out


def _ema(x: list[float], n: int) -> list[float]:
    k, out = 2 / (n + 1), []
    for i, v in enumerate(x):
        out.append(v if i == 0 else v * k + out[-1] * (1 - k))
    return out


def rsi(closes: list[float], n: int = 14) -> Series:
    """Wilder's RSI."""
    out: Series = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = [max(closes[i] - closes[i - 1], 0.0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0.0) for i in range(1, len(closes))]
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n
    for i in range(n, len(closes)):
        if i > n:
            ag = (ag * (n - 1) + gains[i - 1]) / n
            al = (al * (n - 1) + losses[i - 1]) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def signal_series(bars: list[dict[str, Any]]) -> dict[str, Series]:
    """Every signal's value at every bar, using data up to that bar's close only."""
    c = [float(b["adj_close"]) for b in bars]
    v = [float(b.get("volume") or 0.0) for b in bars]
    n = len(c)
    lr = [0.0] + [math.log(c[i] / c[i - 1]) if c[i - 1] > 0 and c[i] > 0 else 0.0 for i in range(1, n)]
    ma20, ma50, ma200 = _rolling_mean(c, 20), _rolling_mean(c, 50), _rolling_mean(c, 200)
    sd20, vol60, hi252 = _rolling_std(c, 20), _rolling_std(lr, 60), _rolling_max(c, 252)
    v20, v120 = _rolling_mean(v, 20), _rolling_mean(v, 120)
    macd_line = [a - b for a, b in zip(_ema(c, 12), _ema(c, 26))]
    macd_sig = _ema(macd_line, 9)
    r14 = rsi(c)

    def ret(i: int, a: int, b: int = 0) -> float | None:
        return c[i - b] / c[i - a] - 1 if i >= a and c[i - a] > 0 else None

    out: dict[str, Series] = {k: [None] * n for k in FEATURES}
    for i in range(n):
        out["momentum_12_1"][i] = ret(i, 252, 21)
        out["return_6m"][i] = ret(i, 126)
        out["reversal_1m"][i] = -r if (r := ret(i, 21)) is not None else None
        out["trend_200dma"][i] = c[i] / ma200[i] - 1 if ma200[i] else None
        out["golden_cross"][i] = ma50[i] / ma200[i] - 1 if ma50[i] and ma200[i] else None
        out["macd"][i] = (macd_line[i] - macd_sig[i]) / c[i] if i >= 35 and c[i] else None
        out["near_52w_high"][i] = c[i] / hi252[i] - 1 if hi252[i] else None
        out["rsi_oversold"][i] = -r14[i] if r14[i] is not None else None
        out["bollinger_reversion"][i] = -(c[i] - ma20[i]) / (2 * sd20[i]) if ma20[i] and sd20[i] else None
        out["low_volatility"][i] = -vol60[i] * math.sqrt(252) if vol60[i] is not None and i >= 60 else None
        out["volume_surge"][i] = v20[i] / v120[i] if v20[i] is not None and v120[i] else None
    return out


# ----------------------------------------------------------------------------- statistics

def _ranks(x: list[float]) -> list[float]:
    order = sorted(range(len(x)), key=lambda i: x[i])
    r = [0.0] * len(x)
    i = 0
    while i < len(order):  # average ranks for ties
        j = i
        while j + 1 < len(order) and x[order[j + 1]] == x[order[i]]:
            j += 1
        for k in range(i, j + 1):
            r[order[k]] = (i + j) / 2
        i = j + 1
    return r


def spearman(a: list[float], b: list[float]) -> float | None:
    if len(a) < 3:
        return None
    ra, rb = _ranks(a), _ranks(b)
    ma, mb = statistics.fmean(ra), statistics.fmean(rb)
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    va, vb = sum((x - ma) ** 2 for x in ra), sum((y - mb) ** 2 for y in rb)
    return cov / math.sqrt(va * vb) if va > 0 and vb > 0 else None


def _zscore(vals: list[float | None]) -> list[float]:
    xs = [v for v in vals if v is not None]
    if len(xs) < 2:
        return [0.0] * len(vals)
    mu, sd = statistics.fmean(xs), statistics.pstdev(xs)
    return [max(-3.0, min(3.0, (v - mu) / sd)) if v is not None and sd > 0 else 0.0 for v in vals]


def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting (small, well-conditioned ridge systems)."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        m[col], m[piv] = m[piv], m[col]
        if abs(m[col][col]) < 1e-12:
            continue
        for r in range(n):
            if r != col:
                f = m[r][col] / m[col][col]
                if f:
                    for k in range(col, n + 1):
                        m[r][k] -= f * m[col][k]
    return [m[i][n] / m[i][i] if abs(m[i][i]) > 1e-12 else 0.0 for i in range(n)]


def _verdict(t: float | None, spread: float | None, cost: float) -> str:
    if t is None or spread is None:
        return "not enough data"
    if t >= 2 and spread <= cost:
        return "too small to trade"
    if t >= 3:
        return "predictive"
    if t >= 2:
        return "could be luck"
    if t <= -2:
        return "reversed"
    return "no edge"


def _summarise(ics: list[float], spreads: list[float], top_hits: list[float], ups: list[float],
               base_up: list[float], cost: float, periods_per_year: float) -> dict[str, Any]:
    n = len(ics)
    if n < 3:
        return {"periods": n, "verdict": "not enough data"}
    mic, sd = statistics.fmean(ics), statistics.pstdev(ics)
    t = mic / (sd / math.sqrt(n)) if sd > 0 else 0.0
    spread = statistics.fmean(spreads) if spreads else None
    return {
        "periods": n, "ic": mic, "t_stat": t, "ic_positive_share": sum(i > 0 for i in ics) / n,
        "top_minus_bottom": spread, "top_beats_index_share": statistics.fmean(top_hits) if top_hits else None,
        "top_up_share": statistics.fmean(ups) if ups else None,
        "all_up_share": statistics.fmean(base_up) if base_up else None,
        "net_annual": (spread - cost) * periods_per_year if spread is not None else None,
        "verdict": _verdict(t, spread, cost),
    }


# ----------------------------------------------------------------------------- the lab

def run_signal_lab(universe: Iterable[dict[str, str]], prices: Any, *, horizons: Iterable[int] = (5, 20, 60),
                   years: int = 5, benchmark: str = "NIFTYBEES", membership: Membership | None = None,
                   cost_model: Any | None = None, notional: float = 25_000.0, workers: int = 8,
                   min_names: int = 15, min_train: int = 12, ridge: float = 5.0,
                   progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    current = [m["symbol"] for m in universe]
    rng = "10y" if years > 4 else "5y" if years > 1 else "2y"
    bench = prices.history(benchmark, rng)
    bd = [b["date"] for b in bench]
    if len(bd) < 300:
        raise ValueError("not enough benchmark history for the signal lab")
    start_idx = max(252, len(bd) - years * 252)
    start_day = bd[start_idx]
    syms = sorted(membership.ever_members(start_day)) if membership else current
    in_index = (lambda d: membership.members_on(d)) if membership else (lambda d: set(current))

    def load(s: str) -> tuple[str, list[dict[str, Any]] | None]:
        try:
            return s, prices.history(s, rng)
        except Exception:  # noqa: BLE001
            return s, None

    if progress:
        progress(f"loading price history for {len(syms)} stocks")
    with ThreadPoolExecutor(max_workers=workers) as ex:
        hist = {s: h for s, h in ex.map(load, syms) if h and len(h) > 60}
    dates = {s: [b["date"] for b in h] for s, h in hist.items()}
    closes = {s: [b["adj_close"] for b in h] for s, h in hist.items()}
    sig = {s: signal_series(h) for s, h in hist.items()}
    bclose = [b["adj_close"] for b in bench]

    cost = cost_model.round_trip_bps(notional) / 10_000 if cost_model is not None else 0.0

    def idx_on(s: str, day: str) -> int | None:
        i = bisect_right(dates[s], day) - 1
        return i if i >= 0 and dates[s][i] >= _days_before(day, 7) else None

    results: dict[str, Any] = {}
    model_weights: dict[str, Any] = {}
    for h in horizons:
        sample = list(range(start_idx, len(bd) - h - 1, h))
        per = {k: {"ics": [], "spreads": [], "hits": [], "ups": [], "base": []} for k in [*SIGNALS, MODEL]}
        xtx = [[0.0] * len(FEATURES) for _ in FEATURES]
        xty = [0.0] * len(FEATURES)
        pending: deque[tuple[list[list[float]], list[float]]] = deque()
        trained = 0
        for j in sample:
            d0, d_in, d_out = bd[j], bd[j + 1], bd[j + 1 + h]
            b_ret = bclose[j + 1 + h] / bclose[j + 1] - 1
            names, feats, ex_rets, raw_rets = [], {k: [] for k in FEATURES}, [], []
            for s in sorted(in_index(d0) & set(hist)):
                i0, i1, i2 = idx_on(s, d0), idx_on(s, d_in), idx_on(s, d_out)
                if i0 is None or i1 is None or i2 is None or i2 <= i1 or closes[s][i1] <= 0:
                    continue
                r = closes[s][i2] / closes[s][i1] - 1
                names.append(s)
                ex_rets.append(r - b_ret)
                raw_rets.append(r)
                for k in FEATURES:
                    feats[k].append(sig[s][k][i0])
            if len(names) < min_names:
                continue
            z = {k: _zscore(feats[k]) for k in FEATURES}
            have = {k: [v is not None for v in feats[k]] for k in FEATURES}
            composite = [z["momentum_12_1"][i] + 0.5 * z["return_6m"][i] + 0.5 * z["low_volatility"][i]
                         if have["momentum_12_1"][i] and have["return_6m"][i] else None
                         for i in range(len(names))]
            values: dict[str, list[float | None]] = {k: feats[k] for k in FEATURES}
            values["composite"] = composite
            # walk-forward model: predict with weights learned from periods that have fully ended
            X = [[z[k][i] for k in FEATURES] for i in range(len(names))]
            rank_y = _ranks(ex_rets)
            y = [(r / max(len(rank_y) - 1, 1)) - 0.5 for r in rank_y]
            if trained >= min_train:
                a = [[xtx[r][c] + (ridge if r == c else 0.0) for c in range(len(FEATURES))] for r in range(len(FEATURES))]
                w = _solve(a, xty)
                values[MODEL] = [sum(wi * xi for wi, xi in zip(w, row)) for row in X]
                model_weights[str(h)] = dict(zip(FEATURES, w))
            pending.append((X, y))
            # a period's outcome is known one period later (exit day passes the next signal day)
            while len(pending) > 1:
                Xp, yp = pending.popleft()
                for row, t in zip(Xp, yp):
                    for r_ in range(len(FEATURES)):
                        xty[r_] += row[r_] * t
                        for c_ in range(len(FEATURES)):
                            xtx[r_][c_] += row[r_] * row[c_]
                trained += 1
            base_up = sum(r > 0 for r in raw_rets) / len(raw_rets)
            for k, vals in values.items():
                pairs = [(v, e, r) for v, e, r in zip(vals, ex_rets, raw_rets) if v is not None]
                if len(pairs) < min_names:
                    continue
                ic = spearman([p[0] for p in pairs], [p[1] for p in pairs])
                if ic is None:
                    continue
                pairs.sort(key=lambda p: p[0], reverse=True)
                q = max(len(pairs) // 5, 1)
                top, bot = pairs[:q], pairs[-q:]
                per[k]["ics"].append(ic)
                per[k]["spreads"].append(statistics.fmean(p[1] for p in top) - statistics.fmean(p[1] for p in bot))
                per[k]["hits"].append(sum(p[1] > 0 for p in top) / q)
                per[k]["ups"].append(sum(p[2] > 0 for p in top) / q)
                per[k]["base"].append(base_up)
        ppy = 252 / h
        results[str(h)] = {k: _summarise(v["ics"], v["spreads"], v["hits"], v["ups"], v["base"], cost, ppy)
                           for k, v in per.items()}
        if progress:
            progress(f"{h}-day horizon done")

    timing = _index_timing(bench, horizons, start_idx)
    return {
        "universe_size": len(syms), "with_history": len(hist), "start": start_day, "end": bd[-1],
        "horizons": [int(h) for h in horizons], "benchmark_symbol": benchmark, "round_trip_cost": cost,
        "point_in_time": membership is not None and membership.known_since <= start_day,
        "signals": {**SIGNALS, MODEL: MODEL_INFO},
        "results": results, "model_weights": model_weights, "index_timing": timing,
        "summary": _headline(results, horizons, membership is not None and membership.known_since <= start_day),
        "membership_source": membership.source if membership else None,
        "membership_warnings": membership.warnings if membership else [],
    }


def _days_before(day: str, n: int) -> str:
    return (date.fromisoformat(day) - timedelta(days=n)).isoformat()


def _index_timing(bench: list[dict[str, Any]], horizons: Iterable[int], start_idx: int) -> dict[str, Any]:
    """Does NIFTY above its 200-day average predict the index's own next move?"""
    c = [b["adj_close"] for b in bench]
    ma = _rolling_mean(c, 200)
    out: dict[str, Any] = {}
    for h in horizons:
        on, off = [], []
        for j in range(start_idx, len(c) - h - 1, h):
            if ma[j] is None:
                continue
            r = c[j + 1 + h] / c[j + 1] - 1
            (on if c[j] > ma[j] else off).append(r)
        def st(x: list[float]) -> dict[str, Any]:
            return {"n": len(x), "mean": statistics.fmean(x) if x else None,
                    "up_share": sum(r > 0 for r in x) / len(x) if x else None}
        diff = (statistics.fmean(on) - statistics.fmean(off)) if on and off else None
        se = math.sqrt(statistics.pvariance(on) / len(on) + statistics.pvariance(off) / len(off)) \
            if len(on) > 1 and len(off) > 1 else None
        out[str(h)] = {"above": st(on), "below": st(off), "difference": diff,
                       "t_stat": diff / se if diff is not None and se else None}
    return out


def _headline(results: dict[str, Any], horizons: Iterable[int], point_in_time: bool) -> str:
    def named(verdict: str) -> list[str]:
        return [f"{(SIGNALS.get(k) or MODEL_INFO)['label']} ({h}d)" for h in map(str, horizons)
                for k, r in results[h].items() if r.get("verdict") == verdict]
    good, maybe = named("predictive"), named("could be luck")
    tests = sum(1 for h in map(str, horizons) for r in results[h].values() if r.get("t_stat") is not None)
    bias = "" if point_in_time else " Today's members only, which flatters trend signals."
    if good:
        return f"Predictive after costs: {', '.join(good)}. Test another period before trusting it.{bias}"
    if maybe:
        return (f"Nothing clears the t >= 3 bar. {', '.join(maybe)} passed t >= 2, but about "
                f"{tests / 20:.1f} of {tests} tests would do that by luck alone.{bias}")
    return ("No signal predicted returns well enough to beat trading costs. Ranking stocks on these "
            f"rules was close to a coin toss over this period.{bias}")


def format_signal_lab(r: dict[str, Any]) -> str:
    pct = lambda v: "   n/a" if v is None else f"{v*100:+6.2f}%"  # noqa: E731
    lines = [f"Signal lab: {r['with_history']}/{r['universe_size']} stocks, {r['start']} to {r['end']}"
             + (" (point-in-time members)" if r["point_in_time"] else " (today's members: survivorship-biased)"),
             f"Round-trip charges {r['round_trip_cost']*100:.2f}% per trade; the top-minus-bottom gap must beat that."]
    for h in map(str, r["horizons"]):
        lines.append(f"\nNext {h} trading days{'':<14}{'IC':>7}{'t':>7}{'IC>0':>7}{'top-bot':>9}  verdict")
        rows = sorted(r["results"][h].items(), key=lambda kv: -(kv[1].get("t_stat") or -99))
        for k, s in rows:
            label = (r["signals"].get(k) or {}).get("label", k)
            if s.get("ic") is None:
                lines.append(f"  {label:<30}{'':>30}  {s['verdict']}")
                continue
            lines.append(f"  {label:<30}{s['ic']:+7.3f}{s['t_stat']:+7.2f}{s['ic_positive_share']*100:6.0f}%"
                         f"{pct(s['top_minus_bottom'])}  {s['verdict']}")
        t = r["index_timing"][h]
        if t["difference"] is not None:
            lines.append(f"  NIFTY above 200-day avg: next {h}d {pct(t['above']['mean'])} (n={t['above']['n']}) vs "
                         f"below {pct(t['below']['mean'])} (n={t['below']['n']}), t {t['t_stat']:+.2f}")
    lines.append("\n" + r["summary"])
    return "\n".join(lines)
