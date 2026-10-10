"""Building, sending and scheduling the daily emails.

``build_digest`` makes one email (data, summary, text and HTML). ``DigestScheduler`` is called from every watch tick:
on NSE trading days it sends the morning email once after DIGEST_MORNING and the evening one once after
DIGEST_EVENING (IST), remembering the day it last sent each in state.json. The build runs in a worker thread with a
timeout, never inside a lock and never on the tick itself.
"""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
from datetime import datetime, time as dtime, timedelta, tzinfo
from typing import Any, Callable

from .config import parse_hhmm
from .digest import KINDS, DigestContext, build_data, make_context
from .digest_render import render
from .digest_writer import write_summary
from .state import STATE_LOCK, State
from .timezones import IST

log = logging.getLogger(__name__)

# A late start still sends if it is before these (IST); after them the day is skipped.
LATEST = {"morning": dtime(12, 0), "evening": dtime(20, 0)}
BUILD_TIMEOUT_S = 900.0
RETRY_AFTER_S = 600.0
MAX_TRIES = 3


def build_digest(kind: str, ctx: DigestContext, *, writer: str | None = None, session: Any = None,
                 client: Any = None) -> dict[str, Any]:
    """The email for ``kind``: {"subject", "text", "html", "writer", "summary", "data"}. ``writer`` overrides
    DIGEST_WRITER for this build ("none" gives rules only)."""
    settings = ctx.settings
    if writer is not None:
        settings = dataclasses.replace(settings, digest_writer=writer)
    data = build_data(kind, ctx)
    summary, name = write_summary(kind, data, settings, session=session, client=client)
    return {**render(data, summary, name), "writer": name, "summary": summary, "data": data}


def send_digest(notifier: Any, email: dict[str, Any]) -> list[str]:
    try:
        return notifier.send(email["subject"], email["text"], html=email["html"])
    except TypeError:  # a notifier that takes no HTML part
        return notifier.send(email["subject"], email["text"])


def _external(delivered: Any) -> bool:
    return bool(set(delivered or []) - {"console"})


class DigestScheduler:
    def __init__(self, settings: Any, ctx_factory: Callable[[], DigestContext], notifier: Any, *, holidays: Any = None,
                 tz: tzinfo = IST, timeout: float = BUILD_TIMEOUT_S, retry_after: float = RETRY_AFTER_S,
                 max_tries: int = MAX_TRIES, build_fn: Callable[[str], dict[str, Any]] | None = None):
        self.settings = settings
        self.ctx_factory = ctx_factory
        self.notifier = notifier
        self.holidays = holidays
        self.tz = tz
        self.timeout = timeout
        self.retry_after = retry_after
        self.max_tries = max_tries
        self._build_fn = build_fn
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._next_try: dict[str, float] = {}
        self.last: dict[str, Any] | None = None

    # -- schedule ------------------------------------------------------------------
    def enabled(self) -> bool:
        s = self.settings
        channels = getattr(self.notifier, "channels", ["console"])
        return bool(s.digest_enabled and s.market == "in" and set(channels) - {"console"})

    def trading_day(self, day: Any) -> bool:
        if day.weekday() >= 5:
            return False
        return self.holidays is None or bool(self.holidays.is_trading_day(day))

    def _wanted(self, kind: str) -> bool:
        return bool(getattr(self.settings, f"digest_{kind}_on", True))

    def _start(self, kind: str) -> dtime:
        return dtime.fromisoformat(parse_hhmm(getattr(self.settings, f"digest_{kind}")))

    def due(self, now: datetime) -> list[str]:
        if not self.enabled() or not self.trading_day(now.date()):
            return []
        t = now.time()
        return [k for k in KINDS if self._wanted(k) and self._start(k) <= t < LATEST[k]]

    def _sent_today(self, kind: str, day: str, path: Any) -> bool:
        return (State(path).data.get("digest_sent") or {}).get(kind) == day

    def tick(self, now: datetime | None = None) -> dict[str, Any]:
        """Start the digest that is due, if any. Returns at once; the work runs on a worker thread."""
        now = (now or datetime.now(self.tz)).astimezone(self.tz)
        kinds = self.due(now)
        if not kinds:
            return {"due": []}
        day, path = now.date().isoformat(), self.settings.state_dir / "state.json"
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return {"busy": True, "due": kinds}
            for kind in kinds:
                if time.monotonic() < self._next_try.get(kind, 0.0):
                    continue
                with STATE_LOCK:  # claim it: restarts, other ticks and other processes then leave it alone
                    st = State(path)
                    if (st.data.get("digest_sent") or {}).get(kind) == day:
                        continue
                    tries = st.data.get("digest_tries", {}).get(kind) or {}
                    if tries.get("date") == day and tries.get("n", 0) >= self.max_tries:
                        continue
                    claim = (st.data.get("digest_claim") or {}).get(kind) or {}
                    if claim.get("date") == day and self._fresh(claim.get("at"), now):
                        continue
                    st.data.setdefault("digest_claim", {})[kind] = {"date": day, "at": now.isoformat(timespec="seconds")}
                    st.save()
                self._worker = threading.Thread(target=self._run, args=(kind, day, path), daemon=True,
                                                name=f"digest-{kind}")
                self._worker.start()
                return {"started": kind, "due": kinds}
        return {"due": kinds, "started": None}

    def _fresh(self, at: Any, now: datetime) -> bool:
        try:
            return now - datetime.fromisoformat(str(at)) < timedelta(seconds=self.timeout + 60)
        except ValueError:
            return False

    # -- worker --------------------------------------------------------------------
    def _build(self, kind: str) -> dict[str, Any]:
        if self._build_fn is not None:
            return self._build_fn(kind)
        return build_digest(kind, self.ctx_factory())

    def _run(self, kind: str, day: str, path: Any) -> None:
        result: dict[str, Any] = {}

        def build() -> None:
            try:
                result["email"] = self._build(kind)
            except Exception as e:  # noqa: BLE001
                result["error"] = f"{type(e).__name__}: {e}"

        inner = threading.Thread(target=build, daemon=True, name=f"digest-build-{kind}")
        inner.start()
        inner.join(self.timeout)
        outcome, detail = "failed", None
        if inner.is_alive():
            outcome, detail = "timeout", f"building took longer than {self.timeout:g}s"
        elif "error" in result:
            detail = result["error"]
        else:
            try:
                delivered = send_digest(self.notifier, result["email"])
                if _external(delivered):
                    outcome = "sent"
                else:
                    detail = "no email or webhook delivery succeeded"
            except Exception as e:  # noqa: BLE001
                detail = f"{type(e).__name__}: {e}"
        with STATE_LOCK:
            st = State(path)
            (st.data.setdefault("digest_claim", {})).pop(kind, None)
            if outcome == "sent":
                st.data.setdefault("digest_sent", {})[kind] = day
            else:
                tries = st.data.setdefault("digest_tries", {})
                cur = tries.get(kind) or {}
                tries[kind] = {"date": day, "n": (cur.get("n", 0) if cur.get("date") == day else 0) + 1}
            st.save()
        if outcome != "sent":
            self._next_try[kind] = time.monotonic() + self.retry_after
            log.warning("%s digest not sent: %s (%s)", kind, outcome, detail)
        else:
            log.info("%s digest sent: %s", kind, result["email"]["subject"])
        self.last = {"kind": kind, "day": day, "outcome": outcome, "detail": detail,
                     "subject": (result.get("email") or {}).get("subject"),
                     "writer": (result.get("email") or {}).get("writer")}

    def wait(self, timeout: float = 5.0) -> None:
        """Wait for the running worker (tests and the CLI)."""
        w = self._worker
        if w is not None:
            w.join(timeout)


def make_scheduler(settings: Any, notifier: Any, *, data: Any = None, prices: Any = None, news: Any = None,
                   holidays: Any = None, practice: Any = None, groww: Callable[[], dict[str, Any]] | None = None,
                   context: Any = None) -> DigestScheduler:
    """The scheduler for the watch service. The context is built fresh for each digest."""
    return DigestScheduler(
        settings, lambda: make_context(settings, data=data, prices=prices, news=news, holidays=holidays,
                                       practice=practice, groww=groww, context=context),
        notifier, holidays=holidays)
