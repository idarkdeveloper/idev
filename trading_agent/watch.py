"""Always-on local mode: poll deals and corporate announcements during market hours.

Runs the normal check on an interval and, between checks, watches NSE announcements for
the stocks you hold or were recently recommended, notifying on anything new. Meant for a
machine with a fixed IP (the April 2026 SEBI rules) rather than a cron runner.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, time as dtime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .config import Settings
from .state import State

log = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")


class Watcher:
    def __init__(self, settings: Settings, *, every: int = 60, window: tuple[str, str] = ("08:45", "18:30"),
                 tz: ZoneInfo = IST, check_fn: Callable[[], Any] | None = None,
                 data: Any | None = None, broker: Any | None = None, notifier: Any | None = None,
                 weekdays_only: bool = True):
        self.settings = settings
        self.every = max(15, int(every))
        self.window = (dtime.fromisoformat(window[0]), dtime.fromisoformat(window[1]))
        self.tz = tz
        self.weekdays_only = weekdays_only
        self._check_fn = check_fn
        self._data = data
        self._broker = broker
        self._notifier = notifier
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.ticks = 0
        self.last_tick: dict[str, Any] | None = None
        self.started_at: str | None = None

    # -- schedule -------------------------------------------------------------
    def market_window_open(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(self.tz)
        if self.weekdays_only and now.weekday() >= 5:
            return False
        return self.window[0] <= now.time() <= self.window[1]

    # -- one iteration ----------------------------------------------------------
    def interesting_tickers(self) -> list[str]:
        st = State(self.settings.state_dir / "state.json")
        tickers: list[str] = []
        if self._broker is not None:
            try:
                tickers += [p.symbol for p in self._broker.positions()]
            except Exception as e:  # noqa: BLE001
                log.warning("positions unavailable: %s", e)
        tickers += [r["ticker"] for r in st.data.get("recommendations", [])[-10:]
                    if r.get("ticker") and r["ticker"] != "PORTFOLIO"]
        seen: set[str] = set()
        return [t for t in tickers if not (t in seen or seen.add(t))]

    def poll_announcements(self) -> list[dict[str, Any]]:
        if self._data is None or not hasattr(self._data, "announcements"):
            return []
        st = State(self.settings.state_dir / "state.json")
        seen = st.data.setdefault("seen_announcements", {})
        fresh: list[dict[str, Any]] = []
        for t in self.interesting_tickers():
            try:
                rows = self._data.announcements(t, limit=5)
            except Exception as e:  # noqa: BLE001
                log.warning("announcements for %s failed: %s", t, e)
                continue
            for a in rows:
                if a["id"] and a["id"] not in seen:
                    seen[a["id"]] = a["at"]
                    fresh.append(a)
        if fresh:
            st.save()
            if self._notifier is not None:
                body = "\n".join(f"{a['at']} {a['symbol']} [{a['category']}] {a['text']}" for a in fresh)
                self._notifier.send(f"[NEWS] {len(fresh)} new announcement(s)", body)
        return fresh

    def tick(self, force: bool = False) -> dict[str, Any]:
        now = datetime.now(self.tz)
        info: dict[str, Any] = {"at": now.isoformat(timespec="seconds"), "in_window": self.market_window_open(now)}
        if not info["in_window"] and not force:
            info["skipped"] = True
            self.last_tick = info
            return info
        try:
            if self._check_fn is not None:
                result = self._check_fn()
                info["check"] = result.to_dict() if hasattr(result, "to_dict") else result
        except Exception as e:  # noqa: BLE001
            log.exception("watch check failed")
            info["check_error"] = f"{type(e).__name__}: {e}"
        info["new_announcements"] = self.poll_announcements()
        self.ticks += 1
        self.last_tick = info
        return info

    # -- loop / thread ----------------------------------------------------------
    def run_forever(self) -> None:
        self.started_at = datetime.now(self.tz).isoformat(timespec="seconds")
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                log.exception("watch tick failed")
            self._stop.wait(self.every)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self.run_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._stop.is_set())

    def status(self) -> dict[str, Any]:
        return {"on": self.running, "every": self.every, "ticks": self.ticks,
                "window": [self.window[0].isoformat(timespec="minutes"), self.window[1].isoformat(timespec="minutes")],
                "in_window": self.market_window_open(), "started_at": self.started_at,
                "last_tick": self.last_tick}
