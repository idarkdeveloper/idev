"""Research C: the Coffee Can screen (Mukherjea, Ranjan & Uniyal, after Kirby 1984).

Buy the companies whose revenue grew more than 10% AND whose return on capital employed stayed above 15% in every
financial year we can see, hold them equal-weight, and look again once a year. Banks and NBFCs are left out (ROCE does
not describe a lender), as the book does.

From the March (full-year) NSE results filings, as known on each date (a filing counts once usable, the 15:00 IST
rule):

* revenue growth = full-year revenue from operations / the year before - 1;
* ROCE = EBIT / capital employed, with EBIT = profit before tax + finance costs and capital employed = equity +
  borrowings on the 31 March balance sheet (the book averages opening and closing capital; one balance sheet a year
  is what the filings give reliably, so the closing one is used and the report says so).

A stock qualifies when it has at least ``min_years`` years of growth figures and EVERY one of them passes both tests.
Filings start around December 2018, so about seven financial years exist at most (the book asks for ten): the
default is 4 years of growth (5 financial years), which first allows a screen in July 2023.

The yearly rebalance is on the first trading day of July, after the March results are out (listed companies file
within 60 days). Results are compared after charges and Indian capital-gains tax with MID150BEES and NIFTYBEES on the
same days. Nothing from the sealed holdout is used unless ``--holdout`` is given, once.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Callable

from .filing_time import usable_by
from .research_sim import Market

GROWTH_MIN = 0.10
ROCE_MIN = 0.15
MIN_YEARS = 4
FINANCIAL_INDUSTRIES = ("financial services", "banks", "bank", "nbfc", "finance", "insurance")


def is_financial(industry: str) -> bool:
    i = (industry or "").lower()
    return any(k in i for k in FINANCIAL_INDUSTRIES)


def annual_rows(records: list[dict[str, Any]], day: str, holidays: Any | None = None) -> list[dict[str, Any]]:
    """Per financial year known by ``day``: revenue, EBIT, capital employed, ROCE and growth (oldest first)."""
    known = sorted((r for r in records if usable_by(r["available"], day, holidays)), key=lambda r: r["period_end"])
    rows: list[dict[str, Any]] = []
    for r in known:
        rev, pbt, fin = r.get("revenue_ytd"), r.get("pbt_ytd"), r.get("finance_ytd")
        eq, debt = r.get("equity"), r.get("borrowings")
        ebit = pbt + (fin or 0.0) if pbt is not None else None
        ce = eq + (debt or 0.0) if eq is not None else None
        rows.append({"fy": r["period_end"], "revenue": rev, "ebit": ebit, "capital": ce,
                     "roce": ebit / ce if ebit is not None and ce and ce > 0 else None})
    for prev, cur in zip(rows, rows[1:]):
        consecutive = int(cur["fy"][:4]) - int(prev["fy"][:4]) == 1
        cur["growth"] = (cur["revenue"] / prev["revenue"] - 1
                         if consecutive and cur["revenue"] and prev["revenue"] and prev["revenue"] > 0 else None)
    if rows:
        rows[0]["growth"] = None
    return rows


def qualifies(rows: list[dict[str, Any]], min_years: int = MIN_YEARS,
              window: int | None = None) -> tuple[bool, str]:
    """(passes, why) for the Coffee Can test on every year with a growth figure (at least ``min_years``). With
    ``window``, only the latest ``window`` years are tested (the book's rolling ten years); older ones are ignored."""
    years = [r for r in rows if r.get("growth") is not None]
    if len(years) < min_years:
        return False, f"only {len(years)} year(s) of growth"
    if window:
        years = years[-window:]
    for r in years:
        if r["growth"] <= GROWTH_MIN:
            return False, f"revenue growth {r['growth'] * 100:.0f}% in FY{r['fy'][:4]}"
        if r["roce"] is None or r["roce"] <= ROCE_MIN:
            shown = "n/a" if r["roce"] is None else f"{r['roce'] * 100:.0f}%"
            return False, f"ROCE {shown} in FY{r['fy'][:4]}"
    return True, "passes every year"


def reason_kind(why: str) -> str:
    """A reason from ``qualifies`` without the stock's own number, so reasons can be counted:
    'revenue growth ≤10% in FY2020', 'ROCE ≤15% in FY2021', 'ROCE n/a in FY2022', 'only 3 year(s) of growth'."""
    if why.startswith("revenue growth"):
        return f"revenue growth ≤{GROWTH_MIN * 100:.0f}% in {why.rsplit(' ', 1)[-1]}"
    if why.startswith("ROCE n/a"):
        return why
    if why.startswith("ROCE"):
        return f"ROCE ≤{ROCE_MIN * 100:.0f}% in {why.rsplit(' ', 1)[-1]}"
    return why


def coffee_can_ranker(annual: dict[str, list[dict[str, Any]]], industries: dict[str, str], *,
                      min_years: int = MIN_YEARS, min_turnover: float = 1e7, window: int | None = None,
                      log: dict[str, Any] | None = None,
                      reasons: dict[str, Counter[str]] | None = None) -> Callable[[Market, int], list[str]]:
    """The qualifying stocks on day t (liquid, not financials), highest latest ROCE first. ``reasons`` collects, per
    day, why the other members were turned down."""
    def rank(m: Market, t: int) -> list[str]:
        day = m.dates[t]
        stats = m.stats_on(t)
        picks = []
        why: Counter[str] = Counter()
        for sym in m.members_on(day):
            if is_financial(industries.get(sym, "")):
                why["bank or finance company (left out)"] += 1
                continue
            if sym not in stats:
                why["no price history that day"] += 1
                continue
            if (stats[sym].get("avg_turnover_60d") or 0) < min_turnover:
                why["traded under ₹1 crore a day"] += 1
                continue
            rows = annual_rows(annual.get(sym) or [], day)
            ok, reason = qualifies(rows, min_years, window)
            if ok:
                picks.append((rows[-1]["roce"] or 0.0, sym))
            else:
                why["no full-year filings cached" if not rows else reason_kind(reason)] += 1
        out = [s for _, s in sorted(picks, reverse=True)]
        if log is not None:
            log[day] = out
        if reasons is not None:
            reasons[day] = why
        return out
    return rank


class UnionMembership:
    """Point-in-time membership of several indices at once (Midcap 150 + Smallcap 250)."""

    def __init__(self, parts: list[Any]):
        self.parts = [p for p in parts if p is not None]

    def members_on(self, day: str) -> set[str]:
        return set().union(*(p.members_on(day) for p in self.parts)) if self.parts else set()

    def ever_members(self, start: str) -> set[str]:
        return set().union(*(p.ever_members(start) for p in self.parts)) if self.parts else set()
