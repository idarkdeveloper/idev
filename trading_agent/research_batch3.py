"""Pre-registered batch 3 (docs/research/preregistered-batch-2026-10-18.md), ONCE: two variants of the adopted rules.

* F, smooth momentum ("frog in the pan", Da, Gurun and Warachka 2014): the momentum top 40 reordered by information
  discreteness over the 12-1 window, smoothest first.
* G, institutional buying: within the momentum top 40, names with net positive bulk or block deal buying in the 60
  calendar days before the decision by clients named FUND, INSURANCE or PENSION come first.

Both run once on the development years beside the control (the adopted rules, unchanged) and are judged by the
backward check's variant rules: +2 points a year over the control, a worst fall within 5 points of it, turnover at most
1.5x. A pass only earns a 12-month paper account. Everything below is fixed by the pre-registration.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .research_sim import Market, simulate, summarize

PREREGISTRATION = "docs/research/preregistered-batch-2026-10-18.md"
STUDY = "Batch3"
TOP = 40
WINDOW, SKIP, MIN_DAYS = 252, 21, 150
INSTITUTION_WORDS = ("FUND", "INSURANCE", "PENSION")
LOOKBACK_DAYS = 60
DEALS_FROM, DEALS_TO = "2019-10-01", "2025-09-30"
DEAL_WINDOW_DAYS = 90
MIN_CAGR_GAIN, FALL_MARGIN, MAX_TURNOVER_RATIO = 0.02, 0.05, 1.5


class DealHistoryError(RuntimeError):
    """A window of deal history could not be downloaded: G does not run and nothing is spent."""


# --------------------------------------------------------------------------- #
# F: smooth momentum
# --------------------------------------------------------------------------- #
def information_discreteness(m: Market, sym: str, t: int, _bars: dict[str, set[str]] | None = None) -> float | None:
    """ID = sign(PRET) x (share of negative days - share of positive days) over sessions t-252 .. t-21, counting only
    days on which the stock has a real bar on both that day and the day before; None with fewer than 150 such days."""
    if t - WINDOW < 0:
        return None
    adj = m.adj.get(sym)
    if adj is None:
        return None
    a0, a1 = adj[t - WINDOW], adj[t - SKIP]
    if not a0 or not a1:
        return None
    real = (_bars or {}).get(sym)
    if real is None:
        real = set(m.raw_dates.get(sym, []))
        if _bars is not None:
            _bars[sym] = real
    pos = neg = n = 0
    for k in range(t - WINDOW + 1, t - SKIP + 1):
        if m.dates[k] not in real or m.dates[k - 1] not in real:
            continue
        p0, p1 = adj[k - 1], adj[k]
        if not p0 or not p1:
            continue
        n += 1
        r = p1 / p0 - 1
        if r > 0:
            pos += 1
        elif r < 0:
            neg += 1
    if n < MIN_DAYS:
        return None
    pret = a1 / a0 - 1
    sign = 1.0 if pret > 0 else -1.0 if pret < 0 else 0.0
    return sign * (neg / n - pos / n)


def smooth_ranker() -> Callable[[Market, int], list[str]]:
    cache: dict[str, set[str]] = {}

    def rank(m: Market, t: int) -> list[str]:
        base = m.momentum_ranking(t)
        head, tail = base[:TOP], base[TOP:]
        ids = {s: information_discreteness(m, s, t, cache) for s in head}
        order = sorted(range(len(head)), key=lambda i: (ids[head[i]] is None, ids[head[i]] or 0.0, i))
        return [head[i] for i in order] + tail
    return rank


# --------------------------------------------------------------------------- #
# G: institutional buying
# --------------------------------------------------------------------------- #
def is_institution(client: str) -> bool:
    name = client.upper()
    return any(w in name for w in INSTITUTION_WORDS)


def _windows(start: str, end: str) -> list[tuple[date, date]]:
    a, b = date.fromisoformat(start), date.fromisoformat(end)
    out = []
    while a <= b:
        e = min(a + timedelta(days=DEAL_WINDOW_DAYS - 1), b)
        out.append((a, e))
        a = e + timedelta(days=1)
    return out


def fetch_deal_history(client: Any, cache_dir: Path, *, start: str = DEALS_FROM, end: str = DEALS_TO,
                       retries: int = 3, pause: float = 2.0, progress: Callable[[str], Any] | None = None,
                       sleep: Callable[[float], Any] = time.sleep) -> list[dict[str, Any]]:
    """Every NSE bulk and block deal in [start, end], one CSV per 90-day window and kind (strict: no capped JSON
    fallback), cached on disk. DealHistoryError when a window still fails after ``retries`` tries."""
    from .nse import _nse_date, _parse_deals_csv
    cache_dir.mkdir(parents=True, exist_ok=True)
    referer = f"{client.base_url}/report-detail/display-bulk-and-block-deals"
    rows: list[dict[str, Any]] = []
    for a, b in _windows(start, end):
        for kind in ("bulk", "block"):
            path = cache_dir / f"{kind}_{a.isoformat()}_{b.isoformat()}.json"
            if path.exists():
                rows += json.loads(path.read_text(encoding="utf-8"))
                continue
            if progress:
                progress(f"deals: {kind} {a} to {b}")
            params = {"optionType": f"{kind}_deals", "from": _nse_date(a), "to": _nse_date(b), "csv": "true"}
            got = None
            for i in range(retries):
                try:
                    client._ensure_warm({"Referer": referer})
                    got = [{**r, "kind": kind} for r in _parse_deals_csv(
                        client._get_text("api/historicalOR/bulk-block-short-deals", params, referer))]
                    break
                except Exception as e:  # noqa: BLE001 - retried, then reported
                    if i == retries - 1:
                        raise DealHistoryError(f"{kind} deals {a} to {b}: {type(e).__name__}: {e}") from e
                    sleep(pause * (2 ** i))
            assert got is not None
            path.write_text(json.dumps(got), encoding="utf-8")
            rows += got
    return rows


def institutional_flows(rows: list[dict[str, Any]]) -> tuple[dict[str, list[tuple[str, float]]], int]:
    """{symbol: [(date, signed value)]} for deals by institutional clients, and how many such rows were skipped
    for a missing or unreadable quantity, price, side or date."""
    from .membership import _norm
    from .nse import _iso
    out: dict[str, list[tuple[str, float]]] = {}
    skipped = 0
    for r in rows:
        if not is_institution(str(r.get("clientName") or "")):
            continue
        side = str(r.get("buySell") or "").strip().upper()
        try:
            qty, px = float(r.get("qty") or ""), float(r.get("watp") or "")
            day = _iso(r.get("date"))
        except (TypeError, ValueError):
            skipped += 1
            continue
        if side not in ("BUY", "SELL") or not day or qty <= 0 or px <= 0:
            skipped += 1
            continue
        out.setdefault(_norm(str(r.get("symbol") or "")), []).append(
            (day[:10], qty * px * (1 if side == "BUY" else -1)))
    for v in out.values():
        v.sort()
    return out, skipped


def net_value(flows: dict[str, list[tuple[str, float]]], sym: str, day: str) -> float:
    """Net institutional value in the 60 calendar days before ``day`` (strictly before it)."""
    since = (date.fromisoformat(day) - timedelta(days=LOOKBACK_DAYS)).isoformat()
    return sum(v for d, v in flows.get(sym, []) if since <= d < day)


def institutional_ranker(flows: dict[str, list[tuple[str, float]]]) -> Callable[[Market, int], list[str]]:
    def rank(m: Market, t: int) -> list[str]:
        base = m.momentum_ranking(t)
        head, tail = base[:TOP], base[TOP:]
        day = m.dates[t]
        buying = [s for s in head if net_value(flows, s, day) > 0]
        return buying + [s for s in head if s not in set(buying)] + tail
    return rank


# --------------------------------------------------------------------------- #
# the run
# --------------------------------------------------------------------------- #
def batch3(m: Market, flows: dict[str, list[tuple[str, float]]], *, cost_model: Any, capital: float = 500_000.0,
           extra_trials: int = 0) -> dict[str, Any]:
    from .research_studies import FINALIST, _first_ranked_day, _pbo, _skill
    start, end = _first_ranked_day(m), len(m.dates) - 1
    names = {"control": "the adopted rules", "F": "smooth momentum", "G": "institutional buying"}
    rankers = {"control": None, "F": smooth_ranker(), "G": institutional_ranker(flows)}
    runs = {k: simulate(m, FINALIST, start, end, capital=capital, cost_model=cost_model, ranker=r)
            for k, r in rankers.items()}
    results = {k: summarize(r, m, start, end) for k, r in runs.items()}
    skill = _skill(runs, m, start, end, extra_trials)
    c = results["control"]
    for k, x in results.items():
        x["name"], x["skill"] = names[k], skill[k]
        if k == "control":
            continue
        checks = {
            "+2 points a year over the control": (x["cagr"] is not None and c["cagr"] is not None
                                                  and x["cagr"] - c["cagr"] >= MIN_CAGR_GAIN),
            "worst fall within 5 points of the control's": (x["max_drawdown"] is not None
                                                            and c["max_drawdown"] is not None
                                                            and x["max_drawdown"] >= c["max_drawdown"] - FALL_MARGIN),
            "turnover at most 1.5x the control's": (x["turnover"] is not None and c["turnover"] is not None
                                                    and x["turnover"] <= MAX_TURNOVER_RATIO * c["turnover"]),
        }
        x["checks"], x["passes"] = checks, all(checks.values())
    return {"start": m.dates[start], "end": m.dates[end], "results": results,
            "passing": [k for k, x in results.items() if k != "control" and x.get("passes")],
            "pbo": _pbo(runs, m, start, end)}


def format_batch3(r: dict[str, Any], deals_note: str) -> str:
    from .research_studies import pbo_line

    def pct(v: Any, f: str = "+.1f") -> str:
        return "n/a" if v is None or (isinstance(v, float) and math.isnan(v)) else f"{v * 100:{f}}%"
    lines = [f"BATCH 3, run once ({PREREGISTRATION}), {r['start']} to {r['end']}, after charges and tax, against the "
             "adopted rules on the same run. Development years: weak evidence; a pass only earns a paper account.",
             f"  {'variant':<28}{'return':>9}{'vs fund':>9}{'a year':>8}{'worst fall':>11}{'turnover':>9}"
             f"{'skill':>7}  verdict"]
    for k, x in r["results"].items():
        p = x["skill"].get("probability")
        verdict = "control" if k == "control" else ("PASS" if x["passes"] else "fail")
        lines.append(f"  {k + ' ' + x['name']:<28}{pct(x['total_return']):>9}{pct(x['excess']):>9}{pct(x['cagr']):>8}"
                     f"{pct(x['max_drawdown']):>11}{pct(x['turnover'], '.0f'):>9}"
                     f"{'n/a' if p is None else f'{p:.2f}':>7}  {verdict}")
    for k, x in r["results"].items():
        failed = [n for n, ok in (x.get("checks") or {}).items() if not ok]
        if failed:
            lines.append(f"  {k} misses: {'; '.join(failed)}.")
    lines.append(pbo_line(r["pbo"]))
    lines.append(f"  Deals: {deals_note}")
    lines.append(f"  Verdict: passing {', '.join(r['passing']) or 'none'}"
                 + ("; each earns a 12-month paper account beside the adopted rules, built only on the owner's "
                    "approval" if r["passing"] else "; the adopted rules stay as they are")
                 + ". Skill odds are information only. This batch is now spent.")
    return "\n".join(lines)
