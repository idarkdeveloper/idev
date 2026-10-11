"""Groww login counter: every token request this agent SENDS to Groww and what came back.

Groww allows 150 access-token generations a day per API key (the Trade API limit; signing in to the Groww app or website
does not use it). This module keeps a small append-only log next to the token cache, one JSON line per request:
``{"at": "<IST time>", "host": "server" | "laptop", "result": "ok" | "refused-429" | "error-401" | "network error" ...}``.
Never the token, the key, the secret, the TOTP or the reply text.

The day runs from 06:00 IST (when Groww's tokens roll over) to 06:00 the next day. The token code asks
``hard_stop`` before it sends: once 145 requests went out in the day it refuses to send more until 06:00.
Counts are of this machine's own requests only (server and laptop each keep their own log).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any

from .timezones import IST

log = logging.getLogger(__name__)

LOG_FILE = "groww_logins.jsonl"
DAY_START = dtime(6, 0)
LIMIT = 150      # Groww's daily token limit per API key
WARN_AT = 100    # amber on the Health tab
RED_AT = 140     # red
STOP_AT = 145    # the token code sends no more requests today
KEEP_LINES = 2000   # the log is trimmed to its newest lines when it grows past twice this
RESULTS = ("ok", "refused-429")   # anything else is "error-<status>" or "network error"


def log_path(state_dir: Any) -> Path:
    return Path(state_dir) / LOG_FILE


def day_start(now: datetime) -> datetime:
    """The latest 06:00 IST at or before ``now``: the start of the Groww token day."""
    n = now.astimezone(IST)
    start = datetime.combine(n.date(), DAY_START, tzinfo=IST)
    return start if n >= start else start - timedelta(days=1)


def result_of(exc: BaseException | None) -> str:
    """'ok', 'refused-429', 'error-401', 'error-no-token' or 'network error' for one request's outcome."""
    if exc is None:
        return "ok"
    status = getattr(exc, "status", None)
    if isinstance(status, int):
        return "refused-429" if status == 429 else f"error-{status}"
    if isinstance(exc, RuntimeError):
        return "error-no-token"   # Groww answered but sent no token
    return "network error"


def record(path: Path, now: datetime, host: str, result: str) -> None:
    """Append one request to the log. Never raises: a full disk must not cost the token itself."""
    line = json.dumps({"at": now.astimezone(IST).isoformat(timespec="seconds"), "host": host or "laptop", "result": result})
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        _trim(path)
    except (OSError, ValueError):
        log.warning("Could not write the Groww login log %s", path)


def _trim(path: Path) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    if len(lines) > 2 * KEEP_LINES:
        from .state import atomic_write
        atomic_write(path, "\n".join(lines[-KEEP_LINES:]) + "\n")


def read(path: Path) -> list[dict[str, Any]]:
    """Every readable entry, oldest first (unreadable lines are skipped)."""
    try:
        raw = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for ln in raw:
        try:
            e = json.loads(ln)
            at = datetime.fromisoformat(str(e["at"]))
        except (ValueError, KeyError, TypeError):
            continue
        out.append({"at": (at if at.tzinfo else at.replace(tzinfo=IST)).astimezone(IST), "host": str(e.get("host") or ""),
                    "result": str(e.get("result") or "")})
    return out


def sent_today(path: Path, now: datetime) -> int:
    start = day_start(now)
    return sum(1 for e in read(path) if e["at"] >= start)


def hard_stop(path: Path, now: datetime) -> bool:
    """True once STOP_AT requests were sent since 06:00 IST: the token code must not send another today."""
    return sent_today(path, now) >= STOP_AT


def level(sent: int) -> str:
    return "stopped" if sent >= STOP_AT else "red" if sent >= RED_AT else "amber" if sent >= WARN_AT else "ok"


def summary(path: Path, now: datetime) -> dict[str, Any]:
    """Today's counts (since 06:00 IST), the level, and the last 10 requests (newest first)."""
    start = day_start(now)
    entries = read(path)
    today = [e for e in entries if e["at"] >= start]
    sent = len(today)
    ok = sum(1 for e in today if e["result"] == "ok")
    refused = sum(1 for e in today if e["result"] == "refused-429")
    return {"since": start.isoformat(timespec="minutes"), "sent": sent, "ok": ok, "refused": refused,
            "errors": sent - ok - refused, "limit": LIMIT, "warn_at": WARN_AT, "red_at": RED_AT, "stop_at": STOP_AT,
            "level": level(sent), "resets_at": (start + timedelta(days=1)).isoformat(timespec="minutes"),
            "last": [{"at": e["at"].isoformat(timespec="seconds"), "host": e["host"], "result": e["result"]}
                     for e in reversed(entries[-10:])]}
