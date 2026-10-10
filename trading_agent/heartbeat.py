"""Dead-man's switch: a heartbeat thread pings an https URL every 60 seconds, at every hour, while the watch loop is
making progress.

Works with healthchecks.io, Better Stack and UptimeRobot heartbeat URLs. The pings come from their own daemon thread
(so one long tick cannot silence them) but are tied to the loop's progress: the watch loop stamps a monotonic time at
the start and end of every iteration, and the thread pings "ok" only while that stamp is recent (3 loop intervals, at
least 20 minutes). Only the stall threshold depends on the hour: in the market window (09:15 to 15:30 IST on a trading day; the watch
loop supplies the test, from cached holiday data, never the network) the loop counts as stalled after
max(3 minutes, 3 loop intervals); outside it after max(3 loop intervals, 20 minutes). A stalled loop pings ``/fail`` with "watch loop stalled N min"; a tick that raised pings ``/fail``
with the error text at once. After a failure the next healthy moment pings "ok" at once.

``/fail`` (a POST with the error as body, the healthchecks.io convention) is only sent to hc-ping.com hosts, or when
HEARTBEAT_FAIL=true; for other providers a failure simply means no ping, which their own grace period turns into an
alert. The URL's path is a secret token: it is never logged, and neither is any exception that could contain it.
Nothing here ever raises into the watch loop.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

import requests

log = logging.getLogger(__name__)

PING_EVERY_S = 60.0
TIMEOUT_S = 10.0
RECOVER_POLL_S = 15.0   # while failing, look this often for the loop to be healthy again
MIN_STALL_S = 1200.0
WINDOW_STALL_S = 180.0


def fail_url(url: str) -> str:
    """``<url>/fail`` built on the path (the query string is kept after it)."""
    u = urlsplit(url)
    return urlunsplit((u.scheme, u.netloc, u.path.rstrip("/") + "/fail", u.query, ""))


def stall_after(every: float) -> float:
    return max(3 * every, MIN_STALL_S)


class Heartbeat:
    def __init__(self, url: str | None, *, session: Any | None = None, every: float = PING_EVERY_S,
                 clock: Callable[[], float] = time.monotonic, fail_enabled: bool | None = None):
        self.url = url or None
        self.session = session or requests.Session()
        self.every = every
        self._clock = clock
        host = (urlsplit(self.url).hostname or "").lower() if self.url else ""
        self.fail_enabled = (host == "hc-ping.com" or host.endswith(".hc-ping.com")) if fail_enabled is None else fail_enabled
        self._last_ok: bool | None = None
        self._failing = False
        self._last_fail: float | None = None
        self._unclean = False
        self._error: str | None = None
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._progress: Callable[[], float | None] = lambda: None
        self._stall = MIN_STALL_S
        self._loop_every = 60.0
        self._window: Callable[[], bool] | None = None
        if self.url:
            from .notify import add_log_secret
            add_log_secret(self.url, urlsplit(self.url).path)

    def _in_window(self) -> bool:
        try:
            return bool(self._window and self._window())
        except Exception:  # noqa: BLE001
            return False

    def _stall_now(self) -> float:
        """3 minutes in the market window (or 3 loop intervals when the loop is slower than a minute), else the
        20 minute rule."""
        return max(WINDOW_STALL_S, 3 * self._loop_every) if self._in_window() else self._stall

    # -- one request ---------------------------------------------------------------
    def ping(self, error: str | None = None) -> bool:
        """A GET on success, a POST of the error text to ``<url>/fail`` for a failure (only where /fail is allowed;
        otherwise nothing is sent). Returns whether the monitoring site answered. Logs only "heartbeat ok" /
        "heartbeat failed", on changes."""
        if not self.url:
            return False
        try:
            if error is None:
                r = self.session.get(self.url, timeout=TIMEOUT_S)
            elif self.fail_enabled:
                r = self.session.post(fail_url(self.url), data=str(error)[:10_000].encode("utf-8", "replace"),
                                      timeout=TIMEOUT_S)
            else:
                return False
            r.raise_for_status()
            ok = True
        except Exception:  # noqa: BLE001 - never the exception text: it can carry the URL
            ok = False
        if ok != self._last_ok:
            log.info("heartbeat ok") if ok else log.warning("heartbeat failed")
        self._last_ok = ok
        return ok

    # -- the loop's side ---------------------------------------------------------------
    def report_error(self, text: str) -> None:
        """A watch tick raised an unexpected exception: the next beat (at once) is a fail with this text, at most one
        per 5 minutes. No ok ping follows until ``report_clean`` says a tick finished without an exception. (A routine
        check error, such as Groww or NSE being down, is not reported here: the loop is alive and alerts already say it.)"""
        self._error = text
        self._unclean = True
        self._wake.set()

    def report_clean(self) -> None:
        """A tick finished without an exception: ok pings may resume, at once."""
        if self._unclean:
            self._unclean = False
            self._wake.set()

    def beat(self) -> str:
        """One decision: returns "fail", "ok" or "skip" (nothing to do yet). Synchronous, for the thread and tests."""
        if not self.url:
            return "skip"
        err, self._error = self._error, None
        if err is not None and self._last_fail is not None and self._clock() - self._last_fail < 5 * self.every:   # one fail per five ping periods (5 minutes by default)
            self._failing = True
            return "skip"   # rate limit: one reported fail per interval
        if err is None:
            stamp = self._progress()
            age = None if stamp is None else self._clock() - stamp
            if age is not None and age >= self._stall_now():
                if self._last_fail is not None and self._clock() - self._last_fail < 5 * self.every:   # one fail per five ping periods (5 minutes by default)
                    return "skip"   # still stalled, and a fail went out within the last interval
                err = f"watch loop stalled {int(age // 60)} min"
        if err is not None:
            self._failing = True
            self._last_fail = self._clock()
            self.ping(err)
            return "fail"
        if self._unclean:   # an exception was reported and no clean tick has finished since
            self._failing = True
            return "skip"
        self._failing = False
        self.ping()
        return "ok"

    def start(self, progress: Callable[[], float | None], *, stall_s: float, stop: threading.Event,
              window: Callable[[], bool] | None = None, loop_every: float = 60.0) -> None:
        if not self.url or (self._thread and self._thread.is_alive()):
            return
        self._progress, self._stall = progress, stall_s
        self._window, self._loop_every = window, float(loop_every)
        self._stop = stop

        def run() -> None:
            self.beat()   # the first one at once: the check turns green right after the start
            while not stop.is_set():
                timeout = RECOVER_POLL_S if self._failing else self.every
                if self._wake.wait(min(timeout, self.every)):
                    self._wake.clear()
                    if stop.is_set():
                        break
                    self.beat()   # an error was reported: fail now
                    continue
                if stop.is_set():
                    break
                self.beat()   # a regular beat, or (while failing) the first healthy moment pings ok at once

        self._thread = threading.Thread(target=run, daemon=True, name="heartbeat")
        self._thread.start()

    def stop(self) -> None:
        self._wake.set()
