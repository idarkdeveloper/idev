"""Leakage test: does any research decision use data from after the day it was made?

The test cuts the market at a decision day d (every price bar, the index fund and the FII/DII flows after d are
removed, the calendar ends at d) and recomputes what the simulator decided on d from the cut market. If the
answer differs, or reading it fails because the data is not there, that decision used later data. Checked on
every sampled decision day:

* the screen's momentum ranking and scores (what the rebalance buys and sells);
* every indicator a rule reads on that day: ATR, the 50-day average and 60-day volatility of each member,
  the fund's 50/200-day averages and 100-day average, breadth, the regime label and the traded value;
* the adopted rules end to end, same-close and next-day fills: run to one session after the decision on d is
  traded, then cut there; the equity curve, every fill and every trade must be identical on the cut market.

Two static checks: no price bar on or after the sealed holdout's first day, and index membership taken at each
date (point in time) rather than today's list. A back-adjustment check rescales every stock's adjusted closes
by a constant (what a later dividend or split does to Yahoo's adjusted series) and requires the same ranking.

Event signals (results, insider trades) are not covered here: they have their own as-of rule (filed at or after
15:00 IST counts from the next session) and none is in the adopted rules.
"""
from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

from .research_sim import Market, Rules, simulate, with_rules

RankFn = Callable[[Market, int], list[str]]


class LeakageError(RuntimeError):
    """A research run would use data published after its decisions."""


def truncate(m: Market, cut: int, *, rescale: dict[str, float] | None = None) -> Market:
    """The market as it was known at the close of day ``cut``: nothing dated later survives. ``rescale`` multiplies a
    stock's adjusted closes by a constant (a later dividend's back-adjustment)."""
    day = m.dates[cut]
    series = {}
    for sym, bars in m.raw.items():
        kept = [b for b in bars if b["date"] <= day]
        if rescale and sym in rescale:
            k = rescale[sym]
            kept = [{**b, "adj_close": float(b.get("adj_close") or b["close"]) * k} for b in kept]
        if kept:
            series[sym] = kept
    regime = [{"date": d, "close": c, "adj_close": a}
              for d, c, a in zip(m.dates[:cut + 1], m.fund_close[:cut + 1], m.fund_adj[:cut + 1]) if c is not None]
    flows = {d: v for d, v in m.flows.items() if d <= day}
    out = type(m)(m.dates[:cut + 1], series, regime, m.members_on, min_turnover=m.min_turnover, flows=flows)
    out.start_day = m.start_day
    out.missing = list(m.missing)
    out.point_in_time = getattr(m, "point_in_time", False)
    return out


def _same(a: Any, b: Any, tol: float = 1e-9) -> bool:
    if isinstance(a, float) or isinstance(b, float):
        if a is None or b is None:
            return a is b
        return math.isclose(a, b, rel_tol=tol, abs_tol=tol)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_same(x, y, tol) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k], tol) for k in a)
    return bool(a == b)


def _indicators(m: Market, t: int) -> dict[str, Any]:
    from .research_studies import _adv, regime_of
    out: dict[str, Any] = {"fund_ema50": m.fund_ema50[t], "fund_ema200": m.fund_ema200[t],
                           "fund_sma100": m.fund_sma100[t], "breadth": m.breadth()[t], "regime": regime_of(m, t)}
    for sym in sorted(m.members_on(m.dates[t])):
        # a member with no price yet on day t (listed later, or a renamed symbol whose history starts at the new
        # name) has no indicators to read; the cut market drops it, so leaving it out here is not hiding a leak
        if sym in m.close and m.close[sym][t] is not None:
            out[f"{sym} atr"] = m.atr(sym)[t]
            out[f"{sym} ema50"] = m.ema50(sym)[t]
            out[f"{sym} vol60"] = m.vol(sym)[t]
            out[f"{sym} traded value"] = _adv(m, sym, t)
    return out


def _first_difference(full: dict[str, Any], cut: dict[str, Any]) -> str:
    for k in full:
        if k in cut and not _same(full[k], cut[k]):
            return f"{k}: {full[k]!r} with later data, {cut.get(k)!r} without"
    for k in full:
        if k not in cut:
            return f"{k} appears only with later data"
    extra = sorted(set(cut) - set(full))
    return f"{extra[0]} appears only without later data" if extra else "differs"


def _run_view(run: dict[str, Any]) -> dict[str, Any]:
    return {"curve": run["curve"], "fills": run["fills"], "trades": run["trades"], "costs": run["costs"]}


def _spread(days: list[int], n: int | None) -> list[int]:
    if n is None or n >= len(days):
        return days
    if n <= 0:
        return []
    if n <= 1:
        return days[len(days) // 2:len(days) // 2 + 1]
    return sorted({days[round(i * (len(days) - 1) / (n - 1))] for i in range(n)})


def leakage_check(m: Market, *, rules: Rules | None = None, ranker: RankFn | None = None, start: int | None = None,
                  ranking_days: int | None = None, simulation_days: int = 4, holdout_start: str | None = None,
                  cost_model: Any | None = None, progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Cut the market at sampled decision days and require every decision to come out the same. ``ranking_days``
    limits how many rebalance days are checked (None: all); ``simulation_days`` how many end-to-end cuts."""
    from .research_studies import FINALIST, HOLDOUT_START, _first_ranked_day
    rules = rules or FINALIST
    rank: RankFn = ranker or (lambda mk, t: mk.momentum_ranking(t))
    holdout = holdout_start or HOLDOUT_START
    start = _first_ranked_day(m) if start is None else start
    end = len(m.dates) - 1
    rebal = [d for d in m.rebalance_days(start, end, rules.rebalance, rules.rebalance_month) if d < end]
    checks: dict[str, dict[str, Any]] = {}

    def record(name: str, day: int | None, detail: str | None) -> None:
        c = checks.setdefault(name, {"checked": 0, "failures": []})
        c["checked"] += 1
        if detail is not None:
            c["failures"].append({"day": m.dates[day] if day is not None else None, "detail": detail})

    # static: the holdout stays sealed, membership is point in time
    late = sorted({b["date"] for bars in m.raw.values() for b in bars if b["date"] >= holdout}
                  | {d for d in m.dates if d >= holdout})
    record("holdout sealed", None, f"{len(late)} day(s) on or after {holdout}, first {late[0]}" if late else None)
    pit = getattr(m, "point_in_time", None)
    warnings = []
    if pit is False:
        warnings.append("Index membership is today's list for every date (no point-in-time history): "
                        "stocks that later left the index are missing, a survivorship bias rather than a leak.")

    for d in _spread(rebal, ranking_days):
        if progress:
            progress(f"leakage: cut at {m.dates[d]}")
        cut = truncate(m, d)
        try:
            full_rank, cut_rank = rank(m, d), rank(cut, d)
            record("ranking", d, None if full_rank == cut_rank else
                   f"top names {full_rank[:5]} with later data, {cut_rank[:5]} without")
        except (IndexError, KeyError) as e:
            record("ranking", d, f"needs data after the decision day ({type(e).__name__}: {e})")
        if ranker is None:
            fs, cs = m.momentum_scores(d), cut.momentum_scores(d)
            record("ranking scores", d, None if _same(fs, cs) else _first_difference(fs, cs))
            k = {s: 1.0 + 0.37 * ((i % 7) + 1) for i, s in enumerate(sorted(m.raw))}
            record("back-adjustment", d, None if truncate(m, d, rescale=k).momentum_ranking(d) == cut_rank else
                   "the ranking changes when adjusted closes are rescaled: it reads price levels, not returns")
        try:
            fi, ci = _indicators(m, d), _indicators(cut, d)
            record("indicators", d, None if _same(fi, ci) else _first_difference(fi, ci))
        except (IndexError, KeyError) as e:
            record("indicators", d, f"needs data after the decision day ({type(e).__name__}: {e})")

    # end to end: run to one session after the decision on day d is traded, then cut there
    for d in _spread([x for x in rebal if start < x and x + 2 <= end], simulation_days):
        for label, lag in (("simulation, same-close fills", 0), ("simulation, next-day fills", 1)):
            stop = d + lag + 1
            if progress:
                progress(f"leakage: {label} to {m.dates[stop]}")
            r = with_rules(rules, lag=lag)
            try:
                full = _run_view(simulate(m, r, start, stop, cost_model=cost_model, ranker=ranker))
                part = _run_view(simulate(truncate(m, stop), r, start, stop, cost_model=cost_model, ranker=ranker))
                record(label, d, None if _same(full, part) else _first_difference(full, part))
            except (IndexError, KeyError) as e:
                record(label, d, f"needs data after the decision day ({type(e).__name__}: {e})")

    failed = sum(len(c["failures"]) for c in checks.values())
    return {"start": m.dates[start], "end": m.dates[end], "rules": rules.label(), "checks": checks,
            "ok": failed == 0, "failures": failed, "warnings": warnings}


def guard(m: Market, **kw: Any) -> dict[str, Any]:
    """A quick leakage check (six ranking days, two simulation cuts) that raises LeakageError on any failure."""
    kw.setdefault("ranking_days", 6)
    kw.setdefault("simulation_days", 2)
    r = leakage_check(m, **kw)
    if not r["ok"]:
        raise LeakageError(format_leakage(r))
    return r


def format_leakage(r: dict[str, Any]) -> str:
    verdict = "PASSED: no decision used later data" if r["ok"] else f"FAILED: {r['failures']} decision(s) used later data"
    lines = [f"Leakage test ({r['rules']}), {r['start']} to {r['end']}: {verdict}.",
             "  Each check cuts the market at a decision day and recomputes that day's decision without the later data.",
             f"  {'check':<34}{'days':>6}{'failed':>8}"]
    for name, c in r["checks"].items():
        lines.append(f"  {name:<34}{c['checked']:>6}{len(c['failures']):>8}")
    for name, c in r["checks"].items():
        for f in c["failures"][:3]:
            lines.append(f"  ! {name}{' on ' + f['day'] if f['day'] else ''}: {f['detail']}")
        if len(c["failures"]) > 3:
            lines.append(f"  ! {name}: {len(c['failures']) - 3} more")
    lines.extend(f"  Note: {w}" for w in r["warnings"])
    lines.append("  Not covered: event signals (results, insider trades), which use their own 15:00 IST as-of rule "
                 "and are not in the adopted rules.")
    return "\n".join(lines)
