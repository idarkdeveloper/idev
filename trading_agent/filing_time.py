"""When a filing becomes usable: the one rule for announcements, results (XBRL) and SEBI PIT disclosures.

Anything disseminated at or after 15:00 IST on day T is usable from the next trading day; anything before 15:00
is usable on T itself (if T is a trading day). A filing stamped on a weekend or an exchange holiday is usable from
the next trading day. A bare date with no time of day is treated as disseminated after the close, because the
time is unknown and the safe answer is the later one.

Every place a filing becomes visible to a decision (point-in-time fundamentals, Replay's announcement view, the
insider backtest) goes through ``usable_from`` so there is a single cut-off.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from .timezones import IST

CUTOFF = time(15, 0)
_MONTHS = {m: i for i, m in enumerate(("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), 1)}


def parse_ts(value: Any) -> datetime | date | None:
    """A datetime (naive means IST; aware is converted to IST), a ``date`` for a bare date, or None if unreadable.
    Accepts ISO strings ('2026-10-08T18:43:50', '2026-10-08 18:43:50+05:30') and NSE's '08-Oct-2026 18:43:50'."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.astimezone(IST) if value.tzinfo else value
    if isinstance(value, date):
        return value
    s = str(value).strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{1,2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?)?\s*(Z|[+-]\d{2}:?\d{2})?$", s)
    if m:
        y, mo, d = int(m[1]), int(m[2]), int(m[3])
        if m[4] is None:
            return date(y, mo, d)
        dt = datetime(y, mo, d, int(m[4]), int(m[5]), int(m[6] or 0))
        if m[7]:
            tz = m[7]
            if tz == "Z":
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                sign = -1 if tz[0] == "-" else 1
                digits = tz[1:].replace(":", "")
                dt = dt.replace(tzinfo=timezone(sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))))
            return dt.astimezone(IST)
        return dt
    m = re.match(r"^(\d{1,2})-([A-Za-z]{3})[A-Za-z]*-(\d{4})(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?$", s)
    if m and m[2].upper() in _MONTHS:
        y, mo, d = int(m[3]), _MONTHS[m[2].upper()], int(m[1])
        if m[4] is None:
            return date(y, mo, d)
        return datetime(y, mo, d, int(m[4]), int(m[5]), int(m[6] or 0))
    return None


def next_trading_day(day: date, holidays: Any | None = None) -> date:
    """The first trading day strictly after ``day`` (weekends and, when a calendar is given, holidays skipped)."""
    d = day + timedelta(days=1)
    while not _is_trading(d, holidays):
        d += timedelta(days=1)
    return d


def previous_trading_day(day: date, holidays: Any | None = None) -> date:
    """The last trading day strictly before ``day``."""
    d = day - timedelta(days=1)
    while not _is_trading(d, holidays):
        d -= timedelta(days=1)
    return d


def count_sessions(first: date, last: date, holidays: Any | None = None) -> int:
    """Trading days in [first, last], both ends included (0 when last is before first)."""
    n, d = 0, first
    while d <= last:
        n += _is_trading(d, holidays)
        d += timedelta(days=1)
    return n


def _is_trading(day: date, holidays: Any | None) -> bool:
    if holidays is not None and hasattr(holidays, "is_trading_day"):
        return bool(holidays.is_trading_day(day))
    return day.weekday() < 5


def usable_from(ts_ist: Any, holidays: Any | None = None) -> date:
    """The first date a filing disseminated at ``ts_ist`` may be acted on. See the module docstring.
    Raises ValueError for a timestamp that cannot be read (callers decide whether to skip the filing)."""
    t = parse_ts(ts_ist)
    if t is None:
        raise ValueError(f"unreadable filing time: {ts_ist!r}")
    if isinstance(t, datetime):
        day, before_cutoff = t.date(), t.time() < CUTOFF
    else:
        day, before_cutoff = t, False   # no time of day: assume after the close
    if before_cutoff and _is_trading(day, holidays):
        return day
    return next_trading_day(day, holidays)


def usable_by(ts_ist: Any, day: Any, holidays: Any | None = None) -> bool:
    """True if a filing stamped ``ts_ist`` may be used on ``day`` (a date or ISO date string). An unreadable stamp is
    not usable."""
    try:
        u = usable_from(ts_ist, holidays)
    except ValueError:
        return False
    d = day if isinstance(day, date) and not isinstance(day, datetime) else (
        day.date() if isinstance(day, datetime) else date.fromisoformat(str(day)[:10]))
    return u <= d
