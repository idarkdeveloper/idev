"""The paper forward test, run by the watch service once per trading day after the close.

The GitHub Actions routine used to do this (``forward --if-due``); the server is now the only engine. The scheduler
is called from every watch tick and starts one background run per trading day after ``RUN_AFTER`` (16:10 IST). A
claim file made with O_CREAT|O_EXCL in the state directory (``forward_<day>.claim``) stops a restart, or a second
process, from running it twice for the same day; a run that fails is released and retried a few times (10 minutes
apart). The run itself is ``forward --if-due``: it does nothing unless a rebalance is due or today's point is missing.
Nothing here can reach a real broker.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any, Callable

from .timezones import IST

log = logging.getLogger(__name__)

RUN_AFTER = dtime(16, 10)   # IST; the 15:30 close plus the daily-email slot and some settling time
LATEST = dtime(23, 30)      # after this the day is skipped
RETRY_AFTER_S = 600.0
MAX_TRIES = 3


class ForwardScheduler:
    def __init__(self, state_dir: Path, run_fn: Callable[[], Any], *, holidays: Any = None,
                 run_after: dtime = RUN_AFTER, threaded: bool = True, clock: Callable[[], float] = time.monotonic):
        self.state_dir = Path(state_dir)
        self.run_fn = run_fn            # the whole forward --if-due run; may take minutes
        self.holidays = holidays        # NSEHolidays: nothing runs on an exchange holiday
        self.run_after = run_after
        self.threaded = threaded
        self._clock = clock
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._tries: dict[str, int] = {}
        self._retry_at: dict[str, float] = {}
        self._done: set[str] = set()

    def _claim_path(self, day: str) -> Path:
        return self.state_dir / f"forward_{day}.claim"

    def _claim(self, day: str) -> bool:
        path = self._claim_path(day)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except OSError:   # exists (already run today, by us before a restart or by another process) or unwritable
            return False
        with os.fdopen(fd, "w") as f:
            f.write(str(os.getpid()))
        return True

    def _release(self, day: str) -> None:
        try:
            self._claim_path(day).unlink()
        except OSError:
            pass

    def _clean_old_claims(self, today: str) -> None:
        try:
            for f in self.state_dir.glob("forward_*.claim"):
                if f.name[len("forward_"):-len(".claim")] < today:
                    f.unlink(missing_ok=True)
        except OSError:
            pass

    def due(self, now: datetime) -> bool:
        if now.weekday() >= 5 or not (self.run_after <= now.time() <= LATEST):
            return False
        if self.holidays is not None and not self.holidays.is_trading_day(now.date()):
            return False
        return True

    def tick(self, now: datetime | None = None) -> dict[str, Any]:
        """Start today's run if it is time and nobody has. Returns at once; never raises."""
        now = now or datetime.now(IST)
        day = now.date().isoformat()
        try:
            self._clean_old_claims(day)
            if day in self._done or not self.due(now):
                return {"due": False}
            with self._lock:
                if self._thread is not None and self._thread.is_alive():
                    return {"due": True, "running": True}
                if self._tries.get(day, 0) >= MAX_TRIES or self._clock() < self._retry_at.get(day, 0.0):
                    return {"due": True, "waiting": True}
                if not self._claim(day):
                    self._done.add(day)   # claimed already: a run today (or a crashed one, the claim is cleaned tomorrow)
                    return {"due": True, "claimed": False}
                self._tries[day] = self._tries.get(day, 0) + 1
                if self.threaded:
                    self._thread = threading.Thread(target=self._run, args=(day,), daemon=True)
                    self._thread.start()
                    return {"due": True, "started": True}
            self._run(day)
            return {"due": True, "started": True}
        except Exception:  # noqa: BLE001 - never stops the watch
            log.exception("forward test scheduling failed")
            return {"due": False, "error": True}

    def _run(self, day: str) -> None:
        try:
            self.run_fn()
            self._done.add(day)   # the claim stays, so a restart cannot run it again
            log.info("forward test finished for %s", day)
        except BaseException as e:  # noqa: BLE001 - SystemExit from a broken settings read included
            log.warning("forward test failed (%s), try %d of %d", type(e).__name__, self._tries.get(day, 1), MAX_TRIES)
            self._release(day)
            self._retry_at[day] = self._clock() + RETRY_AFTER_S

    def wait(self, timeout: float = 5.0) -> None:
        t = self._thread
        if t is not None:
            t.join(timeout)


def run_forward_due(settings: Any, prices: Any, holidays: Any) -> str:
    """One ``forward --if-due`` run, as the CLI does it. Returns the report text."""
    from .costs import cost_model_for
    from .forward import ForwardTest, format_forward
    from .screen import load_universe, run_screen

    ft = ForwardTest(settings.state_dir, universe=settings.forward_universe, capital=settings.paper_starting_cash,
                     price_fn=prices.latest_price, cost_model=cost_model_for("in"), holidays=holidays)
    if not ft.due():
        return "Forward test: nothing due."

    def screen() -> dict[str, Any]:
        return run_screen(load_universe(ft.universe), prices, top=ft.data["top"])

    return format_forward(ft.run(screen))
