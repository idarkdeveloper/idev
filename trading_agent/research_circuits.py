"""Circuit-lock fill audit: a realism diagnostic, not a strategy and not a trial.

The simulator sells at the day's close. On a day a stock is locked at its lower price band there are no buyers, so
that sale would not have filled. This audit runs the adopted rules on the development years, finds every sell made
on a day that looks locked, and re-prices it at the close of the first later session that traded freely (low < high).

NSE's history of each stock's band is not freely available, so a lock is inferred from the daily bar:
* open, high, low and close all equal, and the close below the previous close (a lower-circuit lock); or
* a fall of 4.5% or more on volume under 20% of the median of the previous 60 sessions (little or no buying).
Buys on days that look locked at the upper band are counted, not re-priced.

If the delayed sells cost more than 15 points of the development-years return, the forward test's fills should be
delayed the same way (a realism fix the owner approves; no rule changes here).
"""

from __future__ import annotations

import statistics
from bisect import bisect_left
from typing import Any

from .research_sim import Market, simulate

FALL, THIN_VOLUME, VOLUME_DAYS = -0.045, 0.20, 60
THRESHOLD_POINTS = 0.15


def _bar(m: Market, sym: str, day: str) -> tuple[int, dict[str, Any]] | None:
    dates = m.raw_dates.get(sym) or []
    i = bisect_left(dates, day)
    return (i, m.raw[sym][i]) if i < len(dates) and dates[i] == day else None


def lock(m: Market, sym: str, day: str) -> str | None:
    """"lower" / "upper" when the bar on ``day`` looks locked at a band, else None (also None without the fields)."""
    found = _bar(m, sym, day)
    if not found or found[0] == 0:
        return None
    i, b = found
    prev = m.raw[sym][i - 1].get("close")
    raw_vals = [b.get(k) for k in ("open", "high", "low", "close")]
    if not prev or any(v is None for v in raw_vals):
        return None
    o, h, lo, c = (float(v) for v in raw_vals if v is not None)
    if abs(h - lo) <= 1e-9 * max(abs(h), 1.0) and abs(o - c) <= 1e-9 * max(abs(c), 1.0):
        return "lower" if c < prev else "upper" if c > prev else None
    vols = [x.get("volume") or 0.0 for x in m.raw[sym][max(0, i - VOLUME_DAYS):i]]
    if c / prev - 1 <= FALL and len(vols) >= VOLUME_DAYS // 2:
        med = statistics.median(vols)
        if med > 0 and (b.get("volume") or 0.0) < THIN_VOLUME * med:
            return "lower"
    return None


def first_free_close(m: Market, sym: str, day: str) -> tuple[str, float] | None:
    """The date and close of the first session after ``day`` whose low is below its high."""
    found = _bar(m, sym, day)
    if not found:
        return None
    for b in m.raw[sym][found[0] + 1:]:
        lo, h, c = b.get("low"), b.get("high"), b.get("close")
        if lo is not None and h is not None and c and lo < h:
            return b["date"], float(c)
    return None


def circuit_audit(m: Market, *, cost_model: Any, capital: float = 500_000.0) -> dict[str, Any]:
    from .research_studies import FINALIST, _first_ranked_day
    start, end = _first_ranked_day(m), len(m.dates) - 1
    run = simulate(m, FINALIST, start, end, capital=capital, cost_model=cost_model)
    exits = {(tr["exit"], tr["symbol"]) for tr in run["trades"]}
    sells = buys = 0
    locked_sells: list[dict[str, Any]] = []
    locked_buys: list[dict[str, Any]] = []
    unknown = 0
    for t, sym, amount in run["fills"]:
        day = m.dates[t]
        kind = lock(m, sym, day)
        b = _bar(m, sym, day)
        if b is None or b[1].get("open") is None:
            unknown += 1
        if (day, sym) in exits:
            sells += 1
            if kind == "lower":
                px = m.close[sym][t]
                later = first_free_close(m, sym, day)
                if later and px:
                    cost = amount * (later[1] / px - 1)
                    locked_sells.append({"symbol": sym, "date": day, "filled": later[0], "proceeds": amount,
                                         "cost": cost, "move": later[1] / px - 1})
                else:
                    locked_sells.append({"symbol": sym, "date": day, "filled": None, "proceeds": amount,
                                         "cost": None, "move": None})
        else:
            buys += 1
            if kind == "upper":
                locked_buys.append({"symbol": sym, "date": day, "amount": amount})
    total = sum(x["cost"] for x in locked_sells if x["cost"] is not None)
    points = total / capital
    return {"start": m.dates[start], "end": m.dates[end], "sells": sells, "buys": buys,
            "locked_sells": locked_sells, "locked_buys": locked_buys, "unknown": unknown,
            "cost": total, "points": points, "fix_forward": points < -THRESHOLD_POINTS,
            "total_return": (run["curve"][-1] / capital - 1) if run["curve"] and run["curve"][-1] else None}


def format_circuit_audit(r: dict[str, Any]) -> str:
    ls = r["locked_sells"]
    lines = [f"Circuit-lock fill audit (a realism check, not a trial), the adopted rules, {r['start']} to {r['end']}.",
             f"  Sells: {r['sells']}; on days that look locked at the lower band: {len(ls)}. Buys: {r['buys']}; on days "
             f"that look locked at the upper band: {len(r['locked_buys'])}.",
             f"  Delaying those sells to the first freely traded close changes the result by {r['cost']:+,.0f} "
             f"(about {r['points'] * 100:+.1f} points of return; the run made "
             f"{(r['total_return'] or 0) * 100:+.1f}%)."]
    for x in sorted(ls, key=lambda x: x["cost"] if x["cost"] is not None else 0)[:8]:
        lines.append(f"  {x['date']} {x['symbol']}: filled {x['filled'] or 'never in the data'}"
                     + (f", {x['move'] * 100:+.1f}% vs the locked close" if x["move"] is not None else ""))
    if r["unknown"]:
        lines.append(f"  {r['unknown']} fill(s) had no open/high/low in the data and could not be checked.")
    lines.append("  Locks are inferred from daily bars (NSE's band history is not free).")
    lines.append("  " + ("Above the 15-point threshold: the forward test's fills should be delayed the same way."
                         if r["fix_forward"] else "Within the 15-point threshold: no change to the forward test."))
    return "\n".join(lines)
