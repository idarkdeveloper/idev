"""Is the server clock right? TOTP logins (Groww) fail when it drifts.

Linux only: ``timedatectl show -p NTPSynchronized`` for the sync flag, and the offset from ``timedatectl
timesync-status`` (systemd-timesyncd) or ``chronyc tracking`` when one of them is available. On Windows, or without
``timedatectl``, nothing is checked and the answer says so. The check fails when NTP is not synchronised or the offset
is more than one second. Run at watch start-up (one alert a day) and as a step of the integration check.
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .timezones import IST

log = logging.getLogger(__name__)

MAX_OFFSET_S = 1.0
Runner = Callable[[list[str]], "tuple[int, str] | None"]


def _run(cmd: list[str]) -> tuple[int, str] | None:
    """(return code, stdout) or None when the command does not exist or hangs."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    except (OSError, ValueError, UnicodeDecodeError, subprocess.SubprocessError):
        return None
    return p.returncode, p.stdout


_UNIT = {"us": 1e-6, "µs": 1e-6, "ms": 1e-3, "s": 1.0, "min": 60.0}


def _offset_timesyncd(text: str) -> float | None:
    """Seconds from "Offset: +1min 2.345s" / "-12us"; None when there is no Offset line; ValueError when there is one
    that cannot be read."""
    m = re.search(r"Offset:[ \t]*(.+)", text)
    if not m:
        return None
    line = m.group(1).strip()
    parts = re.findall(r"([\d.]+)\s*(min|µs|us|ms|s)\b", line)
    if not parts:
        raise ValueError("offset unreadable")
    total = sum(float(n) * _UNIT[u] for n, u in parts)
    return -total if line.startswith("-") else total


def _offset_chrony(text: str) -> float | None:
    if "System time" not in text:
        return None
    m = re.search(r"System time\s*:\s*([\d.]+)\s+seconds\s+(fast|slow)", text)
    if not m:
        raise ValueError("offset unreadable")
    return float(m.group(1)) * (1 if m.group(2) == "fast" else -1)


def check_clock(run: Runner | None = None, platform: str | None = None) -> dict[str, Any]:
    """{"checked", "ok", "offset_s", "detail"}. ``ok`` is None when nothing could be checked."""
    run = run or _run
    if (platform or sys.platform) != "linux":
        return {"checked": False, "ok": None, "offset_s": None, "detail": "not checked (Linux only)"}
    got = run(["timedatectl", "show", "-p", "NTPSynchronized"])
    if got is None or got[0] != 0 or "NTPSynchronized=" not in got[1]:
        return {"checked": False, "ok": None, "offset_s": None, "detail": "not checked (timedatectl unavailable)"}
    synced = got[1].strip().split("=", 1)[1].strip().lower() == "yes"
    offset = None
    unreadable = False
    for cmd, parse in ((["timedatectl", "timesync-status"], _offset_timesyncd), (["chronyc", "tracking"], _offset_chrony)):
        r = run(cmd)
        if r is not None and r[0] == 0:
            try:
                offset = parse(r[1])
            except ValueError:
                unreadable = True
                break
            if offset is not None:
                break
    if not synced:
        return {"checked": True, "ok": False, "offset_s": offset, "detail": "NTP is not synchronised"}
    if unreadable:
        return {"checked": True, "ok": False, "offset_s": None, "detail": "offset unreadable"}
    if offset is not None and abs(offset) > MAX_OFFSET_S:
        return {"checked": True, "ok": False, "offset_s": offset,
                "detail": f"clock is {abs(offset):.2f} s {'fast' if offset > 0 else 'slow'} (limit {MAX_OFFSET_S:g} s)"}
    where = "" if offset is None else f", offset {abs(offset) * 1000:.0f} ms"
    return {"checked": True, "ok": True, "offset_s": offset, "detail": f"NTP synchronised{where}"}


def startup_check(settings: Any, notifier: Any, *, run: Runner | None = None, now: datetime | None = None,
                  platform: str | None = None) -> dict[str, Any]:
    """At watch start-up: log the result and, when the clock is wrong, alert once a day (TOTP logins would fail)."""
    res = check_clock(run, platform)
    if res["ok"] is False:
        log.warning("server clock: %s; TOTP logins need a correct clock", res["detail"])
        day = (now or datetime.now(IST)).date().isoformat()
        marker = Path(settings.state_dir) / f"clock_alert_{day}.sent"
        if notifier is not None and not marker.exists():
            try:
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text("sent", encoding="utf-8")
                for old in marker.parent.glob("clock_alert_*.sent"):
                    if old != marker:
                        old.unlink(missing_ok=True)
            except OSError:
                pass
            try:
                notifier.send("[CLOCK] the server clock is not right",
                              f"{res['detail']}. Groww TOTP logins need a correct clock; fix NTP "
                              "(timedatectl set-ntp true) and restart the watch service.")
            except Exception:  # noqa: BLE001
                log.warning("could not send the clock alert")
    else:
        log.info("server clock: %s", res["detail"])
    return res
