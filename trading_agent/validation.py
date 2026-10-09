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


def probabilistic_sharpe(sr: float, n_obs: int, skew: float, kurt: float, sr_benchmark: float = 0.0) -> float:
    """Chance the true Sharpe exceeds ``sr_benchmark``, given the sample's length and shape."""
    denom = 1 - skew * sr + (kurt - 1) / 4 * sr * sr
    if n_obs < 2:
        return 0.5
    if denom <= 0:
        return 1.0 if sr > sr_benchmark else 0.0
    return _N.cdf((sr - sr_benchmark) * math.sqrt(n_obs - 1) / math.sqrt(denom))


def expected_max_sharpe(n_trials: int, var_trial_sr: float) -> float:
    """The best Sharpe you would expect from ``n_trials`` pure-luck tries."""
    if n_trials <= 1 or var_trial_sr <= 0:
        return 0.0
    return math.sqrt(var_trial_sr) * ((1 - _GAMMA) * _N.inv_cdf(1 - 1 / n_trials)
                                      + _GAMMA * _N.inv_cdf(1 - 1 / (n_trials * math.e)))


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


def monte_carlo(rets: list[float], n_paths: int = 5000, block: int = 3, seed: int = 7) -> dict[str, Any] | None:
    """Block-bootstrap the period returns: how bad could the fall have been with the same returns reshuffled?"""
    n = len(rets)
    if n < 6:
        return None
    rng = random.Random(seed)
    finals, dds = [], []
    for _ in range(n_paths):
        path: list[float] = []
        while len(path) < n:
            s = rng.randrange(n)
            path.extend(rets[(s + k) % n] for k in range(block))
        v, peak, worst = 1.0, 1.0, 0.0
        for r in path[:n]:
            v *= 1 + r
            peak = max(peak, v)
            worst = min(worst, v / peak - 1)
        finals.append(v - 1)
        dds.append(min(worst, 0.0))
    finals.sort()
    dds.sort()
    rng_of = lambda xs: {"p5": _pick(xs, 0.05), "p50": _pick(xs, 0.5), "p95": _pick(xs, 0.95)}  # noqa: E731
    return {"paths": n_paths, "block": block, "final_return": rng_of(finals), "max_drawdown": rng_of(dds),
            "loss_probability": sum(f < 0 for f in finals) / n_paths}


def walk_forward(runs: dict[int, dict[str, Any] | list[float | None]], dates: list[str],
                 fund_curve: list[float | None] | None) -> dict[str, Any]:
    """Each year, pick the candidate that did best in all earlier years and record how it did this year.

    ``runs`` maps a candidate (stocks held) to its strategy curve over the shared rebalance ``dates``.
    The first year is in-sample only, so it is never reported."""
    tops = sorted(runs)
    first: dict[str, int] = {}
    for i, d in enumerate(dates):
        first.setdefault(d[:4], i)
    years = sorted(first)
    last = len(dates) - 1
    bounds = [(y, first[y], first[years[k + 1]] if k + 1 < len(years) else last) for k, y in enumerate(years)]
    bounds = [b for b in bounds if b[2] > b[1]]

    def ret(curve: list[float | None] | None, a: int, b: int) -> float | None:
        if not curve or curve[a] is None or curve[b] is None or curve[a] <= 0:
            return None
        return curve[b] / curve[a] - 1

    rets = {t: [ret(runs[t], a, b) for _, a, b in bounds] for t in tops}
    out, oos, fund = [], 1.0, 1.0
    have_fund, beat = fund_curve is not None, 0
    for k in range(1, len(bounds)):
        def past(t: int) -> float:
            g = 1.0
            for r in rets[t][:k]:
                g *= 1 + (r or 0.0)
            return g
        chosen = max(tops, key=lambda t: (past(t), t))
        r = rets[chosen][k]
        fr = ret(fund_curve, bounds[k][1], bounds[k][2]) if have_fund else None
        oos *= 1 + (r or 0.0)
        if fr is None:
            have_fund = False
        else:
            fund *= 1 + fr
        if r is not None and fr is not None and r > fr:
            beat += 1
        out.append({"year": int(bounds[k][0]), "chosen_top": chosen, "return": r, "fund_return": fr})
    return {"candidates": tops, "years": out, "oos_return": oos - 1 if out else None,
            "fund_return": fund - 1 if out and have_fund else None, "beat_years": beat}
