"""NSE announcements as they stood on the replay date."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from ..filing_time import usable_by
from .clock import ReplayClock


class ClockedNews:
    def __init__(self, client: Any, clock: ReplayClock, holidays: Any | None = None):
        self.client, self.clock, self.holidays = client, clock, holidays

    def for_symbol(self, symbol: str, days: int = 60, until: str | None = None) -> dict[str, Any]:
        until = until or self.clock.today
        self.clock.check(until)
        since = (date.fromisoformat(until) - timedelta(days=days)).isoformat()
        try:
            rows = self.client.announcement_history(symbol)
        except Exception as e:  # noqa: BLE001 - NSE throttles; missing news must show, not break a step
            return {"items": [], "error": f"news unavailable for this period ({e})"}
        # An announcement disseminated at or after 15:00 IST on day T is only usable from the next trading day
        # (filing_time.usable_from), so a replay clock on T must not see it.
        items = [a for a in rows if since < a["at"][:10] <= until and usable_by(a["at"], until, self.holidays)]
        return {"items": items, "error": None}
