"""Is a backtest result skill or luck? Walk-forward testing, the deflated Sharpe ratio and Monte Carlo.

Pure functions on lists of numbers (stdlib only). The deflated Sharpe follows Bailey & Lopez de Prado
(2014): a Sharpe ratio is only impressive after accounting for how many variants were tried.
"""

from __future__ import annotations

import math
import random
import statistics
from statistics import NormalDist
from typing import Any

_N = NormalDist()
_GAMMA = 0.5772156649  # Euler-Mascheroni


def period_returns(curve: list[float | None]) -> list[float]:
    """Simple returns between consecutive non-None, positive values."""
    vals = [v for v in curve if v is not None and v > 0]
    return [vals[i] / vals[i - 1] - 1 for i in range(1, len(vals))]


def sharpe(rets: list[float]) -> float | None:
    """Per-period mean / population stdev (not annualised); None when it cannot be computed."""
    if len(rets) < 3:
        return None
    sd = statistics.pstdev(rets)
    return statistics.fmean(rets) / sd if sd > 0 else None


def moments(rets: list[float]) -> tuple[float, float]:
    """Population skew and (non-excess) kurtosis; a normal distribution is (0, 3)."""
    n = len(rets)
    if n < 2:
        return 0.0, 3.0
    mu, sd = statistics.fmean(rets), statistics.pstdev(rets)
    if sd == 0:
        return 0.0, 3.0
    z = [(r - mu) / sd for r in rets]
    return sum(x ** 3 for x in z) / n, sum(x ** 4 for x in z) / n


def excess_returns(strategy: list[float | None], comparison: list[float | None] | None) -> list[float]:
    """Strategy period return minus the comparison's, on the same dates; periods missing either are skipped."""
    if not comparison:
        return []
    out = []
    for i in range(1, min(len(strategy), len(comparison))):
        a0, a1, b0, b1 = strategy[i - 1], strategy[i], comparison[i - 1], comparison[i]
        if None in (a0, a1, b0, b1) or min(a0, a1, b0, b1) <= 0:
            continue
        out.append(a1 / a0 - b1 / b0)
    return out


def probabilistic_sharpe(sr: float, n_obs: int, skew: float, kurt: float, sr_benchmark: float = 0.0) -> float | None:
    """Chance the true Sharpe exceeds ``sr_benchmark``, given the sample's length and shape.
    None when the formula is undefined (too few observations or a non-positive variance term)."""
    denom = 1 - skew * sr + (kurt - 1) / 4 * sr * sr
    if n_obs < 2 or denom <= 0:
        return None
    return _N.cdf((sr - sr_benchmark) * math.sqrt(n_obs - 1) / math.sqrt(denom))


def expected_max_sharpe(n_trials: int, var_trial_sr: float) -> float:
    """The best Sharpe you would expect from ``n_trials`` pure-luck tries."""
    if n_trials <= 1 or var_trial_sr <= 0:
        return 0.0
    return math.sqrt(var_trial_sr) * ((1 - _GAMMA) * _N.inv_cdf(1 - 1 / n_trials)
                                      + _GAMMA * _N.inv_cdf(1 - 1 / (n_trials * math.e)))


def newey_west_t(values: list[float], lags: int) -> float | None:
    """The t-statistic of the mean with Newey-West (Bartlett) standard errors: for overlapping holding periods, where
    neighbouring observations share days and a plain t overstates the evidence. ``lags`` = 0 gives the plain t."""
    n = len(values)
    if n < 3:
        return None
    mu = statistics.fmean(values)
    dev = [v - mu for v in values]
    var = sum(x * x for x in dev) / n
    for j in range(1, min(lags, n - 1) + 1):
        cov = sum(dev[i] * dev[i - j] for i in range(j, n)) / n
        var += 2 * (1 - j / (lags + 1)) * cov
    if var <= 0:
        return None
    return mu / math.sqrt(var / n)


def pbo(series: dict[str, list[float]], blocks: int = 10) -> dict[str, Any] | None:
    """Probability of backtest overfitting by combinatorially symmetric cross-validation (Bailey, Borwein,
    López de Prado and Zhu): the periods are cut into ``blocks`` equal slices; for every way of picking half of them as
    the in-sample part, the variant with the best in-sample Sharpe is ranked out of sample on the other half. PBO is the
    share of splits where that winner ranks at or below the median out of sample: near 0 means picking the best variant
    works, 0.5 or more means it is no better than chance. None with fewer than 2 variants or too few periods."""
    import itertools
    names = [k for k, v in series.items() if v]
    if len(names) < 2:
        return None
    t = min(len(series[k]) for k in names)
    if blocks < 2 or blocks % 2 or t < blocks * 2:
        return None
    size = t // blocks
    chunks = [range(i * size, (i + 1) * size) for i in range(blocks)]

    def sr(xs: list[float]) -> float:
        sd = statistics.pstdev(xs)
        return statistics.fmean(xs) / sd if sd > 0 else 0.0

    below = total = 0
    logits = []
    for ins in itertools.combinations(range(blocks), blocks // 2):
        in_idx = [i for b in ins for i in chunks[b]]
        out_idx = [i for b in range(blocks) if b not in ins for i in chunks[b]]
        ins_sr = {k: sr([series[k][i] for i in in_idx]) for k in names}
        best = max(names, key=lambda k: ins_sr[k])
        oos = {k: sr([series[k][i] for i in out_idx]) for k in names}
        rank = 1 + sum(1 for k in names if oos[k] < oos[best])      # 1 = worst ... N = best
        w = rank / (len(names) + 1)
        lam = math.log(w / (1 - w))
        logits.append(lam)
        below += lam <= 0
        total += 1
    return {"pbo": below / total, "splits": total, "variants": len(names), "periods": t,
            "median_logit": statistics.median(logits)}


def deflated_sharpe(rets: list[float], n_trials: int, trial_srs: list[float]) -> dict[str, Any]:
    sr = sharpe(rets)
    var = statistics.pvariance(trial_srs) if len(trial_srs) >= 2 else 0.0
    threshold = expected_max_sharpe(n_trials, var)
    prob = None
    if sr is not None:
        skew, kurt = moments(rets)
        prob = probabilistic_sharpe(sr, len(rets), skew, kurt, threshold)
    return {"probability": prob, "sharpe": sr, "threshold": threshold, "trials": n_trials, "periods": len(rets)}


def _pick(sorted_vals: list[float], q: float) -> float:
    return sorted_vals[int(round(q * (len(sorted_vals) - 1)))]


FAN_QUANTILES = (0.05, 0.25, 0.5, 0.75, 0.95)


def _path_stats(path: list[float]) -> tuple[list[float], float]:
    """Growth of 1 at each step (starting at 1.0) and the worst fall from a peak (<= 0)."""
    v, peak, worst, values = 1.0, 1.0, 0.0, [1.0]
    for r in path:
        v *= 1 + r
        peak = max(peak, v)
        worst = min(worst, v / peak - 1)
        values.append(v)
    return values, min(worst, 0.0)


def monte_carlo(rets: list[float], n_paths: int = 5000, block: int = 3, seed: int = 7) -> dict[str, Any] | None:
    """Block-bootstrap the period returns: how bad could the fall have been with the same returns reshuffled?

    Besides the 5/50/95% ranges it returns what the page draws:

    * ``fan``: per step (0 = the start), the 5/25/50/75/95% growth of 1 across the reshuffles, and ``actual``,
      the growth of 1 in the order the returns really came;
    * ``drawdown_hist``: entry k counts the reshuffles whose worst fall was k% to just under (k+1)%, so the share
      that fell L% or more is ``sum(drawdown_hist[L:]) / paths`` for any whole-percent limit L;
    * ``actual_drawdown``: the worst fall of the real order."""
    n = len(rets)
    if n < 6:
        return None
    rng = random.Random(seed)
    finals, dds = [], []
    columns: list[list[float]] = [[] for _ in range(n + 1)]
    for _ in range(n_paths):
        path: list[float] = []
        if block >= n:  # a block as long as the series is the series itself
            path = list(rets)
        while len(path) < n:
            s = rng.randrange(n)
            path.extend(rets[(s + k) % n] for k in range(block))
        values, worst = _path_stats(path[:n])
        for k, x in enumerate(values):
            columns[k].append(x)
        finals.append(values[-1] - 1)
        dds.append(worst)
    hist = [0] * (int(-min(dds) * 100 + 1e-9) + 1)
    for d in dds:
        hist[min(len(hist) - 1, int(-d * 100 + 1e-9))] += 1
    fan = {f"p{round(q * 100)}": [] for q in FAN_QUANTILES}
    for col in columns:
        col.sort()
        for q in FAN_QUANTILES:
            fan[f"p{round(q * 100)}"].append(round(_pick(col, q), 5))
    actual, actual_dd = _path_stats(list(rets))
    fan["actual"] = [round(x, 5) for x in actual]
    finals.sort()
    dds.sort()
    rng_of = lambda xs: {"p5": _pick(xs, 0.05), "p50": _pick(xs, 0.5), "p95": _pick(xs, 0.95)}  # noqa: E731
    return {"paths": n_paths, "block": block, "final_return": rng_of(finals), "max_drawdown": rng_of(dds),
            "loss_probability": sum(f < 0 for f in finals) / n_paths,
            "fan": fan, "drawdown_hist": hist, "actual_drawdown": actual_dd}


def breach_share(mc: dict[str, Any], limit_pct: int) -> float | None:
    """Share of the reshuffles whose worst fall was ``limit_pct``% or more (None for an old result)."""
    hist = mc.get("drawdown_hist")
    if not hist or not mc.get("paths"):
        return None
    return sum(hist[max(0, int(limit_pct)):]) / mc["paths"]


def _year_bounds(dates: list[str]) -> list[list[Any]]:
    """[label, start index, end index, partial] per calendar year; a stub first year (under 6 months)
    is folded into the next year."""
    first: dict[str, int] = {}
    for i, d in enumerate(dates):
        first.setdefault(d[:4], i)
    years = sorted(first)
    last = len(dates) - 1
    bounds = [[y, first[y], first[years[k + 1]] if k + 1 < len(years) else last, k + 1 == len(years)]
              for k, y in enumerate(years)]
    bounds = [b for b in bounds if b[2] > b[1]]
    if len(bounds) > 1 and 12 - int(dates[bounds[0][1]][5:7]) + 1 < 6:
        bounds[1][1] = bounds[0][1]
        bounds = bounds[1:]
    for b in bounds:
        # the last year is partial unless the data runs into the next year (Jan to Dec alone is 11 months)
        b[3] = b[3] and dates[b[2]][:4] == dates[b[1]][:4]
    return bounds


def walk_forward(runs: dict[int, list[float | None]], dates: list[str],
                 fund_curve: list[float | None] | None) -> dict[str, Any]:
    """Each year, pick the candidate that did best in all earlier years and record how it did this year.

    ``runs`` maps a candidate (stocks held) to its strategy curve over the shared rebalance ``dates``.
    The first year is in-sample only, so it is never reported. A year with no comparison return (or no
    usable candidate) is marked ``missing`` and left out of both totals."""
    tops = sorted(runs)
    bounds = _year_bounds(dates)

    def ret(curve: list[float | None] | None, a: int, b: int) -> float | None:
        if not curve or curve[a] is None or curve[b] is None or curve[a] <= 0:
            return None
        return curve[b] / curve[a] - 1

    rets = {t: [ret(runs[t], b[1], b[2]) for b in bounds] for t in tops}
    out, oos, fund = [], 1.0, 1.0
    beat = used = full = 0
    for k in range(1, len(bounds)):
        usable = [t for t in tops if all(r is not None for r in rets[t][:k + 1])]
        chosen = None
        if usable:
            chosen = max(usable, key=lambda t: (math.prod(1 + r for r in rets[t][:k]), t))
        r = rets[chosen][k] if chosen is not None else None
        fr = ret(fund_curve, bounds[k][1], bounds[k][2]) if fund_curve is not None else None
        missing = r is None or (fund_curve is not None and fr is None)
        out.append({"year": int(bounds[k][0]), "chosen_top": chosen, "return": r, "fund_return": fr,
                    "missing": missing, "partial": bool(bounds[k][3])})
        if missing:
            continue
        used += 1
        full += 0 if bounds[k][3] else 1
        oos *= 1 + r
        if fr is not None:
            fund *= 1 + fr
            beat += r > fr
    return {"candidates": tops, "years": out, "oos_return": oos - 1 if used else None,
            "fund_return": fund - 1 if used and fund_curve is not None else None,
            "beat_years": beat, "full_years": full, "used_years": used}
