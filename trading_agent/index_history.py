"""Past index membership for any Nifty index, from NSE Indices' own press releases.

NSE publishes only today's constituent list, but every change is announced in a press
release on niftyindices.com ("Replacements in indices w.e.f. ..."). Each release lists,
per index, the stocks being excluded and included, in the same table layout:

    4) Nifty Midcap 150
    The following companies are being excluded:
    Sr. No. Company Name Symbol
    1 3M India Ltd. 3MINDIA
    ...

This module downloads those releases (cached on disk), parses them into a dated
change log, and checks the result: walking back from today's list, the index must keep
exactly its stated size on every date. A release in another format, such as the short
notices that drop a demerged company a few days after it was temporarily added as a
51st stock, is skipped; those add-and-drop pairs cancel out.
"""

from __future__ import annotations

import csv
import html
import io
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

import requests

SITE = "https://www.niftyindices.com"
LIST_URL = SITE + "/press-release"
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/124.0 Safari/537.36", "Accept": "*/*"}

SIZES = {"NIFTY50": 50, "NIFTYNEXT50": 50, "NIFTY100": 100, "NIFTY200": 200, "NIFTY500": 500,
         "NIFTYMIDCAP50": 50, "NIFTYMIDCAP100": 100, "NIFTYMIDCAP150": 150,
         "NIFTYSMALLCAP50": 50, "NIFTYSMALLCAP100": 100, "NIFTYSMALLCAP250": 250}

_RELEVANT = re.compile(r"replacement|changes? in (nifty )?(equity )?indices", re.I)
_SKIP = re.compile(r"fixed income|g-sec|bond|sdl|ipo|fpi|t-bill|debt|target maturity|sme emerge|waves", re.I)
_ROW = re.compile(r"^\s*\d+\s+(.+?)\s+([A-Z0-9][A-Z0-9&\-]*)\s*$")
_SECTION = re.compile(r"^\s*(?:\d+|[a-z])\)\s*(Nifty.+?)\s*$", re.I)
_EFFECTIVE = re.compile(r"(?:effective|with\s+effect)\s+from\s+([A-Z][a-z]+)\s+(\d[\d ]*?)\s*,\s*(\d[\d ]{3,6})", re.S)
_TRACKED_NAMES = ("Nifty Next 50", "Nifty Midcap 150", "Nifty Midcap 100", "Nifty Midcap 50",
                  "Nifty Smallcap 250", "Nifty Smallcap 100", "Nifty Smallcap 50",
                  "Nifty 500", "Nifty 200", "Nifty 100", "Nifty 50")
_REMARK = re.compile(r"^\s*(.*?)\s*\b([A-Z0-9][A-Z0-9&\-]*)\*?#*\s+(Exclusion revoked|Inclusion revoked|Exclusion|Inclusion)\s*$")
_WEF = re.compile(r"w\.?\s?e\.?\s?f\.?\s+([A-Z][a-z]+)\s+(\d{1,2}),?\s*(\d{4})")


def index_key(name: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", name.upper())


@dataclass
class Notice:
    published: str  # ISO date
    url: str
    title: str


def _iso(month: str, day: str, year: str) -> str | None:
    try:
        return datetime.strptime(f"{month[:3]} {int(day.replace(' ', ''))} {int(year.replace(' ', ''))}",
                                 "%b %d %Y").date().isoformat()
    except ValueError:
        return None


def list_notices(session: requests.Session | None = None, since: str = "2021-01-01") -> list[Notice]:
    """Equity index replacement notices on niftyindices.com, newest first."""
    page = (session or requests.Session()).get(LIST_URL, headers=HEADERS, timeout=60).text
    out = []
    for d, url, title in re.findall(r'data-date="([^"]+)"[^>]*>.*?<a href=\'([^\']+\.pdf)\'[^>]*>(.*?)</a>',
                                    page, flags=re.S):
        title = re.sub(r"\s+", " ", html.unescape(re.sub("<[^>]+>", "", title))).strip()
        try:
            published = datetime.strptime(d.strip(), "%b %d, %Y").date().isoformat()
        except ValueError:
            continue
        if published >= since and _RELEVANT.search(title) and not _SKIP.search(title):
            out.append(Notice(published, url if url.startswith("http") else SITE + url, title))
    return out


def parse_notice(text: str, title: str = "", published: str = "") -> list[dict[str, Any]]:
    """[{date, index, added, removed}] from one release's text (one entry per index section)."""
    eff = None
    m = _WEF.search(title)
    if m:
        eff = _iso(*m.groups())
    if not eff:
        m = _EFFECTIVE.search(text)
        eff = _iso(*m.groups()) if m else None
    eff = eff or published
    sections: dict[str, dict[str, list[str]]] = {}
    current: str | None = None
    mode: str | None = None
    pending: str | None = None  # a table row the PDF wrapped over several lines
    for line in text.splitlines():
        s = _SECTION.match(line)
        if s and not _ROW.match(line):
            current, mode, pending = index_key(s.group(1).removesuffix(" index").removesuffix(" Index")), None, None
            continue
        low = line.lower()
        if "being excluded" in low or "are excluded" in low or "is excluded" in low:
            mode, pending = "removed", None
            continue
        if "being included" in low or "are included" in low or "is included" in low:
            mode, pending = "added", None
            continue
        if low.strip().startswith(("note", "the above")):
            mode = None
            continue
        if current and mode:
            if not line.strip() or line.strip().startswith("Sr. No"):
                continue
            candidate = f"{pending} {line.strip()}" if pending else line
            r = _ROW.match(candidate)
            if r and not r.group(2).isdigit():
                pending = None
                sym = r.group(2)
                if not sym.startswith("DUMMY"):
                    sections.setdefault(current, {"added": [], "removed": []})[mode].append(sym)
            elif re.match(r"^\s*\d+(\s|$)", candidate) and candidate.count(" ") < 40:
                pending = candidate.strip()
            else:
                pending = None
    out = [{"date": eff, "index": k, "added": v["added"], "removed": v["removed"]}
           for k, v in sections.items() if v["added"] or v["removed"]]
    if "revoked" in text:
        out += _parse_revocations(text, eff)
    if "dummy" not in text.lower():  # temporary demerger entries cancel out; skip them
        out += _parse_exclusion_lists(text, eff)
    return out


def _parse_exclusion_lists(text: str, eff: str) -> list[dict[str, Any]]:
    """'... (Symbol: XYZ) shall be excluded from the following indices:' then a list of index names,
    used when a stock leaves every index at once (delisting, cancelled share class)."""
    out: dict[str, list[str]] = {}
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if "excluded from the following indices" not in line and "from the following indices" not in line:
            continue
        window = " ".join(lines[max(0, i - 3):i + 1])
        m = re.findall(r"\(Symbol:\s*([A-Z0-9&\-]+)\)", window)
        if not m or "exclu" not in window.lower():
            continue
        sym, started = m[-1], False
        for row in lines[i + 1:]:
            r = re.match(r"^\s*\d+\s+(Nifty.+?)\s*$", row)
            if r:
                started = True
                out.setdefault(index_key(r.group(1)), []).append(sym)
            elif row.strip() and not row.strip().startswith("Sr. No") and started:
                break
    return [{"date": eff, "index": k, "added": [], "removed": v} for k, v in out.items()]


def _parse_revocations(text: str, eff: str) -> list[dict[str, Any]]:
    """The rare table that reverses an announced change ("Exclusion revoked", "Inclusion revoked").
    Each row names an index, then one or more securities with a remark."""
    acts: dict[str, dict[str, list[str]]] = {}
    current: str | None = None
    kind = {"Exclusion revoked": "unremove", "Inclusion revoked": "unadd", "Exclusion": "removed",
            "Inclusion": "added"}
    for line in text.splitlines():
        rest = line
        num = re.match(r"^\s*\d+\s+(Nifty.*)$", line)
        if num:
            body = num.group(1)
            name = next((n for n in _TRACKED_NAMES if re.match(re.escape(n) + r"(?=\s|$)", body)), None)
            current = index_key(name) if name else None
            rest = body[len(name):] if name else ""
        if current is None:
            continue
        r = _REMARK.match(rest)
        if r:
            d = acts.setdefault(current, {"added": [], "removed": [], "unadd": [], "unremove": []})
            d[kind[r.group(3)]].append(r.group(2))
    return [{"date": eff, "index": k, **v} for k, v in acts.items()]


def _pdf_text(data: bytes) -> str:
    from pypdf import PdfReader  # optional dependency, only needed to build histories
    return "\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(data)).pages)


def fetch_changes(cache_dir: Path, *, since: str = "2021-01-01", session: requests.Session | None = None,
                  progress: Any = None) -> list[dict[str, Any]]:
    """Download (once) and parse every relevant notice since `since`; returns all index changes."""
    session = session or requests.Session()
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    notices = list_notices(session, since)
    changes: list[dict[str, Any]] = []
    for k, n in enumerate(notices, 1):
        stem = cache_dir / Path(n.url).stem
        txt = stem.with_suffix(".txt")
        if not txt.exists():
            r = session.get(n.url, headers=HEADERS, timeout=90)
            r.raise_for_status()
            if not r.content.startswith(b"%PDF"):
                continue
            txt.write_text(_pdf_text(r.content))
        if progress:
            progress(f"parsed {k}/{len(notices)} notices")
        for c in parse_notice(txt.read_text(), n.title, n.published):
            changes.append({**c, "source": n.url})
    return changes


SYMBOL_CHANGES_URL = "https://nsearchives.nseindia.com/content/equities/symbolchange.csv"

# Ticker changes that NSE's symbolchange.csv leaves out, found when the reconstructed index
# came out the wrong size. Dates fall between the last notice using the old ticker and the
# first using the new one, which is all the matching needs.
EXTRA_RENAMES = [
    ("BURGERKING", "RBA", "2022-01-01"),        # Burger King India -> Restaurant Brands Asia
    ("WABCOINDIA", "ZFCVINDIA", "2022-06-01"),  # WABCO India -> ZF Commercial Vehicle Control Systems
]


def load_symbol_changes(session: requests.Session | None = None) -> list[tuple[str, str, str]]:
    """[(old, new, effective ISO date)] from NSE's published list of ticker changes."""
    r = (session or requests.Session()).get(SYMBOL_CHANGES_URL, headers=HEADERS, timeout=60)
    r.raise_for_status()
    out = []
    for row in csv.reader(io.StringIO(r.content.decode("latin-1"))):
        if len(row) >= 4:
            try:
                out.append((row[1].strip().upper(), row[2].strip().upper(),
                            datetime.strptime(row[3].strip(), "%d-%b-%Y").date().isoformat()))
            except ValueError:
                continue
    return out + EXTRA_RENAMES


def current_symbol(sym: str, as_of: str, renames: list[tuple[str, str, str]]) -> str:
    """Follow ticker renames made after `as_of` (LTI -> LTIM -> LTM), so old notices match today's list.
    Renames on or before `as_of` are ignored: the notice already used the newer ticker."""
    seen = set()
    while sym not in seen:
        seen.add(sym)
        later = sorted((d, new) for old, new, d in renames if old == sym and d > as_of)
        if not later:
            break
        as_of, sym = later[0]
    return sym


def changes_for(changes: Iterable[dict[str, Any]], index: str,
                renames: list[tuple[str, str, str]] | None = None) -> list[tuple[str, tuple[str, ...], tuple[str, ...]]]:
    """Merge one index's changes per effective date in today's tickers, cancelling a symbol
    added and removed the same day."""
    key = index_key(index)
    fix = (lambda s, d: current_symbol(s, d, renames)) if renames else (lambda s, d: s)
    by_date: dict[str, dict[str, set[str]]] = {}
    for c in changes:
        if c["index"] != key:
            continue
        d = by_date.setdefault(c["date"], {"added": set(), "removed": set(), "unadd": set(), "unremove": set()})
        for k in ("added", "removed", "unadd", "unremove"):
            d[k].update(fix(s, c["date"]) for s in c.get(k, []))
    out = []
    for d in sorted(by_date):
        a, r = by_date[d]["added"] - by_date[d]["unadd"], by_date[d]["removed"] - by_date[d]["unremove"]
        both = a & r
        out.append((d, tuple(sorted(a - both)), tuple(sorted(r - both))))
    return out


def validate(current: Iterable[str], log: list[tuple[str, tuple[str, ...], tuple[str, ...]]],
             size: int | None) -> list[dict[str, Any]]:
    """Walk back from today; report every date where the reconstructed index has the wrong size
    or a change does not fit (removing a stock that is not there)."""
    members = set(current)
    problems = []
    for d, added, removed in reversed(log):
        missing = [s for s in added if s not in members]
        present = [s for s in removed if s in members]
        if missing or present:
            problems.append({"date": d, "added_not_in_index": missing, "removed_still_in_index": present})
        members.difference_update(added)
        members.update(removed)
        if size is not None and len(members) != size:
            problems.append({"date": d, "size_before": len(members), "expected": size})
    return problems


def write_csv(log: list[tuple[str, tuple[str, ...], tuple[str, ...]]], path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "added", "removed"])
        for d, a, r in log:
            w.writerow([d, " ".join(a), " ".join(r)])
    return path


def history_path(state_dir: Path, index: str) -> Path:
    return Path(state_dir) / "index_history" / f"{index_key(index)}.csv"


def build_history(index: str, current: Iterable[str], state_dir: Path, *, since: str = "2021-01-01",
                  session: requests.Session | None = None, progress: Any = None) -> dict[str, Any]:
    """Build, validate and save one index's change log under state_dir/index_history/."""
    changes = fetch_changes(Path(state_dir) / "index_notices", since=since, session=session, progress=progress)
    try:
        renames = load_symbol_changes(session)
    except requests.RequestException:
        renames = []
    log = changes_for(changes, index, renames)
    current = [s for s in current if not s.upper().startswith("DUMMY")]
    problems = validate(current, log, SIZES.get(index_key(index)))
    path = write_csv(log, history_path(state_dir, index))
    meta = {"index": index_key(index), "since": since, "changes": len(log),
            "stocks_added": sum(len(a) for _, a, _ in log), "problems": problems,
            "built": date.today().isoformat(), "path": str(path)}
    path.with_suffix(".json").write_text(json.dumps(meta, indent=2))
    return meta


def ensure_history(index: str, current: Iterable[str], state_dir: Path, *, max_age_days: int = 30,
                   progress: Any = None) -> dict[str, Any] | None:
    """Build the change log if this index has a known size and no fresh log yet; None if unsupported."""
    if index_key(index) not in SIZES or index_key(index) == "NIFTY50":
        return None
    path = history_path(state_dir, index)
    meta = path.with_suffix(".json")
    if path.exists() and meta.exists():
        info = json.loads(meta.read_text())
        if (date.today() - date.fromisoformat(info["built"])).days <= max_age_days:
            return info
    return build_history(index, current, state_dir, progress=progress)


def point_in_time(universe: str, current: Iterable[str], state_dir: Path, *, changes_csv: str | None = None,
                  progress: Any = None):
    """Membership for backtests: an explicit CSV, the built-in NIFTY 50 log, or a history built
    from NSE press releases (downloaded on first use). None if none of these is available."""
    from .membership import membership_for
    current = list(current)
    if not changes_csv:
        try:
            ensure_history(universe, current, state_dir, progress=progress)
        except (requests.RequestException, ImportError, OSError, ValueError) as e:
            if progress:
                progress(f"could not build index history ({type(e).__name__}: {e}); using today's members")
    return membership_for(universe, current, changes_csv, state_dir)
