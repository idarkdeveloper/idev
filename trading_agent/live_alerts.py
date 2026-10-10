"""Once-a-day alerts for live-sell problems (DDPI not confirmed, DDPI/e-DIS rejection, T1-only holdings).

The marks live in their own small file (``live_alerts.json`` in the state directory) so they never
contend with ``state.json``. The same key alerts at most once per IST calendar day, across restarts and
across processes (a file lock). The day is marked only after the alert was sent, so a failed send retries.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .news import _file_lock
from .timezones import IST

log = logging.getLogger(__name__)
_LOCK = threading.Lock()


def alert_once(path: Path, key: str, send: Callable[[str, str], Any] | None, subject: str, body: str,
               now: datetime | None = None) -> bool:
    """Send ``subject``/``body`` unless ``key`` already alerted today (IST). True when it was sent."""
    day = (now or datetime.now(IST)).astimezone(IST).strftime("%Y-%m-%d")
    path = Path(path)
    try:
        with _LOCK, _file_lock(path.with_name(path.name + ".lock"), what="live alert marks"):
            try:
                marks = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
                if not isinstance(marks, dict):
                    marks = {}
            except (OSError, ValueError):
                marks = {}
            if marks.get(key) == day:
                return False
            if send is not None:
                try:
                    send(subject, body)
                except Exception as e:  # noqa: BLE001 - not marked, so the next call tries again
                    log.warning("alert %s not delivered: %s", key, e)
                    return False
            marks = {k: v for k, v in marks.items() if v == day}  # drop yesterday's marks
            marks[key] = day
            try:
                path.write_text(json.dumps(marks), encoding="utf-8")
            except OSError as e:
                log.warning("could not record the alert mark %s: %s", key, e)
            return True
    except TimeoutError as e:
        log.warning("alert %s skipped: %s", key, e)
        return False


def make_alert_fn(state_dir: Path, send: Callable[[str, str], Any] | None) -> Callable[[str, str, str], bool]:
    path = Path(state_dir) / "live_alerts.json"
    return lambda key, subject, body: alert_once(path, key, send, subject, body)
