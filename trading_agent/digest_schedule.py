"""Building, sending and scheduling the daily emails.

``build_digest`` makes one email (data, summary, text and HTML). ``DigestScheduler`` is called from every watch tick:
on NSE trading days it sends the morning email once after DIGEST_MORNING and the evening one once after
DIGEST_EVENING (IST), remembering the day it last sent each in state.json. The build runs in a worker thread with a
timeout, never inside a lock and never on the tick itself.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import threading
import uuid
import time
from datetime import datetime, time as dtime, tzinfo
from pathlib import Path
from typing import Any, Callable

from .config import parse_hhmm
from .digest import DIGEST_STATE_FILE, KINDS, DigestContext, build_data, make_context, read_digest_state
from .digest_render import render
from .digest_writer import write_summary
from .state import atomic_write
from .timezones import IST

log = logging.getLogger(__name__)

# A late start still sends if it is before these (IST); after them the day is skipped.
LATEST = {"morning": dtime(12, 0), "evening": dtime(20, 0)}
BUILD_TIMEOUT_S = 900.0
RETRY_AFTER_S = 600.0
MAX_TRIES = 3


def build_digest(kind: str, ctx: DigestContext, *, writer: str | None = None, session: Any = None,
                 client: Any = None, cancel: threading.Event | None = None,
                 deadline: float | None = None) -> dict[str, Any]:
    """The email for ``kind``: {"subject", "text", "html", "writer", "summary", "data"}. ``writer`` overrides
    DIGEST_WRITER for this build ("none" gives rules only). ``cancel`` / ``deadline`` (time.monotonic) make the
    build stop asking for more data and skip the summary models once passed."""
    if cancel is not None:
        ctx.cancel = cancel
    if deadline is not None:
        ctx.deadline = deadline
    settings = ctx.settings
    if writer is not None:
        settings = dataclasses.replace(settings, digest_writer=writer)
    data = build_data(kind, ctx)
    summary, name = write_summary(kind, data, settings, session=session, client=client, known=ctx.known,
                                  cancelled=ctx.expired)
    return {**render(data, summary, name), "writer": name, "summary": summary, "data": data}


def send_digest(notifier: Any, email: dict[str, Any]) -> list[str]:
    try:
        return notifier.send(email["subject"], email["text"], html=email["html"])
    except TypeError:  # a notifier that takes no HTML part
        return notifier.send(email["subject"], email["text"])


def _external(delivered: Any) -> bool:
    return bool(set(delivered or []) - {"console"})


_FILE_LOCK = threading.Lock()  # digest_state.json read-modify-write inside this process


def _update_state(state_dir: Any, fn: Callable[[dict[str, Any]], None]) -> None:
    with _FILE_LOCK:
        d = read_digest_state(state_dir)
        fn(d)
        atomic_write(Path(state_dir) / DIGEST_STATE_FILE, json.dumps(d, indent=2))


class DigestScheduler:
    """Sends each digest once per trading day. The once-a-day marks are kept in ``digest_state.json`` (its own file:
    ``check()`` rewrites state.json whole and would lose them), and a claim file made with O_CREAT|O_EXCL stops two
    processes or schedulers from building the same email."""

    def __init__(self, settings: Any, ctx_factory: Callable[[], DigestContext], notifier: Any, *, holidays: Any = None,
                 tz: tzinfo = IST, timeout: float = BUILD_TIMEOUT_S, retry_after: float = RETRY_AFTER_S,
                 max_tries: int = MAX_TRIES, build_fn: Callable[..., dict[str, Any]] | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.settings = settings
        self.ctx_factory = ctx_factory
        self.notifier = notifier   # a Notifier, or a function returning one (read again at every check)
        self.holidays = holidays
        self.tz = tz
        self.timeout = timeout
        self.retry_after = retry_after
        self.max_tries = max_tries
        self._build_fn = build_fn
        self._clock = clock
        self._inner_at = 0.0
        self._warned_slow = False
        self._unmarked: dict[str, str] = {}   # sent, but recording it failed: kind -> day (never sent again today)
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._inner: threading.Thread | None = None  # the build; it may outlive its timeout and keeps the slot busy
        self._next_try: dict[str, float] = {}
        self.last: dict[str, Any] | None = None

    def _notifier(self) -> Any:
        n = self.notifier
        return n() if callable(n) and not hasattr(n, "send") else n

    # -- schedule ------------------------------------------------------------------
    def enabled(self) -> bool:
        s = self.settings
        channels = getattr(self._notifier(), "channels", ["console"])
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

    def _busy(self) -> bool:
        if self._worker is not None and self._worker.is_alive():
            return True
        if self._inner is None or not self._inner.is_alive():
            return False
        age = self._clock() - self._inner_at
        if age > 3 * self.timeout:
            return False   # abandoned: it never sends, so a new build is safe
        if age > 2 * self.timeout and not self._warned_slow:
            self._warned_slow = True
            log.error("a daily-email build has been running for %.0fs (limit %.0fs) and is stuck; a new one starts "
                      "at %.0fs", age, self.timeout, 3 * self.timeout)
        return True

    def _claim_path(self, kind: str, day: str) -> Path:
        return Path(self.settings.state_dir) / f"digest_{kind}_{day}.claim"

    def _claim(self, kind: str, day: str) -> bool:
        path = self._claim_path(kind, day)
        path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if not self._take_over_stale(path):
                    return False
                continue
            except OSError:
                return False
            with os.fdopen(fd, "w") as f:
                f.write(str(os.getpid()))
            return True
        return False

    def _take_over_stale(self, path: Path) -> bool:
        """Remove a claim left by a crashed run. The stale file is first renamed to a name of our own: only one
        contender can win that rename, the others find nothing and give up. A file that turns out fresh (someone
        claimed in between) is put back."""
        try:
            if time.time() - path.stat().st_mtime <= self.timeout + 60:
                return False
            mine = path.with_name(f"{path.name}.stale.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}")
            os.replace(path, mine)
        except OSError:
            return False   # gone, or another contender won the rename
        try:
            fresh = time.time() - mine.stat().st_mtime <= self.timeout + 60
            if fresh:
                try:
                    os.link(mine, path)   # fails if someone has claimed again; then theirs stands
                except OSError:
                    pass
            return not fresh
        except OSError:
            return False
        finally:
            try:
                mine.unlink()
            except OSError:
                pass

    def _clean_old_claims(self, today: str) -> None:
        """Claim files dated before today are never needed again."""
        try:
            for f in Path(self.settings.state_dir).glob("digest_*_*.claim"):
                day = f.name[:-len(".claim")].rsplit("_", 1)[-1]
                if day < today:
                    try:
                        f.unlink()
                    except OSError:
                        pass
        except OSError:
            pass

    def _retry_marks(self, sd: Any) -> None:
        """A digest that was sent but could not be recorded: try the record again (its claim is still held)."""
        for kind, day in list(self._unmarked.items()):
            def mark(d: dict[str, Any], kind: str = kind, day: str = day) -> None:
                d.setdefault("sent", {})[kind] = day
            try:
                _update_state(sd, mark)
            except OSError:
                continue
            del self._unmarked[kind]
            self._release(kind, day)

    def tick(self, now: datetime | None = None) -> dict[str, Any]:
        """Start the digest that is due, if any. Returns at once; the work runs on a worker thread."""
        now = (now or datetime.now(self.tz)).astimezone(self.tz)
        day, sd = now.date().isoformat(), self.settings.state_dir
        self._clean_old_claims(day)
        self._retry_marks(sd)
        kinds = self.due(now)
        if not kinds:
            return {"due": []}
        with self._lock:
            if self._busy():
                return {"busy": True, "due": kinds}
            for kind in kinds:
                if self._clock() < self._next_try.get(kind, 0.0) or self._unmarked.get(kind) == day:
                    continue
                st = read_digest_state(sd)
                tries = (st.get("tries") or {}).get(kind) or {}
                if (st.get("sent") or {}).get(kind) == day or (tries.get("date") == day and tries.get("n", 0) >= self.max_tries):
                    continue
                if not self._claim(kind, day):
                    continue
                if (read_digest_state(sd).get("sent") or {}).get(kind) == day:   # sent while we were claiming
                    self._release(kind, day)
                    continue
                self._worker = threading.Thread(target=self._run, args=(kind, day), daemon=True, name=f"digest-{kind}")
                self._worker.start()
                return {"started": kind, "due": kinds}
        return {"due": kinds, "started": None}

    def _release(self, kind: str, day: str) -> None:
        try:
            self._claim_path(kind, day).unlink()
        except OSError:
            pass

    # -- worker --------------------------------------------------------------------
    def _build(self, kind: str, cancel: threading.Event, deadline: float) -> dict[str, Any]:
        if self._build_fn is not None:
            return self._build_fn(kind, cancel)
        return build_digest(kind, self.ctx_factory(), cancel=cancel, deadline=deadline)

    def _run(self, kind: str, day: str) -> None:
        result: dict[str, Any] = {}
        cancel = threading.Event()
        deadline = time.monotonic() + self.timeout

        def build() -> None:
            try:
                result["email"] = self._build(kind, cancel, deadline)
            except Exception as e:  # noqa: BLE001
                result["error"] = f"{type(e).__name__}: {e}"

        self._inner = inner = threading.Thread(target=build, daemon=True, name=f"digest-build-{kind}")
        self._inner_at, self._warned_slow = self._clock(), False
        inner.start()
        inner.join(self.timeout)
        outcome, detail = "failed", None
        if inner.is_alive():
            cancel.set()   # it stops asking for data and skips the summary models; it never sends
            outcome, detail = "timeout", f"building took longer than {self.timeout:g}s"
        elif "error" in result:
            detail = result["error"]
        else:
            try:
                delivered = send_digest(self._notifier(), result["email"])
                if _external(delivered):
                    outcome = "sent"
                else:
                    detail = "no email or webhook delivery succeeded"
            except Exception as e:  # noqa: BLE001
                detail = f"{type(e).__name__}: {e}"

        def mark(d: dict[str, Any]) -> None:
            if outcome == "sent":
                d.setdefault("sent", {})[kind] = day
            else:
                tries = d.setdefault("tries", {})
                cur = tries.get(kind) or {}
                tries[kind] = {"date": day, "n": (cur.get("n", 0) if cur.get("date") == day else 0) + 1}
        marked = True
        try:
            _update_state(self.settings.state_dir, mark)   # marked sent first, then the claim goes
        except OSError as e:
            marked = False
            log.error("the %s digest %s but recording it in %s failed (%s)", kind,
                      "was sent" if outcome == "sent" else "failed", DIGEST_STATE_FILE, e)
        if marked:
            self._release(kind, day)
        else:   # keep the claim (it expires) so nobody sends it again; this scheduler also remembers
            if outcome == "sent":
                self._unmarked[kind] = day
            self._next_try[kind] = self._clock() + max(self.retry_after, 60.0)
        if outcome != "sent":
            self._next_try[kind] = self._clock() + self.retry_after
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
