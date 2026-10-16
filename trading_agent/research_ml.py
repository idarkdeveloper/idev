"""Research: triple-barrier labels, purged cross-validation, meta-labeling and the prediction model.

Everything here runs on the daily simulator's ``Market`` (research_sim), month start by month start, with only data
known on each day's close; nothing from the sealed holdout is read unless a one-time holdout run is asked for.

Triple-barrier label (``barrier_exit``)
    Entry at the close after the signal day. The trade ends at the first of: the profit target (entry + ``target_r``
    x R, with R = 2.5 x ATR(14) at entry), the chosen stop rule (the same code as the simulator), or the time limit
    (``max_days`` trading days). Its result counts after a round trip of charges and slippage and after what the
    money would have earned elsewhere: the liquid fund (signal study) or the index fund (meta-labeling: a trade is a
    success only if it beat the index fund after charges).

Purged cross-validation (``cpcv_splits``)
    The months are cut into 6 blocks; every pair of blocks is a test set (15 splits). Training samples whose label
    span touches a test block are dropped (purged), and so are those starting within ``embargo`` trading days after
    one (embargo). Each block is tested in 5 splits, so the out-of-sample predictions form 5 complete paths: 5 account
    curves instead of one lucky line. Samples are weighted by uniqueness (the average of 1 / number of labels alive on
    each day of their span), because overlapping labels are not independent.

Models
    * the signal-lab model: ridge regression on all price signals, ranking every member; the top fifth is held
      (``cpcv_signal_model``);
    * meta-labeling: a small logistic model that only SIZES the momentum screen's picks (0 to full size), never picks
      direction; fitted three ways on the same splits: plain (log loss), fee-penalised asymmetric (bigger moves and
      losers that also pay fees weigh more), and a direct Sharpe surrogate (the monthly Sharpe of the sized result,
      maximised by gradient ascent). Every variant counts as a trial for the deflated Sharpe.
    * the prediction model is the same sizing model with the earnings-surprise and promoter-buying features added and
      the winning stop rule's labels; it must beat the best simple rule after charges, then gets ONE sealed-holdout run.
"""

from __future__ import annotations

import itertools
import math
import statistics
from bisect import bisect_right
from typing import Any, Callable

from .research_sim import Market, Position, stop_triggered

BLOCKS, TEST_BLOCKS, EMBARGO = 6, 2, 21
PRICE_FEATURES = ("momentum_12_1", "return_6m", "trend_200dma", "golden_cross", "macd", "near_52w_high",
                  "reversal_1m", "rsi_oversold", "bollinger_reversion", "low_volatility", "volume_surge")
EVENT_FEATURES = ("sue", "dsue", "margin_x_sales", "promo_x_mom", "promo_quiet")
OBJECTIVES = ("plain", "fee_asymmetric", "sharpe")


# --------------------------------------------------------------------------- #
# labels
# --------------------------------------------------------------------------- #
def barrier_exit(m: Market, sym: str, t_in: int, rule: str, *, target_r: float = 3.0,
                 max_days: int = 60) -> dict[str, Any] | None:
    """The triple-barrier exit of a trade entered at day t_in's close: {t_out, ret, reason, days}."""
    close, adj = m.close[sym], m.adj[sym]
    c0 = close[t_in] if t_in < len(m.dates) else None
    a_in = adj[t_in] if t_in < len(m.dates) else None
    if c0 is None or not a_in:
        return None
    a = m.atr(sym)[t_in]
    p = Position(value=1.0, cost=1.0, entry_t=t_in, entry_px=float(c0), entry_atr=a, high=float(c0),
                 bought=m.dates[t_in])
    target = p.entry_px + target_r * 2.5 * a if a else None
    last = min(t_in + max_days, len(m.dates) - 1)
    t_out, reason = last, "time"
    for t in range(t_in + 1, last + 1):
        c = close[t]
        if c is None:
            continue
        if target is not None and c >= target:
            t_out, reason = t, "target"
            break
        if stop_triggered(m, sym, p, t, rule):
            t_out, reason = t, "stop"
            break
    a1 = adj[t_out]
    if not a1:
        return None
    return {"t_in": t_in, "t_out": t_out, "ret": a1 / a_in - 1, "reason": reason, "days": t_out - t_in}


def fund_return(m: Market, t0: int, t1: int) -> float:
    a0, a1 = m.fund_adj[t0], m.fund_adj[t1]
    return a1 / a0 - 1 if a0 and a1 else 0.0


def liquid_return(m: Market, t0: int, t1: int, rate: float = 0.065) -> float:
    from datetime import date
    d = (date.fromisoformat(m.dates[t1]) - date.fromisoformat(m.dates[t0])).days
    return (1 + rate) ** (d / 365.0) - 1


# --------------------------------------------------------------------------- #
# features
# --------------------------------------------------------------------------- #
class Features:
    """Price signals (signal_lab.signal_series) per symbol, and the cross-sectional z-scores on a day."""

    def __init__(self, m: Market, ev: Any | None = None):
        self.m, self.ev = m, ev
        self._series: dict[str, dict[str, list[float | None]]] = {}
        self._z: dict[int, dict[str, dict[str, float]]] = {}

    def raw(self, sym: str, t: int) -> dict[str, float | None]:
        from .signal_lab import signal_series
        bars = self.m.raw.get(sym)
        if not bars:
            return {}
        if sym not in self._series:
            self._series[sym] = signal_series(bars)
        i = bisect_right(self.m.raw_dates[sym], self.m.dates[t]) - 1
        if i < 0:
            return {}
        return {k: self._series[sym][k][i] for k in PRICE_FEATURES}

    def z(self, t: int) -> dict[str, dict[str, float]]:
        """{symbol: {feature: z}} over that day's members; a missing value is 0 (the cross-sectional mean)."""
        if t in self._z:
            return self._z[t]
        syms = sorted(s for s in self.m.members_on(self.m.dates[t]) if s in self.m.raw)
        rows = {s: self.raw(s, t) for s in syms}
        if self.ev is not None:
            sig = self.ev.signals(t)
            for s in syms:
                rows[s].update({k: (sig.get(s) or {}).get(k) for k in EVENT_FEATURES})
        names = list(PRICE_FEATURES) + (list(EVENT_FEATURES) if self.ev is not None else [])
        out: dict[str, dict[str, float]] = {s: {} for s in syms}
        for k in names:
            vals = [rows[s].get(k) for s in syms]
            have = [v for v in vals if v is not None]
            mu = statistics.fmean(have) if have else 0.0
            sd = statistics.pstdev(have) if len(have) > 1 else 0.0
            for s, v in zip(syms, vals):
                out[s][k] = 0.0 if v is None or sd == 0 else max(-4.0, min(4.0, (v - mu) / sd))
        self._z[t] = out
        return out

    def names(self) -> list[str]:
        return list(PRICE_FEATURES) + (list(EVENT_FEATURES) if self.ev is not None else [])


# --------------------------------------------------------------------------- #
# the triple-barrier signal study (signal lab, fourth horizon)
# --------------------------------------------------------------------------- #
def barrier_study(m: Market, start: int, end: int, *, rule: str, cost: float, target_r: float = 3.0,
                  max_days: int = 60, liquid_rate: float = 0.065) -> dict[str, Any]:
    """Each price signal's top fifth at every month start, exited by the triple barrier; the average result per
    trade after charges, slippage and the liquid fund, its t-statistic over months (plain, and Newey-West for the
    months whose trades overlap; the verdict uses the latter), and the exits by reason."""
    from .signal_lab import _verdict
    feats = Features(m)
    out: dict[str, Any] = {}
    month_starts = [t for t in m.rebalance_days(start, end) if t + 1 < end]
    per: dict[str, list[float]] = {k: [] for k in (*PRICE_FEATURES, "composite")}
    trades: dict[str, list[dict[str, Any]]] = {k: [] for k in per}
    for t in month_starts:
        z = feats.z(t)
        scores = {k: {s: z[s][k] for s in z} for k in PRICE_FEATURES}
        scores["composite"] = {s: z[s]["momentum_12_1"] + 0.5 * z[s]["return_6m"] + 0.5 * z[s]["low_volatility"]
                               for s in z}
        for k, sc in scores.items():
            need = ("momentum_12_1", "return_6m") if k == "composite" else (k,)
            ranked = sorted((s for s in sc if all(feats.raw(s, t).get(f) is not None for f in need)),
                            key=lambda s: sc[s], reverse=True)
            top = ranked[:max(1, len(ranked) // 5)]
            nets = []
            for s in top:
                ex = barrier_exit(m, s, t + 1, rule, target_r=target_r, max_days=max_days)
                if ex is None:
                    continue
                net = ex["ret"] - cost - liquid_return(m, ex["t_in"], ex["t_out"], liquid_rate)
                nets.append(net)
                trades[k].append({**ex, "net": net})
            if nets:
                per[k].append(statistics.fmean(nets))
    from .validation import newey_west_t
    lags = math.ceil(max_days / 21)   # a trade lasts up to this many months, so neighbouring months overlap
    for k, months in per.items():
        mean = statistics.fmean(months) if months else None
        sd = statistics.pstdev(months) if len(months) > 2 else 0.0
        tstat = mean / (sd / math.sqrt(len(months))) if mean is not None and sd > 0 else None
        t_nw = newey_west_t(months, lags) if len(months) > 2 else None
        tr = trades[k]
        out[k] = {"months": len(months), "mean_net": mean, "t": tstat, "t_overlap": t_nw, "overlap_lags": lags,
                  "hit_rate": sum(x["net"] > 0 for x in tr) / len(tr) if tr else None,
                  "avg_days": statistics.fmean(x["days"] for x in tr) if tr else None,
                  "exits": {r: sum(x["reason"] == r for x in tr) for r in ("target", "stop", "time")},
                  # judged on the overlap-adjusted t: the plain t treats overlapping months as independent
                  "verdict": _verdict(t_nw, mean, 0.0) if months else "no data"}
    return out


# --------------------------------------------------------------------------- #
# samples, overlap weights and purged cross-validation
# --------------------------------------------------------------------------- #
def uniqueness(samples: list[dict[str, Any]]) -> list[float]:
    """Average over each label's days of 1 / number of labels alive that day."""
    if not samples:
        return []
    lo, hi = min(s["t_in"] for s in samples), max(s["t_out"] for s in samples)
    alive = [0] * (hi - lo + 1)
    for s in samples:
        for d in range(s["t_in"], s["t_out"] + 1):
            alive[d - lo] += 1
    return [statistics.fmean(1 / alive[d - lo] for d in range(s["t_in"], s["t_out"] + 1)) for s in samples]


def cpcv_splits(samples: list[dict[str, Any]], *, blocks: int = BLOCKS, test_blocks: int = TEST_BLOCKS,
                embargo: int = EMBARGO) -> tuple[list[dict[str, Any]], list[list[int]]]:
    """The splits ({test_blocks, train, test} as sample indices) and each sample month's block number."""
    months = sorted({s["month"] for s in samples})
    size = math.ceil(len(months) / blocks)
    block_of = {mth: min(i // size, blocks - 1) for i, mth in enumerate(months)}
    spans: dict[int, tuple[int, int]] = {}
    for s in samples:
        b = block_of[s["month"]]
        lo, hi = spans.get(b, (s["t_in"], s["t_out"]))
        spans[b] = (min(lo, s["t_in"]), max(hi, s["t_out"]))
    splits = []
    for combo in itertools.combinations(range(blocks), test_blocks):
        test = [i for i, s in enumerate(samples) if block_of[s["month"]] in combo]
        train = []
        for i, s in enumerate(samples):
            if block_of[s["month"]] in combo:
                continue
            bad = False
            for b in combo:
                lo, hi = spans[b]
                if s["t_in"] <= hi and s["t_out"] >= lo:                 # purge: the label overlaps a test block
                    bad = True
                if hi < s["t_in"] <= hi + embargo:                       # embargo after a test block
                    bad = True
            if not bad:
                train.append(i)
        splits.append({"test_blocks": combo, "train": train, "test": test})
    by_block = [[i for i, s in enumerate(samples) if block_of[s["month"]] == b] for b in range(blocks)]
    return splits, by_block


def paths(splits: list[dict[str, Any]], blocks: int = BLOCKS) -> list[list[int]]:
    """For each path, the split index that supplies each block's predictions (path k = the k-th split testing it)."""
    per_block = {b: [i for i, sp in enumerate(splits) if b in sp["test_blocks"]] for b in range(blocks)}
    n_paths = min(len(v) for v in per_block.values())
    return [[per_block[b][k] for b in range(blocks)] for k in range(n_paths)]


# --------------------------------------------------------------------------- #
# models (stdlib: small and few features)
# --------------------------------------------------------------------------- #
def _sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-max(-30.0, min(30.0, x))))


def _design(X: list[list[float]]) -> Any:
    import numpy as np
    a = np.asarray(X, dtype=float)
    return np.hstack([np.ones((a.shape[0], 1)), a]) if a.size else np.ones((0, 1))


def fit_logistic(X: list[list[float]], y: list[int], w: list[float], *, l2: float = 1.0, steps: int = 400,
                 lr: float = 0.5) -> list[float]:
    """Weighted logistic regression (bias first, L2 on the slopes), gradient descent (numpy; matplotlib's dependency)."""
    import numpy as np
    if not X:
        return [0.0]
    A, yy, ww = _design(X), np.asarray(y, dtype=float), np.asarray(w, dtype=float)
    tw = ww.sum() or 1.0
    beta = np.zeros(A.shape[1])
    pen = np.full(A.shape[1], l2)
    pen[0] = 0.0
    for _ in range(steps):
        p = 1 / (1 + np.exp(-np.clip(A @ beta, -30, 30)))
        beta -= lr * ((A.T @ (ww * (p - yy))) / tw + pen * beta / tw)
    return [float(b) for b in beta]


def predict(beta: list[float], X: list[list[float]]) -> list[float]:
    import numpy as np
    if not X:
        return []
    z = _design(X) @ np.asarray(beta, dtype=float)
    return [float(v) for v in 1 / (1 + np.exp(-np.clip(z, -30, 30)))]


def monthly_sized(samples: list[dict[str, Any]], sizes: list[float]) -> list[float]:
    """Per month, the average of size x net over that month's picks (money not used counts as 0 above the fund)."""
    by: dict[int, list[float]] = {}
    for s, z in zip(samples, sizes):
        by.setdefault(s["month"], []).append(z * s["net"])
    return [statistics.fmean(v) for _, v in sorted(by.items())]


def _month_sharpe(series: list[float]) -> float:
    if len(series) < 3:
        return 0.0
    sd = statistics.pstdev(series)
    return statistics.fmean(series) / sd if sd > 0 else 0.0


def fit_sharpe(samples: list[dict[str, Any]], start: list[float], *, steps: int = 60, lr: float = 0.2,
               eps: float = 1e-3) -> list[float]:
    """Maximise the monthly Sharpe of the sized result by finite-difference gradient ascent (few features)."""
    import numpy as np
    beta = list(start)
    A = _design([s["x"] for s in samples])
    nets = np.asarray([s["net"] for s in samples])
    months = np.asarray([s["month"] for s in samples])
    uniq, inv = np.unique(months, return_inverse=True)
    counts = np.bincount(inv)

    def score(b: list[float]) -> float:
        sizes = 1 / (1 + np.exp(-np.clip(A @ np.asarray(b), -30, 30)))
        monthly = np.bincount(inv, weights=sizes * nets) / counts
        sd = monthly.std()
        return float((monthly.mean() / sd if sd > 0 else 0.0) - 0.01 * sum(v * v for v in b[1:]))
    for _ in range(steps):
        base = score(beta)
        grad = []
        for j in range(len(beta)):
            b2 = list(beta)
            b2[j] += eps
            grad.append((score(b2) - base) / eps)
        beta = [b + lr * g for b, g in zip(beta, grad)]
    return beta


def fit(objective: str, train: list[dict[str, Any]], weights: list[float], cost: float) -> list[float]:
    X, y = [s["x"] for s in train], [int(s["net"] > 0) for s in train]
    if objective == "plain":
        return fit_logistic(X, y, weights)
    if objective == "fee_asymmetric":
        w2 = [wi * (abs(s["net"]) + (cost if s["net"] <= 0 else 0.0)) for wi, s in zip(weights, train)]
        return fit_logistic(X, y, w2)
    if objective == "sharpe":
        return fit_sharpe(train, fit_logistic(X, y, weights))
    raise ValueError(objective)


# --------------------------------------------------------------------------- #
# meta-labeling dataset and evaluation
# --------------------------------------------------------------------------- #
def meta_samples(m: Market, start: int, end: int, *, rule: str, cost: float, ev: Any | None = None, top: int = 20,
                 target_r: float = 3.0, max_days: int = 60) -> tuple[list[dict[str, Any]], list[str]]:
    """One sample per momentum pick per month start: features, the triple-barrier result over the index fund."""
    feats = Features(m, ev)
    out = []
    breadth = m.breadth()
    for k, t in enumerate(m.rebalance_days(start, end)):
        if t + 1 >= end:
            continue
        z = feats.z(t)
        c, e50 = m.fund_close[t], m.fund_ema50[t]
        bt = breadth[t]
        regime = [1.0 if c is not None and e50 is not None and c >= e50 else 0.0,
                  (bt - 0.5) * 4 if bt is not None else 0.0]
        for rank, s in enumerate(m.momentum_ranking(t)[:top]):
            ex = barrier_exit(m, s, t + 1, rule, target_r=target_r, max_days=min(max_days, end - t - 1))
            if ex is None or s not in z:
                continue
            net = ex["ret"] - cost - fund_return(m, ex["t_in"], ex["t_out"])
            out.append({"month": k, "t": t, "t_in": ex["t_in"], "t_out": ex["t_out"], "symbol": s, "rank": rank + 1,
                        "x": [z[s][f] for f in feats.names()] + regime, "net": net, "reason": ex["reason"]})
    return out, feats.names() + ["regime_above_ema50", "breadth"]


def evaluate_cpcv(samples: list[dict[str, Any]], *, cost: float, objectives: tuple[str, ...] = OBJECTIVES,
                  progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Fit every objective on the 15 purged splits; the 5 out-of-sample paths' monthly sized results per objective,
    the simple rules on the same months, and the plain model's calibration."""
    splits, by_block = cpcv_splits(samples)
    route = paths(splits)
    weights = uniqueness(samples)
    preds: dict[str, list[list[float]]] = {}
    for obj in objectives:
        per_split = []
        for i, sp in enumerate(splits):
            if progress:
                progress(f"{obj}: split {i + 1} of {len(splits)}")
            train = [samples[j] for j in sp["train"]]
            beta = fit(obj, train, [weights[j] for j in sp["train"]], cost) if train else [0.0]
            pred = dict(zip(sp["test"], predict(beta, [samples[j]["x"] for j in sp["test"]])))
            per_split.append(pred)
        preds[obj] = [[0.0] * len(samples) for _ in route]
        for k, path in enumerate(route):
            for b, split_idx in enumerate(path):
                for j in by_block[b]:
                    preds[obj][k][j] = per_split[split_idx][j]
    result: dict[str, Any] = {"splits": len(splits), "paths": len(route), "samples": len(samples),
                              "months": len({s["month"] for s in samples}), "objectives": {}}
    for obj in objectives:
        curves = [monthly_sized(samples, p) for p in preds[obj]]
        result["objectives"][obj] = {"paths": curves, "sharpe": [_month_sharpe(c) for c in curves],
                                     "total": [sum(c) for c in curves]}
    full = monthly_sized(samples, [1.0] * len(samples))
    top10 = monthly_sized(samples, [1.0 if s["rank"] <= 10 else 0.0 for s in samples])
    result["simple"] = {"all picks, full size": full, "top 10 of the 20 only": top10}
    result["simple_sharpe"] = {k: _month_sharpe(v) for k, v in result["simple"].items()}
    if "plain" in preds:
        avg = [statistics.fmean(p[j] for p in preds["plain"]) for j in range(len(samples))]
        bins = []
        for b in range(10):
            idx = [j for j, p in enumerate(avg) if b / 10 <= p < (b + 1) / 10 or (b == 9 and p == 1.0)]
            if idx:
                bins.append({"from": b / 10, "to": (b + 1) / 10, "n": len(idx),
                             "predicted": statistics.fmean(avg[j] for j in idx),
                             "actual": statistics.fmean(1.0 if samples[j]["net"] > 0 else 0.0 for j in idx)})
        result["calibration"] = bins
    return result


def verdict(r: dict[str, Any], extra_trials: int = 0) -> dict[str, Any]:
    """Best objective by median path Sharpe vs the best simple rule; the deflated Sharpe counts every variant."""
    from .validation import deflated_sharpe
    meds = {k: statistics.median(v["sharpe"]) for k, v in r["objectives"].items()}
    best = max(meds, key=lambda k: meds[k])
    best_simple = max(r["simple_sharpe"], key=lambda k: r["simple_sharpe"][k])
    k_mid = sorted(range(len(r["objectives"][best]["sharpe"])), key=lambda i: r["objectives"][best]["sharpe"][i])
    median_path = r["objectives"][best]["paths"][k_mid[len(k_mid) // 2]]
    trial_srs = list(meds.values()) + list(r["simple_sharpe"].values())
    dsr = deflated_sharpe(median_path, len(trial_srs) + extra_trials, trial_srs)
    beats = meds[best] > r["simple_sharpe"][best_simple] and min(r["objectives"][best]["sharpe"]) > 0
    return {"best_objective": best, "median_sharpe": meds, "best_simple": best_simple,
            "best_simple_sharpe": r["simple_sharpe"][best_simple], "deflated": dsr,
            "beats_simple": beats, "passes": beats and (dsr["probability"] or 0) >= 0.95}


def fan_chart(r: dict[str, Any], objective: str, path: Any, title: str) -> None:
    """The 5 out-of-sample paths as cumulative sized results, with the simple rules, as a PNG."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(8, 4.5), dpi=110)
    for i, c in enumerate(r["objectives"][objective]["paths"]):
        ax.plot(list(itertools.accumulate(c)), color="#3b6fd8", alpha=0.55, lw=1.4,
                label="out-of-sample paths" if i == 0 else None)
    for (name, c), col in zip(r["simple"].items(), ("#888888", "#c07a2c")):
        ax.plot(list(itertools.accumulate(c)), color=col, lw=1.6, ls="--", label=name)
    ax.axhline(0, color="#999", lw=0.8)
    ax.set_title(title)
    ax.set_xlabel("month")
    ax.set_ylabel("cumulative result above the index fund")
    ax.legend(frameon=False, fontsize=8)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def cpcv_signal_model(m: Market, start: int, end: int, *, cost: float, horizon: int = 20, ridge: float = 5.0,
                      progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    """The signal lab's ridge model under purged cross-validation: each month, every member, label = the next
    ``horizon`` days' return above the index fund; the predicted top fifth is held; 5 out-of-sample paths."""
    from .signal_lab import _solve
    feats = Features(m)
    samples: list[dict[str, Any]] = []
    for k, t in enumerate(m.rebalance_days(start, end)):
        if t + 1 + horizon > end:
            continue
        z = feats.z(t)
        f = fund_return(m, t + 1, t + 1 + horizon)
        for s, row in z.items():
            a0, a1 = m.adj[s][t + 1], m.adj[s][t + 1 + horizon]
            if not a0 or not a1:
                continue
            samples.append({"month": k, "t": t, "t_in": t + 1, "t_out": t + 1 + horizon, "symbol": s,
                            "x": [row[f_] for f_ in PRICE_FEATURES], "y": a1 / a0 - 1 - f})
    splits, by_block = cpcv_splits(samples)
    route = paths(splits)
    w = uniqueness(samples)
    preds = []
    for i, sp in enumerate(splits):
        if progress:
            progress(f"ridge: split {i + 1} of {len(splits)}")
        nf = len(PRICE_FEATURES)
        xtx = [[0.0] * nf for _ in range(nf)]
        xty = [0.0] * nf
        for j in sp["train"]:
            x, y, wj = samples[j]["x"], samples[j]["y"], w[j]
            for a in range(nf):
                xty[a] += wj * x[a] * y
                for b in range(nf):
                    xtx[a][b] += wj * x[a] * x[b]
        beta = _solve([[xtx[a][b] + (ridge if a == b else 0.0) for b in range(nf)] for a in range(nf)], xty)
        preds.append({j: sum(bb * v for bb, v in zip(beta, samples[j]["x"])) for j in sp["test"]})
    curves = []
    for path in route:
        score = {}
        for b, si in enumerate(path):
            for j in by_block[b]:
                score[j] = preds[si][j]
        by_month: dict[int, list[int]] = {}
        for j in score:
            by_month.setdefault(samples[j]["month"], []).append(j)
        curve = []
        for _, js in sorted(by_month.items()):
            js.sort(key=lambda j: score[j], reverse=True)
            top = js[:max(1, len(js) // 5)]
            curve.append(statistics.fmean(samples[j]["y"] for j in top) - cost)
        curves.append(curve)
    return {"paths": curves, "sharpe": [_month_sharpe(c) for c in curves], "total": [sum(c) for c in curves],
            "splits": len(splits), "samples": len(samples), "horizon": horizon}


def calibration_chart(bins: list[dict[str, Any]], path: Any, title: str) -> None:
    """Predicted chance of success against how often it happened, per tenth."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(4.8, 4.5), dpi=110)
    ax.plot([0, 1], [0, 1], color="#999", lw=0.8, ls="--", label="perfectly calibrated")
    ax.plot([b["predicted"] for b in bins], [b["actual"] for b in bins], marker="o", color="#3b6fd8", label="model")
    for b in bins:
        ax.annotate(str(b["n"]), (b["predicted"], b["actual"]), fontsize=7, xytext=(3, 3), textcoords="offset points")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.set_xlabel("predicted chance of beating the fund")
    ax.set_ylabel("how often it did")
    ax.set_title(title)
    ax.legend(frameon=False, fontsize=8)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def fit_final(objective: str, samples: list[dict[str, Any]], cost: float) -> list[float]:
    """The chosen objective fitted on every development sample (for the one holdout run)."""
    return fit(objective, samples, uniqueness(samples), cost)


def format_meta(r: dict[str, Any], v: dict[str, Any], features: list[str]) -> str:
    lines = [f"  {r['samples']} picks over {r['months']} months; {r['splits']} purged splits (6 blocks, 2 tested, "
             f"{EMBARGO}-day embargo), {r['paths']} out-of-sample paths; features: {', '.join(features)}",
             f"  {'sizing':<26}{'median Sharpe':>14}{'worst path':>11}{'best path':>10}{'median total':>13}"]
    for k, x in r["objectives"].items():
        tot = sorted(x["total"])
        lines.append(f"  {k:<26}{statistics.median(x['sharpe']):>14.3f}{min(x['sharpe']):>11.3f}{max(x['sharpe']):>10.3f}"
                     f"{tot[len(tot) // 2] * 100:>+12.1f}%")
    for k, sr in r["simple_sharpe"].items():
        lines.append(f"  {k:<26}{sr:>14.3f}{'':>11}{'':>10}{sum(r['simple'][k]) * 100:>+12.1f}%")
    p = v["deflated"]["probability"]
    lines.append(f"  Best: {v['best_objective']} (median monthly Sharpe {v['median_sharpe'][v['best_objective']]:.3f}) "
                 f"vs the best simple rule '{v['best_simple']}' ({v['best_simple_sharpe']:.3f}); skill odds "
                 + ("n/a" if p is None else f"{p:.2f}") + " counting every variant.")
    lines.append("  Verdict: " + ("PASSES: beats the best simple rule on every path and clears the 0.95 bar."
                                  if v["passes"] else "does not pass (needs to beat the best simple rule with skill "
                                  "odds of 0.95 or more)."))
    if r.get("calibration"):
        cal = ", ".join(f"{b['predicted']:.2f}->{b['actual']:.2f} (n={b['n']})" for b in r["calibration"])
        lines.append(f"  Calibration of the plain model (predicted -> actual): {cal}")
    return "\n".join(lines)
