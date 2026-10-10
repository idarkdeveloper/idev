"""A circuit breaker for flaky public sites (NSE, Yahoo), shared across watch ticks.

After ``threshold`` (3) consecutive refusals (HTTP 403, 429, 5xx, a timeout or a connection error) the breaker opens and
the next calls are skipped, not retried: they raise ``CircuitOpen`` at once, which is a ``requests.ConnectionError``, so
every caller that already handles a failed request handles this too. The pause grows 30 s, 60 s, then 300 s (the cap);
after it calls are allowed again (there is no single probe: the first one that fails re-opens the breaker for the
next, longer pause) and a success closes it and resets the count. A 404 or another client error is
not a refusal (a file that is not published yet is normal).

It logs once per state change ("NSE connection degraded" / "NSE connection recovered") and, when given a state file,
writes ``{"degraded", "since", "retry_at"}`` so the dashboard (another process) can show "NSE degraded".
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import requests

log = logging.getLogger(__name__)

THRESHOLD = 3
DELAYS = (30.0, 60.0, 300.0)
STALE_AFTER_S = 900.0   # a degraded state file nobody has touched for this long is ignored (the watch service is gone)


class CircuitOpen(requests.ConnectionError):
    """The breaker is open: the call was skipped without touching the network."""


class CircuitBreaker:
    def __init__(self, name: str, *, threshold: int = THRESHOLD, delays: tuple[float, ...] = DELAYS,
                 clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time,
                 state_file: Path | None = None):
        self.name = name
        self.threshold = threshold
        self.delays = delays
        self._clock, self._wall = clock, wall
        self.state_file = Path(state_file) if state_file else None
        self.failures = 0
        self.open_until = 0.0
        self.degraded = False
        self._since: str | None = None
        self._lock = threading.Lock()

    def before(self) -> None:
        """Raise ``CircuitOpen`` while the breaker is open."""
        with self._lock:
            wait = self.open_until - self._clock()
        if wait > 0:
            raise CircuitOpen(f"{self.name} requests are paused for {int(wait) + 1} s after repeated refusals")

    def success(self) -> None:
        with self._lock:
            was = self.degraded
            self.failures, self.open_until, self.degraded = 0, 0.0, False
            if was:
                log.info("%s connection recovered", self.name)
                self._write()

    def failure(self) -> None:
        with self._lock:
            self.failures += 1
            if self.failures < self.threshold:
                return
            delay = self.delays[min(self.failures - self.threshold, len(self.delays) - 1)]
            self.open_until = self._clock() + delay
            if not self.degraded:
                self.degraded = True
                self._since = datetime.now(timezone.utc).isoformat(timespec="seconds")
                log.warning("%s connection degraded", self.name)
            self._write(delay)

    def _write(self, delay: float = 0.0) -> None:
        if self.state_file is None:
            return
        body = {"name": self.name, "degraded": self.degraded, "since": self._since if self.degraded else None,
                "retry_at": self._wall() + delay, "written": self._wall()}
        try:
            from .state import atomic_write
            atomic_write(self.state_file, json.dumps(body))
        except OSError:
            pass


def read_degraded(state_file: Path, now: float | None = None) -> dict[str, Any] | None:
    """The breaker's state file when it says degraded and was written recently, else None."""
    try:
        data = json.loads(Path(state_file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("degraded"):
        return None
    if (now if now is not None else time.time()) - float(data.get("written") or 0) > STALE_AFTER_S:
        return None
    return data


class GuardedSession:
    """Wraps a requests session: ``get`` goes through the breaker; everything else is passed to the session."""

    def __init__(self, inner: Any, breaker: CircuitBreaker):
        self.inner = inner
        self.breaker = breaker

    def get(self, url: str, **kw: Any) -> Any:
        self.breaker.before()
        try:
            resp = self.inner.get(url, **kw)
        except requests.RequestException:
            self.breaker.failure()
            raise
        code = getattr(resp, "status_code", 200)
        if isinstance(code, int) and (code in (403, 429) or code >= 500):
            self.breaker.failure()
        else:
            self.breaker.success()
        return resp

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)
