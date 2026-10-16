"""Research: post-results drift (PEAD) and who-traded signals, as known on each date.

Earnings signals, from the NSE results filings (fundamentals_history), using only filings usable by the day
(a broadcast at or after 15:00 IST counts from the next trading day):

* ``sue``: standardised unexpected earnings. The latest quarter's EPS (profit when EPS is missing) minus the same
  quarter a year earlier, divided by the standard deviation of that year-on-year change over up to the last 8
  quarters (at least 4 needed).
* ``dsue``: the change in SUE from the previous quarter.
* ``margin_x_sales``: margin acceleration (this quarter's year-on-year margin change minus last quarter's) times the
  sales surprise (revenue's year-on-year change, standardised the same way as SUE).

Only a filing broadcast in the last ``FRESH_DAYS`` counts: the drift is a few weeks to months long.

Who-traded signals, from NSE insider (PIT) disclosures by their broadcast date:

* ``promo_x_mom``: promoters' net buying (purchases minus sales, rupees) over the last 90 days is positive: the
  stock's 6-month return; otherwise no value.
* ``promo_quiet``: 1 when promoters bought on net and the stock is in a quiet base (ATR20 / ATR120 <= 0.70), else 0.

``signal_study`` measures each signal's rank correlation (IC) with the next 5, 20 and 60 trading days' return above
the index fund, and the top fifth's excess after a round trip of charges, at every month start of the window.
``pead_rankers`` turns the earnings signals into a ranking for the daily simulator: the screen's own score plus the
standardised earnings composite (post-results drift instead of static quality), optionally plus the promoter signals.
"""

from __future__ import annotations

import json
import logging
import statistics
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

from .filing_time import usable_by
from .research_sim import Market, atr_close

log = logging.getLogger(__name__)

FRESH_DAYS = 95
PROMOTER_LOOKBACK = 90
QUIET_RATIO = 0.70
SIGNALS = ("sue", "dsue", "margin_x_sales", "promo_x_mom", "promo_quiet")


# --------------------------------------------------------------------------- #
# earnings
# --------------------------------------------------------------------------- #
def _yoy_key(pe: str) -> str:
    d = date.fromisoformat(pe)
    return date(d.year - 1, d.month, d.day if d.month != 2 else 28).isoformat()


def _standardised(values: dict[str, float], at: str, hist: int = 8, need: int = 4) -> float | None:
    """(x_at - x_at-1y) / stdev of that change over the last ``hist`` quarters up to ``at``."""
    ends = sorted(e for e in values if e <= at)
    diffs: dict[str, float] = {}
    for e in ends:
        prev = values.get(_yoy_key(e))
        if prev is not None:
            diffs[e] = values[e] - prev
    if at not in diffs:
        return None
    window = [diffs[e] for e in sorted(diffs)[-hist:]]
    if len(window) < need:
        return None
    sd = statistics.pstdev(window)
    return diffs[at] / sd if sd > 0 else None


def earnings_signals(records: list[dict[str, Any]], day: str, holidays: Any | None = None,
                     fresh_days: int = FRESH_DAYS) -> dict[str, float | None] | None:
    """SUE, its change and margin acceleration x sales surprise as known on ``day``; None when the latest usable
    filing is older than ``fresh_days`` or there is none."""
    known = [r for r in records if usable_by(r["available"], day, holidays)]
    if not known:
        return None
    latest = max(known, key=lambda r: r["period_end"])
    if (date.fromisoformat(day) - date.fromisoformat(latest["available"][:10])).days > fresh_days:
        return None
    by = {r["period_end"]: r for r in known}
    use_eps = all(r.get("eps_q") is not None for r in known[-8:])
    raw_earn = {e: (r["eps_q"] if use_eps else r.get("profit_q")) for e, r in by.items()}
    earn: dict[str, float] = {e: float(v) for e, v in raw_earn.items() if v is not None}
    rev = {e: float(r["revenue_q"]) for e, r in by.items() if r.get("revenue_q")}
    margin = {e: earn_p / rev[e] for e, r in by.items() if e in rev and rev[e] > 0
              and (earn_p := r.get("profit_q")) is not None}
    pe = latest["period_end"]
    prev_ends = sorted(e for e in by if e < pe)
    prev = prev_ends[-1] if prev_ends else None
    sue = _standardised(earn, pe)
    sue_prev = _standardised(earn, prev) if prev else None

    def yoy_margin(e: str | None) -> float | None:
        if e is None or e not in margin or _yoy_key(e) not in margin:
            return None
        return margin[e] - margin[_yoy_key(e)]
    m_now, m_prev = yoy_margin(pe), yoy_margin(prev)
    sales = _standardised(rev, pe)
    accel = (m_now - m_prev) if m_now is not None and m_prev is not None else None
    return {"sue": sue, "dsue": (sue - sue_prev) if sue is not None and sue_prev is not None else None,
            "margin_x_sales": accel * sales if accel is not None and sales is not None else None,
            "quarter": pe, "filed": latest["available"][:10]}


# --------------------------------------------------------------------------- #
# insider (PIT) history
# --------------------------------------------------------------------------- #
def _iso(v: Any) -> str | None:
    from .nse import _iso as nse_iso
    text = str(v or "").strip().split(" ")[0]   # "16-Oct-2024 19:20" -> the date part
    try:
        out = nse_iso(text)
        date.fromisoformat(out[:10])
        return out[:10]
    except (ValueError, TypeError):
        return None


def norm_pit(row: dict[str, Any]) -> dict[str, Any] | None:
    """The fields the signals use from one ``api/corporates-pit`` row; None for an unusable row."""
    sym = str(row.get("symbol") or "").upper().strip()
    kind = str(row.get("tdpTransactionType") or row.get("transactionType") or "").upper().strip()
    seen = _iso(row.get("date")) or _iso(row.get("intimDt"))
    try:
        value = float(str(row.get("secVal") or "0").replace(",", ""))
    except ValueError:
        value = 0.0
    if not sym or kind not in ("BUY", "SELL") or not seen or value <= 0:
        return None
    return {"symbol": sym, "category": str(row.get("personCategory") or "").strip(), "side": kind,
            "value": value, "seen": seen, "traded": _iso(row.get("acqfromDt"))}


class InsiderHistory:
    """NSE insider disclosures by month, cached in ``<cache>/insider_history/YYYY-MM.json`` (a month that has ended is
    never fetched again). ``fetch(path, params, referer)`` is ``NSEClient._get``."""

    def __init__(self, cache_dir: Path, fetch: Callable[..., Any] | None = None, *, pause: float = 1.0,
                 sleep: Callable[[float], None] = time.sleep, today: Callable[[], date] = date.today):
        self.dir = Path(cache_dir) / "insider_history"
        self.fetch, self.pause, self.sleep, self.today = fetch, pause, sleep, today

    def _path(self, ym: str) -> Path:
        return self.dir / f"{ym}.json"

    def month(self, ym: str) -> list[dict[str, Any]] | None:
        p = self._path(ym)
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except ValueError:
                return None
        return None

    def download(self, start: date, end: date, progress: Any = None, max_months: int = 120) -> dict[str, int]:
        """Fill the cache for every month from ``start`` to ``end`` (resumable; the current month is refreshed)."""
        from .nse import _nse_date
        if self.fetch is None:
            raise ValueError("no NSE client to download with")
        done = fetched = failed = 0
        d = date(start.year, start.month, 1)
        while d <= end and fetched < max_months:
            nxt = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
            ym = d.strftime("%Y-%m")
            current = nxt > self.today()
            if self._path(ym).exists() and not current:
                done += 1
                d = nxt
                continue
            try:
                if fetched and self.pause:
                    self.sleep(self.pause)
                data = self.fetch("api/corporates-pit", params={"index": "equities", "from_date": _nse_date(d),
                                                                "to_date": _nse_date(nxt - timedelta(days=1))},
                                  referer="https://www.nseindia.com/companies-listing/corporate-filings-insider-trading")
                rows = data.get("data", []) if isinstance(data, dict) else data
                out = [x for x in (norm_pit(r) for r in rows or []) if x]
                self.dir.mkdir(parents=True, exist_ok=True)
                self._path(ym).write_text(json.dumps(out), encoding="utf-8")
                fetched += 1
                if progress:
                    progress(f"insider disclosures {ym}: {len(out)} trades")
            except Exception as e:  # noqa: BLE001 - one month failing is retried next run
                failed += 1
                log.warning("insider history %s failed: %s", ym, e)
                if failed >= 3:
                    break
            d = nxt
        return {"cached": done, "fetched": fetched, "failed": failed}

    def trades(self, start: str, end: str) -> list[dict[str, Any]]:
        out = []
        d = date.fromisoformat(start[:7] + "-01")
        while d.isoformat()[:7] <= end[:7]:
            out += self.month(d.strftime("%Y-%m")) or []
            d = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
        return out


def promoter_net(trades: list[dict[str, Any]], day: str, lookback: int = PROMOTER_LOOKBACK) -> dict[str, float]:
    """Promoters' net buying (rupees) per symbol, disclosed in the ``lookback`` days up to ``day``."""
    since = (date.fromisoformat(day) - timedelta(days=lookback)).isoformat()
    out: dict[str, float] = {}
    for t in trades:
        if not (since < t["seen"] <= day) or "promoter" not in t["category"].lower():
            continue
        out[t["symbol"]] = out.get(t["symbol"], 0.0) + (t["value"] if t["side"] == "BUY" else -t["value"])
    return out


def atr_ratio(m: Market, sym: str, t: int) -> float | None:
    """ATR20 / ATR120 on close-to-close moves: below 0.70 is a quiet base."""
    key = ("ratio", sym)
    cache = m.__dict__.setdefault("_atr_ratio", {})
    if key not in cache:
        a20, a120 = atr_close(m.close[sym], 20), atr_close(m.close[sym], 120)
        cache[key] = [x / y if x is not None and y else None for x, y in zip(a20, a120)]
    return cache[key][t]


# --------------------------------------------------------------------------- #
# signals on a day, the IC study, and the rankers
# --------------------------------------------------------------------------- #
class EventData:
    """Results records and insider trades for the market's symbols, read from the caches only."""

    def __init__(self, m: Market, results: dict[str, list[dict[str, Any]]], insider: list[dict[str, Any]],
                 holidays: Any | None = None):
        self.m, self.results, self.insider, self.holidays = m, results, insider, holidays
        self._cache: dict[int, dict[str, dict[str, float | None]]] = {}

    def signals(self, t: int) -> dict[str, dict[str, float | None]]:
        if t in self._cache:
            return self._cache[t]
        day = self.m.dates[t]
        stats = self.m.stats_on(t)
        promo = promoter_net(self.insider, day)
        out: dict[str, dict[str, float | None]] = {}
        for sym in stats:
            e = earnings_signals(self.results.get(sym) or [], day, self.holidays) or {}
            bought = promo.get(sym, 0.0) > 0
            r6 = stats[sym].get("ret_6m")
            ratio = atr_ratio(self.m, sym, t)
            out[sym] = {"sue": e.get("sue"), "dsue": e.get("dsue"), "margin_x_sales": e.get("margin_x_sales"),
                        "promo_x_mom": r6 if bought else None,
                        "promo_quiet": 1.0 if bought and ratio is not None and ratio <= QUIET_RATIO else 0.0}
        self._cache[t] = out
        return out


def _z(values: dict[str, float]) -> dict[str, float]:
    if len(values) < 3:
        return {k: 0.0 for k in values}
    mu, sd = statistics.fmean(values.values()), statistics.pstdev(values.values())
    return {k: ((v - mu) / sd if sd > 0 else 0.0) for k, v in values.items()}


def signal_study(m: Market, ev: EventData, start: int, end: int, *, horizons: tuple[int, ...] = (5, 20, 60),
                 round_trip_cost: float = 0.004) -> dict[str, Any]:
    """Rank IC and top-fifth excess (after a round trip of charges) per signal and horizon, at every month start."""
    from .signal_lab import _verdict, spearman
    days = m.rebalance_days(start, end)
    out: dict[str, Any] = {}
    for sig in SIGNALS:
        out[sig] = {}
        for h in horizons:
            ics, tops, ns = [], [], []
            for t in days:
                if t + h > end:
                    continue
                f0, f1 = m.fund_adj[t], m.fund_adj[t + h]
                if not f0 or not f1:
                    continue
                fund_ret = f1 / f0 - 1
                vals, fwd = [], []
                for sym, s in ev.signals(t).items():
                    v = s.get(sig)
                    a0, a1 = m.adj[sym][t], m.adj[sym][t + h]
                    if v is None or not a0 or not a1:
                        continue
                    vals.append(float(v))
                    fwd.append(a1 / a0 - 1 - fund_ret)
                if len(vals) < 10 or len(set(vals)) < 3:
                    continue
                ic = spearman(vals, fwd)
                if ic is not None:
                    ics.append(ic)
                order = sorted(range(len(vals)), key=lambda i: vals[i], reverse=True)
                top = order[:max(1, len(order) // 5)]
                tops.append(statistics.fmean(fwd[i] for i in top) - round_trip_cost)
                ns.append(len(vals))
            mean_ic = statistics.fmean(ics) if ics else None
            t_stat = (mean_ic / (statistics.pstdev(ics) / len(ics) ** 0.5)
                      if ics and len(ics) > 2 and statistics.pstdev(ics) > 0 else None)
            spread = statistics.fmean(tops) if tops else None
            out[sig][str(h)] = {"months": len(ics), "mean_ic": mean_ic, "t": t_stat, "top_fifth_net": spread,
                                "avg_names": statistics.fmean(ns) if ns else None,
                                "verdict": _verdict(t_stat, spread, 0.0) if ics else "no data"}
    return out


def pead_ranker(ev: EventData, *, with_promoters: bool = False) -> Callable[[Market, int], list[str]]:
    """The screen's score plus z(SUE + dSUE + margin x sales) among eligible names (no earnings data adds 0);
    with promoters, +0.5 for promoter buying in a quiet base and +0.5 z(promo_x_mom)."""
    def rank(m: Market, t: int) -> list[str]:
        base = m.momentum_scores(t)
        sig = ev.signals(t)
        parts = {k: {s: float(sig[s][k]) for s in base if sig.get(s, {}).get(k) is not None}  # type: ignore[arg-type]
                 for k in ("sue", "dsue", "margin_x_sales", "promo_x_mom")}
        zs = {k: _z(v) for k, v in parts.items()}
        score = {}
        for s, b in base.items():
            earn = sum(zs[k].get(s, 0.0) for k in ("sue", "dsue", "margin_x_sales")) / 3
            extra = 0.0
            if with_promoters:
                extra = 0.5 * (sig.get(s, {}).get("promo_quiet") or 0.0) + 0.5 * zs["promo_x_mom"].get(s, 0.0)
            score[s] = b + earn + extra
        return sorted(score, key=lambda s: score[s], reverse=True)
    return rank


def format_signal_study(r: dict[str, Any], horizons: tuple[int, ...] = (5, 20, 60)) -> str:
    lines = ["  Signals at each month start: rank IC with the return above the index fund, and the top fifth's "
             "excess after a round trip of charges",
             f"  {'signal':<16}" + "".join(f"{f'{h}d IC (t)':>16}{f'{h}d top5th':>11}" for h in horizons)]
    for sig, per in r.items():
        row = f"  {sig:<16}"
        for h in horizons:
            x = per[str(h)]
            ic = "n/a" if x["mean_ic"] is None else f"{x['mean_ic']:+.3f}" + (
                f" ({x['t']:+.1f})" if x["t"] is not None else "")
            top = "n/a" if x["top_fifth_net"] is None else f"{x['top_fifth_net'] * 100:+.2f}%"
            row += f"{ic:>16}{top:>11}"
        lines.append(row)
    return "\n".join(lines)
