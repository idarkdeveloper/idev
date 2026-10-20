"""One saved portfolio value per trading day, so the Portfolio page can draw the real curve as days go by.

Groww gives no history of your portfolio value, only today's holdings. So each priced read of the holdings writes (or
overwrites) one line for its trading day; the last write of a day is the close. Read-only data: it never touches Groww
or an order, it only keeps numbers the page has already shown."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .state import atomic_write

FILE = "equity_history.json"
KEEP_DAYS = 1000   # about four trading years


def read_days(state_dir: Path) -> list[dict[str, Any]]:
    """[{day, value, invested}] oldest first; an unreadable file is an empty history, never an error."""
    try:
        days = json.loads((Path(state_dir) / FILE).read_text(encoding="utf-8")).get("days", {})
    except (OSError, ValueError, AttributeError):
        return []
    return [{"day": d, "value": v.get("value"), "invested": v.get("invested")} for d, v in sorted(days.items()) if isinstance(v, dict)]


def record_day(state_dir: Path, day: str, value: float, invested: float | None) -> None:
    """Save ``value`` for ``day`` (YYYY-MM-DD), replacing that day's earlier figure."""
    days = {r["day"]: {"value": r["value"], "invested": r["invested"]} for r in read_days(state_dir)}
    days[day] = {"value": round(float(value), 2), "invested": None if invested is None else round(float(invested), 2)}
    keep = dict(sorted(days.items())[-KEEP_DAYS:])
    atomic_write(Path(state_dir) / FILE, json.dumps({"days": keep}, indent=1))
