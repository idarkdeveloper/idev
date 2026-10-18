"""Indian-market filters for BUYS: ASM/GSM surveillance, the results blackout and the promoter pledge screen.

Rules (``FilterBook.check``), each with its own switch (default on, India only):
* ASM/GSM: a stock on NSE's Additional Surveillance Measure list (short- or long-term, any stage) or its Graded
  Surveillance Measure list is never bought.
* Results blackout: no new buy when the company's board meets for financial results within the next 5 trading days
  (today included).
* Promoter pledge: no new buy when pledged promoter shares are more than 20% of the promoter holding, or the pledged
  share rose by more than 5 percentage points in the latest filing.
Every input is point-in-time: a board-meeting notice or pledge filing disseminated at or after 15:00 IST counts from the
next trading day (``filing_time.usable_from``), so a decision never sees a filing it could not have seen.

Only buys are ever affected. Holdings get flags (``holding_flags``), never a sell. Nothing here places, modifies or
cancels an order. The lists come through the existing NSE client (its circuit breaker and back-off), once per trading
day, cached in ``<state_dir>/indian_filters.json``. When a list is missing, unreadable or more than 2 trading days old,
that filter blocks nothing and says so ("filter data unavailable"): buys are then held back only by the other rules.

NSE's JSON shapes (reportASM, reportGSM, corporate-board-meetings, corporate-pledgedata) are not documented; the parsers
accept the field names seen on the NSE pages and skip rows they cannot read.

Not built here: the tax-aware B2 rank buffer (defer a profitable rank-buffer exit aged 335-364 days to day 366). It needs
the B2 rank buffer, which does not exist yet.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .state import atomic_write
from .timezones import IST

log = logging.getLogger(__name__)

FILE = "indian_filters.json"
PARTS = ("asm", "gsm", "results", "pledge")
LABEL = {"asm": "ASM list", "gsm": "GSM list", "results": "board meetings", "pledge": "promoter pledges"}
SWITCH = {"asm": "asm_gsm_filter", "gsm": "asm_gsm_filter", "results": "results_blackout_filter", "pledge": "pledge_filter"}
PATHS = {"asm": ("api/reportASM", None), "gsm": ("api/reportGSM", None),
         "results": ("api/corporate-board-meetings", {"index": "equities"}),
         "pledge": ("api/corporate-pledgedata", {"index": "equities"})}
REFERER = {"asm": "https://www.nseindia.com/reports/asm", "gsm": "https://www.nseindia.com/reports/gsm",
           "results": "https://www.nseindia.com/companies-listing/corporate-filings-board-meetings",
           "pledge": "https://www.nseindia.com/companies-listing/corporate-filings-pledged-data"}
BLACKOUT_TRADING_DAYS = 5
RESULTS_AHEAD_DAYS = 14   # board meetings are fetched for this many calendar days ahead (covers 5 trading days and holidays)
PLEDGE_MAX_PCT = 20.0
PLEDGE_MAX_RISE_PP = 5.0
MAX_AGE_TRADING_DAYS = 2
# Promoter pledges change slowly and each row carries its own filing time, so the last good list stays in use for two
# trading weeks. NSE's pledge list was empty on NSE's own page from 16 Oct 2026 (its count read 0 companies); with the
# 2-day rule the pledge check would have switched itself off by the Wednesday.
MAX_AGE_BY_PART = {"pledge": 10}
PLEDGE_COUNT_PATH = "api/corporate-pledge-holding"
MAX_TRIES = 6
BACKOFF_MINUTES = (5, 15, 60)
_LOCK = threading.Lock()
_warned: set[str] = set()


# -- parsing NSE's answers ---------------------------------------------------------------------------
def _rows(payload: Any, *keys: str) -> list[dict[str, Any]]:
    """The list of row dicts in an NSE answer: the payload itself, or under one of ``keys`` (nested one level too)."""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for k in keys:
            v = payload.get(k)
            if isinstance(v, list):
                return [r for r in v if isinstance(r, dict)]
            if isinstance(v, dict) and isinstance(v.get("data"), list):
                return [r for r in v["data"] if isinstance(r, dict)]
    return []


def _pick(row: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if row.get(k) not in (None, ""):
            return row[k]
    return None


def _sym(row: dict[str, Any]) -> str:
    return str(_pick(row, "symbol", "sym", "SYMBOL", "bm_symbol", "Symbol") or "").strip().upper()


def parse_asm(payload: Any) -> dict[str, str]:
    """symbol -> "ASM long-term stage II" / "ASM short-term stage I" from NSE's reportASM answer."""
    out: dict[str, str] = {}
    for part, word in (("longterm", "long-term"), ("shortterm", "short-term")):
        block = payload.get(part) if isinstance(payload, dict) else None
        for r in _rows(block, "data") if block is not None else []:
            sym = _sym(r)
            if sym:
                stage = str(_pick(r, "asmSurvIndicator", "stage", "asmStage", "survDesc") or "").strip()
                out[sym] = f"ASM {word}" + (f" {stage}" if stage else "")
    if not out:   # a flat list
        for r in _rows(payload, "data"):
            sym = _sym(r)
            if sym:
                out[sym] = "ASM " + str(_pick(r, "asmSurvIndicator", "stage", "survDesc") or "").strip()
    return {k: v.strip() for k, v in out.items()}


def parse_gsm(payload: Any) -> dict[str, str]:
    """symbol -> "GSM stage 2" from NSE's reportGSM answer."""
    out: dict[str, str] = {}
    for r in _rows(payload, "data", "gsm"):
        sym = _sym(r)
        if sym:
            stage = str(_pick(r, "gsmStage", "stage", "survDesc", "gsm_stage") or "").strip()
            out[sym] = "GSM" + (f" {stage}" if stage else "")
    return out


_RESULTS = re.compile(r"result", re.I)


def parse_meetings(payload: Any) -> dict[str, list[dict[str, Any]]]:
    """symbol -> [{"date": meeting day, "filed": dissemination time}] for board meetings about financial results."""
    from .filing_time import parse_ts
    out: dict[str, list[dict[str, Any]]] = {}
    for r in _rows(payload, "data"):
        sym = _sym(r)
        what = " ".join(str(_pick(r, k) or "") for k in ("bm_purpose", "purpose", "bm_desc", "desc"))
        if not sym or not _RESULTS.search(what):
            continue
        day = parse_ts(_pick(r, "bm_date", "meetingDate", "date"))
        if isinstance(day, datetime):
            day = day.date()
        if not isinstance(day, date):
            continue
        filed = parse_ts(_pick(r, "bm_timestamp", "attachmentDate", "broadcastDt", "disseminationDate", "an_dt"))
        out.setdefault(sym, []).append({"date": day.isoformat(), "filed": filed.isoformat() if filed is not None else None})
    return out


def _pct(v: Any) -> float | None:
    try:
        x = float(str(v).replace("%", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return x if 0 <= x <= 100 else None


_NAME_NOISE = re.compile(r"\b(the|limited|ltd|ltd\.|pvt|private|co|company|corporation|corp|inc)\b")


def norm_name(name: Any) -> str:
    """A company name reduced for matching: 'The Tata Power Company Ltd.' -> 'tata power'."""
    s = str(name or "").lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", _NAME_NOISE.sub(" ", s)).strip()


def name_index(names: dict[str, str]) -> dict[str, str]:
    """normalised company name -> NSE symbol, from {symbol: company name} (NSE's equity list). A name that two
    symbols share is left out rather than guessed."""
    out: dict[str, str] = {}
    seen: set[str] = set()
    for sym, name in names.items():
        key = norm_name(name)
        if not key:
            continue
        if key in seen:
            out.pop(key, None)
            continue
        seen.add(key)
        out[key] = sym.upper()
    return out


def parse_pledges(payload: Any, names: Any = None) -> dict[str, dict[str, Any]]:
    """symbol -> {"pct": pledged % of the promoter holding, "filed": dissemination time} from NSE's pledged data.
    NSE's rows name the company (``comName``) and carry no symbol: ``names`` ({symbol: company name}, or a callable
    giving it, called only when a row has no symbol) maps it back through ``name_index``."""
    from .filing_time import parse_ts
    out: dict[str, dict[str, Any]] = {}
    index: dict[str, str] | None = None
    for r in _rows(payload, "data"):
        sym = _sym(r)
        if not sym:
            if index is None:
                index = name_index(names() if callable(names) else (names or {}))
            sym = index.get(norm_name(_pick(r, "comName", "companyName", "name")), "")
        pct = _pct(_pick(r, "percPromoterShares", "perPromoterShares", "pledgedPctOfPromoter", "promoterPledgePct"))
        if not sym or pct is None:
            continue
        filed = parse_ts(_pick(r, "broadcastDt", "disclosureDate", "disclosureToDate", "date", "filingDate"))
        out[sym] = {"pct": round(pct, 2), "filed": filed.isoformat() if filed is not None else None}
    return out


def _nse_pledge_count(client: Any) -> int | None:
    """NSE's own count of companies with a reported promoter pledge (the line above its pledge table), or None when it
    cannot be read. 0 means NSE itself is publishing no pledge data, not that our request failed."""
    try:
        rows = _rows(client._get(PLEDGE_COUNT_PATH, params=None, referer=REFERER["pledge"]), "data")
        return int(str((rows[0] if rows else {}).get("NUMBER_OF_COMPANY")).replace(",", ""))
    except Exception:  # noqa: BLE001 - only used to word the error
        return None


PARSERS = {"asm": parse_asm, "gsm": parse_gsm, "results": parse_meetings, "pledge": parse_pledges}


# -- the book --------------------------------------------------------------------------------------
def _trading(day: date, cal: Any) -> bool:
    from .safety import is_trading_day
    return is_trading_day(day, cal)


def trading_days_ahead(start: date, n: int, cal: Any) -> date:
    """The ``n``-th trading day after ``start`` (``start`` itself is day 0)."""
    d, left = start, n
    while left > 0:
        d += timedelta(days=1)
        if _trading(d, cal):
            left -= 1
    return d


class FilterBook:
    """The stored lists and the rules over them. ``now_fn`` and ``calendar`` are for tests."""

    def __init__(self, state_dir: Path, enabled: dict[str, bool] | None = None, now_fn: Any = None, calendar: Any = None):
        self.path = Path(state_dir) / FILE
        self.enabled = {p: True for p in PARTS} if enabled is None else {p: bool(enabled.get(p, False)) for p in PARTS}
        self.now_fn = now_fn or (lambda: datetime.now(IST))
        self.calendar = calendar
        self._cache: tuple[float, dict[str, Any]] | None = None

    # -- storage ----------------------------------------------------------------------------------
    def load(self) -> dict[str, Any]:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return {}
        if self._cache and self._cache[0] == mtime:
            return self._cache[1]
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        d = d if isinstance(d, dict) else {}
        self._cache = (mtime, d)
        return d

    def _save(self, data: dict[str, Any]) -> None:
        with _LOCK:
            atomic_write(self.path, json.dumps(data, sort_keys=True))
        self._cache = None

    def store(self, part: str, parsed: Any, day: date, now: datetime) -> None:
        """Save one list. The pledge list keeps each symbol's previous filing too, so a rise can be measured."""
        data = self.load()
        if part == "pledge":
            old = ((data.get("pledge") or {}).get("symbols") or {}) if isinstance(data.get("pledge"), dict) else {}
            merged: dict[str, Any] = {}
            for sym, cur in parsed.items():
                hist = [h for h in (old.get(sym) or []) if isinstance(h, dict)]
                if not hist or hist[-1].get("filed") != cur.get("filed") or hist[-1].get("pct") != cur.get("pct"):
                    hist = (hist + [cur])[-3:]
                merged[sym] = hist
            parsed = merged
        data[part] = {"date": day.isoformat(), "fetched_at": now.isoformat(timespec="seconds"), "symbols": parsed}
        self._save(data)

    def _tries(self, day: str) -> tuple[int, datetime | None]:
        t = (self.load().get("tries") or {}).get(day) or {}
        try:
            last = datetime.fromisoformat(t["last"]) if t.get("last") else None
        except ValueError:
            last = None
        return int(t.get("tries") or 0), last

    def _note_try(self, day: str, now: datetime, errors: dict[str, str]) -> None:
        data = self.load()
        n, _ = self._tries(day)
        data["tries"] = {day: {"tries": n + 1, "last": now.isoformat(timespec="seconds"), "errors": errors}}
        self._save(data)

    # -- fetching (the watch loop) -------------------------------------------------------------------
    def wanted(self) -> list[str]:
        return [p for p in PARTS if self.enabled[p]]

    def due(self, now: datetime) -> list[str]:
        """The lists still missing for today on a trading day, within the try budget (5, 15 then 60 min apart)."""
        now = now.astimezone(IST)
        if not _trading(now.date(), self.calendar):
            return []
        data = self.load()
        missing = [p for p in self.wanted() if (data.get(p) or {}).get("date") != now.date().isoformat()]
        if not missing:
            return []
        n, last = self._tries(now.date().isoformat())
        if n >= MAX_TRIES:
            return []
        if last is not None and now - last < timedelta(minutes=BACKOFF_MINUTES[min(n - 1, len(BACKOFF_MINUTES) - 1)]):
            return []
        return missing

    def fetch(self, client: Any, part: str, now: datetime | None = None, names: Any = None) -> Any:
        """One list through the NSE client (breaker, back-off and cookies included). Raises on failure, and on an
        answer with no usable rows (these lists are never empty in practice: an empty one means NSE changed or refused).
        Board meetings are asked for by meeting date, today to two weeks on: without dates NSE sends only its latest 20
        notices. ``names`` is a callable giving {symbol: company name} for the pledge rows, which name no symbol."""
        path, params = PATHS[part]
        if part == "results":
            day = (now or self.now_fn()).astimezone(IST).date()
            params = {**(params or {}), "from_date": f"{day:%d-%m-%Y}", "to_date": f"{day + timedelta(days=RESULTS_AHEAD_DAYS):%d-%m-%Y}"}
        payload = client._get(path, params=params, referer=REFERER[part])
        parsed: Any = parse_pledges(payload, names) if part == "pledge" else PARSERS[part](payload)
        if not isinstance(parsed, dict) or not parsed:
            if part == "pledge" and _nse_pledge_count(client) == 0:
                raise ValueError(f"{LABEL[part]}: NSE is publishing no pledge data right now (its own page counts 0 "
                                 f"companies); the last good list stays in use for up to {MAX_AGE_BY_PART['pledge']} "
                                 "trading days")
            raise ValueError(f"{LABEL[part]}: NSE answered with no usable rows")
        return parsed

    def tick(self, client: Any, now: datetime, deadline: float | None = None, names: Any = None) -> dict[str, int]:
        """For the watch loop: fetch what is due, store it, never raise. Returns {part: symbols stored}. ``deadline``
        (time.monotonic) stops between lists when the tick's network budget is spent; the rest wait for a later tick."""
        import time
        out: dict[str, int] = {}
        if client is None or not hasattr(client, "_get"):
            return out
        parts = self.due(now)
        if not parts:
            return out
        errors: dict[str, str] = {}
        for part in parts:
            if deadline is not None and time.monotonic() >= deadline:
                break
            try:
                parsed = self.fetch(client, part, now, names if names is not None else self._names)
                self.store(part, parsed, now.astimezone(IST).date(), now)
                out[part] = len(parsed)
            except Exception as e:  # noqa: BLE001 - a missing list only means that filter blocks nothing
                errors[part] = f"{type(e).__name__}: {e}"[:200]
                self._warn_once(f"Indian filter data unavailable ({LABEL[part]}): {e}", now.date())
        if errors:
            self._note_try(now.astimezone(IST).date().isoformat(), now, errors)
        return out

    def _names(self) -> dict[str, str]:
        """{symbol: company name} from NSE's equity list (EQUITY_L.csv, cached a day beside the other caches)."""
        from .instruments import CompanyNames
        return CompanyNames(self.path.parent / "cache")._nse_names()

    def _warn_once(self, msg: str, day: date | None = None) -> None:
        key = f"{day}:{msg[:50]}"
        if key not in _warned:
            _warned.add(key)
            log.warning("%s", msg)

    # -- answers -----------------------------------------------------------------------------------
    def _today(self) -> date:
        return self.now_fn().astimezone(IST).date()

    def current(self, part: str) -> dict[str, Any] | None:
        """The stored list for ``part`` unless the filter is off, it is missing, or too old: more than 2 trading days
        (10 for promoter pledges, see MAX_AGE_BY_PART)."""
        if not self.enabled.get(part):
            return None
        block = self.load().get(part)
        if not isinstance(block, dict) or not isinstance(block.get("symbols"), dict):
            return None
        try:
            listed = date.fromisoformat(str(block.get("date")))
        except ValueError:
            return None
        today = self._today()
        age = sum(1 for i in range(1, (today - listed).days + 1) if _trading(listed + timedelta(days=i), self.calendar))
        return None if age > MAX_AGE_BY_PART.get(part, MAX_AGE_TRADING_DAYS) else block["symbols"]

    def unavailable(self) -> list[str]:
        """The switched-on filters whose data is missing or stale (they block nothing until it comes back)."""
        names = []
        for p in self.wanted():
            if self.current(p) is None:
                names.append({"asm": "ASM/GSM", "gsm": "ASM/GSM", "results": "results blackout", "pledge": "promoter pledges"}[p])
        return list(dict.fromkeys(names))

    def note(self) -> str | None:
        """'filter data unavailable: ASM/GSM, promoter pledges' when any switched-on filter has no usable data."""
        miss = self.unavailable()
        return ("filter data unavailable: " + ", ".join(miss) + " (buys are held back only by the other rules)") if miss else None

    def _usable(self, filed: Any) -> bool:
        """A filing counts from ``usable_from`` (15:00 IST rule); one with no readable time counts (it was in the list)."""
        if not filed:
            return True
        from .filing_time import usable_from
        try:
            return usable_from(filed, self.calendar) <= self._today()
        except ValueError:
            return True

    def check(self, symbol: str) -> dict[str, Any]:
        """{"block": bool, "reasons": [why a buy is refused], "flags": [short labels], "results_due": iso | None}.
        Only ever about buying."""
        sym = str(symbol or "").strip().upper().removesuffix(".NS")
        reasons: list[str] = []
        flags: list[str] = []
        due_on = None
        for part in ("asm", "gsm"):
            lst = self.current(part)
            if lst and sym in lst:
                label = str(lst[sym]).strip()
                flags.append(label)
                reasons.append(f"on NSE's {'Additional' if part == 'asm' else 'Graded'} Surveillance Measure list ({label}); not bought")
        meetings = self.current("results")
        if meetings and sym in meetings:
            today = self._today()
            last_day = trading_days_ahead(today, BLACKOUT_TRADING_DAYS, self.calendar)
            for m in sorted(meetings[sym], key=lambda x: str(x.get("date"))):
                try:
                    d = date.fromisoformat(str(m.get("date")))
                except ValueError:
                    continue
                if today <= d <= last_day and self._usable(m.get("filed")):
                    due_on = d.isoformat()
                    flags.append(f"Results due {d.day} {d:%b}")
                    reasons.append(f"results due {d.day} {d:%b} (within {BLACKOUT_TRADING_DAYS} trading days); no new buy before them")
                    break
        pledges = self.current("pledge")
        if pledges and sym in pledges:
            hist = [h for h in pledges[sym] if isinstance(h, dict) and self._usable(h.get("filed"))]
            if hist and isinstance(hist[-1].get("pct"), (int, float)):
                pct = float(hist[-1]["pct"])
                prev = hist[-2].get("pct") if len(hist) > 1 else None
                rise = pct - float(prev) if isinstance(prev, (int, float)) else None
                if pct > PLEDGE_MAX_PCT:
                    flags.append(f"Pledge {pct:.0f}%")
                    reasons.append(f"promoters have pledged {pct:.1f}% of their shares (more than {PLEDGE_MAX_PCT:.0f}%); not bought")
                elif rise is not None and rise > PLEDGE_MAX_RISE_PP:
                    flags.append(f"Pledge +{rise:.0f} pts")
                    reasons.append(f"promoter pledges rose {rise:.1f} percentage points in the latest filing (more than "
                                   f"{PLEDGE_MAX_RISE_PP:.0f}); not bought")
                elif pct > 0:
                    flags.append(f"Pledge {pct:.0f}%")   # shown on holdings, never a block
        return {"block": bool(reasons), "reasons": reasons, "flags": flags, "results_due": due_on}

    def refuse_buy(self, symbol: str) -> str | None:
        """The message for a buy that must not happen, else None."""
        r = self.check(symbol)
        return f"{str(symbol).strip().upper()}: " + "; ".join(r["reasons"]) if r["block"] else None

    def holding_flags(self, symbol: str) -> list[str]:
        """Short flags for a stock you hold (review it; nothing here ever sells)."""
        return self.check(symbol)["flags"]

    def active(self) -> bool:
        """True when at least one switched-on filter has usable data, i.e. buys are really being filtered."""
        return any(self.current(p) is not None for p in self.wanted())


def filters_for(settings: Any, now_fn: Any = None, calendar: Any = None) -> FilterBook:
    """The book for these settings: each switch on by default; India only. The cached holiday list, never the network."""
    india = getattr(settings, "market", "in") == "in"
    enabled = {p: india and bool(getattr(settings, SWITCH[p], True)) for p in PARTS}
    if calendar is None:
        from .groww_hours import default_calendar
        try:
            calendar = default_calendar(settings)
        except Exception:  # noqa: BLE001
            calendar = None
    return FilterBook(Path(settings.state_dir), enabled=enabled, now_fn=now_fn, calendar=calendar)
