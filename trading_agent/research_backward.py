"""The pre-registered backward check (docs/research/preregistered-batch-2026-10-16.md): the adopted rules and four
variants, once, on Apr 2017 to Sep 2020, a window no research has judged. Every number here comes from that file and
must not change once the backward data exists.

Data: the Nifty Midcap 150 total return index from NSE (niftyindices.com) less 0.20% a year for a fund's costs, as
the calendar and the benchmark; point-in-time membership rebuilt from NSE's index notices since 2016; stock prices
from Yahoo. Taxes as they applied then (research_sim.tax_due and the 31 Jan 2018 grandfathering).
"""
from __future__ import annotations

import json
import math
import statistics
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .research_sim import Market, Rules, simulate, summarize, with_rules

# --- fixed by the pre-registration (16 Oct 2026); do not edit -------------------------------------------------- #
TRADING_FROM = "2017-04-01"     # first month start on or after this with 20 ranked names
WINDOW_END = "2020-10-01"       # first date NOT used: the development window starts here
LOOKBACK_FROM = "2016-04-01"    # the index's launch; the calendar starts here, trading a year later
FUND_FEE = 0.0020               # a year, taken off the total return index (MID150BEES did not exist)
CONTROL_FALL_MARGIN = 0.10      # control: worst fall within 10 points of the benchmark's
MIN_CAGR_GAIN = 0.02            # a variant: +2 points a year over the control
VARIANT_FALL_MARGIN = 0.05      # a variant: worst fall within 5 points of the control's
MAX_TURNOVER_RATIO = 1.5        # a variant: turnover at most 1.5x the control's
INDUSTRY_CAP = 5
SECTOR_MIN_NAMES = 3
RESIDUAL_WINDOW = 252           # variant E (preregistered-batch-2026-10-16b.md): sessions t-252 .. t-21
RESIDUAL_SKIP = 21
RESIDUAL_MIN_DAYS = 150
STUDY = "Backward-batch-v2"   # the first run used monthly bars and is void (rerun-addendum-2026-10-18c.md)
PREREGISTRATION = "docs/research/preregistered-batch-2026-10-16.md (+ batch 2, -16b, variant E)"
# ------------------------------------------------------------------------------------------------------------------ #

TRI_URL = "https://www.niftyindices.com/BackPage/getTotalReturnIndexString"
TRI_PAGE = "https://www.niftyindices.com/reports/historical-data"
TRI_NAME = "NIFTY MIDCAP 150"
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
           "Content-Type": "application/json; charset=utf-8", "Accept": "application/json, text/javascript, */*",
           "X-Requested-With": "XMLHttpRequest", "Origin": "https://www.niftyindices.com", "Referer": TRI_PAGE}


# --------------------------------------------------------------------------- #
# the benchmark: NSE's total return index
# --------------------------------------------------------------------------- #
def _tri_date(text: str) -> str | None:
    for fmt in ("%d %b %Y", "%d-%b-%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_tri_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """[{date, close}] oldest first from NSE's rows (JSON keys or a downloaded CSV's columns), duplicates dropped."""
    out: dict[str, float] = {}
    for r in rows:
        keys = {k.strip().lower().replace(" ", ""): v for k, v in r.items() if k}
        d = _tri_date(str(keys.get("date") or ""))
        raw = keys.get("totalreturnsindex") or keys.get("totalreturnindex") or keys.get("close")
        try:
            v = float(str(raw).replace(",", ""))
        except (TypeError, ValueError):
            continue
        if d and v > 0:
            out[d] = v
    return [{"date": d, "close": out[d]} for d in sorted(out)]


def fetch_tri(cache_dir: Path, *, start: str = LOOKBACK_FROM, end: str = WINDOW_END, session: Any = None,
              name: str = TRI_NAME) -> list[dict[str, Any]]:
    """The total return index from ``start`` to the day before ``end``, a year per request (NSE's limit), cached."""
    path = Path(cache_dir) / f"tri_{name.replace(' ', '_')}_{start}_{end}.json"
    if path.exists():
        return list(json.loads(path.read_text()))
    import requests
    s = session or requests.Session()
    s.get(TRI_PAGE, headers={"User-Agent": HEADERS["User-Agent"]}, timeout=30)
    rows: list[dict[str, Any]] = []
    a, stop = date.fromisoformat(start), date.fromisoformat(end) - timedelta(days=1)
    while a <= stop:
        b = min(a + timedelta(days=360), stop)
        cinfo = "{'name':'%s','startDate':'%s','endDate':'%s','indexName':'%s'}" % (
            name, a.strftime("%d-%b-%Y"), b.strftime("%d-%b-%Y"), name)
        r = s.post(TRI_URL, headers=HEADERS, data=json.dumps({"cinfo": cinfo}), timeout=60)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and "d" in data:
            data = json.loads(data["d"])
        if not isinstance(data, list):
            raise ValueError("NSE returned no total return rows")
        rows.extend(data)
        a = b + timedelta(days=1)
    bars = [b for b in parse_tri_rows(rows) if b["date"] < end]
    if not bars:
        raise ValueError("NSE returned no total return rows")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(bars))
    return bars


def tri_from_csv(path: str | Path) -> list[dict[str, Any]]:
    """A total return index downloaded by hand from niftyindices.com (columns Date and Total Returns Index)."""
    import csv
    with open(path, newline="", encoding="utf-8-sig") as f:
        return parse_tri_rows(list(csv.DictReader(f)))


def with_fee(bars: list[dict[str, Any]], fee: float = FUND_FEE) -> list[dict[str, Any]]:
    """The index as a fund would have returned it: less ``fee`` a year, accrued daily from the first bar."""
    if not bars:
        return []
    d0 = date.fromisoformat(bars[0]["date"])
    out = []
    for b in bars:
        k = (1 - fee) ** ((date.fromisoformat(b["date"]) - d0).days / 365.25)
        out.append({"date": b["date"], "close": b["close"] * k, "adj_close": b["close"] * k, "volume": 0})
    return out


# --------------------------------------------------------------------------- #
# the market
# --------------------------------------------------------------------------- #
def load_backward_market(membership: Any, prices: Any, benchmark: list[dict[str, Any]], *,
                         workers: int = 8) -> Market:
    """Every stock ever in the index from its launch to the window's end, on the benchmark's trading days, nothing
    dated on or after WINDOW_END kept."""
    bench = [b for b in with_fee(benchmark) if LOOKBACK_FROM <= b["date"] < WINDOW_END]
    calendar = [b["date"] for b in bench]
    universe = sorted(membership.ever_members(LOOKBACK_FROM))

    from .research_replication import drop_bad_prices, unit_jumps

    def load(sym: str) -> tuple[str, list[dict[str, Any]] | None]:
        try:
            return sym, drop_bad_prices([b for b in prices.history(sym, "max") if b["date"] < WINDOW_END])[0]
        except Exception:  # noqa: BLE001 - a missing stock is counted, not fatal
            return sym, None

    with ThreadPoolExecutor(max_workers=workers) as ex:
        series = {s: b for s, b in ex.map(load, universe) if b and b[0]["date"] < WINDOW_END}
    jumped = unit_jumps(series)   # rerun addendum: a >50x one-day move is a data error; the stock is left out
    series = {k: v for k, v in series.items() if k not in jumped}
    window_members = set()
    for d in calendar:
        if d >= TRADING_FROM and d[8:] <= "07":     # month starts are enough to list who could be bought
            window_members |= membership.members_on(d)
    m = Market(calendar, series, bench, membership.members_on)
    m.missing = sorted(window_members - set(series))
    m.start_day = TRADING_FROM
    m.point_in_time = True
    m.excluded = jumped
    return m


# --------------------------------------------------------------------------- #
# the variants
# --------------------------------------------------------------------------- #
def sector_relative_ranker(industries: dict[str, str]) -> Callable[[Market, int], list[str]]:
    """Variant C: the screen's composite score z-scored within each industry that has at least SECTOR_MIN_NAMES
    eligible names that day; other names keep their z across all eligible names; ranked on that."""
    def z(values: dict[str, float]) -> dict[str, float]:
        if len(values) < 2:
            return {k: 0.0 for k in values}
        mu, sd = statistics.fmean(values.values()), statistics.pstdev(values.values())
        return {k: (v - mu) / sd if sd else 0.0 for k, v in values.items()}

    def rank(m: Market, t: int) -> list[str]:
        scores = m.momentum_scores(t)
        overall = z(scores)
        groups: dict[str, dict[str, float]] = {}
        for s, v in scores.items():
            if industries.get(s):
                groups.setdefault(industries[s], {})[s] = v
        final = dict(overall)
        for members in groups.values():
            if len(members) >= SECTOR_MIN_NAMES:
                final.update(z(members))
        return sorted(final, key=lambda s: (-final[s], s))
    return rank


def residual_momentum_ranker() -> Callable[[Market, int], list[str]]:
    """Variant E (preregistered-batch-2026-10-16b.md): the eligible names ranked by the t-statistic of their residual
    daily returns over sessions t-252 .. t-21, after an OLS fit through the origin on the benchmark's daily returns (an
    intercept fitted on the same days would absorb each stock's excess return and make every residual sum zero)."""
    def score(stock: list[float | None], bench: list[float | None], t: int) -> float | None:
        xs, ys = [], []
        for k in range(max(1, t - RESIDUAL_WINDOW + 1), t - RESIDUAL_SKIP + 1):
            s0, s1, b0, b1 = stock[k - 1], stock[k], bench[k - 1], bench[k]
            if s0 and s1 and b0 and b1:
                xs.append(math.log(b1 / b0))
                ys.append(math.log(s1 / s0))
        n = len(xs)
        if n < RESIDUAL_MIN_DAYS:
            return None
        sxx = sum(x * x for x in xs)
        b = sum(x * y for x, y in zip(xs, ys)) / sxx if sxx else 0.0   # through the origin: alpha stays in e
        e = [y - b * x for x, y in zip(xs, ys)]
        sd = statistics.pstdev(e)
        return sum(e) / (sd * math.sqrt(n)) if sd else None

    def rank(m: Market, t: int) -> list[str]:
        scores = {}
        for sym in m.momentum_ranking(t):
            v = score(m.adj[sym], m.fund_adj, t)
            if v is not None:
                scores[sym] = v
        return sorted(scores, key=lambda s: (-scores[s], s))
    return rank


def variants(industries: dict[str, str]) -> list[tuple[str, str, Rules, Callable[[Market, int], list[str]] | None]]:
    """(id, name, rules, ranker) exactly as pre-registered; A0 is the adopted rules."""
    from .research_studies import FINALIST, size_ranker
    return [("A0", "control: the adopted rules", FINALIST, None),
            ("A", "larger half by traded value", FINALIST, size_ranker("larger")),
            ("B", "top 10", with_rules(FINALIST, top=10, buffer=18, tax_aware_floor=25), None),
            ("C", "sector-relative strength", FINALIST, sector_relative_ranker(industries)),
            ("D", f"at most {INDUSTRY_CAP} per industry", with_rules(FINALIST, industry_cap=INDUSTRY_CAP), None),
            ("E", "residual momentum", FINALIST, residual_momentum_ranker())]


def backward_batch(m: Market, industries: dict[str, str], *, cost_model: Any, capital: float = 500_000.0,
                   extra_trials: int = 0) -> dict[str, Any]:
    """Run the five pre-registered rule sets once and judge them by the pre-registered rules."""
    from .research_studies import _first_ranked_day, _pbo, _skill
    start, end = _first_ranked_day(m), len(m.dates) - 1
    if m.dates[start] < TRADING_FROM or m.dates[end] >= WINDOW_END:
        raise ValueError(f"the window must run {TRADING_FROM} to before {WINDOW_END}")
    runs: dict[str, dict[str, Any]] = {}
    names: dict[str, str] = {}
    for vid, name, rules, ranker in variants(industries):
        runs[vid] = simulate(m, rules, start, end, capital=capital, cost_model=cost_model, ranker=ranker,
                             industries=industries)
        names[vid] = name
    results = {k: summarize(r, m, start, end) for k, r in runs.items()}
    skill = _skill(runs, m, start, end, extra_trials)
    c = results["A0"]
    control_checks = {
        "beats the benchmark": c["excess"] is not None and c["excess"] > 0,
        "worst fall within 10 points of the benchmark's": (c["max_drawdown"] is not None
                                                           and c["fund_max_drawdown"] is not None
                                                           and c["max_drawdown"] >= c["fund_max_drawdown"]
                                                           - CONTROL_FALL_MARGIN),
    }
    control_pass = all(control_checks.values())
    for k, x in results.items():
        x["name"], x["skill"] = names[k], skill[k]
        if k == "A0":
            x["checks"], x["passes"] = control_checks, control_pass
            continue
        checks = {
            "beats the benchmark": x["excess"] is not None and x["excess"] > 0,
            "+2 points a year over the control": (x["cagr"] is not None and c["cagr"] is not None
                                                  and x["cagr"] - c["cagr"] >= MIN_CAGR_GAIN),
            "worst fall within 5 points of the control's": (x["max_drawdown"] is not None and c["max_drawdown"] is not None
                                                            and x["max_drawdown"] >= c["max_drawdown"] - VARIANT_FALL_MARGIN),
            "turnover at most 1.5x the control's": (x["turnover"] is not None and c["turnover"] is not None
                                                    and x["turnover"] <= MAX_TURNOVER_RATIO * c["turnover"]),
        }
        x["checks"], x["passes"] = checks, all(checks.values())
    return {"start": m.dates[start], "end": m.dates[end], "results": results, "control_passes": control_pass,
            "passing_variants": [k for k, x in results.items() if k != "A0" and x["passes"]],
            "pbo": _pbo(runs, m, start, end), "missing": list(m.missing), "members": len(m.raw) + len(m.missing)}


def format_backward(r: dict[str, Any], membership_notes: list[str] | None = None) -> str:
    def pct(v: Any, f: str = "+.1f") -> str:
        return "n/a" if v is None or (isinstance(v, float) and math.isnan(v)) else f"{v * 100:{f}}%"
    from .research_studies import pbo_line
    first = r["results"]["A0"]
    lines = [f"THE BACKWARD CHECK, run once: the pre-registered batch ({PREREGISTRATION}), {r['start']} to {r['end']}, "
             "after charges and tax as they applied then.",
             f"  Benchmark: Nifty Midcap 150 total return index less {FUND_FEE * 100:.2f}% a year: "
             f"{pct(first['fund_total_return'])}, worst fall {pct(first['fund_max_drawdown'])}.",
             f"  {'variant':<36}{'return':>9}{'vs bench':>10}{'a year':>8}{'worst fall':>11}{'turnover':>9}"
             f"{'skill':>7}  verdict"]
    for k, x in r["results"].items():
        p = x["skill"].get("probability")
        lines.append(f"  {k + ' ' + x['name']:<36}{pct(x['total_return']):>9}{pct(x['excess']):>10}{pct(x['cagr']):>8}"
                     f"{pct(x['max_drawdown']):>11}{pct(x['turnover'], '.0f'):>9}{'n/a' if p is None else f'{p:.2f}':>7}"
                     f"  {'PASS' if x['passes'] else 'fail'}")
    for k, x in r["results"].items():
        failed = [name for name, ok in x["checks"].items() if not ok]
        if failed:
            lines.append(f"  {k} misses: {'; '.join(failed)}.")
    lines.append(pbo_line(r["pbo"]))
    k, n = len(r["missing"]), r["members"]
    lines.append(f"  Survivorship: {k} of {n} index members in the window have no price history and are left out"
                 + (f" ({', '.join(r['missing'][:12])}{' …' if k > 12 else ''})" if k else "")
                 + ". Stocks that later collapsed or delisted are the usual reason, so these returns may be flattered.")
    for note in membership_notes or []:
        lines.append(f"  Membership: {note}")
    verdict = ("the adopted rules PASSED the backward check" if r["control_passes"] else
               "the adopted rules did NOT pass the backward check")
    lines.append(f"  Verdict: {verdict}. Variants passing: {', '.join(r['passing_variants']) or 'none'}"
                 + ("; each must still beat the control over 12 months of forward paper trading before it can replace "
                    "it" if r["passing_variants"] else "; the adopted rules stay as they are")
                 + ". Skill odds are information only (3.5 years cannot reach 0.95). This window is now spent.")
    return "\n".join(lines)
