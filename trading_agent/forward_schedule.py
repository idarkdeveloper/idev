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
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Callable

from .timezones import IST

log = logging.getLogger(__name__)

RUN_AFTER = dtime(16, 10)   # IST; the 15:30 close plus the daily-email slot and some settling time
LATEST = dtime(23, 30)      # after this the day is skipped
RETRY_AFTER_S = 600.0
MAX_TRIES = 3
PRUNE_DAYS = 7
STALE_AFTER_S = 30 * 60.0   # a claim older than this whose process is gone is a crashed run


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
                f.write(f"{os.getpid()} {int(time.time())}")
            return True
        return False

    @staticmethod
    def _pid_gone(pid: int) -> bool:
        if os.name == "nt":   # os.kill(pid, 0) would terminate the process on Windows: go by age alone there
            return True
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except OSError:
            return False   # exists but is not ours
        return False

    def _take_over_stale(self, path: Path) -> bool:
        """A claim from a crashed run: older than 30 minutes and its process gone. The file is renamed to a name of
        our own first, so only one contender wins; a file that turns out fresh is put back."""
        try:
            text = path.read_text(encoding="utf-8").split()
            pid, at = int(text[0]), float(text[1])
        except (OSError, ValueError, IndexError):
            return False   # unreadable or an old-format claim: leave it
        if time.time() - at <= STALE_AFTER_S or not self._pid_gone(pid):
            return False
        mine = path.with_name(f"{path.name}.stale.{os.getpid()}.{threading.get_ident()}")
        try:
            os.replace(path, mine)
        except OSError:
            return False
        mine.unlink(missing_ok=True)
        return True

    def _release(self, day: str) -> None:
        try:
            self._claim_path(day).unlink()
        except OSError:
            pass

    def _clean_old_claims(self, today: str) -> None:
        """Once a day: forward claim files and Telegram send markers older than 7 days are removed."""
        marker = self.state_dir / f"prune_{today}.done"
        if marker.exists():
            return
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            cutoff = (date.fromisoformat(today) - timedelta(days=PRUNE_DAYS)).isoformat()
            for f in self.state_dir.glob("forward_*.claim"):
                if f.name[len("forward_"):-len(".claim")] < cutoff:
                    f.unlink(missing_ok=True)
            limit = time.time() - PRUNE_DAYS * 86400
            for f in (self.state_dir / "telegram_sent").glob("*"):
                if f.is_file() and f.stat().st_mtime < limit:
                    f.unlink(missing_ok=True)
            for old in self.state_dir.glob("prune_*.done"):
                old.unlink(missing_ok=True)
            marker.write_text("done", encoding="utf-8")
        except (OSError, ValueError):
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


def rebuild_for_settings(settings: Any, prices: Any, holidays: Any, asof: str, *, universe: str | None = None,
                         top: int = 20, capital: float | None = None, force: bool = False,
                         progress: Callable[[str], Any] | None = None) -> dict[str, Any]:
    """The real wiring of ``forward.rebuild_from`` (current universe CSV for names, point-in-time membership, the
    factor screen on as-of prices). Used by ``forward --rebuild-from`` and by FORWARD_START on the server."""
    from .costs import cost_model_for
    from .forward import rebuild_from
    from .index_history import point_in_time
    from .screen import load_universe, run_screen

    universe = (universe or settings.forward_universe).upper()
    cache: dict[str, list[dict[str, str]]] = {}

    def members_loader() -> list[dict[str, str]]:
        if "m" not in cache:
            cache["m"] = load_universe(universe)
        return cache["m"]

    def screen(asof_prices: Any, members: list[dict[str, str]]) -> dict[str, Any]:
        if progress:
            progress(f"Ranking {len(members)} {universe} members as of {asof}...")
        return run_screen(members, asof_prices, top=top)

    membership = point_in_time(universe, [m["symbol"] for m in members_loader()], settings.state_dir, progress=progress)
    return rebuild_from(settings.state_dir, asof, universe=universe, top=top,
                        capital=capital or settings.paper_starting_cash, prices=prices, cost_model=cost_model_for("in"),
                        screen_fn=screen, members_loader=members_loader, membership=membership, holidays=holidays,
                        force=force)


def _alert_once_a_day(settings: Any, notifier: Any, message: str, today: str) -> None:
    marker = Path(settings.state_dir) / f"forward_alert_{today}.sent"
    if marker.exists():
        return
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("sent", encoding="utf-8")
        for old in marker.parent.glob("forward_alert_*.sent"):
            if old != marker:
                old.unlink(missing_ok=True)
    except OSError:
        pass
    if notifier is not None:
        try:
            notifier.send("[FORWARD] paper forward test is not running", message)
        except Exception:  # noqa: BLE001
            log.warning("could not send the forward-test alert")


def run_forward_due(settings: Any, prices: Any, holidays: Any, notifier: Any = None, *,
                    today: date | None = None) -> str:
    """One ``forward --if-due`` run, as the CLI does it. With no forward account yet it never starts a fresh one
    silently: with FORWARD_START (earlier than today) it rebuilds from that date first; otherwise it refuses, says so
    in its report and alerts once a day. Returns the report text."""
    from .costs import cost_model_for
    from .forward import ForwardTest, format_forward, forward_exists, format_rebuild
    from .screen import load_universe, run_screen

    today = today or datetime.now(IST).date()
    prefix = ""
    if not forward_exists(settings.state_dir, settings.forward_universe):
        start = getattr(settings, "forward_start", None)
        if start and start < today.isoformat():
            try:
                prefix = format_rebuild(rebuild_for_settings(settings, prices, holidays, start)) + "\n"
            except Exception as e:  # noqa: BLE001 - FileExistsError cannot happen here; the rest is reported
                msg = f"the forward account could not be rebuilt from FORWARD_START={start}: {type(e).__name__}: {e}"
                _alert_once_a_day(settings, notifier, msg, today.isoformat())
                return msg
        else:
            msg = "no forward account; run forward --rebuild-from DATE or set FORWARD_START"
            _alert_once_a_day(settings, notifier, msg + " (on the server: forward --rebuild-from 2026-10-09).",
                              today.isoformat())
            return msg
    ft = ForwardTest(settings.state_dir, universe=settings.forward_universe, capital=settings.paper_starting_cash,
                     price_fn=prices.latest_price, cost_model=cost_model_for("in"), holidays=holidays)
    if not ft.due():
        return prefix + "Forward test: nothing due."

    def screen() -> dict[str, Any]:
        from .bands import book_for   # registered change: 2% / 5% band stocks are not picked (date kept in the forward state)
        return run_screen(load_universe(ft.universe), prices, top=ft.data["top"], bands=book_for(settings))

    return prefix + format_forward(ft.run(screen))
