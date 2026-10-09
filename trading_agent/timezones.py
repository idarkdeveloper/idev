"""India Standard Time that works everywhere.

Windows Python ships without a time-zone database, so ZoneInfo("Asia/Kolkata") fails there
unless the `tzdata` package is installed. India has no daylight saving, so a fixed UTC+05:30
is exactly equivalent and is used as the fallback.
"""

from __future__ import annotations

from datetime import timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def india_tz() -> tzinfo:
    try:
        return ZoneInfo("Asia/Kolkata")
    except ZoneInfoNotFoundError:
        return timezone(timedelta(hours=5, minutes=30), "IST")


IST = india_tz()
