"""Blind-ticker test for Replay's Ask Claude: does Claude's answer change when it cannot tell which company it is?

Claude's weights may know what happened after a replay date (parametric hindsight) even though the tools only show
past data. For each case (replay date + ticker) the exact Ask-Claude input is built twice and sent through the same
forced ``record_view`` call:

* unmasked: the real ticker, company name and sector, announcements as category and date (no text);
* masked: ticker and company become COMPANY_A, the sector stays as a generic label, dates become relative trading
  days (D-250 ... D0), prices are indexed to 100 at the start of the window (returns, ATR % and volume ratios are
  unchanged), announcements keep only their category and relative day (the text names the company), and the
  market's index level is indexed the same way so it cannot date the case.

Headlines are dropped from both runs (Ask Claude's input carries none), so the only difference is identity.

The comparison: stance (buy / sell / neutral = hold or watch / none) and confidence (low 25, medium 50, high 75, none 0)
per case, and the hindsight signature, computed AFTER both answers from prices after the replay date: the unmasked
answer is more confident, in signed terms, in the direction the stock then actually went. That last number uses the
future on purpose and is only for judging the test.

Noise baseline: each input is asked twice (U1, U2 unmasked; M1, M2 masked). Identity effect = U1 vs M1; noise = U1 vs
U2 and M1 vs M2 (mean of the two pairs per case). Verdict: "possible hindsight" only when (identity stance changes -
noise stance changes) is at least 40% of the cases (2 of 5) or (mean identity confidence gap - mean noise gap) is at
least 15 points; otherwise "no sign of hindsight". With few cases this is a smoke test, not proof.
It costs real API money (four calls per case), so the CLI needs --yes and an API key, and it never runs by itself.
"""

from __future__ import annotations

import bisect
import json
import math
import random
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from .claude import SYSTEM, build_context, request_view
from .clock import EARLIEST_START, ClockedPrices, ReplayClock
from .news import ClockedNews

ALIAS = "COMPANY_A"
DEFAULT_CASES = 5
HORIZON_BARS = 60          # outcome window after the replay date, in trading days
MIN_OUTCOME_BARS = 20      # fewer bars than this after the date: the case is not scored
FLIP_FRACTION = 0.4        # net stance changes in at least 2 of 5 cases
CALLS_PER_CASE = 4        # unmasked twice, masked twice (the repeats measure sampling noise)
CONF_GAP_POINTS = 15.0     # or a mean absolute confidence gap of this many points
CONF_POINTS = {"low": 25.0, "medium": 50.0, "high": 75.0}
OUTPUT_TOKENS_GUESS = 700  # per call, for the cost estimate
_STOP_WORDS = {"ltd", "limited", "the", "and", "of", "co", "pvt", "inc", "corp"}
_ISO = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass
class Case:
    slug: str
    date: str
    ticker: str

    def to_dict(self) -> dict[str, str]:
        return {"slug": self.slug, "date": self.date, "ticker": self.ticker}


# -- picking cases -------------------------------------------------------------------------
def pick_cases(trial_data: dict[str, Any], n: int = DEFAULT_CASES, *, today: str | None = None,
               seed: int = 0, min_age_days: int = 45) -> list[Case]:
    """N (date, ticker) cases from a replay's rebalance history, spread over distinct tickers where possible.
    Old enough to have an outcome when there are enough of them; the same seed gives the same cases."""
    slug = trial_data.get("slug", "")
    today = today or date.today().isoformat()
    cutoff = (date.fromisoformat(today) - timedelta(days=min_age_days)).isoformat()
    pairs = sorted({(r["date"], t) for r in trial_data.get("rebalances", []) for t in r.get("picks", [])})
    old = [p for p in pairs if p[0] <= cutoff]
    pool = old if len(old) >= n else pairs
    rng = random.Random(seed)
    rng.shuffle(pool)
    chosen: list[tuple[str, str]] = []
    seen: set[str] = set()
    for d, t in pool:          # distinct tickers first
        if t not in seen and len(chosen) < n:
            chosen.append((d, t))
            seen.add(t)
    for p in pool:
        if len(chosen) >= n:
            break
        if p not in chosen:
            chosen.append(p)
    return [Case(slug, d, t) for d, t in sorted(chosen)]


# -- masking -------------------------------------------------------------------------------
class MaskedPrices:
    """History with relative-day labels and prices indexed to 100 at the first bar of the window."""

    def __init__(self, prices: ClockedPrices, real: str, alias: str = ALIAS):
        self.prices, self.real, self.alias = prices, real.upper(), alias

    def _sym(self, symbol: str) -> str:
        return self.real if symbol.upper() == self.alias else symbol

    def history(self, symbol: str, range_: str = "2y") -> list[dict[str, Any]]:
        bars = self.prices.history(self._sym(symbol), range_)
        if not bars or not bars[0].get("close"):
            return []
        k = 100.0 / bars[0]["close"]
        n = len(bars)
        out = []
        for i, b in enumerate(bars):
            row = {key: v for key, v in b.items() if key != "date"}
            for key in ("close", "adj_close", "open", "high", "low"):
                if row.get(key) is not None:
                    row[key] = row[key] * k
            row["date"] = f"D-{n - 1 - i}" if i < n - 1 else "D0"
            out.append(row)
        return out

    def relative_day(self, day: str) -> str:
        _, dates = self.prices._all(self.real)
        today = self.prices.clock.today
        k = bisect.bisect_right(dates, today) - bisect.bisect_right(dates, day[:10])
        return "D0" if k <= 0 else f"D-{k}"


class MaskedNews:
    """Announcements reduced to category and relative day: the text names the company."""

    def __init__(self, news: ClockedNews, prices: MaskedPrices):
        self.news, self.prices = news, prices

    def for_symbol(self, symbol: str, days: int = 60, until: str | None = None) -> dict[str, Any]:
        r = self.news.for_symbol(self.prices._sym(symbol), days=days)
        items = [{"at": self.prices.relative_day(a["at"]), "category": a.get("category", ""), "text": ""}
                 for a in r["items"]]
        return {"items": items, "error": "news unavailable for this period" if r["error"] else None}


def _terms(ticker: str, company: str) -> list[str]:
    words = [w for w in re.split(r"[^A-Za-z0-9&]+", company) if len(w) >= 3 and w.lower() not in _STOP_WORDS]
    out = [t for t in [ticker, company, *words] if len(t) >= 3]
    return sorted(set(out), key=len, reverse=True)


def _scrub(obj: Any, terms: list[str]) -> Any:
    """Belt and braces: any remaining identifier inside a string becomes COMPANY_A."""
    if isinstance(obj, str):
        for t in terms:
            obj = re.sub(r"(?<![A-Za-z0-9_])" + re.escape(t) + r"(?![A-Za-z0-9_])", ALIAS, obj, flags=re.I)
        return obj
    if isinstance(obj, dict):
        return {k: _scrub(v, terms) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub(v, terms) for v in obj]
    return obj


def _values(obj: Any) -> list[str]:
    """Every string and number VALUE in a context (keys are our own field names, not data)."""
    if isinstance(obj, dict):
        return [v for x in obj.values() for v in _values(x)]
    if isinstance(obj, (list, tuple)):
        return [v for x in obj for v in _values(x)]
    return [] if obj is None else [str(obj)]


def leaks(masked_ctx: Any, ticker: str, company: str, sector: str = "") -> list[str]:
    """Identifiers and ISO dates still present in a masked input (empty when it is clean).

    Only the VALUES of the masked context itself are checked (not its field names), never the system prompt (whose plain words, like "Indian" or "Data",
    are also parts of real company names), and the generic sector label is allowed to keep its own words."""
    text = masked_ctx if isinstance(masked_ctx, str) else " | ".join(_values(masked_ctx))
    if sector:
        text = text.replace(sector, " ")
    found = [t for t in _terms(ticker, company)
             if re.search(r"(?<![A-Za-z0-9_])" + re.escape(t) + r"(?![A-Za-z0-9_])", text, re.I)]
    return found + _ISO.findall(text)


# -- building the paired inputs --------------------------------------------------------------
class _Book:
    def __init__(self, cash: float):
        self.cash = cash

    def account(self) -> Any:
        return SimpleNamespace(cash=self.cash)

    def positions(self) -> list[Any]:
        return []


def _shim(day_label: str, prices: Any, cash: float) -> Any:
    return SimpleNamespace(clock=SimpleNamespace(today=day_label), prices=prices, you=_Book(cash), agent=_Book(cash),
                           data={"picks": None})


class PlainNews:
    """The unmasked run's announcements: category and date only, no text, so the runs differ only in identity."""

    def __init__(self, news: ClockedNews):
        self.news = news

    def for_symbol(self, symbol: str, days: int = 60, until: str | None = None) -> dict[str, Any]:
        r = self.news.for_symbol(symbol, days=days)
        return {"items": [{"at": a["at"], "category": a.get("category", ""), "text": ""} for a in r["items"]],
                "error": r["error"]}


def build_pair(case: Case, *, source: Any, news_client: Any, members: list[dict[str, str]], field: str = "adj_close",
               cash: float = 100_000.0) -> dict[str, Any]:
    """The exact Ask-Claude input twice: {"unmasked": ctx, "masked": ctx, "labels": {...}}.

    Both contexts are build_context's output for a lookup of the ticker with an empty practice book. Both carry
    the company name and sector in the lookup block (the unmasked run is otherwise identified only by ticker) and
    both drop announcement texts (category and date stay; the date is relative in the masked run)."""
    ticker = case.ticker.upper()
    member = next((m for m in members if m["symbol"].upper() == ticker), {})
    company = member.get("name") or ticker
    sector = (member.get("industry") or "").strip().lower() or "unspecified sector"
    clock = ReplayClock(case.date)
    prices = ClockedPrices(source, clock, field=field)
    real_news = ClockedNews(news_client, clock)

    unmasked = build_context(_shim(case.date, prices, cash), PlainNews(real_news), lookup=ticker)
    unmasked["lookup"].update(company=company, sector=sector)

    mprices = MaskedPrices(prices, ticker)
    masked = build_context(_shim("D0", mprices, cash), MaskedNews(real_news, mprices), lookup=ALIAS)
    masked = _scrub(masked, _terms(ticker, company))      # company words first ...
    masked["lookup"].update(company=ALIAS, sector=sector)  # ... then the sector label, which must stay as it is
    return {"unmasked": unmasked, "masked": masked,
            "labels": {"ticker": ticker, "company": company, "sector": sector}}


def prompt_text(ctx: dict[str, Any], day_label: str) -> str:
    """Everything Claude sees for one run (system prompt plus the user message)."""
    return SYSTEM.format(date=day_label) + "\n" + json.dumps(ctx, default=str)


# -- reading answers -------------------------------------------------------------------------
def stance_of(view: dict[str, Any], ticker: str) -> dict[str, Any]:
    """What a recorded view says about the case's stock."""
    recs = [r for r in (view.get("recommendations") or []) if isinstance(r, dict)]
    mine = [r for r in recs if str(r.get("ticker", "")).upper() in {ticker.upper(), ALIAS}]
    if not mine and len(recs) == 1:
        mine = recs
    if not mine:
        return {"action": None, "stance": "none", "confidence": None, "points": 0.0, "signed": 0.0,
                "rationale": "", "summary": str(view.get("summary", ""))}
    r = mine[0]
    action = str(r.get("action", "")).lower()
    conf = str(r.get("confidence", "")).lower()
    points = CONF_POINTS.get(conf, 0.0)
    cls = "buy" if action == "buy" else "sell" if action == "sell" else "neutral" if action in ("hold", "watch") else "none"
    signed = points if cls == "buy" else -points if cls == "sell" else 0.0
    return {"action": action, "stance": cls, "confidence": conf, "points": points, "signed": signed,
            "rationale": str(r.get("rationale", "")), "headline": str(r.get("headline", "")),
            "summary": str(view.get("summary", ""))}


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{4,}", text.lower())} - {w.lower() for w in (ALIAS,)}


def reason_overlap(a: str, b: str) -> float | None:
    wa, wb = _words(a), _words(b)
    return round(len(wa & wb) / len(wa | wb), 3) if wa | wb else None


def outcome_after(source: Any, ticker: str, day: str, field: str = "adj_close") -> dict[str, Any] | None:
    """What the stock actually did after the replay date (HINDSIGHT, used only to grade the test)."""
    try:
        bars = source.history(ticker, "10y")
    except Exception:  # noqa: BLE001
        return None
    dates = [b["date"] for b in bars]
    i = bisect.bisect_right(dates, day) - 1
    if i < 0 or not bars[i].get(field):
        return None
    j = min(i + HORIZON_BARS, len(bars) - 1)
    if j - i < MIN_OUTCOME_BARS:
        return None
    ret = bars[j][field] / bars[i][field] - 1
    return {"horizon_bars": j - i, "return": round(ret, 4), "direction": 1 if ret > 0 else -1 if ret < 0 else 0,
            "until": bars[j]["date"]}


def _pair_effect(a: dict[str, Any], b: dict[str, Any]) -> tuple[bool, float]:
    return a["stance"] != b["stance"], abs(a["points"] - b["points"])


def compare_case(case: Case, u1: dict[str, Any], m1: dict[str, Any], outcome: dict[str, Any] | None,
                 u2: dict[str, Any] | None = None, m2: dict[str, Any] | None = None) -> dict[str, Any]:
    """Identity effect = U1 vs M1. Noise = U1 vs U2 and M1 vs M2 (the same input asked twice). Without the
    repeats the noise is zero."""
    flip, gap = _pair_effect(u1, m1)
    noise = [_pair_effect(a, b) for a, b in ((u1, u2), (m1, m2)) if a is not None and b is not None]
    noise_flips = sum(1 for f, _ in noise if f) / len(noise) if noise else 0.0   # mean of the two pairs: 0, 0.5 or 1
    noise_gap = sum(g for _, g in noise) / len(noise) if noise else 0.0
    sig: bool | None = None
    if outcome and outcome["direction"]:
        sig = outcome["direction"] * (u1["signed"] - m1["signed"]) > 0
    return {"case": case.to_dict(), "unmasked": u1, "masked": m1, "unmasked_repeat": u2, "masked_repeat": m2,
            "stance_flip": flip, "confidence_gap": gap, "noise_flips": noise_flips, "noise_gap": noise_gap,
            "reason_overlap": reason_overlap(u1["rationale"], m1["rationale"]),
            "outcome": outcome, "hindsight_signature": sig}


def verdict(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Possible hindsight only when the identity effect beats the noise: (identity flips - noise flips) at least
    40% of the cases (2 of 5), or (mean identity gap - mean noise gap) at least 15 points."""
    m = len(results)
    if not m:
        return {"cases": 0, "verdict": "no cases were run", "possible_hindsight": False}
    flips = sum(1 for r in results if r["stance_flip"])
    noise_flips = sum(r.get("noise_flips", 0.0) for r in results)
    mean_gap = sum(r["confidence_gap"] for r in results) / m
    noise_gap = sum(r.get("noise_gap", 0.0) for r in results) / m
    net_flips, net_gap = flips - noise_flips, mean_gap - noise_gap
    scored = [r for r in results if r["hindsight_signature"] is not None]
    sig = sum(1 for r in scored if r["hindsight_signature"])
    affected = sum(1 for r in results if r["stance_flip"] or r["confidence_gap"] >= CONF_GAP_POINTS)
    need = math.ceil(FLIP_FRACTION * m)
    flagged = net_flips >= need or net_gap >= CONF_GAP_POINTS
    thresholds = (f"thresholds: (identity stance changes - noise stance changes) at least {need} of {m} cases "
                  f"({FLIP_FRACTION:.0%}), or (mean identity confidence gap - mean noise gap) at least "
                  f"{CONF_GAP_POINTS:g} points")
    text = (f"possible hindsight: {affected} of {m} cases" if flagged else "no sign of hindsight")
    return {"cases": m, "verdict": text, "possible_hindsight": flagged, "thresholds": thresholds,
            "stance_agreement": round(1 - flips / m, 3), "stance_flips": flips, "identity_flips": flips,
            "noise_flips": round(noise_flips, 2), "net_flips": round(net_flips, 2),
            "mean_abs_confidence_gap": round(mean_gap, 2), "mean_identity_gap": round(mean_gap, 2),
            "mean_noise_gap": round(noise_gap, 2), "net_gap": round(net_gap, 2), "affected_cases": affected,
            "hindsight_signature_cases": sig, "scored_cases": len(scored),
            "note": ("Hindsight signature (graded afterwards with prices after the replay date): the unmasked answer "
                     f"was more confident than the masked one in the direction the stock went, in {sig} of "
                     f"{len(scored)} scored cases.")}


# -- cost ------------------------------------------------------------------------------------
def estimate_cost(model: str, pairs: list[dict[str, Any]], days: list[str]) -> dict[str, Any]:
    """Approximate USD for four calls per case (unmasked twice, masked twice): input from the prompt size
    (about 3.5 characters a token)."""
    from ..agent import PRICES_PER_MTOK
    tokens_in = 2 * sum(math.ceil(len(prompt_text(p[k], d if k == "unmasked" else "D0")) / 3.5)
                        for p, d in zip(pairs, days) for k in ("unmasked", "masked"))
    calls = CALLS_PER_CASE * len(pairs)
    price = PRICES_PER_MTOK.get(model)
    usd = None if price is None else (tokens_in * price[0] + calls * OUTPUT_TOKENS_GUESS * price[1]) / 1e6
    return {"calls": calls, "input_tokens": tokens_in, "output_tokens": calls * OUTPUT_TOKENS_GUESS, "usd": usd,
            "model": model}


# -- running ---------------------------------------------------------------------------------
def run_blind_test(cases: list[Case], pairs: list[dict[str, Any]], *, client: Any, model: str, source: Any,
                   field: str = "adj_close", progress: Callable[[str], None] | None = None,
                   checkpoint: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """Per case: unmasked twice and masked twice through record_view, then grade with the prices that came after.
    `checkpoint(report_so_far)` runs after every case, so a crash keeps what was already paid for."""
    results: list[dict[str, Any]] = []

    def report(done: bool) -> dict[str, Any]:
        return {"run_at": datetime.now().isoformat(timespec="seconds"), "model": model, "complete": done,
                "summary": verdict(results), "results": list(results),
                "labels": {"outcome": "HINDSIGHT: returns after the replay date, used only to grade the test, "
                                      "never shown to Claude"}}

    for case, pair in zip(cases, pairs):
        ticker, lab = pair["labels"]["ticker"], pair["labels"]
        bad = leaks(pair["masked"], ticker, lab["company"], lab.get("sector", ""))
        if bad:   # never pay for a masked run that is not masked
            raise ValueError(f"masked input for {case.ticker} on {case.date} still contains {bad}")
        if progress:
            progress(f"{case.slug} {case.date} {case.ticker}: unmasked x2, masked x2")
        views = [request_view(client, model, label, pair[kind])[1]
                 for kind, label in (("unmasked", case.date), ("unmasked", case.date), ("masked", "D0"), ("masked", "D0"))]
        u1, u2, m1, m2 = (stance_of(v, ticker) for v in views)
        results.append(compare_case(case, u1, m1, outcome_after(source, ticker, case.date, field), u2, m2))
        if checkpoint:
            checkpoint(report(len(results) == len(cases)))
    return report(True)


# -- saving and showing ----------------------------------------------------------------------
def research_dir(state_dir: Any) -> Path:
    return Path(state_dir) / "research"


def new_report_path(state_dir: Any, today: str | None = None) -> Path:
    d = research_dir(state_dir)
    d.mkdir(parents=True, exist_ok=True)
    day = today or date.today().isoformat()
    path, n = d / f"blind_test_{day}.json", 1
    while path.exists():
        n += 1
        path = d / f"blind_test_{day}_{n}.json"
    return path


def write_report(path: Path, report: dict[str, Any]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    tmp.replace(path)


def save_report(state_dir: Any, report: dict[str, Any], today: str | None = None) -> Path:
    path = new_report_path(state_dir, today)
    write_report(path, report)
    return path


def latest_verdict(state_dir: Any) -> dict[str, Any] | None:
    """The newest saved blind test, for the Replay page: None when there is none or it is unreadable."""
    files = sorted(research_dir(state_dir).glob("blind_test_*.json"), key=lambda p: p.stat().st_mtime)
    for p in reversed(files):
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
            s = r["summary"]
            return {"file": p.name, "run_at": r.get("run_at"), "model": r.get("model"), "verdict": s["verdict"],
                    "cases": s.get("cases"), "stance_flips": s.get("stance_flips"),
                    "mean_abs_confidence_gap": s.get("mean_abs_confidence_gap"),
                    "net_flips": s.get("net_flips"), "net_gap": s.get("net_gap"),
                    "noise_flips": s.get("noise_flips"), "mean_noise_gap": s.get("mean_noise_gap"),
                    "possible_hindsight": s.get("possible_hindsight"),
                    "hindsight_signature_cases": s.get("hindsight_signature_cases"),
                    "scored_cases": s.get("scored_cases")}
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return None


def format_report(report: dict[str, Any]) -> str:
    s = report["summary"]
    lines = [f"Blind-ticker test: {s['cases']} cases, model {report.get('model')}", f"  Verdict: {s['verdict']}"]
    if s["cases"]:
        lines += [f"  {s['thresholds']}",
                  f"  Identity effect: {s['identity_flips']} stance changes, mean confidence gap "
                  f"{s['mean_identity_gap']:g} points. Noise (same input asked twice): {s['noise_flips']:g} stance "
                  f"changes, mean gap {s['mean_noise_gap']:g} points. Net: {s['net_flips']:g} changes, "
                  f"{s['net_gap']:g} points.",
                  f"  {s['note']}"]
    for r in report["results"]:
        c, o = r["case"], r["outcome"]
        after = f"{o['return']:+.1%} in {o['horizon_bars']} days" if o else "no outcome yet"
        lines.append(f"  {c['date']} {c['ticker']}: unmasked {r['unmasked']['action']}/{r['unmasked']['confidence']}, "
                     f"masked {r['masked']['action']}/{r['masked']['confidence']}"
                     f"{' (changed)' if r['stance_flip'] else ''}; afterwards {after} (hindsight, grading only)")
    return "\n".join(lines)


# -- the command -----------------------------------------------------------------------------
def run_cli(args: Any, settings: Any, *, source: Any | None = None, news_client: Any | None = None,
            universe: Any | None = None, client: Any | None = None, today: str | None = None,
            out: Callable[[str], None] = print) -> int:
    """`replay blind-test`: 2 = refused, 1 = estimate shown but --yes not given, 0 = ran."""
    if not getattr(settings, "anthropic_api_key", None):
        out("Refusing: the blind test makes paid Claude calls and ANTHROPIC_API_KEY is not set.")
        return 2
    root = Path(settings.state_dir) / "replay" / args.slug
    trial_file = root / "trial.json"
    if not trial_file.exists():
        out(f"No replay called {args.slug} (looked for {trial_file}).")
        return 2
    data = json.loads(trial_file.read_text(encoding="utf-8"))
    today = today or date.today().isoformat()
    cases = []
    for spec in getattr(args, "case", None) or []:
        d, _, t = str(spec).partition(":")
        try:
            d = date.fromisoformat(d).isoformat()
        except ValueError:
            out(f"--case wants DATE:TICKER (got {spec!r}).")
            return 2
        if not t.strip() or d < EARLIEST_START or d >= today:
            out(f"--case {spec!r}: need a ticker and a date from {EARLIEST_START} to before today.")
            return 2
        cases.append(Case(data["slug"], d, t.strip().upper()))
    if not cases:
        cases = pick_cases(data, int(getattr(args, "cases", DEFAULT_CASES) or DEFAULT_CASES), today=today)
    if not cases:
        out("This replay has no recorded picks to build cases from; pass --case DATE:TICKER.")
        return 2

    if source is None:
        from ..prices import YahooPrices
        source = YahooPrices(suffix=".NS", cache_dir=Path(settings.state_dir) / "cache", cache_ttl=7 * 86400)
    if news_client is None:
        from ..nse import NSEClient
        news_client = NSEClient(cache_dir=Path(settings.state_dir) / "cache")
    if universe is None:
        from .trial import ReplayUniverse
        universe = ReplayUniverse(data["universe"], settings.state_dir)
    field = "close" if data.get("dividends") == "cash" else "adj_close"
    model = getattr(args, "model", None) or settings.claude_model
    pairs = [build_pair(c, source=source, news_client=news_client, members=universe.members_on(c.date),
                        field=field, cash=float(data.get("cash") or 100_000)) for c in cases]
    kept = []
    for c, pr in zip(cases, pairs):
        bad = leaks(pr["masked"], pr["labels"]["ticker"], pr["labels"]["company"], pr["labels"]["sector"])
        if bad:
            out(f"Skipping {c.date} {c.ticker}: the masked input still contains {', '.join(sorted(set(bad)))}.")
        else:
            kept.append((c, pr))
    if not kept:
        out("No case could be masked cleanly, so nothing can be tested.")
        return 2
    cases, pairs = [c for c, _ in kept], [pr for _, pr in kept]
    est = estimate_cost(model, pairs, [c.date for c in cases])
    cost = "cost unknown for this model" if est["usd"] is None else f"about ${est['usd']:.2f}"
    out(f"Blind-ticker test on {data['name']}: {len(cases)} cases, {est['calls']} Claude calls with {model}, "
        f"estimated {cost} (about {est['input_tokens']:,} input tokens).")
    for c in cases:
        out(f"  {c.date} {c.ticker}")
    if not getattr(args, "yes", False):
        out("Nothing was sent. This costs real API money: run again with --yes to go ahead.")
        return 1
    if client is None:
        from ..agent import make_client
        client = make_client(settings)
    path = new_report_path(settings.state_dir, today)

    def keep(partial: dict[str, Any]) -> None:   # after every case: a crash loses nothing already paid for
        partial["slug"], partial["estimate"] = data["slug"], est
        write_report(path, partial)

    report = run_blind_test(cases, pairs, client=client, model=model, source=source, field=field, progress=out,
                            checkpoint=keep)
    keep(report)
    out(format_report(report))
    out(f"Saved {path}")
    return 0
