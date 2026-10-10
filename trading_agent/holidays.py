"""NSE trading holidays, from the exchange's own list.

NSE publishes the year's equity (CM segment) trading holidays at ``api/holiday-master``.
The list is cached on disk for a week, so the routine and watch mode don't ask every run.
If NSE can't be reached, weekdays count as trading days, as before this module existed,
and the reason is kept in ``error`` so callers can say so.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .timezones import IST

log = logging.getLogger(__name__)


def _parse(d: str) -> str | None:
    try:
        return datetime.strptime(d.strip(), "%d-%b-%Y").date().isoformat()
    except (ValueError, AttributeError):
        return None


class NSEHolidays:
    def __init__(self, client: Any | None = None, cache_dir: Path | None = None, max_age_s: float = 7 * 86400):
        self._client = client
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.max_age_s = max_age_s
        self._days: dict[str, str] | None = None
        self._failed_at: float | None = None
        self.error: str | None = None

    def _fetch(self) -> dict[str, str]:
        if self._client is None:
            from .nse import NSEClient
            self._client = NSEClient(timeout=45)  # NSE is slow to answer this list
        c = self._client
        data = c._get("api/holiday-master", params={"type": "trading"},
                      referer=f"{c.base_url}/resources/exchange-communication-holidays")
        out = {}
        for row in (data or {}).get("CM", []):
            day = _parse(str(row.get("tradingDate") or ""))
            if day:
                out[day] = str(row.get("description") or "NSE holiday").strip().rstrip("*").strip()
        if not out:
            raise ValueError("NSE returned no equity holidays")
        return out

    def days(self) -> dict[str, str]:
        """{ISO date: holiday name} for the years NSE has published (usually the current one)."""
        if self._days is not None and (self._failed_at is None or time.time() - self._failed_at < 3600):
            return self._days  # after a failure, try NSE again an hour later
        path = self.cache_dir / "nse_holidays.json" if self.cache_dir else None
        if path and path.exists() and time.time() - path.stat().st_mtime < self.max_age_s:
            try:
                self._days = json.loads(path.read_text(encoding="utf-8"))
                return self._days
            except ValueError:
                pass
        try:
            self._days = self._fetch()
            self._failed_at, self.error = None, None
            if path:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(self._days, indent=1), encoding="utf-8")
        except Exception as e:  # noqa: BLE001 - fall back to weekdays; never stop the agent
            self._failed_at = time.time()
            self.error = f"NSE holiday list unavailable ({e}); treating weekdays as trading days"
            log.warning(self.error)
            stale = None
            if path and path.exists():
                try:
                    stale = json.loads(path.read_text(encoding="utf-8"))
                except ValueError:
                    stale = None
            self._days = stale or {}
        return self._days

    def peek_trading_day(self, day: date) -> bool:
        """Like ``is_trading_day`` but from what is already loaded or cached on disk (any age): never fetches, never
        blocks. For threads that must not touch the network; weekdays count when nothing is known."""
        days = self._days
        if days is None:
            path = self.cache_dir / "nse_holidays.json" if self.cache_dir else None
            try:
                days = json.loads(path.read_text(encoding="utf-8")) if path and path.exists() else {}
            except (OSError, ValueError):
                days = {}
        return day.weekday() < 5 and day.isoformat() not in days

    def holiday(self, day: date) -> str | None:
        """The holiday's name if NSE is closed for one on ``day`` (weekends aside)."""
        return self.days().get(day.isoformat())

    def is_trading_day(self, day: date) -> bool:
        return day.weekday() < 5 and self.holiday(day) is None

    def today(self, now: datetime | None = None) -> dict[str, Any]:
        """What the dashboard shows: is NSE open today, and if not, why."""
        d = (now or datetime.now(IST)).date()
        name = self.holiday(d)
        if d.weekday() >= 5:
            return {"date": d.isoformat(), "open": False, "reason": "weekend", "holiday": name}
        return {"date": d.isoformat(), "open": name is None, "reason": "holiday" if name else None,
                "holiday": name, "warning": self.error}
