"""Always-on local mode: poll deals and corporate announcements during market hours.

Runs the normal check on an interval and, between checks, watches NSE announcements for
the stocks you hold or were recently recommended, notifying on anything new. Meant for a
machine with a fixed IP (the April 2026 SEBI rules) rather than a cron runner.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, time as dtime, tzinfo
from typing import Any, Callable

from .config import Settings
from .risk import check_stops
from .state import State
from .timezones import IST

log = logging.getLogger(__name__)




class Watcher:
    def __init__(self, settings: Settings, *, every: int = 60, window: tuple[str, str] = ("08:45", "18:30"),
                 tz: tzinfo = IST, check_fn: Callable[[], Any] | None = None,
                 data: Any | None = None, broker: Any | None = None, notifier: Any | None = None,
                 weekdays_only: bool = True, prices: Any | None = None, auto_exit: bool = False):
        self.settings = settings
        self._prices = prices  # object with .history(symbol, range) for ATR-based stops
        self.auto_exit = auto_exit  # sell paper positions that hit their trailing stop
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

    def check_trailing_stops(self) -> list[dict[str, Any]]:
        if self._broker is None:
            return []
        try:
            positions = self._broker.positions()
        except Exception as e:  # noqa: BLE001
            log.warning("positions unavailable for stop check: %s", e)
            return []
        bars_fn = (lambda sym: self._prices.history(sym, "1y")) if self._prices is not None else (lambda sym: [])
        hits = check_stops(positions, bars_fn)
        if not hits:
            return []
        st = State(self.settings.state_dir / "state.json")
        alerted = st.data.setdefault("stop_alerts", {})
        fresh = []
        for h in hits:
            key = f"{h['symbol']}:{round(h['stop'], 2)}"
            if key in alerted:
                continue
            alerted[key] = h["price"]
            if self.auto_exit and getattr(self._broker, "name", "").startswith("local-paper"):
                try:
                    h["order"] = self._broker.submit_order(h["symbol"], "sell", qty=h["qty"])
                except Exception as e:  # noqa: BLE001
                    h["order_error"] = str(e)
            fresh.append(h)
        st.save()
        if fresh and self._notifier is not None:
            body = "\n".join(f"{h['symbol']}: {h['price']:.2f} at/below trailing stop {h['stop']:.2f} "
                             f"({h['drawdown_from_high']*100:+.1f}% from high)"
                             + (" - paper SOLD" if h.get("order") else "") for h in fresh)
            self._notifier.send(f"[STOP] {len(fresh)} position(s) hit trailing stop", body)
        return fresh

    def sync_live(self) -> dict[str, Any] | None:
        """Live Groww only: re-check open orders and keep GTT stop-losses in line."""
        from .live import is_live_broker, refresh_open_orders, sync_gtt_stops

        if not is_live_broker(self._broker):
            return None
        st = State(self.settings.state_dir / "state.json")
        out: dict[str, Any] = {}
        try:
            out["orders"] = refresh_open_orders(self._broker, st, self._notifier)
        except Exception as e:  # noqa: BLE001
            out["orders_error"] = str(e)
        bars_fn = (lambda sym: self._prices.history(sym, "1y")) if self._prices is not None else None
        out["gtt"] = sync_gtt_stops(self.settings, self._broker, st, bars_fn=bars_fn, notifier=self._notifier)
        st.save()
        return out

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
        info["stop_hits"] = self.check_trailing_stops()
        live = self.sync_live()
        if live is not None:
            info["live"] = live
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
