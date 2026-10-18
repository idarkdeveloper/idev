"""The feed the phone app reads: the portfolio as one small JSON file, rebuilt while the market is live.

Read-only. ``AppFeed.tick`` is called by the watch service; it reads Groww only inside the Groww window (through the
injected ``read_fn``), writes ``state/app_feed.json`` at most once per ``settings.app_feed_every_s``, and never raises.
When the read fails or the account is not linked, the last good feed stays and only ``error`` is set."""

from __future__ import annotations

import json
import logging
import math
import os
import threading
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable

from . import groww_hours
from .timezones import IST

log = logging.getLogger(__name__)

FEED_FILE = "app_feed.json"
HISTORY_FILE = "app_value_history.json"


def load_feed(state_dir: Path) -> dict[str, Any] | None:
    try:
        data = json.loads((Path(state_dir) / FEED_FILE).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _write_private(path: Path, body: Any) -> None:
    """Owner-only from the moment the file exists, then swapped in (as runner.save_groww_snapshot does)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(body, indent=2))
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


RECORD_AFTER = time(15, 35)
_RANGE_DAYS = {"1m": 31, "3m": 92, "1y": 366}


def _rows(state_dir: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads((Path(state_dir) / HISTORY_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [r for r in data if isinstance(r, dict) and _good_date(r.get("date")) and _is_number(r.get("value"))]         if isinstance(data, list) else []


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _good_date(v: Any) -> bool:
    try:
        date.fromisoformat(v)
        return True
    except (TypeError, ValueError):
        return False


def record_daily_value(state_dir: Path, feed: dict[str, Any], session: date) -> bool:
    """One row per session date; a second call for the same date replaces it."""
    if not _is_number(feed.get("value")):
        return False   # a live read with no value must not erase the day's good row
    try:
        key = session.isoformat()
        rows = [r for r in _rows(state_dir) if r["date"] != key]
        rows.append({"date": key, "value": feed.get("value"), "cash": feed.get("cash")})
        _write_private(Path(state_dir) / HISTORY_FILE, sorted(rows, key=lambda r: r["date"]))
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("could not record the daily value: %s", e)
        return False


def load_history(state_dir: Path, range_: str) -> list[dict[str, Any]]:
    """Oldest first; ``range_`` is 1m|3m|1y|all, counted back from the newest row."""
    rows = sorted(_rows(state_dir), key=lambda r: r["date"])
    days = _RANGE_DAYS.get(range_)
    if days and rows:
        cut = (date.fromisoformat(rows[-1]["date"]) - timedelta(days=days)).isoformat()
        rows = [r for r in rows if r["date"] > cut]
    return rows


class AppFeed:
    def __init__(self, settings: Any, read_fn: Callable[[], dict[str, Any]], cash_fn: Callable[[], float | None],
                 prev_close_fn: Callable[[str], float | None], trend_fn: Callable[[str], list[float]],
                 holidays: Any | None = None):
        self.settings = settings
        self.read_fn = read_fn
        self.cash_fn = cash_fn
        self.prev_close_fn = prev_close_fn
        self.trend_fn = trend_fn
        self.holidays = holidays
        self._last: datetime | None = None

    def _path(self) -> Path:
        return Path(self.settings.state_dir) / FEED_FILE

    def _holding(self, h: dict[str, Any]) -> dict[str, Any]:
        sym, qty, price = h["symbol"], h["qty"], h.get("price")
        day = day_pct = None
        try:
            prev = self.prev_close_fn(sym)
            if prev and price is not None:
                day, day_pct = round((price - prev) * qty, 2), price / prev - 1
        except Exception as e:  # noqa: BLE001 - one bad quote must not hide the rest
            log.warning("no previous close for %s: %s", sym, e)
        try:
            trend = list(self.trend_fn(sym))
        except Exception as e:  # noqa: BLE001
            log.warning("no trend for %s: %s", sym, e)
            trend = []
        return {"symbol": sym, "name": h.get("name"), "qty": qty, "avg_price": h.get("avg_price"), "price": price,
                "value": h.get("value"), "pl": h.get("pl"), "pl_pct": h.get("pl_pct"),
                "day_change": day, "day_change_pct": day_pct, "trend": trend}

    def _cash(self) -> float | None:
        try:
            return self.cash_fn()
        except Exception as e:  # noqa: BLE001
            log.warning("could not read the cash balance: %s", e)
            return None

    def _build(self, out: dict[str, Any], now: datetime) -> dict[str, Any]:
        rows = [self._holding(h) for h in out.get("holdings") or []]
        with_day = [(r, r["price"] / (1 + r["day_change_pct"])) for r in rows if r["day_change"] is not None]
        day = round(sum(r["day_change"] for r, _ in with_day), 2) if with_day else None
        base = sum(prev * r["qty"] for r, prev in with_day)
        return {"as_of": now.isoformat(timespec="seconds"), "age_s": 0, "stale": False,
                "market_closed": bool(out.get("market_closed")), "source": out.get("source") or "groww",
                "cash": self._cash(), "value": out.get("value"), "invested": out.get("invested"),
                "pl": out.get("pl"), "pl_pct": out.get("pl_pct"),
                "day_change": day, "day_change_pct": day / base if day is not None and base else None,
                "holdings": rows, "unpriced": list(out.get("unpriced") or []), "error": None}

    def _failed(self, text: str, now: datetime) -> dict[str, Any]:
        """The last good feed with only ``error`` changed (an empty feed when there is none yet)."""
        feed = load_feed(self.settings.state_dir) or {
            "as_of": now.isoformat(timespec="seconds"), "age_s": 0, "stale": True, "market_closed": False,
            "source": "none", "cash": None, "value": None, "invested": None, "pl": None, "pl_pct": None,
            "day_change": None, "day_change_pct": None, "holdings": [], "unpriced": [], "error": None}
        feed["error"] = text
        return feed

    def tick(self, now: datetime | None = None) -> dict[str, Any] | None:
        try:
            now = (now or groww_hours.now_ist()).astimezone(IST)
            cal = self.holidays if self.holidays is not None else groww_hours.default_calendar(self.settings)
            if not groww_hours.window_open(self.settings, now, cal):
                return None
            every = float(getattr(self.settings, "app_feed_every_s", 60))
            if self._last is not None and (now - self._last).total_seconds() < every:
                return None
            self._last = now
            try:
                out = self.read_fn()
                err = None if out.get("linked") and not out.get("error") else (
                    str(out.get("error") or "Groww is not linked"))
            except Exception as e:  # noqa: BLE001 - the text may carry secrets, so only the type is kept
                log.warning("app feed read failed: %s", e)
                out, err = {}, f"read failed ({type(e).__name__})"
            feed = self._failed(err, now) if err else self._build(out, now)
            _write_private(self._path(), feed)
            if not err and feed["source"] == "groww" and now.time() >= RECORD_AFTER:
                record_daily_value(self.settings.state_dir, feed, now.date())
            return feed
        except Exception as e:  # noqa: BLE001 - never stops the watch
            log.warning("app feed tick failed: %s", e)
            return None
