"""Point-in-time index membership, so backtests stop seeing only today's winners.

NSE publishes only the current constituent list. Testing a strategy on it means every
stock that was dropped for falling (UPL, IndusInd Bank, ...) silently disappears from
the past, which flattens losses and flatters results (survivorship bias).

`Membership` starts from today's list and walks the dated changes backwards to recover
who was in the index on any past day. NIFTY 50 changes since March 2021 are built in,
checked against NSE announcements as reported by Business Standard, ICICI Direct and
Wikipedia's index history. Temporary demerger entries (Jio Financial in 2023, Tata
Motors CV in 2025) lasted days and are left out. Other indices can be supplied as a CSV:

    date,added,removed
    2025-09-30,INDIGO MAXHEALTH,HEROMOTOCO INDUSINDBK

Symbols are today's tickers: LTIMindtree is LTM (renamed 27 Feb 2026) and Zomato is
ETERNAL, because that is where their full price history lives.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

# effective date (first trading day in the new index), added, removed
NIFTY50_CHANGES: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = [
    ("2021-03-31", ("TATACONSUM",), ("GAIL",)),
    ("2022-03-31", ("APOLLOHOSP",), ("IOC",)),
    ("2022-09-30", ("ADANIENT",), ("SHREECEM",)),
    ("2023-07-13", ("LTM",), ("HDFC",)),  # HDFC Ltd merged into HDFC Bank and delisted
    ("2024-03-28", ("SHRIRAMFIN",), ("UPL",)),
    ("2024-09-30", ("BEL", "TRENT"), ("DIVISLAB", "LTM")),
    ("2025-03-28", ("ETERNAL", "JIOFIN"), ("BPCL", "BRITANNIA")),
    ("2025-09-30", ("INDIGO", "MAXHEALTH"), ("HEROMOTOCO", "INDUSINDBK")),
    ("2026-09-30", ("BSE",), ("WIPRO",)),
]

# Why a former member may have no price history at all.
DELISTED = {"HDFC": "merged into HDFC Bank in July 2023 and delisted"}

RENAMES = {"LTIM": "LTM", "ZOMATO": "ETERNAL", "TATAMOTORS": "TMPV"}

BUILT_IN = {"NIFTY50": NIFTY50_CHANGES}


def _norm(sym: str) -> str:
    s = sym.strip().upper()
    return RENAMES.get(s, s)


@dataclass
class Membership:
    current: frozenset[str]
    changes: list[tuple[str, tuple[str, ...], tuple[str, ...]]]
    source: str = "built-in"

    def __post_init__(self) -> None:
        self.current = frozenset(_norm(s) for s in self.current)
        self.changes = sorted(((d, tuple(map(_norm, a)), tuple(map(_norm, r))) for d, a, r in self.changes),
                              key=lambda c: c[0])

    @property
    def known_since(self) -> str:
        """Membership before the first recorded change is assumed, not known."""
        return self.changes[0][0] if self.changes else "9999-12-31"

    def members_on(self, day: str) -> set[str]:
        out = set(self.current)
        for date, added, removed in reversed(self.changes):
            if date <= day:
                break
            out.difference_update(added)
            out.update(removed)
        return out

    def ever_members(self, start: str) -> set[str]:
        """Everyone in the index at any point from `start` until today."""
        out = self.members_on(start)
        for date, added, _ in self.changes:
            if date > start:
                out.update(added)
        return out | set(self.current)

    def changes_between(self, start: str, end: str) -> list[dict[str, object]]:
        return [{"date": d, "added": list(a), "removed": list(r)} for d, a, r in self.changes if start < d <= end]


def load_changes_csv(path: str | Path) -> list[tuple[str, tuple[str, ...], tuple[str, ...]]]:
    out = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            split = lambda v: tuple(s for s in (v or "").replace(";", " ").replace(",", " ").split() if s)  # noqa: E731
            date = (row.get("date") or "").strip()
            if date:
                out.append((date, split(row.get("added")), split(row.get("removed"))))
    return out


def membership_for(universe: str, current: Iterable[str], changes_csv: str | Path | None = None
                   ) -> Membership | None:
    """Point-in-time membership for a universe, or None when its history is unknown."""
    if changes_csv:
        return Membership(frozenset(current), load_changes_csv(changes_csv), source=str(changes_csv))
    changes = BUILT_IN.get(universe.upper().replace(" ", ""))
    return Membership(frozenset(current), list(changes)) if changes else None
