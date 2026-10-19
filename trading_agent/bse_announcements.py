"""BSE corporate announcements for BSE-only stocks (tracked as ``<scrip code>.BO``).

NSE's announcement feed has nothing for a company that is not listed on NSE, so the watch, the morning email, the
dashboard's Look up and Claude's ``get_announcements`` tool saw no filings for BSE-only holdings. Two public BSE
sources, no key or login:

1. ``api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w``: the JSON the BSE site's announcements page reads,
   per scrip code and date range (the last ``DAYS`` days). It is behind Akamai, which refuses some cloud IP ranges
   (403 "Access Denied"); after a refusal it is not asked again for ``API_BLOCK_S``.
2. ``www.bseindia.com/data/xml/announcements.xml``: the RSS feed of the latest announcements of every company, each
   with its scrip code. Read at most once a minute and filtered by code; it only holds the newest items, so what it
   shows is merged into the cache and kept.

Both are normalised to the NSE row shape (``id``, ``symbol``, ``company``, ``at``, ``category``, ``text``, ``file``)
with ids prefixed ``bse:`` and keyed by the filing's PDF name when it has one, so the same filing seen in both
sources counts once. The per-company cache (``cache/bse_ann/<code>.json``) keeps the last ``KEEP_DAYS`` days.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import requests

from .timezones import IST

log = logging.getLogger(__name__)

API_URL = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
RSS_URL = "https://www.bseindia.com/data/xml/announcements.xml"
ATTACH_URL = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/{name}"
HEADERS = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
           "Accept": "application/json, text/plain, */*", "Accept-Language": "en-US,en;q=0.9",
           "Referer": "https://www.bseindia.com/", "Origin": "https://www.bseindia.com"}
DAYS = 30                 # the API window asked for
KEEP_DAYS = 90            # the cache keeps this much
API_TTL_S = 900.0         # one API read per company every 15 minutes at most
API_BLOCK_S = 6 * 3600.0  # after a 403 (Akamai), the API is left alone this long; the RSS feed carries on
RSS_TTL_S = 60.0          # the RSS feed at most once a minute, for all companies
TIMEOUT_S = 20.0
CODE = re.compile(r"\d{5,6}")


class BSEAnnouncementError(RuntimeError):
    """Neither BSE source could be read and nothing is cached."""


_CODES: dict[tuple[str, float], dict[str, str]] = {}


def cached_bse_code(cache_dir: Path, symbol: str) -> str | None:
    """The BSE scrip code of a BSE trading symbol (a holding such as ``NSE``), from the Groww instrument list already
    cached on this machine (``groww_instruments.csv``). Offline: never downloads anything; None when the file is missing
    or the symbol is not in it."""
    import csv
    path = Path(cache_dir) / "groww_instruments.csv"
    try:
        key = (str(path), path.stat().st_mtime)
        table = _CODES.get(key)
        if table is None:
            table = {}
            with path.open(encoding="utf-8", errors="replace", newline="") as f:
                for r in csv.DictReader(f):
                    if (r.get("exchange") or "").upper() == "BSE" and (r.get("segment") or "").upper() == "CASH":
                        code = (r.get("exchange_token") or "").strip()
                        sym = (r.get("trading_symbol") or "").strip().upper()
                        if sym and CODE.fullmatch(code):
                            table.setdefault(sym, code)
            _CODES.clear()
            _CODES[key] = table
        return table.get(str(symbol or "").strip().upper().removesuffix(".BO"))
    except OSError:
        return None


def scrip_code(symbol: str) -> str | None:
    """'543287.BO' or '543287' -> '543287'; None for anything that is not a BSE scrip code."""
    s = str(symbol or "").strip().upper().removesuffix(".BO")
    return s if CODE.fullmatch(s) else None


def _at(raw: Any) -> str:
    """BSE times ('2026-10-16T01:01:02.063', '16-Oct-2026 01:01:02', '16/10/2026 01:01:02') as
    'YYYY-MM-DD HH:MM:SS' in IST, like NSE's rows; '' when unreadable."""
    t = str(raw or "").strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%d-%b-%Y %H:%M:%S", "%d/%m/%Y %H:%M:%S",
                "%d %b %Y %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(t, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return ""


def _clean(v: Any, limit: int = 600) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()[:limit]


def _company(v: Any) -> str:
    """BSE's long name without its trailing markers ('… India Ltd-$')."""
    return re.sub(r"\s*-?\s*[$#*]+\s*$", "", _clean(v, 200))


def _id(attachment: str, news_id: Any) -> str:
    stem = re.sub(r"\.pdf$", "", str(attachment or "").strip(), flags=re.I)
    if stem:
        return f"bse:{stem}"
    return f"bse:{_clean(news_id, 80)}" if news_id else ""


def norm_api(row: dict[str, Any], code: str) -> dict[str, Any] | None:
    """One row of the API's ``Table``."""
    att = _clean(row.get("ATTACHMENTNAME"), 200)
    rid = _id(att, row.get("NEWSID"))
    at = _at(row.get("DT_TM") or row.get("NEWS_DT"))
    if not rid or not at:
        return None
    sub, cat = _clean(row.get("SUBCATNAME"), 120), _clean(row.get("CATEGORYNAME"), 120)
    return {"id": rid, "symbol": f"{code}.BO", "company": _company(row.get("SLONGNAME")),
            "at": at, "category": sub if sub and sub != "-" else cat or "Announcement",
            "text": _clean(row.get("HEADLINE") or row.get("NEWSSUB")),
            "file": ATTACH_URL.format(name=att) if att else ""}


def parse_rss(xml_text: str) -> list[dict[str, Any]]:
    """Every item of the latest-announcements feed: {code, row}. Refuses a DOCTYPE/ENTITY declaration."""
    if re.search(r"<!(DOCTYPE|ENTITY)", xml_text, re.I):
        raise ValueError("refusing XML with a DOCTYPE/ENTITY declaration")
    root = ET.fromstring(xml_text.lstrip("﻿"))
    out = []
    for it in root.iter("item"):
        code = _clean(it.findtext("scripcode"), 10)
        title = _clean(it.findtext("title"), 250)
        if not CODE.fullmatch(code):
            m = re.search(r"\((\d{5,6})\)\s*$", title)
            code = m.group(1) if m else ""
        link = _clean(it.findtext("link"), 300)
        at = _at(it.findtext("pubDate"))
        rid = _id(link.rsplit("/", 1)[-1] if link.lower().endswith(".pdf") else "", link)
        if not code or not at or not rid:
            continue
        company = _company(re.sub(r"\s*\(\d{5,6}\)\s*$", "", title))
        out.append({"code": code, "row": {"id": rid, "symbol": f"{code}.BO", "company": company, "at": at,
                                          "category": "Announcement", "text": _clean(it.findtext("description")),
                                          "file": link if link.startswith("https://") else ""}})
    return out


class BSEAnnouncements:
    """Pass ``session``, ``clock`` and ``monotonic`` to inject fakes in tests."""

    def __init__(self, session: Any | None = None, cache_dir: Path | None = None, timeout: float = TIMEOUT_S,
                 clock: Callable[[], datetime] | None = None, monotonic: Callable[[], float] = time.monotonic):
        self.session = session or requests.Session()
        self.cache_dir = Path(cache_dir) / "bse_ann" if cache_dir else None
        self.timeout = timeout
        self.clock = clock or (lambda: datetime.now(IST))
        self.monotonic = monotonic
        self._lock = threading.RLock()
        self._api_at: dict[str, float] = {}
        self._api_ok: set[str] = set()   # codes whose last API read answered (an empty answer counts)
        self._api_blocked_until = -1e9
        self._rss: tuple[float, list[dict[str, Any]]] | None = None
        self._mem: dict[str, dict[str, dict[str, Any]]] = {}
        self.last_error: str | None = None

    # -- cache ---------------------------------------------------------------
    def _path(self, code: str) -> Path | None:
        return self.cache_dir / f"{code}.json" if self.cache_dir else None

    def _load(self, code: str) -> dict[str, dict[str, Any]]:
        if code in self._mem:
            return self._mem[code]
        p = self._path(code)
        rows: dict[str, dict[str, Any]] = {}
        if p and p.exists():
            try:
                rows = {r["id"]: r for r in json.loads(p.read_text(encoding="utf-8")) if isinstance(r, dict) and r.get("id")}
            except (OSError, ValueError):
                rows = {}
        self._mem[code] = rows
        return rows

    def _store(self, code: str, new: list[dict[str, Any]]) -> None:
        rows = self._load(code)
        for r in new:
            old = rows.get(r["id"])
            # the API row has the real category; an RSS copy of the same filing never overwrites it
            if old is None or (old.get("category") == "Announcement" and r.get("category") != "Announcement"):
                rows[r["id"]] = r
        cutoff = (self.clock() - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
        for k in [k for k, r in rows.items() if r["at"][:10] < cutoff]:
            del rows[k]
        p = self._path(code)
        if p:
            from .state import atomic_write
            p.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(p, json.dumps(sorted(rows.values(), key=lambda r: r["at"], reverse=True)))

    def _note(self, source: str, ok: bool, rows: int | None = None, error: str | None = None) -> None:
        """The Health tab's record (state/source_runs.json), only in the real state/cache layout."""
        if self.cache_dir is None or self.cache_dir.parent.name != "cache":
            return
        from .health import note_run
        note_run(self.cache_dir.parent.parent, source, ok=ok, rows=rows, error=error)

    # -- sources -------------------------------------------------------------
    def _from_api(self, code: str) -> list[dict[str, Any]] | None:
        """None when not asked (fresh, or blocked); raises on a failure."""
        now = self.monotonic()
        if now < self._api_blocked_until or now - self._api_at.get(code, -1e9) < API_TTL_S:
            return None
        self._api_at[code] = now
        self._api_ok.discard(code)
        end = self.clock()
        params: dict[str, Any] = {"pageno": 1, "strCat": -1, "strPrevDate": (end - timedelta(days=DAYS)).strftime("%Y%m%d"),
                  "strScrip": code, "strSearch": "P", "strToDate": end.strftime("%Y%m%d"), "strType": "C",
                  "subcategory": -1}
        r = self.session.get(API_URL, params=params, headers=HEADERS, timeout=self.timeout)
        if r.status_code == 403:
            self._api_blocked_until = now + API_BLOCK_S
            self._note("bse_ann_api", False, error="refused this server (403)")
            raise BSEAnnouncementError("BSE announcements API refused this server (403); using the RSS feed")
        r.raise_for_status()
        data = r.json()
        table = data.get("Table") if isinstance(data, dict) else None
        if not isinstance(table, list):
            raise BSEAnnouncementError("BSE announcements API answered without a Table")
        self._api_ok.add(code)
        self._note("bse_ann_api", True, rows=len(table))
        return [n for n in (norm_api(row, code) for row in table if isinstance(row, dict)) if n]

    def _from_rss(self, code: str) -> list[dict[str, Any]]:
        now = self.monotonic()
        if self._rss is None or now - self._rss[0] >= RSS_TTL_S:
            r = self.session.get(RSS_URL, headers={**HEADERS, "Accept": "application/rss+xml, text/xml, */*"},
                                 timeout=self.timeout)
            r.raise_for_status()
            self._rss = (now, parse_rss(r.content.decode("utf-8", errors="replace")))
        return [i["row"] for i in self._rss[1] if i["code"] == code]

    # -- the call --------------------------------------------------------------
    def announcements(self, symbol: str, limit: int = 20) -> list[dict[str, Any]]:
        """Recent BSE announcements for one BSE-only stock, newest first. Raises BSEAnnouncementError only when no
        source answered and nothing is cached; ValueError for a symbol that is not a BSE scrip code."""
        code = scrip_code(symbol)
        if code is None:
            raise ValueError(f"{symbol!r} is not a BSE scrip code (like 543287.BO)")
        errors: list[str] = []
        with self._lock:
            answered = code in self._api_ok   # a recent API answer still stands while it is fresh
            for name, fetch in (("api", self._from_api), ("rss", self._from_rss)):
                try:
                    got = fetch(code)
                except (requests.RequestException, ValueError, ET.ParseError, BSEAnnouncementError) as e:
                    errors.append(f"{name}: {type(e).__name__}: {e}"[:200])
                    continue
                if got is not None:
                    answered = True
                    if got:
                        self._store(code, got)
            rows = list(self._load(code).values())
        if errors:
            self.last_error = "; ".join(errors)
            log.info("BSE announcements for %s: %s", code, self.last_error)
        if not answered and not rows:   # nothing read now, nothing recent, nothing cached
            self._note("bse_ann", False, error=self.last_error)
            raise BSEAnnouncementError(self.last_error or "BSE announcements unavailable")
        if answered:
            self._note("bse_ann", True, rows=len(rows))
        rows.sort(key=lambda r: r["at"], reverse=True)
        return rows[:limit]
