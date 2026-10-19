"""The pre-registered India test (docs/research/preregistered-india-2007-2016.md): the adopted rules, and B and E for
information, once, on Oct 2007 to Mar 2016, on a point-in-time top-500 universe rebuilt from NSE's own daily files.

Data, all from NSE and nothing dated on or after WINDOW_END:
- every daily bhavcopy (``cmDDMONYYYYbhav.csv.zip``) from DATA_FROM, EQ series, symbols mapped to today's;
- corporate actions (``api/corporates-corporateActions``): splits and bonuses adjust ``close`` (and volume), cash
  dividends also adjust ``adj_close``;
- the Nifty 500 total return index less FUND_FEE a year: the calendar, the regime filter and the benchmark.

A data gate (``gate``) runs before any rule set is simulated; a failed gate prints its counts and no returns.
"""
from __future__ import annotations

import csv
import io
import json
import math
import re
import zipfile
from bisect import bisect_left
from collections.abc import Callable, Iterable
from datetime import date, datetime
from pathlib import Path
from typing import Any

# --- fixed by the pre-registration (commit 75a27ba, 19 Oct 2026); do not edit ---------------------------------- #
DATA_FROM = "2006-07-01"
TRADING_FROM = "2007-10-01"
WINDOW_END = "2016-04-01"        # first date NOT loaded
FUND_FEE = 0.0020
UNIVERSE_SIZE = 500
LOOKBACK_SESSIONS = 126
MIN_TRADED_SESSIONS = 100
RECENT_SESSIONS = 5
COST_MULT_PRIMARY = 2.0
COST_MULT_SENSITIVITY = 1.0
GATE_RATIO_READ = 0.98
GATE_MIN_MEMBERS = 400
GATE_FILES = 0.99
GATE_SYMBOL_MATCH = 0.95
GATE_MAX_UNRECORDED = 15
SPLIT_RATIOS = (1 / 2, 1 / 3, 2 / 3, 1 / 4, 3 / 4, 1 / 5, 2 / 5, 1 / 10, 1 / 2.5)
SPLIT_TOLERANCE = 0.015
SPLIT_WINDOW = 3                 # sessions either side
FUND_PATTERN = re.compile(r"BEES$|ETF|^GOLD|^LIQUID|^NIFTY|^BANKNIFTY|^JUNIOR|^PSUBNK|^INFRA|^SHARIA")
STUDY = "India-2007-2016"
PREREGISTRATION = "docs/research/preregistered-india-2007-2016.md (commit 75a27ba) + addendum"
# ------------------------------------------------------------------------------------------------------------------ #

TRI_NAME = "NIFTY 500"
BHAV_URL = "https://nsearchives.nseindia.com/content/historical/EQUITIES/{y}/{mon}/cm{d:02d}{mon}{y}bhav.csv.zip"
MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 "
                         "Safari/537.36", "Accept": "*/*"}

_SPLIT_OLD = re.compile(r"split\W*(?:from\s*)?r[se]\.?\s*([\d.]+)\D*?to\s*r[se]\.?\s*([\d.]+)", re.I)
_BONUS_OLD = re.compile(r"bonus\s*[-:]?\s*(\d+)\s*:\s*(\d+)", re.I)
_DIV = re.compile(r"div(?:idend)?[a-z.]*\s*[-:]?\s*(?:of\s*)?r[se]\.?\s*([\d.]+)", re.I)


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #
def bhav_url(day: str) -> str:
    d = date.fromisoformat(day)
    return BHAV_URL.format(y=d.year, mon=MONTHS[d.month - 1], d=d.day)


def parse_bhav(text: str) -> list[dict[str, Any]]:
    """One day's NSE bhavcopy -> EQ rows {symbol, close, volume, value, isin}."""
    out = []
    for r in csv.DictReader(io.StringIO(text)):
        r = {str(k).strip().upper(): (v or "").strip() for k, v in r.items() if k}
        if r.get("SERIES") != "EQ":
            continue
        try:
            close, qty, val = float(r["CLOSE"]), float(r["TOTTRDQTY"]), float(r["TOTTRDVAL"])
        except (KeyError, ValueError):
            continue
        if close > 0 and qty > 0:
            out.append({"symbol": r["SYMBOL"].upper(), "close": close, "volume": qty, "value": val,
                        "isin": r.get("ISIN", "")})
    return out


def split_ratio(subject: str) -> float | None:
    """Shares after / before for a split or bonus subject (1:1 bonus = 2, Rs 10 -> Rs 2 = 5), or None. Bonus
    debentures and other non-share bonuses are not read."""
    from .corp_actions import parse_subject
    text = str(subject or "")
    if re.search(r"bonus\s*deb", text, re.I):
        return None
    got = parse_subject(text)
    if got and got.get("ratio"):
        return float(got["ratio"])
    ratio = 1.0
    for m in _BONUS_OLD.finditer(text):
        a, b = int(m.group(1)), int(m.group(2))
        if a > 0 and b > 0:
            ratio *= (a + b) / b
    fv = _SPLIT_OLD.search(text)
    if fv:
        try:
            old, new = float(fv.group(1).rstrip(".")), float(fv.group(2).rstrip("."))
        except ValueError:
            old = new = 0.0
        if old > 0 and new > 0 and old != new:
            ratio *= old / new
    return None if ratio == 1.0 else round(ratio, 6)


def is_split_or_bonus(subject: str) -> bool:
    text = str(subject or "")
    return bool(re.search(r"split|sub-?division|bonus|consolidation", text, re.I)) \
        and not re.search(r"bonus\s*deb", text, re.I)


def dividend_amount(subject: str) -> float | None:
    """Cash per share from a dividend subject (all amounts in it summed), or None when none is stated in rupees."""
    text = str(subject or "")
    if not re.search(r"\bdiv", text, re.I):
        return None
    total = 0.0
    for g in _DIV.findall(text):
        try:
            total += float(g.rstrip("."))
        except ValueError:
            pass
    return total or None


def parse_actions(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """NSE corporate-action rows -> {"splits": {sym: [(ex, ratio)]}, "dividends": {sym: [(ex, amount)]},
    "split_rows": n, "split_read": n, "split_records": [(sym, ex)]}."""
    splits: dict[str, list[tuple[str, float]]] = {}
    divs: dict[str, list[tuple[str, float]]] = {}
    n = read = 0
    records: list[tuple[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for r in rows:
        sym = str(r.get("symbol") or "").strip().upper()
        subject = str(r.get("subject") or "")
        try:
            ex = datetime.strptime(str(r.get("exDate") or "").strip(), "%d-%b-%Y").date().isoformat()
        except ValueError:
            continue
        key = (sym, ex, subject)
        if not sym or key in seen:
            continue
        seen.add(key)
        if is_split_or_bonus(subject):
            n += 1
            records.append((sym, ex))
            ratio = split_ratio(subject)
            if ratio:
                read += 1
                splits.setdefault(sym, []).append((ex, ratio))
        amount = dividend_amount(subject)
        if amount:
            divs.setdefault(sym, []).append((ex, amount))
    return {"splits": splits, "dividends": divs, "split_rows": n, "split_read": read, "split_records": records}


# --------------------------------------------------------------------------- #
# building the series
# --------------------------------------------------------------------------- #
def build_series(days: list[str], files: dict[str, list[dict[str, Any]]], rename: Callable[[str, str], str],
                 actions: dict[str, Any]) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    """Per-symbol daily bars {date, close, adj_close, volume, value, raw_close} in today's symbols, adjusted for
    splits and bonuses (close, volume) and dividends (adj_close). Also returns {"funds": set, "dividends_skipped": n}."""
    raw: dict[str, list[dict[str, Any]]] = {}
    funds: set[str] = set()
    for d in days:
        for r in files.get(d) or []:
            sym = rename(r["symbol"], d)
            if r.get("isin", "").startswith("INF"):
                funds.add(sym)
            raw.setdefault(sym, []).append({"date": d, "close": r["close"], "volume": r["volume"],
                                            "value": r["value"], "raw_close": r["close"]})
    skipped = 0
    for sym, bars in raw.items():
        dates = [b["date"] for b in bars]
        splits = [(ex, ratio) for ex, ratio in actions["splits"].get(sym, []) if dates[0] < ex <= dates[-1]]
        for ex, ratio in splits:                   # every bar before the ex-date: price / ratio, volume * ratio
            for b in bars[:bisect_left(dates, ex)]:
                b["close"] /= ratio
                b["volume"] *= ratio
        for b in bars:
            b["adj_close"] = b["close"]
        for ex, amount in actions["dividends"].get(sym, []):
            i = bisect_left(dates, ex)
            if i <= 0 or i >= len(bars):
                continue
            same_day = math.prod(r for e, r in splits if e == dates[i])
            prev = bars[i - 1]["raw_close"] / same_day   # in the ex-date's shares
            if not prev or amount / prev > 0.3:        # a percentage read as rupees, or a capital return
                skipped += 1
                continue
            f = 1 - amount / prev
            for b in bars[:i]:
                b["adj_close"] *= f
    return raw, {"funds": funds, "dividends_skipped": skipped}


def month_starts(days: list[str]) -> list[int]:
    out, seen = [], set()
    for i, d in enumerate(days):
        if d[:7] not in seen:
            seen.add(d[:7])
            out.append(i)
    return out


def universe_by_month(days: list[str], series: dict[str, list[dict[str, Any]]], funds: set[str]) -> dict[str, set[str]]:
    """{YYYY-MM: members} decided at each month's first session from the sessions before it (the pre-registered
    rule): traded on >= 100 of the last 126 sessions and on one of the last 5; not a fund; top 500 by average daily
    traded value over those 126 sessions."""
    pos = {d: i for i, d in enumerate(days)}
    idx: dict[str, list[int]] = {}
    cum: dict[str, list[float]] = {}
    for s, bars in series.items():
        if s in funds or FUND_PATTERN.search(s):
            continue
        ks, c = [], [0.0]
        for bar in bars:
            if bar["date"] in pos:
                ks.append(pos[bar["date"]])
                c.append(c[-1] + bar["value"])
        idx[s], cum[s] = ks, c
    out: dict[str, set[str]] = {}
    for t in month_starts(days):
        if t < LOOKBACK_SESSIONS:
            out[days[t][:7]] = set()
            continue
        lo, recent = t - LOOKBACK_SESSIONS, t - RECENT_SESSIONS
        scores = {}
        for sym, ks in idx.items():
            a, b = bisect_left(ks, lo), bisect_left(ks, t)
            if b - a < MIN_TRADED_SESSIONS or ks[b - 1] < recent:
                continue
            scores[sym] = (cum[sym][b] - cum[sym][a]) / LOOKBACK_SESSIONS
        out[days[t][:7]] = set(sorted(scores, key=lambda s: (-scores[s], s))[:UNIVERSE_SIZE])
    return out


# --------------------------------------------------------------------------- #
# the data gate
# --------------------------------------------------------------------------- #
def unrecorded_splits(days: list[str], series: dict[str, list[dict[str, Any]]], members: dict[str, set[str]],
                      actions: dict[str, Any]) -> list[dict[str, Any]]:
    """Member-days whose raw one-day close ratio is within SPLIT_TOLERANCE of a split ratio, with no split or bonus
    recorded within SPLIT_WINDOW sessions."""
    pos = {d: i for i, d in enumerate(days)}
    recorded: dict[str, list[int]] = {}
    for sym, ex in actions["split_records"]:
        k = bisect_left(days, ex)
        recorded.setdefault(sym, []).append(k)
    out = []
    for sym, bars in series.items():
        for a, b in zip(bars, bars[1:]):
            if sym not in members.get(b["date"][:7], ()):
                continue
            r = b["raw_close"] / a["raw_close"] if a["raw_close"] else 0
            if not any(abs(r - x) <= SPLIT_TOLERANCE * x for x in SPLIT_RATIOS):
                continue
            at = pos.get(b["date"])
            if at is None or any(abs(at - j) <= SPLIT_WINDOW for j in recorded.get(sym, [])):
                continue
            out.append({"symbol": sym, "date": b["date"], "ratio": round(r, 4)})
    return out


def gate(*, days: list[str], present: set[str], actions: dict[str, Any], series: dict[str, list[dict[str, Any]]],
         members: dict[str, set[str]], trading_months: list[str]) -> dict[str, Any]:
    """The five pre-registered data checks. ``days``: the benchmark's sessions in the window; ``present``: the days
    with a price file."""
    n, read = actions["split_rows"], actions["split_read"]
    by_month: dict[str, set[str]] = {}
    for sym, bars in series.items():
        for b in bars:
            by_month.setdefault(b["date"][:7], set()).add(sym)
    recs = [(s, ex) for s, ex in actions["split_records"] if DATA_FROM <= ex < WINDOW_END]
    matched = sum(1 for s, ex in recs if s in by_month.get(ex[:7], ()))
    sizes = {mth: len(members.get(mth, ())) for mth in trading_months}
    smallest = min(sizes.items(), key=lambda kv: kv[1]) if sizes else ("", 0)
    unrec = unrecorded_splits(days, series, members, actions)
    checks = {
        f"split and bonus ratios read >= {GATE_RATIO_READ:.0%} ({read} of {n})": n > 0 and read / n >= GATE_RATIO_READ,
        f"universe >= {GATE_MIN_MEMBERS} at every month start (smallest {smallest[1]}, {smallest[0]})":
            bool(sizes) and smallest[1] >= GATE_MIN_MEMBERS,
        f"price files for >= {GATE_FILES:.0%} of sessions ({len(present & set(days))} of {len(days)})":
            bool(days) and len(present & set(days)) / len(days) >= GATE_FILES,
        f"split and bonus records matching a traded symbol >= {GATE_SYMBOL_MATCH:.0%} ({matched} of {len(recs)})":
            bool(recs) and matched / len(recs) >= GATE_SYMBOL_MATCH,
        f"unrecorded split-like moves <= {GATE_MAX_UNRECORDED} ({len(unrec)})": len(unrec) <= GATE_MAX_UNRECORDED,
    }
    return {"passes": all(checks.values()), "checks": checks, "unrecorded": unrec}


def format_gate(g: dict[str, Any]) -> str:
    lines = ["DATA GATE (pre-registered; checked before any rule set is simulated):"]
    lines += [f"  [{'ok' if ok else 'FAIL'}] {name}" for name, ok in g["checks"].items()]
    for u in g["unrecorded"][:40]:
        lines.append(f"    split-like move without a record: {u['symbol']} {u['date']} x{u['ratio']}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# downloads (cached)
# --------------------------------------------------------------------------- #
def fetch_bhav(day: str, cache: Path, session: Any) -> list[dict[str, Any]] | None:
    """One day's EQ rows, cached as CSV text; None when NSE has no file for the day."""
    cache.mkdir(parents=True, exist_ok=True)
    f = cache / f"{day}.csv"
    miss = cache / f"{day}.none"
    if f.exists():
        return parse_bhav(f.read_text(encoding="latin-1"))
    if miss.exists():
        return None
    r = session.get(bhav_url(day), headers=HEADERS, timeout=60)
    if r.status_code != 200 or not r.content.startswith(b"PK"):
        if r.status_code in (403, 404) or not r.content.startswith(b"PK"):
            miss.write_text(str(r.status_code))
        return None
    z = zipfile.ZipFile(io.BytesIO(r.content))
    text = z.read(z.namelist()[0]).decode("latin-1")
    f.write_text(text, encoding="latin-1")
    return parse_bhav(text)


def fetch_actions(cache: Path, client: Any) -> list[dict[str, Any]]:
    """NSE's corporate actions from DATA_FROM to WINDOW_END, by half-year, cached."""
    cache.mkdir(parents=True, exist_ok=True)
    f = cache / f"corporate_actions_{DATA_FROM}_{WINDOW_END}.json"
    if f.exists():
        return json.loads(f.read_text())
    rows: list[dict[str, Any]] = []
    y0, y1 = int(DATA_FROM[:4]), int(WINDOW_END[:4])
    for y in range(y0, y1 + 1):
        for a, b in (("01-01", "30-06"), ("01-07", "31-12")):
            start = f"{y}-{a[3:]}-{a[:2]}"
            if start >= WINDOW_END or f"{y}-{b[3:]}-{b[:2]}" < DATA_FROM:
                continue
            d = client._get("api/corporates-corporateActions",
                            {"index": "equities", "from_date": f"{a}-{y}", "to_date": f"{b}-{y}"})
            got = d if isinstance(d, list) else (d.get("data") or [])
            rows += [r for r in got if isinstance(r, dict)]
    f.write_text(json.dumps(rows))
    return rows


# --------------------------------------------------------------------------- #
# the run
# --------------------------------------------------------------------------- #
def rule_sets() -> list[tuple[str, str, Any, Callable[..., list[str]] | None]]:
    from .research_backward import residual_momentum_ranker
    from .research_sim import with_rules
    from .research_studies import FINALIST
    return [("A0", "the adopted rules", FINALIST, None),
            ("B", "top 10", with_rules(FINALIST, top=10, buffer=18, tax_aware_floor=25), None),
            ("E", "residual momentum", FINALIST, residual_momentum_ranker()),
            # addendum (preregistered-india-2007-2016-addendum.md): four ideas that failed in the research years
            ("BRK", "drawdown brake (100-day)", with_rules(FINALIST, brake="sma100"), None),
            ("STP", "stop-loss 3x ATR", with_rules(FINALIST, stop="atr3"), None),
            ("IVW", "inverse-volatility weights", with_rules(FINALIST, weights="inverse_vol"), None),
            ("PLN", "plain top 20, no buffer", with_rules(FINALIST, buffer=None, tax_aware=False), None)]


def make_market(days: list[str], series: dict[str, list[dict[str, Any]]], bench: list[dict[str, Any]],
                members: dict[str, set[str]]) -> Any:
    from .research_replication import unit_jumps
    from .research_sim import Market
    jumped = unit_jumps(series)
    series = {k: v for k, v in series.items() if k not in jumped}
    m = Market(days, series, bench, lambda d: members.get(d[:7], set()))
    m.start_day = TRADING_FROM
    m.point_in_time = True
    m.excluded = list(jumped)
    return m


def delisting_exits(run: dict[str, Any], m: Any) -> tuple[int, float]:
    """Positions sold after their stock had stopped trading (last bar more than RECENT_SESSIONS sessions before the
    sale), and the share of the run's sales they made up."""
    pos = {d: i for i, d in enumerate(m.dates)}
    n = 0
    for tr in run["trades"]:
        dates = m.raw_dates.get(tr["symbol"]) or []
        k = bisect_left(dates, tr["exit"])
        last = dates[k] if k < len(dates) and dates[k] == tr["exit"] else (dates[k - 1] if k else None)
        if last and pos.get(tr["exit"], 0) - pos.get(last, 0) > RECENT_SESSIONS:
            n += 1
    return n, (n / len(run["trades"]) if run["trades"] else 0.0)


def run_india(m: Any, *, cost_model: Any, capital: float = 500_000.0, extra_trials: int = 0) -> dict[str, Any]:
    """The pre-registered run: A0, B and E at charges x2 (judged) and x1 (reported only)."""
    from .research_backward import CONTROL_FALL_MARGIN, MAX_TURNOVER_RATIO, MIN_CAGR_GAIN, VARIANT_FALL_MARGIN
    from .research_sim import simulate, summarize, with_rules
    from .research_studies import _first_ranked_day, _pbo, _skill
    start, end = _first_ranked_day(m), len(m.dates) - 1
    if m.dates[start] < TRADING_FROM or m.dates[end] >= WINDOW_END:
        raise ValueError(f"the window must run {TRADING_FROM} to before {WINDOW_END}")
    out: dict[str, Any] = {"start": m.dates[start], "end": m.dates[end]}
    for label, mult in (("primary", COST_MULT_PRIMARY), ("sensitivity", COST_MULT_SENSITIVITY)):
        runs, names = {}, {}
        for vid, name, rules, ranker in rule_sets():
            runs[vid] = simulate(m, with_rules(rules, cost_mult=mult), start, end, capital=capital,
                                 cost_model=cost_model, ranker=ranker)
            names[vid] = name
        results = {k: summarize(r, m, start, end) for k, r in runs.items()}
        for k, x in results.items():
            x["name"] = names[k]
            x["delisting_exits"] = delisting_exits(runs[k], m)[0]
        if label == "primary":
            skill = _skill(runs, m, start, end, extra_trials)
            c = results["A0"]
            for k, x in results.items():
                x["skill"] = skill[k]
                if k == "A0":
                    x["checks"] = {
                        "beats the benchmark": c["excess"] is not None and c["excess"] > 0,
                        "worst fall within 10 points of the benchmark's": (
                            c["max_drawdown"] is not None and c["fund_max_drawdown"] is not None
                            and c["max_drawdown"] >= c["fund_max_drawdown"] - CONTROL_FALL_MARGIN)}
                else:
                    x["checks"] = {
                        "beats the benchmark": x["excess"] is not None and x["excess"] > 0,
                        "+2 points a year over A0": (x["cagr"] is not None and c["cagr"] is not None
                                                     and x["cagr"] - c["cagr"] >= MIN_CAGR_GAIN),
                        "worst fall within 5 points of A0's": (
                            x["max_drawdown"] is not None and c["max_drawdown"] is not None
                            and x["max_drawdown"] >= c["max_drawdown"] - VARIANT_FALL_MARGIN),
                        "turnover at most 1.5x A0's": (x["turnover"] is not None and c["turnover"] is not None
                                                       and x["turnover"] <= MAX_TURNOVER_RATIO * c["turnover"])}
                x["passes"] = all(x["checks"].values())
            out["pbo"] = _pbo(runs, m, start, end)
        out[label] = results
    return out


def format_india(r: dict[str, Any], notes: list[str]) -> str:
    from .research_studies import pbo_line

    def pct(v: Any, f: str = "+.1f") -> str:
        return "n/a" if v is None or (isinstance(v, float) and math.isnan(v)) else f"{v * 100:{f}}%"
    p = r["primary"]
    a0 = p["A0"]
    lines = [f"THE INDIA TEST, run once ({PREREGISTRATION}), {r['start']} to {r['end']}, top-500 NSE universe by traded "
             f"value, after charges x{COST_MULT_PRIMARY:g} and tax as it applied then.",
             f"  Benchmark: Nifty 500 total return index less {FUND_FEE * 100:.2f}% a year: "
             f"{pct(a0['fund_total_return'])}, worst fall {pct(a0['fund_max_drawdown'])}.",
             f"  {'rule set':<34}{'return':>10}{'vs bench':>10}{'a year':>8}{'worst fall':>11}{'turnover':>9}"
             f"{'skill':>7}  verdict"]
    for k, x in p.items():
        sp = x["skill"].get("probability")
        lines.append(f"  {k + ' ' + x['name']:<34}{pct(x['total_return']):>10}{pct(x['excess']):>10}{pct(x['cagr']):>8}"
                     f"{pct(x['max_drawdown']):>11}{pct(x['turnover'], '.0f'):>9}"
                     f"{'n/a' if sp is None else f'{sp:.2f}':>7}  {'PASS' if x['passes'] else 'fail'}")
    for k, x in p.items():
        failed = [n for n, ok in x["checks"].items() if not ok]
        if failed:
            lines.append(f"  {k} misses: {'; '.join(failed)}.")
    lines.append(f"  A0 verdict: {'PASSED' if a0['passes'] else 'NOT passed'}. B and E are information for their paper "
                 "accounts; BRK, STP or IVW passing earns a paper account; PLN failing supports the rank buffer. "
                 "Nothing is adopted from this test.")
    lines.append(pbo_line(r["pbo"]))
    s = r["sensitivity"]
    lines.append("  Sensitivity, charges x1 (reported, not judged): " + "; ".join(
        f"{k} {pct(x['total_return'])} ({pct(x['excess'])} vs bench)" for k, x in s.items()) + ".")
    lines.append("  Positions sold after their stock stopped trading (at its last close, no haircut): "
                 + ", ".join(f"{k} {x['delisting_exits']}" for k, x in p.items()) + ".")
    lines += [f"  Data: {n}" for n in notes]
    return "\n".join(lines)
