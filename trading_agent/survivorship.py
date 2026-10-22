"""Survivorship repairs for the Indian studies (docs/research/survivorship-2026-10-22.md).

Repair 1, here: index members with no Yahoo history are filled from NSE's own daily files (the bhavcopy archive), the
India test's pipeline unchanged: ``research_india.fetch_bhav`` and ``parse_bhav`` read each day's EQ rows, NSE's
symbol-change file renames them to today's symbols, and ``build_series`` adjusts them for splits, bonuses and dividends
from NSE's corporate actions. ``nse_filler`` returns the callable ``research_sim.load_market`` takes as ``fill``.

Repair 2, the delisting haircut, is ``research_sim.Rules.delist_haircut`` (30%, ``DELIST_HAIRCUT``).

A study stops before its claim, with nothing spent, when fewer than ``GATE_FILES`` of the trading days' files could be
loaded or the corporate actions could not be fetched (``FillError``).
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

DELIST_HAIRCUT = 0.30     # Shumway (1997): the delisting return for performance-related delistings; not varied
GATE_FILES = 0.99         # the share of trading days whose NSE file must load
BHAV_DIR = Path("research") / "nse_bhav"
NOTE = "survivorship-2026-10-22.md"


class FillError(RuntimeError):
    """The NSE data for the fill could not be loaded: the study does not run and nothing is spent."""


def fill_from_files(missing: list[str], days: list[str], files: dict[str, list[dict[str, Any]]],
                    renames: list[tuple[str, str, str]], actions: dict[str, Any]) -> dict[str, Any]:
    """The pure part: series for the ``missing`` members found in the parsed daily ``files`` (by day), under today's
    symbols. Returns {"series": {symbol: bars}, "still_missing": [...], "dividends_skipped": n, "stocks": n}."""
    from .index_history import current_symbol
    from .membership import _norm
    from .research_india import build_series
    series, info = build_series(days, files, lambda s, d: current_symbol(s, d, renames), actions)
    by_name = {_norm(k): v for k, v in series.items()}
    found = {s: by_name[_norm(s)] for s in missing if _norm(s) in by_name}
    return {"series": found, "still_missing": sorted(set(missing) - set(found)),
            "dividends_skipped": info["dividends_skipped"], "stocks": len(series)}


def load_files(days: list[str], cache: Path, session_factory: Callable[[], Any], *, workers: int = 4,
               progress: Callable[[str], None] | None = None) -> dict[str, list[dict[str, Any]]]:
    """Every day's EQ rows (cached under ``cache``); a day NSE has no file for is left out."""
    import requests

    from .research_india import fetch_bhav

    def one(day: str) -> tuple[str, Any]:
        s = session_factory()
        for _ in range(3):
            try:
                return day, fetch_bhav(day, cache, s)
            except requests.RequestException:
                continue
        return day, None

    files: dict[str, list[dict[str, Any]]] = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for k, (day, rows) in enumerate(ex.map(one, days), 1):
            if rows:
                files[day] = rows
            if progress and (k % 250 == 0 or k == len(days)):
                progress(f"  NSE daily files {k}/{len(days)}")
    return files


def nse_filler(state_dir: Path, *, session_factory: Callable[[], Any] | None = None,
               client_factory: Callable[[], Any] | None = None, renames: list[tuple[str, str, str]] | None = None,
               progress: Callable[[str], None] | None = None) -> Callable[[list[str], list[str]], dict[str, Any]]:
    """The ``fill`` callable for ``load_market``: given the members with no Yahoo bars and the calendar, loads NSE's
    daily files for every calendar day (cached), the symbol changes and the corporate actions, and returns the found
    series with a report. Raises ``FillError`` at the gate, before any study is claimed."""
    def fill(missing: list[str], calendar: list[str]) -> dict[str, Any]:
        import requests

        from .index_history import load_symbol_changes
        from .nse import NSEClient
        from .research_india import fetch_actions, parse_actions
        if not missing or not calendar:
            return {"series": {}, "still_missing": list(missing), "files_loaded": 0, "files_wanted": 0,
                    "dividends_skipped": 0, "stocks": 0}
        cache = Path(state_dir) / BHAV_DIR
        if progress:
            progress(f"Filling {len(missing)} member(s) with no Yahoo history from NSE's daily files, "
                     f"{calendar[0]} to {calendar[-1]} (cached under {cache})…")
        files = load_files(calendar, cache, session_factory or requests.Session, progress=progress)
        share = len(files) / len(calendar)
        if share < GATE_FILES:
            raise FillError(f"only {len(files)} of {len(calendar)} trading days' NSE files loaded ({share:.1%}; the "
                            f"gate is {GATE_FILES:.0%})")
        try:
            rows = fetch_actions(Path(state_dir) / "research", (client_factory or NSEClient)(),
                                 start=calendar[0], end=calendar[-1])
        except Exception as e:  # noqa: BLE001 - whatever NSE did, the study must not be claimed
            raise FillError(f"NSE's corporate actions could not be fetched ({type(e).__name__}: {e})") from e
        try:
            ren = renames if renames is not None else load_symbol_changes()
        except requests.RequestException as e:
            raise FillError(f"NSE's symbol-change file could not be fetched ({e})") from e
        out = fill_from_files(missing, [d for d in calendar if d in files], files, ren, parse_actions(rows))
        out.update(files_loaded=len(files), files_wanted=len(calendar))
        if progress:
            progress(f"  filled {len(out['series'])} of {len(missing)}: {', '.join(sorted(out['series'])) or 'none'}"
                     + (f"; still missing {', '.join(out['still_missing'])}" if out["still_missing"] else ""))
        return out
    return fill


def fill_note(m: Any) -> str:
    """One line for a report: what was filled and what is still missing."""
    rep = getattr(m, "fill_report", None) or {}
    if not getattr(m, "filled", None) and not rep:
        return "Survivorship: no member filled from NSE's files."
    return (f"Survivorship ({NOTE}): {len(m.filled)} member(s) with no Yahoo history filled from NSE's daily files "
            f"({rep.get('files_loaded', 0)} of {rep.get('files_wanted', 0)} days loaded)"
            + (f"; still missing: {', '.join(m.missing)}" if m.missing else "; none still missing") + ".")
