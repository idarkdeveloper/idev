"""Dead-man's switch: the watch loop pings an https URL every 5 minutes while it runs.

Works with healthchecks.io, Better Stack and UptimeRobot heartbeat URLs. If the pings stop (the server is down, the
service crashed, the loop is stuck) the monitoring site emails you. A tick that raised is reported by calling the
same URL with ``/fail`` appended and the error text as the body (the healthchecks.io convention; harmless elsewhere).

The URL's path is a secret token: it is never logged, and neither is any exception that could contain it.
Nothing here ever raises into the watch loop.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

import requests

log = logging.getLogger(__name__)

PING_EVERY_S = 300.0
TIMEOUT_S = 10.0


class Heartbeat:
    def __init__(self, url: str | None, *, session: Any | None = None, every: float = PING_EVERY_S,
                 clock: Callable[[], float] = time.monotonic):
        self.url = url or None
        self.session = session or requests.Session()
        self.every = every
        self._clock = clock
        self._last: float | None = None
        self._last_ok: bool | None = None

    def due(self) -> bool:
        return bool(self.url) and (self._last is None or self._clock() - self._last >= self.every)

    def ping(self, error: str | None = None) -> bool:
        """One request now: a GET on success, a POST of the error text to ``<url>/fail`` for a failure. Returns
        whether the monitoring site answered; logs only "heartbeat ok" / "heartbeat failed" (state changes)."""
        if not self.url:
            return False
        self._last = self._clock()
        try:
            if error is None:
                r = self.session.get(self.url, timeout=TIMEOUT_S)
            else:
                r = self.session.post(self.url.rstrip("/") + "/fail", data=str(error)[:10_000].encode("utf-8", "replace"),
                                      timeout=TIMEOUT_S)
            r.raise_for_status()
            ok = True
        except Exception:  # noqa: BLE001 - never the exception text: it can carry the URL
            ok = False
        if ok != self._last_ok:
            log.info("heartbeat ok") if ok else log.warning("heartbeat failed")
        self._last_ok = ok
        return ok

    def tick(self, error: str | None = None) -> None:
        """Called once per watch-loop iteration: pings when five minutes have passed since the last ping, or at once
        for an error (a failure is reported on the iteration it happens, not five minutes later)."""
        if not self.url:
            return
        if error is not None or self.due():
            self.ping(error)
