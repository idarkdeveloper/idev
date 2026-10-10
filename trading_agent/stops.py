"""Practice stop-losses: a GTT-like stop on the practice account, checked by the dashboard process.

The real GTT stop at Groww (Live page, ``GROWW_GTT_STOPS``) is a separate thing and is not touched here. This
checker only ever uses the practice ``LocalPaperBroker`` it is given: it has no Groww client and builds no
notifier. Every minute in NSE market hours (09:15 to 15:30 IST on a trading day) it sells, at the latest price,
any practice position whose price is at or below its stop (``risk.position_stop``), and records the fill in
``state.json`` under ``practice_stop_fills``. The broker's ``sell_if_stopped`` re-checks inside its lock, so the
checker, a second checker and watch-mode auto-exit do not sell the same position twice: the re-check runs under the
broker's in-process lock and a lock file on the account file, and reads the file afresh, so it also holds against a
separate ``watch`` process using the same practice account file.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, time as dtime, tzinfo
from pathlib import Path
from typing import Any, Callable

from .risk import position_stop
from .state import STATE_LOCK, State
from .timezones import IST

log = logging.getLogger(__name__)

OPEN, CLOSE = dtime(9, 15), dtime(15, 30)
FILLS_KEY = "practice_stop_fills"


def record_stop_fill(state_path: Path, order: dict[str, Any], stop: dict[str, Any]) -> dict[str, Any]:
    """Append a stop fill to state.json (load, add, save under the state lock, so other writes are kept)."""
    fill = {"at": order.get("filled_at"), "symbol": order["symbol"], "qty": order["qty"],
            "price": order["filled_avg_price"], "stop": round(stop["level"], 2), "type": stop["type"],
            "label": stop.get("label", stop["type"])}
    with STATE_LOCK:
        st = State(state_path)
        st.data[FILLS_KEY] = (st.data.get(FILLS_KEY, []) + [fill])[-200:]
        st.save()
    return fill


class PracticeStopChecker:
    def __init__(self, broker_fn: Callable[[], Any], state_path: Path, *, bars_fn: Callable[[str], Any] | None = None,
                 holidays: Any | None = None, every: int = 60, tz: tzinfo = IST,
                 now_fn: Callable[[], datetime] | None = None, after_fill: Callable[[], Any] | None = None,
                 enabled_fn: Callable[[], bool] | None = None):
        self._broker_fn = broker_fn  # called each tick, so the practice account is opened lazily
        self.state_path = Path(state_path)
        self._bars_fn = bars_fn
        self.holidays = holidays
        self.every = max(15, int(every))
        self.tz = tz
        self._now_fn = now_fn
        self._after_fill = after_fill
        self._enabled_fn = enabled_fn  # read every tick: e.g. the dashboard's market is still India
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error: str | None = None

    def market_open(self, now: datetime | None = None) -> bool:
        now = now or (self._now_fn() if self._now_fn else datetime.now(self.tz))
        if now.tzinfo is not None:
            now = now.astimezone(self.tz)
        if now.weekday() >= 5:
            return False
        # The holiday list fails open: if a holiday is unknown to it, that weekday counts as a trading day. That is
        # harmless here, since prices do not move on a holiday and so nothing reaches a stop.
        if self.holidays is not None and not self.holidays.is_trading_day(now.date()):
            return False
        return OPEN <= now.time() <= CLOSE

    def check_once(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """One pass. Returns the fills made (empty outside market hours)."""
        if self._enabled_fn is not None and not self._enabled_fn():
            return []
        if not self.market_open(now):
            return []
        broker = self._broker_fn()
        fills: list[dict[str, Any]] = []
        for p in broker.positions():
            if p.current_price is None:
                continue
            bars: list[dict[str, Any]] = []
            if (p.stop_type or "trailing") == "trailing" and self._bars_fn is not None:
                try:
                    bars = self._bars_fn(p.symbol)
                except Exception as e:  # noqa: BLE001 - no bars: the 15% rule alone
                    log.debug("no bars for %s: %s", p.symbol, e)
            stop = position_stop(p, bars)
            if stop["level"] is None or p.current_price > stop["level"]:
                continue
            try:
                order = broker.sell_if_stopped(p.symbol, qty=p.qty, level=stop["level"],
                                               stop={"type": stop["type"], "value": stop["value"]},
                                               extra={"stop_level": round(stop["level"], 2), "stop_type": stop["type"]})
            except Exception as e:  # noqa: BLE001 - e.g. a price outage: try again next minute
                log.warning("practice stop sell for %s failed: %s", p.symbol, e)
                continue
            if order is None:
                continue
            fills.append(record_stop_fill(self.state_path, order, stop))
        if fills and self._after_fill is not None:
            try:
                self._after_fill()
            except Exception as e:  # noqa: BLE001
                log.debug("after-fill hook failed: %s", e)
        return fills

    # -- thread ---------------------------------------------------------------
    def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                self.check_once()
                self.last_error = None
            except Exception as e:  # noqa: BLE001 - never let the thread die
                self.last_error = f"{type(e).__name__}: {e}"
                log.warning("practice stop check failed: %s", e)
            self._stop.wait(self.every)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self.run_forever, daemon=True, name="practice-stops")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._stop.is_set())
