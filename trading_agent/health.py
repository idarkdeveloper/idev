"""Data health: every data source the agent reads, with when it last worked and whether it is on time.

GET /api/health (the Live page's Health tab) is built here from what the app already records (state files, the NSE
breaker file, the integration check, the watch service's heartbeat file, the Groww snapshot and cool-down files, the
Groww login log) plus ``source_runs.json``: a small "last run" record that the deals, BSE, announcement and news fetches
write (``note_run`` / ``recorded``), since they kept nothing on disk before.

Per source: what it feeds, the last success and its age, rows in the last fetch, how late it is against its schedule,
the back-off state, the last error (redacted of anything that looks like a secret) and a status: ok, late, failing or
not configured. Weekends and NSE holidays are never late: every schedule is measured from the latest trading day.
Read-only: nothing here calls a data source, Groww or anything else on the network.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Callable, TypeVar

from .timezones import IST

log = logging.getLogger(__name__)

RUNS_FILE = "source_runs.json"
_LOCK = threading.Lock()
T = TypeVar("T")

# what each source feeds (shown on the page)
AGENT, ALERTS, MORNING, EVENING, SCREEN, DASH = "agent", "alerts", "morning email", "evening email", "screen", "dashboard"
STATUS_ORDER = {"failing": 0, "late": 1, "ok": 2, "off": 3}
STATUS_TEXT = {"failing": "failing", "late": "late", "ok": "ok", "off": "not configured"}

WINDOW_OPEN, WINDOW_CLOSE = dtime(9, 15), dtime(18, 30)   # the watch service polls deals, news and announcements
INTRADAY_GAP = timedelta(hours=1)                          # inside that window a success older than this is late


# -- the "last run" record ---------------------------------------------------------------------------
def _dict(x: Any) -> dict[str, Any]:
    return x if isinstance(x, dict) else {}


def runs_path(state_dir: Any) -> Path:
    return Path(state_dir) / RUNS_FILE


def read_runs(state_dir: Any) -> dict[str, Any]:
    try:
        data = json.loads(runs_path(state_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def note_run(state_dir: Any, source: str, *, ok: bool, rows: int | None = None, error: str | None = None,
             now: datetime | None = None) -> None:
    """Record one fetch of ``source``: a success with its row count, or a failure with its (redacted) error.
    Never raises: the record must not cost the fetch."""
    if state_dir is None:
        return
    at = (now or datetime.now(IST)).astimezone(IST).isoformat(timespec="seconds")
    try:
        from .state import atomic_write
        with _LOCK:
            runs = read_runs(state_dir)
            r = dict(_dict(runs.get(source)))
            r["last_try"] = at
            if ok:
                r.update(last_ok=at, rows=rows, ok=True)
            else:
                r.update(ok=False, error=redact(error or "failed"), error_at=at)
            runs[source] = r
            atomic_write(runs_path(state_dir), json.dumps(runs, indent=1, sort_keys=True))
    except Exception:  # noqa: BLE001
        log.debug("could not record the %s run", source, exc_info=True)


def deals_source(settings: Any) -> str:
    """The run-record name of the followed-deals fetch: "insider" for NSE insider filings, else "deals"."""
    return "insider" if str(getattr(settings, "watch_source", "") or "") == "insider" else "deals"


def recorded(state_dir: Any, source: str, fn: Callable[[], T]) -> T:
    """Run ``fn`` (a fetch) and record its outcome: rows = len(result) when it has one. Exceptions pass through."""
    try:
        out = fn()
    except Exception as e:
        note_run(state_dir, source, ok=False, error=f"{type(e).__name__}: {e}")
        raise
    try:
        rows = len(out)   # type: ignore[arg-type]
    except TypeError:
        rows = None
    note_run(state_dir, source, ok=True, rows=rows)
    return out


# -- redaction ----------------------------------------------------------------------------------------
_REDACTIONS: list[tuple[re.Pattern[str], Any]] = [
    (re.compile(r"\b(https?://[^\s/'\"<>]+)[^\s'\"<>]*"), lambda m: m.group(1) + "/…"),   # URL paths and queries can hold keys
    (re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}"), "[redacted]"),                            # a Telegram bot token
    (re.compile(r"(?i)\bbearer\s+\S+"), "Bearer [redacted]"),
    (re.compile(r"(?i)\b(token|secret|password|passwd|api[_-]?key|apikey|key|authorization|checksum|totp|chat[_-]?id)"
                r"(\s*[=:]\s*|\s+)['\"]?[^\s,;'\"]+"), lambda m: m.group(1) + "=[redacted]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9._-]+"), "[redacted]"),                # a JWT
    (re.compile(r"[A-Za-z0-9_+/=-]{32,}"), "[redacted]"),                                 # any long opaque string
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[email]"),
]


def redact(text: Any, limit: int = 240) -> str:
    """The error text with anything secret-looking replaced: URL paths and queries, bearer tokens, key=value secrets,
    Telegram bot tokens, JWTs, long opaque strings and e-mail addresses. Cut to ``limit`` characters."""
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    for pat, rep in _REDACTIONS:
        s = pat.sub(rep, s)
    return s if len(s) <= limit else s[:limit - 1].rstrip() + "…"


# -- schedules ------------------------------------------------------------------------------------------
def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=IST)).astimezone(IST)


def _trading(day: date, cal: Any) -> bool:
    from .safety import is_trading_day
    return is_trading_day(day, cal)


def latest_at(now: datetime, t: dtime, cal: Any) -> datetime:
    """The latest moment ``t`` (IST) on a trading day that is at or before ``now``."""
    n = now.astimezone(IST)
    day = n.date()
    for _ in range(20):
        at = datetime.combine(day, t, tzinfo=IST)
        if _trading(day, cal) and at <= n:
            return at
        day -= timedelta(days=1)
    return datetime.combine(day, t, tzinfo=IST)


def intraday_due(now: datetime, cal: Any) -> datetime:
    """A source the watch service polls during the day (deals, news, announcements): it must have worked since the
    latest trading day's 09:15, and inside 09:15 to 18:30 on a trading day within the last hour."""
    due = latest_at(now, WINDOW_OPEN, cal)
    n = now.astimezone(IST)
    if _trading(n.date(), cal) and WINDOW_OPEN <= n.time() <= WINDOW_CLOSE:
        due = max(due, n - INTRADAY_GAP)
    return due


def _ago(dt: datetime | None, now: datetime) -> str | None:
    if dt is None:
        return None
    s = max(0, int((now - dt).total_seconds()))
    return (f"{s // 60} min" if s < 5400 else f"{s // 3600} h" if s < 2 * 86400 else f"{s // 86400} days") + " ago"


def _day_text(d: date | datetime) -> str:
    return f"{d.day} {d:%b}" if isinstance(d, date) and not isinstance(d, datetime) else f"{d.day} {d:%b %H:%M}"


# -- one row per source ---------------------------------------------------------------------------------
def _row(sid: str, name: str, feeds: list[str], *, configured: bool = True, last_ok: datetime | None = None,
         rows: int | None = None, due: datetime | None = None, due_text: str = "", schedule: str = "",
         error: str | None = None, error_at: datetime | None = None, failing: bool = False, backoff: str | None = None,
         detail: str = "", late: bool | None = None, now: datetime) -> dict[str, Any]:
    """One source. Failing: told so, or its last error is newer than its last success. Late: ``late`` when given,
    else no success at or after ``due``. Not configured wins over both."""
    if late is None:
        late = due is not None and (last_ok is None or last_ok < due)
    if not configured:
        status = "off"
    elif failing or (error and error_at and (last_ok is None or error_at > last_ok)):
        status = "failing"
    elif late:
        status = "late"
    else:
        status = "ok"
    delay = None
    if status == "late":
        delay = due_text or (f"expected since {_day_text(due)}" if due is not None else "behind its schedule")
    return {"id": sid, "name": name, "feeds": feeds, "status": status, "status_text": STATUS_TEXT[status],
            "last_success": last_ok.isoformat(timespec="seconds") if last_ok else None, "age": _ago(last_ok, now),
            "rows": rows, "schedule": schedule, "delay": delay, "backoff": backoff,
            "error": redact(error) if error and status != "off" else None, "detail": detail}


def _run(runs: dict[str, Any], sid: str) -> tuple[datetime | None, int | None, str | None, datetime | None]:
    r = _dict(runs.get(sid))
    rows = r.get("rows")
    return (_parse(r.get("last_ok")), rows if isinstance(rows, int) else None, r.get("error") if r.get("ok") is False else None,
            _parse(r.get("error_at")))


def _newest_mtime(paths: list[Path]) -> datetime | None:
    best = None
    for p in paths:
        try:
            m = p.stat().st_mtime
        except OSError:
            continue
        best = m if best is None or m > best else best
    return datetime.fromtimestamp(best, IST) if best is not None else None


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _nse_backoff(state_dir: Path) -> str | None:
    from .circuit import read_degraded
    b = read_degraded(state_dir / "nse_breaker.json")
    if not b:
        return None
    try:
        until = datetime.fromtimestamp(float(str(b.get("retry_at"))), IST).strftime("%H:%M IST")
    except (TypeError, ValueError, OverflowError, OSError):
        until = "shortly"
    return f"NSE refused or did not answer several requests; paused until {until}"


def _daily_store(path: Path, sid: str, name: str, feeds: list[str], now: datetime, cal: Any, cutoff: dtime,
                 schedule: str, rows_of: Callable[[dict[str, Any]], int | None], backoff: str | None) -> dict[str, Any]:
    """flows.json / breadth.json: {"days": {date: row}, "attempts": {key: {tries, done, last, outcome}}}. Late when the
    latest trading day whose ``cutoff`` has passed has no figures yet. An attempt's outcome "still <date>" (NSE has not
    published yet) is not an error."""
    data = _dict(_read_json(path))
    days = _dict(data.get("days"))
    attempts = [a for a in _dict(data.get("attempts")).values() if isinstance(a, dict)]
    have = None
    for k in sorted(days, reverse=True):
        try:
            have = date.fromisoformat(k)
            break
        except ValueError:
            continue
    oks = [t for t in (_parse(a.get("last")) for a in attempts if a.get("outcome") == "ok") if t is not None]
    last_ok = max(oks) if oks else (datetime.combine(have, dtime(19, 0), tzinfo=IST) if have else None)
    errs = [(t, str(a.get("outcome"))) for a in attempts
            for t in [_parse(a.get("last"))]
            if t is not None and a.get("outcome") not in (None, "ok") and not str(a.get("outcome")).startswith("still")]
    err_at, err = max(errs) if errs else (None, None)
    need = latest_at(now, cutoff, cal).date()
    latest = have.isoformat() if have else None
    rows = rows_of(days[latest]) if latest and isinstance(days.get(latest), dict) else None
    return _row(sid, name, feeds, last_ok=last_ok, rows=rows, schedule=schedule, error=err, error_at=err_at,
                failing=bool(backoff), backoff=backoff, now=now, late=have is None or have < need,
                due_text=f"no figures for {_day_text(need)} yet (expected by {cutoff:%H:%M} that day)",
                detail=f"latest day stored: {_day_text(have)}" if have else "nothing stored yet")


# -- the report -----------------------------------------------------------------------------------------
def report(settings: Any, now: datetime | None = None, holidays: Any | None = None) -> dict[str, Any]:
    """Every data source with its status, failing first, plus the Groww login counter. No network."""
    from .groww_hours import default_calendar
    now = (now or datetime.now(IST)).astimezone(IST)
    cal = holidays if holidays is not None else default_calendar(settings)
    sd = Path(settings.state_dir)
    cache = sd / "cache"
    runs = read_runs(sd)
    nse = str(getattr(settings, "data_source", "nse") or "nse") == "nse"
    insider = str(getattr(settings, "watch_source", "") or "") == "insider"
    nse_off = _nse_backoff(sd) if nse else None
    rows: list[dict[str, Any]] = []
    due_intra = intraday_due(now, cal)
    intra = "every minute 09:15–18:30 IST on trading days (watch service)"

    # deals
    ok, n, err, err_at = _run(runs, "deals")
    rows.append(_row("deals", "NSE bulk and block deals" if nse else "Disclosed deals (QuiverQuant)",
                     [AGENT, ALERTS, MORNING, EVENING, DASH], configured=not insider, last_ok=ok, rows=n, due=due_intra,
                     schedule=intra, error=err, error_at=err_at, failing=bool(nse_off), backoff=nse_off, now=now))
    ok, n, err, err_at = _run(runs, "bse")
    if ok is None and (cache / "bse_deals").exists():
        ok = _newest_mtime(list((cache / "bse_deals").glob("*.json")))
    rows.append(_row("bse", "BSE bulk and block deals", [AGENT, ALERTS, MORNING, EVENING, DASH],
                     configured=bool(getattr(settings, "bse_deals", False)) and nse and not insider, last_ok=ok, rows=n,
                     due=due_intra, schedule=intra + "; today's deals after 16:00", error=err, error_at=err_at, now=now,
                     detail="paused 15 min after a failure"))
    ok, n, err, err_at = _run(runs, "insider")
    rows.append(_row("insider", "Insider filings (NSE PIT)", [AGENT, ALERTS, MORNING, EVENING], configured=insider and nse,
                     last_ok=ok or _newest_mtime(list((cache / "nse_pit").glob("*.json"))), rows=n, due=due_intra,
                     schedule=intra, error=err, error_at=err_at, failing=bool(nse_off), backoff=nse_off, now=now))
    ok, n, err, err_at = _run(runs, "announcements")
    rows.append(_row("announcements", "NSE announcements", [AGENT, ALERTS, MORNING, DASH], configured=nse, last_ok=ok,
                     rows=n, due=due_intra, schedule=intra + " for held and recommended stocks", error=err,
                     error_at=err_at, failing=bool(nse_off), backoff=nse_off, now=now))

    # prices
    ycache = list(cache.glob("yahoo_*.json")) if cache.exists() else []
    yok = _newest_mtime(ycache)
    fresh = sum(1 for p in ycache if _safe_mtime(p) is not None and now.timestamp() - (_safe_mtime(p) or 0) < 86400)
    ydue = latest_at(now, dtime(12, 0), cal)
    rows.append(_row("yahoo", "Yahoo prices", [AGENT, SCREEN, MORNING, EVENING, DASH], last_ok=yok, rows=fresh,
                     late=yok is None or yok.date() < ydue.date(), due_text=f"no price refreshed on {_day_text(ydue.date())}", now=now,
                     schedule="on demand; each price is kept up to 6 h", detail="rows: price files refreshed in the last 24 h"))

    # market data the watch service stores in the evening
    rows.append(_daily_store(sd / "flows.json", "flows", "FII/DII flows", [MORNING], now, cal, dtime(21, 0),
                             "from 19:00 IST on trading days, up to 6 tries 20 min apart; catch-up before 09:00",
                             lambda d: 1, nse_off))
    rows.append(_daily_store(sd / "breadth.json", "breadth", "NIFTY 500 breadth", [MORNING], now, cal, dtime(21, 0),
                             "from 19:00 IST on trading days, up to 8 tries 20 min apart",
                             lambda d: d.get("n") if isinstance(d.get("n"), int) else None, nse_off))
    rows.append(_bands(sd, now, cal, nse_off))
    rows.append(_filters_row(settings, sd, now, cal, nse_off))
    rows.append(_corp_row(settings, sd, now, cal, runs, nse_off))
    rows.append(_clearing_row(settings, sd, now, runs, nse_off))

    # news
    ok, n, err, err_at = _run(runs, "news")
    if ok is None and (cache / "news").exists():
        ok = _newest_mtime(list((cache / "news").glob("*.xml")))
    rows.append(_row("news", "News (RSS feeds)", [ALERTS, AGENT, MORNING, EVENING, DASH], last_ok=ok, rows=n,
                     due=due_intra, schedule=intra + " for held and recommended stocks; feeds kept 30 min", error=err,
                     error_at=err_at, now=now))

    # Groww, the canary, the watch service
    groww = groww_panel(settings, now)
    rows.append(_groww_row(settings, sd, now, cal, groww))
    rows.append(_canary_row(settings, sd, now, cal))
    rows.append(_watch_row(settings, sd, now, cal))
    rows.append(_telegram_commands_row(settings, sd, now))

    rows.sort(key=lambda r: (STATUS_ORDER[r["status"]], r["name"]))
    counts = {k: sum(1 for r in rows if r["status"] == k) for k in STATUS_ORDER}
    from . import killswitch
    halt = killswitch.status(sd)
    return {"at": now.isoformat(timespec="seconds"), "sources": rows, "counts": counts, "groww": groww,
            "halt": None if halt is None else {**halt, "text": killswitch.reason(sd)},
            "note": "Read from what the agent records on this machine; nothing here asks a data source again."}


def _safe_mtime(p: Path) -> float | None:
    try:
        return p.stat().st_mtime
    except OSError:
        return None


def _bands(sd: Path, now: datetime, cal: Any, nse_off: str | None) -> dict[str, Any]:
    data = _read_json(sd / "price_bands.json") or {}
    bands = data.get("bands") if isinstance(data.get("bands"), dict) else {}
    try:
        fetched = date.fromisoformat(str(data.get("date"))) if data.get("date") else None
    except ValueError:
        fetched = None
    ok = _newest_mtime([sd / "price_bands.json"]) if fetched else None
    due = latest_at(now, dtime(10, 0), cal)
    tries = (_read_json(sd / "price_bands_tries.json") or {}).get(now.date().isoformat()) or {}
    from .bands import MAX_TRIES
    spent = isinstance(tries, dict) and int(tries.get("tries") or 0) >= MAX_TRIES and (fetched is None or fetched < now.date())
    return _row("bands", "NSE price bands", [AGENT, SCREEN, MORNING, DASH], last_ok=ok, rows=len(bands) if bands else None,
                late=fetched is None or fetched < due.date(), due_text=f"no list for {_day_text(due.date())} yet (expected by 10:00)", failing=spent or bool(nse_off),
                backoff=nse_off or ("all of today's tries used; the next try is tomorrow" if spent else None),
                error="every try today failed" if spent else None, error_at=now if spent else None, now=now,
                schedule="once each trading day from the open; retries 5, 15 then 60 min apart, 6 a day",
                detail=f"list of {_day_text(fetched)}" if fetched else "no list stored yet")


def _telegram_commands_row(settings: Any, sd: Path, now: datetime) -> dict[str, Any]:
    """telegram_commands.json: the command poller's last getUpdates round and last error (written by the watch)."""
    from .telegram_bot import commands_configured
    data = _dict(_read_json(sd / "telegram_commands.json"))
    ok = _parse(data.get("last_poll_at"))
    err_at = _parse(data.get("last_error_at"))
    error = data.get("last_error") if err_at and (ok is None or err_at > ok) else None
    on = commands_configured(settings)
    return _row("telegram_commands", "Telegram commands (practice)", [ALERTS], configured=on, last_ok=ok,
                rows=data.get("handled") if isinstance(data.get("handled"), int) else None,
                late=on and (ok is None or (now - ok).total_seconds() > 300),
                due_text="no getUpdates round in the last 5 minutes (is the watch service running?)",
                error=error, error_at=err_at if error else None, failing=bool(error),
                schedule="long-poll every 25 s by the watch service while TELEGRAM_COMMANDS=true",
                detail="rows: updates handled since the start; only TELEGRAM_CHAT_ID is obeyed; orders are practice only",
                now=now)


def _clearing_row(settings: Any, sd: Path, now: datetime, runs: dict[str, Any], nse_off: str | None) -> dict[str, Any]:
    """cache/nse_clearing_holidays.json: the year's clearing holidays (T-1 sale proceeds are not counted on them)."""
    data = _dict(_read_json(sd / "cache" / "nse_clearing_holidays.json"))
    ok, _n, err, err_at = _run(runs, "clearing")
    have = data.get("year") == now.year and isinstance(data.get("days"), dict)
    return _row("clearing", "NSE clearing holidays", [AGENT, DASH],
                configured=str(getattr(settings, "market", "in") or "in") == "in",
                last_ok=ok or _parse(data.get("fetched_at")), rows=len(data["days"]) if have else None, late=not have,
                due_text=f"no list for {now.year} yet", error=err, error_at=err_at, failing=bool(nse_off), backoff=nse_off,
                schedule="once a year through the NSE client (again after 30 days); retried hourly after a failure",
                detail="until the list is known every day counts as a clearing holiday: the previous day's sale "
                       "proceeds are not used for buys", now=now)


def _corp_row(settings: Any, sd: Path, now: datetime, cal: Any, runs: dict[str, Any], nse_off: str | None) -> dict[str, Any]:
    """corporate_actions.json: NSE's corporate actions, 30 days ahead, once each trading day."""
    data = _dict(_read_json(sd / "corporate_actions.json"))
    india = str(getattr(settings, "market", "in") or "in") == "in"
    ok, _n, err, err_at = _run(runs, "corp_actions")
    due = latest_at(now, dtime(9, 0), cal).date()
    try:
        listed = date.fromisoformat(str(data.get("date")))
    except ValueError:
        listed = None
    rows = data.get("rows")
    return _row("corp_actions", "Corporate actions (NSE): splits, bonuses, special dividends", [AGENT, MORNING, DASH],
                configured=india, last_ok=ok or _parse(data.get("fetched_at")),
                rows=len(rows) if isinstance(rows, list) else None, late=listed is None or listed < due,
                due_text=f"no list for {_day_text(due)} yet (expected by 09:00)", error=err, error_at=err_at,
                failing=bool(nse_off), backoff=nse_off,
                schedule="once each trading day, 30 days ahead, through the NSE client; retries 5, 15 then 60 min apart",
                detail="missing or older than 2 trading days: holdings are flagged \"corporate actions unknown\" and GTT "
                       "stops are left as they are (never cancelled blindly)", now=now)


def _filters_row(settings: Any, sd: Path, now: datetime, cal: Any, nse_off: str | None) -> dict[str, Any]:
    """indian_filters.json: the ASM, GSM, board-meeting and pledge lists, each with its own date."""
    from .indian_filters import LABEL, PARTS, SWITCH
    data = _dict(_read_json(sd / "indian_filters.json"))
    india = str(getattr(settings, "market", "in") or "in") == "in"
    on = [p for p in PARTS if india and bool(getattr(settings, SWITCH[p], True))]
    due = latest_at(now, dtime(10, 0), cal).date()
    stamps, rows, missing = [], 0, []
    for p in on:
        blk = _dict(data.get(p))
        t = _parse(blk.get("fetched_at"))
        try:
            fresh = date.fromisoformat(str(blk.get("date"))) >= due
        except ValueError:
            fresh = False
        if t:
            stamps.append(t)
        rows += len(_dict(blk.get("symbols")))
        if not fresh:
            missing.append(LABEL[p])
    tries = _dict(_dict(data.get("tries")).get(now.date().isoformat()))
    errors = _dict(tries.get("errors"))
    last_err = "; ".join(f"{LABEL.get(k, k)}: {v}" for k, v in errors.items()) or None
    return _row("filters", "ASM/GSM, board meetings, pledges (NSE)", [AGENT, SCREEN, MORNING, DASH], configured=bool(on),
                last_ok=min(stamps) if stamps else None, rows=rows or None, late=bool(missing),
                due_text="not fetched for " + _day_text(due) + ": " + ", ".join(missing) if missing else "",
                error=last_err, error_at=_parse(tries.get("last")) if last_err else None, failing=bool(nse_off), backoff=nse_off,
                schedule="once each trading day from the open, through the NSE client; retries 5, 15 then 60 min apart",
                detail="a missing or stale list blocks nothing (buys are then held back only by the other rules)", now=now)


def _groww_row(settings: Any, sd: Path, now: datetime, cal: Any, panel: dict[str, Any]) -> dict[str, Any]:
    from .runner import SNAPSHOT_FILE
    snap = _read_json(sd / SNAPSHOT_FILE) or {}
    saved = _parse(snap.get("saved_at")) if isinstance(snap, dict) else None
    holdings = snap.get("holdings") if isinstance(snap, dict) and isinstance(snap.get("holdings"), list) else None
    cool = panel.get("cooldown")
    logins = panel.get("logins") or {}
    stopped = logins.get("level") == "stopped"
    due = latest_at(now, dtime(16, 0), cal)
    session_ok = False
    if isinstance(snap, dict) and snap.get("kind") == "close":
        try:
            session_ok = date.fromisoformat(str(snap.get("session"))) >= due.date()
        except ValueError:
            session_ok = False
    session_ok = session_ok or (saved is not None and saved >= datetime.combine(due.date(), dtime(15, 30), tzinfo=IST))
    backoff = None
    if cool:
        backoff = f"token requests paused until {_parse(cool['until']):%H:%M IST}" if _parse(cool.get("until")) else "token requests paused"
    if stopped:
        backoff = f"{logins.get('stop_at')} token requests sent today: no more until 06:00 IST"
    return _row("groww", "Groww holdings (read-only)", [DASH, MORNING, EVENING, AGENT],
                configured=bool(getattr(settings, "has_groww_credentials", False)), last_ok=saved,
                rows=len(holdings) if holdings is not None else None, late=not session_ok, due_text=f"no holdings saved for the {_day_text(due.date())} close yet", now=now,
                failing=bool(cool) or stopped, backoff=backoff, error=(cool or {}).get("reason") if cool else None,
                error_at=now if cool else None,
                schedule="read 08:30–16:00 IST on trading days; one saved read at the close (15:35–15:50)",
                detail="from the saved holdings file; this page never logs in to Groww")


def _canary_row(settings: Any, sd: Path, now: datetime, cal: Any) -> dict[str, Any]:
    from .integration import counts, load_result
    res = load_result(sd)
    at = _parse((res or {}).get("at"))
    due = latest_at(now, dtime(9, 10), cal)
    failed = [s.get("name") for s in (res or {}).get("steps", []) if isinstance(s, dict) and not s.get("ok")]
    passed, ran = counts(res) if res else (0, 0)
    flag = _read_json(sd / "canary_failed.json")
    return _row("canary", "Pre-market check (canary)", [AGENT, MORNING, DASH],
                configured=bool(getattr(settings, "integration_check", True)), last_ok=at if res and res.get("ok") else None,
                rows=ran or None, late=at is None or at.date() < due.date(), due_text=f"no check on {_day_text(due.date())} (expected by 09:10)", now=now,
                error=("failed: " + ", ".join(str(x) for x in failed)) if res and not res.get("ok") else None,
                error_at=at if res and not res.get("ok") else None,
                backoff="new buys paused until a check passes" if flag else None,
                schedule="each trading day from 08:30 IST; due by 09:10",
                detail=f"{passed}/{ran} live checks passed" if res else "not run yet")


def _watch_row(settings: Any, sd: Path, now: datetime, cal: Any) -> dict[str, Any]:
    from .safety import market_open, watch_info
    w = watch_info(sd, now, market_open(now, cal))
    at = _parse(w.get("at"))
    level = w.get("level")
    hb = bool(getattr(settings, "heartbeat_url", None))
    return _row("watch", "Watch service and heartbeat", [AGENT, ALERTS, MORNING, EVENING], configured=bool(w.get("seen")),
                last_ok=at, rows=None, now=now, failing=level == "bad",
                late=level == "warn", due_text="the watch service has not ticked on time", error=w.get("last_error"), error_at=at if w.get("last_error") else None,
                schedule="ticks every minute; the heartbeat file is written every 30 s",
                detail=("heartbeat pings are on" if hb else "no HEARTBEAT_URL: nothing outside watches it")
                + ("" if w.get("seen") else "; the watch service has not run on this machine"))


# -- Groww login counter and token state for the page ---------------------------------------------------------
def groww_panel(settings: Any, now: datetime) -> dict[str, Any]:
    """The login counter (groww_logins.summary), the cached token's age and expiry (never the token), and the cool-down.
    Reads files only: never logs in to Groww."""
    from . import groww_logins
    sd = Path(settings.state_dir)
    out: dict[str, Any] = {"configured": bool(getattr(settings, "has_groww_credentials", False)),
                           "logins": groww_logins.summary(groww_logins.log_path(sd), now),
                           "note": ("Counts only this agent's token requests on this machine (the server and a laptop each keep "
                                    "their own log). 150 a day is the Groww Trade API token limit; signing in to the Groww app "
                                    "or website does not use it. Other API scripts using the same API key would also count, and "
                                    "those are not seen here."),
                           "token": None, "cooldown": None}
    tok = _read_json(sd / "groww_token.json")
    if isinstance(tok, dict):
        created, expires = _parse(tok.get("created_at")), _parse(tok.get("expires_at"))
        out["token"] = {"created_at": created.isoformat(timespec="seconds") if created else None,
                        "expires_at": expires.isoformat(timespec="seconds") if expires else None,
                        "age": _ago(created, now), "valid": bool(expires and expires > now)}
    blk = _read_json(sd / "groww_token.block")
    until = _parse(blk.get("until")) if isinstance(blk, dict) else None
    if isinstance(blk, dict) and until and until > now:
        out["cooldown"] = {"until": until.isoformat(timespec="seconds"), "status": blk.get("status"),
                           "reason": redact(blk.get("reason") or "")}
    return out
