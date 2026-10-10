"""A read-only check of the real services the agent depends on, once per trading day.

The unit tests use fakes, so they cannot notice that NSE changed a CSV column or BSE moved a form. This module makes
one small real request per service and checks the answer has the shape the code reads. It runs

* from the watch service at 08:30 IST on trading days (``IntegrationScheduler``, claim-file guarded like the forward
  job, so a restart or a second process does not run it twice),
* on demand: ``python -m trading_agent integration-check``, and the Settings button on the dashboard.

Every step is independent and has its own timeout; one failing never hides another. The result is written to
``state/integration_check.json`` as ``{at, steps: [{name, ok, ms, detail}], ok}``. A failure sends ONE alert a day
through the notifier (email, webhook, Telegram); a clean run sends nothing.

It is also the pre-market canary. When a run fails, or the check did not run by 09:10 on a trading day, the state file
``canary_failed.json`` is set and automated BUYS are refused (``buy_block``: the agent's paper/live buys and any live Groww
buy) until a run passes; sells and stop exits are never blocked. The dashboard shows an amber banner meanwhile, and the
morning email carries a one-line result.

Safety: nothing here can place, change or cancel an order, or spend a Groww token.

* Groww is used only with a token that is already cached (or set in .env). A new token is never requested (the daily
  budget is 150), and nothing is attempted while the token cool-down file is active: the step reports
  "skipped (no cached token)".
* The Groww client is wrapped twice: ``ReadOnlySession`` raises on any HTTP method except GET, and ``ReadOnlyGroww``
  exposes only ``holdings`` and ``order_list``.
* The Claude step is off unless INTEGRATION_CLAUDE=true, and then makes one call with ``max_tokens=5``.
* Resend and Telegram are checked for configuration only (filled or empty); nothing is sent.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Callable

import requests

from .forward_schedule import DailyJobScheduler
from .state import atomic_write
from .timezones import IST

log = logging.getLogger(__name__)

RESULT_FILE = "integration_check.json"
RUN_AFTER = dtime(8, 30)        # IST; before the morning email (09:00) and the open (09:15)
OVERDUE_AFTER = dtime(9, 10)    # no run by now on a trading day: the canary counts as failed
FLAG_FILE = "canary_failed.json"
LATEST = dtime(12, 0)           # after this the day is skipped: the point is to know before the market is busy
STEP_TIMEOUT_S = 45.0
STALE_TRADING_DAYS = 2          # the dashboard line turns red when the last run is older than this

_UNSET: Any = object()


class OrderBlocked(PermissionError):
    """The integration check tried something that is not a plain read."""


class Skip(Exception):
    """A step that has nothing to check right now (off, or no credentials): reported as ok and skipped."""


# --------------------------------------------------------------------------- #
# The read-only Groww wrapper
# --------------------------------------------------------------------------- #
class ReadOnlySession:
    """A requests.Session that only ever sends GET. Any other method raises ``OrderBlocked`` before a byte is sent."""

    _PASS = {"headers", "proxies", "cookies", "close", "mount", "auth", "verify"}

    def __init__(self, inner: Any):
        self._inner = inner

    def request(self, method: str, url: str, **kw: Any) -> Any:
        if str(method).upper() != "GET":
            raise OrderBlocked(f"the integration check only reads; {str(method).upper()} {url} was refused")
        return self._inner.request("GET", url, **kw)

    def get(self, url: str, **kw: Any) -> Any:
        return self.request("GET", url, **kw)

    def _refuse(self, method: str) -> Any:
        raise OrderBlocked(f"the integration check only reads; {method} was refused")

    def post(self, url: str, **kw: Any) -> Any:
        return self._refuse("POST")

    def put(self, url: str, **kw: Any) -> Any:
        return self._refuse("PUT")

    def patch(self, url: str, **kw: Any) -> Any:
        return self._refuse("PATCH")

    def delete(self, url: str, **kw: Any) -> Any:
        return self._refuse("DELETE")

    def __getattr__(self, name: str) -> Any:
        if name in self._PASS:
            return getattr(self._inner, name)
        raise AttributeError(name)


class ReadOnlyGroww:
    """The two Groww reads the check needs and nothing else; any other attribute raises ``OrderBlocked``."""

    ALLOWED = ("holdings", "order_list", "available_cash")

    def __init__(self, broker: Any):
        self._broker = broker

    def __getattr__(self, name: str) -> Any:
        if name in self.ALLOWED:
            return getattr(self._broker, name)
        raise OrderBlocked(f"the integration check may not call Groww {name}()")


def cached_groww(settings: Any, *, session: Any | None = None) -> ReadOnlyGroww | None:
    """A read-only Groww client, or None when there is no token to reuse. NEVER requests a token: only
    GROWW_ACCESS_TOKEN, or a still-valid token in the cache, with no cool-down in force."""
    from .groww import GrowwBroker
    from .runner import groww_session, token_cache

    token = getattr(settings, "groww_access_token", None)
    if not token:
        key = getattr(settings, "groww_api_key", None)
        if not key:
            return None
        cache = token_cache(settings)
        if cache.active_block(key):
            return None
        token = cache.get(key)
        if not token:
            return None
    guarded: Any = ReadOnlySession(session or groww_session(settings))
    return ReadOnlyGroww(GrowwBroker(token, live_orders=False, exchange=settings.groww_exchange, session=guarded))


# --------------------------------------------------------------------------- #
# Steps
# --------------------------------------------------------------------------- #
def _clip(text: object, n: int = 300) -> str:
    from .notify import _scrub
    t = " ".join(_scrub(str(text)).split())
    return t if len(t) <= n else t[:n - 3] + "..."


def _run_step(name: str, fn: Callable[[], str], timeout: float) -> dict[str, Any]:
    """Run one step on its own daemon thread so a hung request cannot hold the others up."""
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["detail"] = fn()
        except Skip as e:
            box["skip"] = str(e)
        except Exception as e:  # noqa: BLE001 - whatever went wrong is the step's result
            box["error"] = f"{type(e).__name__}: {e}"

    t0 = time.monotonic()
    th = threading.Thread(target=target, name=f"integration-{name}", daemon=True)
    th.start()
    th.join(timeout)
    ms = int((time.monotonic() - t0) * 1000)
    if th.is_alive():
        return {"name": name, "ok": False, "ms": ms, "detail": f"timed out after {timeout:g} s"}
    if "skip" in box:
        return {"name": name, "ok": True, "ms": ms, "detail": _clip(box["skip"]), "skipped": True}
    if "error" in box:
        return {"name": name, "ok": False, "ms": ms, "detail": _clip(box["error"])}
    return {"name": name, "ok": True, "ms": ms, "detail": _clip(box.get("detail", ""))}


def _trading(day: date, holidays: Any | None) -> bool:
    if holidays is not None and hasattr(holidays, "is_trading_day"):
        return bool(holidays.is_trading_day(day))
    return day.weekday() < 5


def previous_trading_day(day: date, holidays: Any | None = None) -> date:
    d = day - timedelta(days=1)
    while not _trading(d, holidays):
        d -= timedelta(days=1)
    return d


def claude_ping(settings: Any) -> dict[str, Any]:
    """One tiny real call: ``max_tokens=5``, "Reply OK". Returns {model, ms, text}."""
    from .agent import make_client
    client = make_client(settings)
    t0 = time.monotonic()
    msg = client.messages.create(model=settings.digest_claude_model, max_tokens=5,
                                 messages=[{"role": "user", "content": "Reply OK"}])
    text = "".join(getattr(b, "text", "") for b in msg.content)
    return {"model": getattr(msg, "model", settings.digest_claude_model), "ms": int((time.monotonic() - t0) * 1000),
            "text": text}


def run_check(settings: Any, *, now: datetime | None = None, holidays: Any | None = None, session: Any | None = None,
              nse: Any | None = None, bse: Any = _UNSET, prices: Any | None = None, groww: Any = _UNSET,
              claude: Callable[[Any], dict[str, Any]] | None = None, clock: Callable[[], dict[str, Any]] | None = None,
              timeout: float = STEP_TIMEOUT_S) -> dict[str, Any]:
    """Run every step once and return the result dict. Tests inject fakes for ``session`` (archive files), ``nse``,
    ``bse``, ``prices``, ``groww`` (a callable returning a read-only client, or None) and ``claude``."""
    from .bands import URL as BANDS_URL
    from .bands import parse_sec_list
    from .breadth import URL as BHAV_URL
    from .breadth import parse_bhavcopy
    from .nse import HEADERS, NSEClient
    from .prices import YahooPrices

    now = now or datetime.now(IST)
    if holidays is None and getattr(settings, "market", "in") == "in":
        from .holidays import NSEHolidays
        holidays = NSEHolidays(cache_dir=Path(settings.state_dir) / "cache")
    last_day = previous_trading_day(now.date(), holidays)
    sess = session or requests.Session()
    nse = nse or NSEClient(session=sess, timeout=30.0)
    prices = prices or YahooPrices(suffix=".NS", session=sess, timeout=20.0)   # no disk cache, no archive: live reads
    if bse is _UNSET:
        from .bse import make_bse_client
        bse = make_bse_client(settings)
    if groww is _UNSET:
        def groww() -> Any:
            return cached_groww(settings)
    claude = claude or claude_ping
    if clock is None:
        from .clockcheck import check_clock
        clock = check_clock

    def nse_deals() -> str:
        got = nse.probe_deals(last_day)
        return f"bulk and block deals CSV for {last_day} parsed ({got['bulk']} bulk, {got['block']} block rows)"

    def nse_announcements() -> str:
        rows = nse.announcements(limit=3)
        if not rows:
            raise ValueError("no announcements came back")
        for r in rows:
            if not r.get("symbol") or not r.get("at"):
                raise ValueError("an announcement lacks its symbol or time")
        return f"{len(rows)} announcements parsed, newest {rows[0]['at']}"

    def bse_deals() -> str:
        if bse is None:
            raise Skip("skipped (BSE_DEALS is off)")
        rows = bse.fetch_range(last_day, last_day)   # one request flow per deal type, through the client's throttle
        return f"bulk and block CSV for {last_day} has the expected header ({len(rows)} rows)"

    def yahoo() -> str:
        for sym in ("NIFTYBEES.NS", "^NSEI"):
            bars = prices.history(sym, "1mo")
            have = {b["date"] for b in bars}
            if last_day.isoformat() not in have:
                raise ValueError(f"{sym} has no bar for {last_day} (newest {max(have) if have else 'none'})")
        return f"NIFTYBEES.NS and ^NSEI both have a daily bar for {last_day}"

    def archive_get(url: str) -> str:
        resp = sess.get(url, headers={**HEADERS, "Accept": "*/*"}, timeout=30)
        resp.raise_for_status()
        return str(resp.content.decode("utf-8-sig", errors="replace"))

    def bands() -> str:
        table = parse_sec_list(archive_get(BANDS_URL))
        return f"price band file parsed ({len(table)} symbols)"

    def bhavcopy() -> str:
        got_day, closes = parse_bhavcopy(archive_get(BHAV_URL.format(ddmmyyyy=last_day.strftime("%d%m%Y"))))
        if got_day != last_day.isoformat():
            raise ValueError(f"bhavcopy is for {got_day}, not {last_day}")
        return f"bhavcopy for {got_day} parsed ({len(closes)} EQ symbols)"

    def groww_cash() -> str:
        client = groww()
        if client is None:
            raise Skip("skipped (no cached token)")
        cash = client.available_cash()
        if not isinstance(cash, (int, float)) or cash < 0:
            raise ValueError(f"available cash is not a usable number ({cash!r})")
        return "available cash read (the margin fields the code uses are present)"

    def groww_read() -> str:
        client = groww()
        if client is None:
            raise Skip("skipped (no cached token)")
        holdings = client.holdings()
        missing = sorted({f for h in holdings for f in ("trading_symbol", "quantity", "average_price") if f not in h})
        if missing:
            raise ValueError("holdings rows lack: " + ", ".join(missing))
        orders = client.order_list()
        if not isinstance(orders, list):
            raise ValueError("the order list is not a list")
        missing = sorted({f for o in orders for f in ("groww_order_id", "order_status") if f not in o})
        if missing:
            raise ValueError("order rows lack: " + ", ".join(missing))
        return f"{len(holdings)} holdings and {len(orders)} orders read; the fields the code uses are present"

    def claude_step() -> str:
        if not getattr(settings, "integration_claude", False):
            raise Skip("skipped (INTEGRATION_CLAUDE is off)")
        if not getattr(settings, "anthropic_api_key", None):
            raise ValueError("ANTHROPIC_API_KEY is empty")
        r = claude(settings)
        return f"{r.get('model')} answered in {r.get('ms')} ms"

    def clock_step() -> str:
        res = clock()
        if not res.get("checked"):
            raise Skip(str(res.get("detail", "not checked")))
        if not res.get("ok"):
            raise ValueError(str(res.get("detail")) + "; TOTP logins need a correct clock")
        return str(res.get("detail"))

    def presence(label: str, *filled: Any) -> Callable[[], str]:
        def check() -> str:
            if all(filled):
                return "filled"
            raise Skip(f"empty: {label} is not configured")
        return check

    steps: list[tuple[str, Callable[[], str]]] = [
        ("NSE deals", nse_deals),
        ("NSE announcements", nse_announcements),
        ("BSE deals", bse_deals),
        ("Yahoo prices", yahoo),
        ("NSE price bands", bands),
        ("NSE bhavcopy", bhavcopy),
        ("Groww (read-only)", groww_read),
        ("Groww cash", groww_cash),
        ("Server clock", clock_step),
        ("Claude", claude_step),
        ("Resend", presence("Resend", getattr(settings, "resend_api_key", None), getattr(settings, "notify_email_to", None))),
        ("Telegram", presence("Telegram", getattr(settings, "telegram_bot_token", None),
                              getattr(settings, "telegram_chat_id", None))),
    ]
    results = [_run_step(name, fn, timeout) for name, fn in steps]
    return {"at": now.isoformat(timespec="seconds"), "last_trading_day": last_day.isoformat(), "steps": results,
            "ok": all(s["ok"] for s in results)}


# --------------------------------------------------------------------------- #
# Result file, alert, dashboard line
# --------------------------------------------------------------------------- #
def result_path(state_dir: Path) -> Path:
    return Path(state_dir) / RESULT_FILE


def load_result(state_dir: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(result_path(state_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("steps"), list) else None


def counts(result: dict[str, Any]) -> tuple[int, int]:
    """(passed, ran): skipped steps are neither."""
    ran = [s for s in result.get("steps", []) if not s.get("skipped")]
    return sum(1 for s in ran if s.get("ok")), len(ran)


def format_result(result: dict[str, Any]) -> str:
    lines = [f"Integration check at {result.get('at')} (last trading day {result.get('last_trading_day')})"]
    for s in result["steps"]:
        mark = "skip" if s.get("skipped") else ("ok  " if s["ok"] else "FAIL")
        lines.append(f"  [{mark}] {s['name']:<20} {s['ms']:>6} ms  {s['detail']}")
    passed, ran = counts(result)
    lines.append(f"{'OK' if result['ok'] else 'FAILED'}: {passed}/{ran} checks passed")
    return "\n".join(lines)


def alert_once_a_day(state_dir: Path, notifier: Any, result: dict[str, Any], today: str, *,
                     prefix: str = "integration_alert") -> bool:
    """One alert a day listing the failed steps. A clean run sends nothing. True if an alert was sent. ``prefix``
    names the marker file, so the "did not run" alert and the "a step failed" alert each get their own."""
    failed = [s for s in result.get("steps", []) if not s.get("ok")]
    if not failed or notifier is None:
        return False
    marker = Path(state_dir) / f"{prefix}_{today}.sent"
    if marker.exists():
        return False
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("sent", encoding="utf-8")
        for old in marker.parent.glob(f"{prefix}_*.sent"):
            if old != marker:
                old.unlink(missing_ok=True)
    except OSError:
        pass
    body = "The pre-market check failed, so new automated buys are paused until a check passes (sells and stops are not affected):\n" + "\n".join(f"- {s['name']}: {s['detail']}" for s in failed)
    try:
        notifier.send(f"[INTEGRATION] {len(failed)} live check{'s' if len(failed) != 1 else ''} failed", body)
    except Exception:  # noqa: BLE001
        log.warning("could not send the integration-check alert")
        return False
    return True


# --------------------------------------------------------------------------- the canary failsafe
def flag_path(state_dir: Path) -> Path:
    return Path(state_dir) / FLAG_FILE


def load_flag(state_dir: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(flag_path(state_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def set_flag(state_dir: Path, reasons: list[str], at: datetime) -> None:
    try:
        atomic_write(flag_path(state_dir), json.dumps({"at": at.isoformat(timespec="seconds"), "steps": reasons}))
    except OSError as e:
        log.warning("could not store the canary flag: %s", e)


def clear_flag(state_dir: Path) -> None:
    try:
        flag_path(state_dir).unlink()
    except OSError:
        pass


def buy_block(settings: Any, now: datetime | None = None) -> str | None:
    """The reason automated BUYS are refused right now, or None. Sells and stop exits never call this. Read-only.

    A stored failure refuses buys. Besides that, with live orders on, a trading day after 09:10 IST with no PASSING
    result for today refuses them too, so the failsafe holds without the watch service. Paper and practice buys are
    only affected by the stored failure."""
    if not getattr(settings, "integration_check", True) or getattr(settings, "market", "in") != "in":
        return None
    flag = load_flag(settings.state_dir)
    if flag:
        return "pre-market check failed: " + (", ".join(str(x) for x in flag.get("steps") or []) or "unknown")
    if getattr(settings, "groww_live_orders", False):
        n = (now or datetime.now(IST)).astimezone(IST)
        if n.weekday() < 5 and n.time() >= OVERDUE_AFTER:
            res = load_result(settings.state_dir)
            if not (res and res.get("ok") and str(res.get("at", ""))[:10] == n.date().isoformat()):
                return "pre-market check has not passed today"
    return None


def premarket_line(settings: Any, now: datetime) -> str | None:
    """One line for the morning email: today's canary result, or None when there is nothing to say yet."""
    if not getattr(settings, "integration_check", True) or getattr(settings, "market", "in") != "in":
        return None
    res = load_result(settings.state_dir)
    flag = load_flag(settings.state_dir)
    if res is not None and str(res.get("at", ""))[:10] == now.astimezone(IST).date().isoformat():
        if res.get("ok"):
            passed, ran = counts(res)
            return f"Pre-market check: ok {passed}/{ran}"
        failed = ", ".join(s["name"] for s in res["steps"] if not s.get("ok"))
        return f"Pre-market check: FAILED: {failed}. New buys are paused."
    if flag:
        return "Pre-market check: FAILED: " + ", ".join(str(x) for x in flag.get("steps") or []) + ". New buys are paused."
    return None


def mark_overdue(settings: Any, notifier: Any, now: datetime, holidays: Any | None = None) -> bool:
    """On a trading day after 09:10 with no run today: set the flag and alert (once a day). True if newly set."""
    now = now.astimezone(IST)
    if not getattr(settings, "integration_check", True) or now.time() < OVERDUE_AFTER or not _trading(now.date(), holidays):
        return False
    res = load_result(settings.state_dir)
    if (res is not None and str(res.get("at", ""))[:10] == now.date().isoformat()) or load_flag(settings.state_dir):
        return False
    reason = f"did not run by {OVERDUE_AFTER.strftime('%H:%M')}"
    set_flag(settings.state_dir, [reason], now)
    if callable(notifier) and not hasattr(notifier, "send"):
        notifier = notifier()
    alert_once_a_day(settings.state_dir, notifier,
                     {"steps": [{"name": "Pre-market check", "ok": False, "detail": reason}]}, now.date().isoformat(),
                     prefix="integration_overdue")
    return True


def run_and_record(settings: Any, *, notifier: Any | None = None, now: datetime | None = None,
                   **injections: Any) -> dict[str, Any]:
    """Run the check, store the result, alert once a day on failure. Used by the CLI, the scheduler and the dashboard."""
    now = now or datetime.now(IST)
    result = run_check(settings, now=now, **injections)
    try:
        atomic_write(result_path(settings.state_dir), json.dumps(result, indent=1))
    except OSError as e:
        log.warning("could not store the integration-check result: %s", e)
    if result["ok"]:
        clear_flag(settings.state_dir)
    else:
        set_flag(settings.state_dir, [s["name"] for s in result["steps"] if not s.get("ok")], now)
    if callable(notifier) and not hasattr(notifier, "send"):
        notifier = notifier()   # a cached_notifier getter
    alert_once_a_day(settings.state_dir, notifier, result, now.date().isoformat())
    return result


def status_line(state_dir: Path, now: datetime, holidays: Any | None = None) -> dict[str, Any]:
    """The dashboard line near the freshness chip: "Integration: ok 08:35 · 6/6". Level "bad" (red) when the last run
    failed or is older than two trading days; "idle" when it has never run."""
    res = load_result(state_dir)
    if res is None:
        return {"level": "idle", "text": "Integration: not run yet", "title": "", "at": None, "ok": None}
    try:
        at = datetime.fromisoformat(res["at"])
        at = at if at.tzinfo else at.replace(tzinfo=IST)
        at = at.astimezone(IST)
    except (KeyError, ValueError, TypeError):
        return {"level": "bad", "text": "Integration: result unreadable", "title": "", "at": None, "ok": None}
    now = now.astimezone(IST)
    passed, ran = counts(res)
    when = at.strftime("%H:%M") if at.date() == now.date() else at.strftime("%d %b %H:%M")
    age, d = 0, at.date()
    while d < now.date():
        d += timedelta(days=1)
        if _trading(d, holidays):
            age += 1
    failed = [s["name"] for s in res["steps"] if not s.get("ok")]
    if not res.get("ok"):
        level, text = "bad", f"Integration: failed {when} · {passed}/{ran}"
    elif age > STALE_TRADING_DAYS:
        level, text = "bad", f"Integration: ok {when} · {passed}/{ran} · not run for {age} trading days"
    else:
        level, text = "ok", f"Integration: ok {when} · {passed}/{ran}"
    title = ("Failed: " + ", ".join(failed)) if failed else "All live checks passed"
    return {"level": level, "text": text, "title": title, "at": res["at"], "ok": bool(res.get("ok"))}


# --------------------------------------------------------------------------- #
# The daily job
# --------------------------------------------------------------------------- #
class IntegrationScheduler(DailyJobScheduler):
    """Runs the check once per trading day from 08:35 IST, with the forward job's claim-file guard
    (``integration_<day>.claim``). The scheduler is ticked by the watch loop."""

    claim_prefix = "integration"
    label = "integration check"
    latest = LATEST

    def __init__(self, state_dir: Path, run_fn: Callable[[], Any], *, holidays: Any = None,
                 run_after: dtime = RUN_AFTER, threaded: bool = True, clock: Callable[[], float] = time.monotonic,
                 overdue_fn: Callable[[datetime], Any] | None = None):
        super().__init__(state_dir, run_fn, holidays=holidays, run_after=run_after, threaded=threaded, clock=clock)
        self.overdue_fn = overdue_fn   # called on every tick: sets the canary flag when no run happened by 09:10

    def _in_flight(self, day: str) -> bool:
        """A run is going on right now (its thread is alive, or today's claim is held and the day is not done): it
        has not had its chance to write a result yet, so "did not run" would be a false alarm."""
        t = self._thread
        if t is not None and t.is_alive():
            return True
        return self._claim_path(day).exists() and day not in self._done

    def tick(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(IST)
        out = super().tick(now)
        if self.overdue_fn is not None and not self._in_flight(now.date().isoformat()):
            try:
                if self.overdue_fn(now):
                    out["overdue"] = True
            except Exception:  # noqa: BLE001 - never stops the watch
                log.exception("integration overdue check failed")
        return out

RETRY_UNTIL = dtime(9, 0)   # a failed run before this raises, so the scheduler retries it; after it the failure stands


def make_scheduler(settings: Any, notifier: Any = None, holidays: Any = None, *,
                   now_fn: Callable[[], datetime] | None = None) -> IntegrationScheduler | None:
    """The scheduler for the watch service, or None when INTEGRATION_CHECK is off (or the market is not India)."""
    if not getattr(settings, "integration_check", True) or getattr(settings, "market", "in") != "in":
        return None

    def job() -> None:
        result = run_and_record(settings, notifier=notifier, holidays=holidays)
        log.info("%s", format_result(result).splitlines()[-1])
        if not result["ok"] and (now_fn or (lambda: datetime.now(IST)))().astimezone(IST).time() < RETRY_UNTIL:
            raise RuntimeError("integration check failed before 09:00; retrying")   # the scheduler releases the claim

    return IntegrationScheduler(Path(settings.state_dir), job, holidays=holidays,
                                overdue_fn=lambda now: mark_overdue(settings, notifier, now, holidays))
