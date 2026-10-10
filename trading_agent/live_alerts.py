"""Once-a-day alerts for live-sell problems (DDPI not confirmed, DDPI/e-DIS rejection, T1-only holdings).

The marks live in their own small file (``live_alerts.json`` in the state directory) so they never
contend with ``state.json``. The same key alerts at most once per IST calendar day, across restarts and
across processes. No lock is held while sending: the key is claimed under the locks (a short-lived claim
in the file, so another process also holds off), the locks are released, the alert is sent, and the day
is marked only on success. A failed send releases the claim so the next call retries.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .news import _file_lock
from .timezones import IST

log = logging.getLogger(__name__)
_LOCK = threading.Lock()
CLAIM_SECONDS = 120  # a claim older than this (a crashed sender) is ignored


def _read(path: Path) -> dict[str, Any]:
    try:
        marks = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        return {}
    return marks if isinstance(marks, dict) else {}


def _write(path: Path, marks: dict[str, Any]) -> None:
    try:
        path.write_text(json.dumps(marks), encoding="utf-8")
    except OSError as e:
        log.warning("could not record the alert marks: %s", e)


def _locked(path: Path) -> ExitStack:
    stack = ExitStack()
    stack.enter_context(_LOCK)
    try:
        stack.enter_context(_file_lock(path.with_name(path.name + ".lock"), what="live alert marks"))
    except BaseException:
        stack.close()
        raise
    return stack


def _claimed(v: Any, day: str, now: float) -> bool:
    return isinstance(v, dict) and v.get("day") == day and now - float(v.get("at", 0)) < CLAIM_SECONDS


def alert_once(path: Path, key: str, send: Callable[[str, str], Any] | None, subject: str, body: str,
               now: datetime | None = None) -> bool:
    """Send ``subject``/``body`` unless ``key`` already alerted today (IST). True when it was sent."""
    day = (now or datetime.now(IST)).astimezone(IST).strftime("%Y-%m-%d")
    path = Path(path)
    clock = time.time()
    try:
        with _locked(path):  # 1. check and claim, briefly
            marks = _read(path)
            if marks.get(key) == day or _claimed(marks.get(key), day, clock):
                return False
            marks = {k: v for k, v in marks.items() if v == day or _claimed(v, day, clock)}
            marks[key] = {"day": day, "at": clock}
            _write(path, marks)
    except TimeoutError as e:
        log.warning("alert %s skipped: %s", key, e)
        return False

    ok = True
    if send is not None:  # 2. send with no lock held
        try:
            send(subject, body)
        except Exception as e:  # noqa: BLE001
            log.warning("alert %s not delivered: %s", key, e)
            ok = False

    try:
        with _locked(path):  # 3. mark done, or release the claim
            marks = _read(path)
            if ok:
                marks[key] = day
            else:
                marks.pop(key, None)
            _write(path, marks)
    except TimeoutError as e:
        log.warning("alert %s: could not update the marks: %s", key, e)
    return ok


def make_alert_fn(state_dir: Path, send: Callable[[str, str], Any] | None) -> Callable[[str, str, str], bool]:
    path = Path(state_dir) / "live_alerts.json"
    return lambda key, subject, body: alert_once(path, key, send, subject, body)
