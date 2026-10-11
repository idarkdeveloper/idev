"""When the agent may call Groww: mainly while the market is live, with one saved read at the close.

The rule: Groww is called inside a window (GROWW_WINDOW_START to GROWW_WINDOW_END, default 08:30 to 16:00 IST) on NSE
trading days. The watch service takes ONE holdings read after the close (15:35 to 15:50) and saves it with
``"kind": "close"``; outside the window the dashboard and the emails use that saved copy, priced from Yahoo. Pure
functions over settings and a clock (no network, no orders), so a fake clock drives the tests.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, time as dtime
from pathlib import Path
from typing import Any, Callable

from .safety import last_close
from .timezones import IST

log = logging.getLogger(__name__)

CLOSE_FROM, CLOSE_UNTIL = dtime(15, 35), dtime(15, 50)   # the close snapshot is taken in this slice, IST
CLOSE_MAX_ATTEMPTS = 3
CLOSE_RETRY_GAP_S = 180
CLOSE_FILE = "groww_close.json"
OFFHOURS_FILE = "groww_offhours.json"


def now_ist() -> datetime:
    """The clock for every gate here (tests replace it)."""
    return datetime.now(IST)


class CachedCalendar:
    """NSE holidays from what is already on disk: never a network call. Weekdays count when nothing is known."""

    def __init__(self, holidays: Any):
        self._h = holidays

    def is_trading_day(self, day: date) -> bool:
        h = self._h
        peek = getattr(h, "peek_trading_day", None)
        if peek is not None:
            return bool(peek(day))
        return bool(h.is_trading_day(day)) if h is not None else day.weekday() < 5


def default_calendar(settings: Any) -> Any:
    """The cached holiday calendar for the settings' state dir (weekdays only until the list has been fetched once)."""
    from .holidays import NSEHolidays
    return CachedCalendar(NSEHolidays(cache_dir=Path(settings.state_dir) / "cache"))


def _hhmm(text: Any, default: dtime) -> dtime:
    try:
        return dtime.fromisoformat(str(text))
    except ValueError:
        return default


def window_bounds(settings: Any) -> tuple[dtime, dtime]:
    return (_hhmm(getattr(settings, "groww_window_start", "08:30"), dtime(8, 30)),
            _hhmm(getattr(settings, "groww_window_end", "16:00"), dtime(16, 0)))


def window_open(settings: Any, now: datetime | None = None, holidays: Any | None = None) -> bool:
    """True on an NSE trading day between the window's start and end (IST). ``holidays`` None reads the cached list."""
    from .safety import is_trading_day
    n = (now or now_ist()).astimezone(IST)
    cal = holidays if holidays is not None else default_calendar(settings)
    start, end = window_bounds(settings)
    return is_trading_day(n.date(), cal) and start <= n.time() <= end


def latest_session(now: datetime, holidays: Any | None = None) -> date:
    """The date of the latest completed session (its 15:30 IST close is at or before ``now``)."""
    return last_close(now, holidays).date()


def _parse(value: Any) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=IST)


def snapshot_covers(snap: dict[str, Any] | None, now: datetime, holidays: Any | None = None) -> bool:
    """Does this saved snapshot stand for the latest completed session? Yes when it is a close snapshot of that
    session (or a later one), or was saved after that session's close."""
    if not snap:
        return False
    session = latest_session(now, holidays)
    if snap.get("kind") == "close":
        try:
            if date.fromisoformat(str(snap.get("session"))) >= session:
                return True
        except ValueError:
            pass
    saved = _parse(snap.get("saved_at"))
    return saved is not None and saved >= last_close(now, holidays)


def closed_reason(now: datetime, holidays: Any | None = None) -> str:
    return f"market closed — holdings saved at the {latest_session(now, holidays):%d %b %Y} close"


# -- one fallback read a day outside the window -------------------------------------------------
def offhours_attempted(settings: Any, day: date) -> bool:
    try:
        data = json.loads((Path(settings.state_dir) / OFFHOURS_FILE).read_text(encoding="utf-8"))
        return str(data.get("date")) == day.isoformat()
    except (OSError, ValueError, AttributeError):
        return False


def record_offhours_attempt(settings: Any, day: date) -> None:
    from .state import atomic_write
    try:
        atomic_write(Path(settings.state_dir) / OFFHOURS_FILE, json.dumps({"date": day.isoformat()}))
    except OSError as e:
        log.warning("could not record the off-hours Groww read: %s", e)


# -- the close snapshot ---------------------------------------------------------------------------
class CloseSnapshot:
    """Takes ONE Groww holdings read in 15:35 to 15:50 IST on a trading day (inside the Groww window) and saves it as
    the close snapshot. Once per session: ``state/groww_close.json`` records the session, the attempts and whether it
    is done. A failed read is retried at least ``CLOSE_RETRY_GAP_S`` later while the slice lasts, at most
    ``CLOSE_MAX_ATTEMPTS`` times. ``read_fn(session_date)`` does the read-and-save and returns the portfolio dict."""

    def __init__(self, settings: Any, read_fn: Callable[[date], dict[str, Any]], holidays: Any | None = None):
        self.settings = settings
        self.read_fn = read_fn
        self.holidays = holidays

    def _path(self) -> Path:
        return Path(self.settings.state_dir) / CLOSE_FILE

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self._path().read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict[str, Any]) -> None:
        from .state import atomic_write
        try:
            atomic_write(self._path(), json.dumps(data))
        except OSError as e:
            log.warning("could not record the close snapshot attempt: %s", e)

    def tick(self, now: datetime | None = None) -> dict[str, Any] | None:
        now = (now or now_ist()).astimezone(IST)
        if not getattr(self.settings, "has_groww_credentials", False):
            return None
        cal = self.holidays if self.holidays is not None else default_calendar(self.settings)
        if not (CLOSE_FROM <= now.time() <= CLOSE_UNTIL) or not window_open(self.settings, now, cal):
            return None
        day = now.date()
        rec = self._load()
        if rec.get("session") != day.isoformat():
            rec = {"session": day.isoformat(), "attempts": 0, "done": False}
        if rec.get("done"):
            return {"session": rec["session"], "done": True}
        attempts = int(rec.get("attempts") or 0)
        if attempts >= CLOSE_MAX_ATTEMPTS:
            return {"session": rec["session"], "done": False, "gave_up": True}
        last = _parse(rec.get("last_attempt"))
        if last is not None and (now - last).total_seconds() < CLOSE_RETRY_GAP_S:
            return {"session": rec["session"], "done": False, "waiting": True}
        rec.update(attempts=attempts + 1, last_attempt=now.isoformat(timespec="seconds"))
        self._save(rec)   # counted before the call: a crash mid-read still uses up the attempt
        try:
            out = self.read_fn(day)
            ok = bool(out.get("linked")) and not out.get("error") and out.get("source") != "saved"
        except Exception as e:  # noqa: BLE001 - never stops the watch
            log.warning("close snapshot read failed: %s", e)
            ok = False
        if ok:
            rec["done"] = True
            self._save(rec)
        elif rec["attempts"] >= CLOSE_MAX_ATTEMPTS:
            log.warning("the Groww close snapshot for %s failed %d times; the next session's first read will fill it",
                        day, CLOSE_MAX_ATTEMPTS)
        return {"session": rec["session"], "done": ok, "attempts": rec["attempts"]}
