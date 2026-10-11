"""Corporate-action guard and normaliser (splits, bonuses, consolidations, large special dividends, demergers).

A 1:1 bonus halves the share price at the ex-date open; a GTT stop left at the old level would sell at once. So:

* ``ActionBook`` fetches NSE's corporate-actions list (``api/corporates-corporateActions``, today to 30 days ahead)
  once each trading day through the existing NSE client (breaker, back-off, cookies), and keeps rows whose ex-date
  passed in the last 10 days, so a holding can still be normalised after a missed day.
* Flags: the morning email and the dashboard show held stocks with a split, bonus, consolidation, demerger or large
  special dividend (at least 2% of the price) with an ex-date within 7 calendar days.
* GTT guard (live): from 15:00 on the session before the ex-date (or at once on the ex-date if that was missed) the
  GTT is cancelled and the holding marked suspended. After the ex-date it is re-created at the adjusted level once
  Groww's quantity shows the new shares, or the day after the ex-date whatever Groww shows. No stop alert either
  while suspended.
* Normaliser (practice account): from 08:30 IST on the ex-date, cost basis, quantity, trailing high and fixed stop are
  rescaled by the ratio, once per holding and action (a large special dividend lowers the trailing high instead).
* Fail safe: when the list is missing or older than 2 trading days the holdings are flagged "corporate actions
  unknown" and GTTs are left exactly as they are: nothing is ever cancelled blindly.
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any

from .state import atomic_write
from .timezones import IST

log = logging.getLogger(__name__)

FILE = "corporate_actions.json"
PATH = "api/corporates-corporateActions"
REFERER = "https://www.nseindia.com/companies-listing/corporate-filings-actions"
AHEAD_DAYS = 30
KEEP_PAST_DAYS = 10
FLAG_DAYS = 7
LARGE_DIVIDEND_PCT = 2.0
SUSPEND_FROM = dtime(15, 0)      # on the session before the ex-date
NORMALISE_FROM = dtime(8, 30)    # on the ex-date
MAX_TRIES = 6
BACKOFF_MINUTES = (5, 15, 60)
STALE_TRADING_DAYS = 2
UNKNOWN = "corporate actions unknown (NSE's list could not be read): splits and bonuses were not checked"
_LOCK = threading.RLock()

_RS = r"r[se]\.?\s*([\d.]+)"
_BONUS = re.compile(r"bonus\s*(\d+)\s*:\s*(\d+)", re.I)
_SPLIT = re.compile(r"split.*?from\s*" + _RS + r".*?to\s*" + _RS, re.I)
_CONSOL = re.compile(r"consolidation.*?from\s*" + _RS + r".*?to\s*" + _RS, re.I)
_DIVIDEND = re.compile(r"dividend\s*-?\s*" + _RS, re.I)
_SPECIAL = re.compile(r"special\s+dividend\s*-?\s*" + _RS, re.I)


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #
def _num(text: str) -> float | None:
    try:
        x = float(text.rstrip("."))
    except ValueError:
        return None
    return x if x > 0 else None


def parse_subject(subject: str) -> dict[str, Any] | None:
    """What one NSE subject line means for a holding, or None when it does not matter here (rights, AGMs, ordinary
    dividends, interest payments). ``ratio`` multiplies the quantity (1:1 bonus = 2.0, Rs 10 -> Rs 2 split = 5.0,
    Re 1 -> Rs 10 consolidation = 0.1); ``dividend`` is the cash per share of a subject with a special dividend."""
    text = str(subject or "")
    ratio, labels = 1.0, []
    for m in _BONUS.finditer(text):
        a, b = int(m.group(1)), int(m.group(2))
        if a > 0 and b > 0:
            ratio *= (a + b) / b
            labels.append(f"bonus {a}:{b}")
    for rx, word in ((_SPLIT, "split"), (_CONSOL, "consolidation")):
        fv = rx.search(text)
        if fv:
            old, new = _num(fv.group(1)), _num(fv.group(2))
            if old and new and old != new:
                ratio *= old / new
                labels.append(f"{word} ₹{old:g} → ₹{new:g} face value")
    special = _SPECIAL.search(text)
    demerger = "demerger" in text.lower()
    if ratio == 1.0 and not special and not demerger:
        return None
    dividend = sum(x for x in (_num(g) for g in _DIVIDEND.findall(text)) if x) if special else None
    if special:
        labels.append(f"special dividend ₹{_num(special.group(1)) or 0:g}"
                      + (f" (₹{dividend:g} in all)" if dividend and dividend != _num(special.group(1)) else ""))
    if demerger:
        labels.append("demerger")
    return {"ratio": None if ratio == 1.0 else round(ratio, 6), "dividend": dividend, "special": bool(special),
            "demerger": demerger, "label": ", ".join(labels)}


def parse_actions(payload: Any) -> list[dict[str, Any]]:
    """NSE rows -> [{symbol, ex_date (ISO), subject, ratio, dividend, special, demerger, label}] for the actions that
    matter here. Rows with no symbol, no readable ex-date or an irrelevant subject are dropped."""
    rows = payload if isinstance(payload, list) else (payload.get("data") if isinstance(payload, dict) else None)
    out = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        sym = str(r.get("symbol") or "").strip().upper()
        try:
            ex = datetime.strptime(str(r.get("exDate") or "").strip(), "%d-%b-%Y").date()
        except ValueError:
            continue
        what = parse_subject(str(r.get("subject") or ""))
        if not sym or what is None:
            continue
        out.append({"symbol": sym, "ex_date": ex.isoformat(), "subject": str(r.get("subject"))[:200], **what})
    return out


def _trading(day: date, calendar: Any) -> bool:
    return day.weekday() < 5 if calendar is None else bool(calendar.is_trading_day(day))


def session_before(ex: date, calendar: Any) -> date:
    d = ex - timedelta(days=1)
    for _ in range(10):
        if _trading(d, calendar):
            return d
        d -= timedelta(days=1)
    return d


# --------------------------------------------------------------------------- #
# the book
# --------------------------------------------------------------------------- #
class ActionBook:
    """``state/corporate_actions.json``: {"date", "fetched_at", "rows", "tries", "normalised"}."""

    def __init__(self, state_dir: Path, now_fn: Any = None, calendar: Any = None):
        self.path = Path(state_dir) / FILE
        self.now_fn = now_fn or (lambda: datetime.now(IST))
        self.calendar = calendar

    # -- storage ----------------------------------------------------------------------------------
    def load(self) -> dict[str, Any]:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return d if isinstance(d, dict) else {}

    def _save(self, data: dict[str, Any]) -> None:
        with _LOCK:
            atomic_write(self.path, json.dumps(data, sort_keys=True))

    # -- fetching (the watch loop) ------------------------------------------------------------------
    def due(self, now: datetime) -> bool:
        now = now.astimezone(IST)
        if not _trading(now.date(), self.calendar):
            return False
        data = self.load()
        if data.get("date") == now.date().isoformat():
            return False
        t = (data.get("tries") or {}).get(now.date().isoformat()) or {}
        n = int(t.get("tries") or 0)
        if n >= MAX_TRIES:
            return False
        try:
            last = datetime.fromisoformat(t["last"]) if t.get("last") else None
        except ValueError:
            last = None
        return last is None or now - last >= timedelta(minutes=BACKOFF_MINUTES[min(n - 1, len(BACKOFF_MINUTES) - 1)])

    def fetch(self, client: Any, now: datetime) -> list[dict[str, Any]]:
        """Today to 30 days on, by ex-date. Raises on failure and on an answer with no usable rows (a month of NSE
        corporate actions is never empty: an empty answer means NSE changed or refused)."""
        day = now.astimezone(IST).date()
        payload = client._get(PATH, params={"index": "equities", "from_date": f"{day:%d-%m-%Y}",
                                            "to_date": f"{day + timedelta(days=AHEAD_DAYS):%d-%m-%Y}"}, referer=REFERER)
        if not isinstance(payload, list) or not payload:
            raise ValueError("corporate actions: NSE answered with no rows")
        return parse_actions(payload)

    def store(self, rows: list[dict[str, Any]], now: datetime) -> None:
        data = self.load()
        day = now.astimezone(IST).date()
        keep_from = (day - timedelta(days=KEEP_PAST_DAYS)).isoformat()
        old = [r for r in data.get("rows") or [] if isinstance(r, dict) and keep_from <= str(r.get("ex_date")) < day.isoformat()]
        data.update({"date": day.isoformat(), "fetched_at": now.isoformat(timespec="seconds"), "rows": old + rows})
        self._save(data)

    def tick(self, client: Any, now: datetime) -> int | None:
        """Fetch when due, store, never raise. Returns the rows stored, or None when nothing was fetched."""
        if client is None or not hasattr(client, "_get") or not self.due(now):
            return None
        from . import health
        try:
            rows = self.fetch(client, now)
        except Exception as e:  # noqa: BLE001 - a missing list means "unknown": flagged, GTTs left alone
            data = self.load()
            key = now.astimezone(IST).date().isoformat()
            n = int(((data.get("tries") or {}).get(key) or {}).get("tries") or 0)
            data["tries"] = {key: {"tries": n + 1, "last": now.isoformat(timespec="seconds"),
                                   "error": f"{type(e).__name__}: {e}"[:200]}}
            self._save(data)
            health.note_run(self.path.parent, "corp_actions", ok=False, error=f"{type(e).__name__}: {e}")
            log.warning("NSE corporate actions unavailable: %s", e)
            return None
        self.store(rows, now)
        health.note_run(self.path.parent, "corp_actions", ok=True, rows=len(rows))
        return len(rows)

    # -- answers -----------------------------------------------------------------------------------
    def known(self, now: datetime | None = None) -> bool:
        """True when the stored list is from today or the last 2 trading days."""
        try:
            listed = date.fromisoformat(str(self.load().get("date")))
        except ValueError:
            return False
        today = (now or self.now_fn()).astimezone(IST).date()
        age = sum(1 for i in range(1, (today - listed).days + 1) if _trading(listed + timedelta(days=i), self.calendar))
        return age <= STALE_TRADING_DAYS

    def rows_for(self, symbol: str) -> list[dict[str, Any]]:
        sym = symbol.upper()
        return sorted((r for r in self.load().get("rows") or [] if isinstance(r, dict) and r.get("symbol") == sym),
                      key=lambda r: str(r.get("ex_date")))

    @staticmethod
    def moves_price(row: dict[str, Any], price: float | None) -> bool:
        """A ratio change, or a special dividend of at least 2% of the price (unknown price: counted)."""
        if row.get("ratio"):
            return True
        if row.get("special") and row.get("dividend"):
            return price is None or price <= 0 or row["dividend"] / price * 100 >= LARGE_DIVIDEND_PCT
        return False

    def upcoming(self, symbol: str, now: datetime | None = None, price: float | None = None,
                 days: int = FLAG_DAYS) -> list[dict[str, Any]]:
        """Actions with an ex-date from today to ``days`` calendar days on that move the price (and demergers)."""
        today = (now or self.now_fn()).astimezone(IST).date()
        last = (today + timedelta(days=days)).isoformat()
        return [r for r in self.rows_for(symbol) if today.isoformat() <= str(r["ex_date"]) <= last
                and (self.moves_price(r, price) or r.get("demerger"))]

    def reasons(self, symbol: str, now: datetime | None = None, price: float | None = None) -> list[str]:
        """Email lines for a holding: 'corporate action: bonus 1:1, ex-date 15 Oct ...'."""
        out = []
        for r in self.upcoming(symbol, now, price):
            ex = date.fromisoformat(r["ex_date"])
            tail = ("the GTT stop is paused over the ex-date and re-set at the adjusted level" if self.moves_price(r, price)
                    else "the price will drop by the demerged part: check the stop by hand")
            out.append(f"corporate action: {r['label']}, ex-date {ex:%d %b} ({tail})")
        return out

    def flags(self, symbol: str, now: datetime | None = None, price: float | None = None) -> list[str]:
        """Short dashboard pills: 'Bonus 1:1 ex 15 Oct'."""
        return [f"{r['label'][0].upper()}{r['label'][1:]} ex {date.fromisoformat(r['ex_date']):%d %b}"
                for r in self.upcoming(symbol, now, price)]

    def note(self, now: datetime | None = None) -> str | None:
        return None if self.known(now) else UNKNOWN

    # -- the GTT guard (live) --------------------------------------------------------------------------
    def gtt_step(self, symbol: str, now: datetime, qty: int, suspended: dict[str, Any] | None,
                 gtt_qty: int | None = None, price: float | None = None) -> tuple[str, dict[str, Any] | None]:
        """What the GTT manager does for one holding: ("normal", None), ("suspend", record to keep), ("wait", the
        suspension) or ("resume", the suspension). ``gtt_qty`` is the quantity of the GTT now at Groww (the
        quantity before the action)."""
        now = now.astimezone(IST)
        today = now.date()
        if suspended:
            ex = date.fromisoformat(str(suspended["ex_date"]))
            if today < ex:
                return "wait", suspended
            expected = int(suspended.get("expected_qty") or 0)
            if today == ex and qty < expected:
                return "wait", suspended     # Groww has not credited the new shares yet: no stop until it has
            return "resume", suspended       # shares credited, or the ex-date is over (whatever Groww shows)
        if not self.known(now):
            return "normal", None            # fail safe: the list is unknown, GTTs stay exactly as they are
        for r in self.rows_for(symbol):
            if not self.moves_price(r, price):
                continue
            ex = date.fromisoformat(str(r["ex_date"]))
            if today > ex or ex - today > timedelta(days=10):
                continue
            if today == ex or (today == session_before(ex, self.calendar) and now.time() >= SUSPEND_FROM):
                before = int(gtt_qty if gtt_qty else qty)
                ratio = float(r.get("ratio") or 1.0)
                return "suspend", {"ex_date": r["ex_date"], "label": r["label"], "ratio": r.get("ratio"),
                                   "dividend": r.get("dividend") if not r.get("ratio") else None,
                                   "qty_before": before, "expected_qty": int(math.floor(before * ratio + 1e-9)),
                                   "at": now.isoformat(timespec="seconds")}
        return "normal", None

    def held_back(self, symbol: str, now: datetime, suspended: dict[str, Any] | None = None,
                  account: str | None = None) -> bool:
        """No stop evaluation for this holding right now: its GTT is suspended, or today is its ex-date and (for a
        practice ``account``) the position is not normalised yet. Groww holdings (no account) are held back all of
        the ex-date: Groww's average price and quantity catch up during the day, the GTT protects meanwhile."""
        if suspended:
            return True
        today = now.astimezone(IST).date().isoformat()
        done = self.load().get("normalised") or {}
        sym = symbol.upper()
        return any(r["ex_date"] == today and self.moves_price(r, None)
                   and (account is None or f"{account}:{sym}:{today}" not in done) for r in self.rows_for(sym))

    # -- the normaliser (practice account) ---------------------------------------------------------------
    def normalise_practice(self, broker: Any, now: datetime | None = None, account: str = "paper_broker") -> list[dict[str, Any]]:
        """From 08:30 IST on (or after) an ex-date: rescale the paper positions held from before it. Once per account,
        holding and action; returns what changed. Positions bought on or after the ex-date are left alone."""
        now = (now or self.now_fn()).astimezone(IST)
        if broker is None or not hasattr(broker, "rescale_position"):
            return []
        data = self.load()
        done = data.setdefault("normalised", {})
        today = now.date().isoformat()
        out = []
        for r in data.get("rows") or []:
            if not isinstance(r, dict) or not self.moves_price(r, None):
                continue
            ex = str(r.get("ex_date"))
            key = f"{account}:{r.get('symbol')}:{ex}"
            if key in done or ex > today or (ex == today and now.time() < NORMALISE_FROM):
                continue
            try:
                changed = broker.rescale_position(r["symbol"], ratio=r.get("ratio"),
                                                  dividend=None if r.get("ratio") else r.get("dividend"), before=ex)
            except Exception as e:  # noqa: BLE001 - tried again next tick
                log.warning("practice normalise %s failed: %s", key, e)
                continue
            done[key] = now.isoformat(timespec="seconds")
            if changed:
                out.append({"symbol": r["symbol"], "ex_date": ex, "label": r["label"], **changed})
        if out or done != (self.load().get("normalised") or {}):
            data["normalised"] = dict(list(done.items())[-200:])
            self._save(data)
        return out


def paper_accounts(state_dir: Path) -> list[Path]:
    """The practice account and the forward-test accounts on disk."""
    sd = Path(state_dir)
    return [p for p in [sd / "paper_broker.json", *sorted((sd / "forward").glob("*_broker.json"))] if p.exists()]


def normalise_all(settings: Any, now: datetime | None = None, book: ActionBook | None = None) -> list[dict[str, Any]]:
    """Run the normaliser over every paper account on disk (practice and forward test). Never raises."""
    from .broker import LocalPaperBroker
    book = book or book_for(settings)
    n = (now or book.now_fn()).astimezone(IST)
    data = book.load()
    done = data.get("normalised") or {}
    today = n.date().isoformat()
    due = [r for r in data.get("rows") or [] if isinstance(r, dict) and book.moves_price(r, None)
           and (str(r.get("ex_date")) < today or (str(r.get("ex_date")) == today and n.time() >= NORMALISE_FROM))]
    out: list[dict[str, Any]] = []
    accounts = paper_accounts(Path(settings.state_dir))
    if not any(f"{a.stem}:{r['symbol']}:{r['ex_date']}" not in done for r in due for a in accounts):
        return out   # nothing to do: no account file is opened
    for path in accounts:
        try:
            b = LocalPaperBroker(path, whole_shares=True, shared=True)
            out += [{**x, "account": path.stem} for x in book.normalise_practice(b, now, account=path.stem)]
        except Exception as e:  # noqa: BLE001
            log.warning("corporate-action normaliser on %s failed: %s", path.name, e)
    return out


def book_for(settings: Any, calendar: Any | None = None) -> ActionBook:
    if calendar is None:
        from .groww_hours import default_calendar
        calendar = default_calendar(settings)
    return ActionBook(Path(settings.state_dir), calendar=calendar)
