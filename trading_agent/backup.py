"""Daily backup of the state files, kept 30 days in ``state/backups/YYYYMMDD/``.

Copies ``state/*.json``, ``state/forward/*.json`` (30 days) and the price archive (7 days)'s SQLite file (through SQLite's own backup API, so a writer in the middle of
a transaction cannot leave a torn copy). Anything that looks like a secret (a name with "token" or "secret", ``.env``)
is never copied. ``state.json`` itself also gets a ``state.json.bak`` on every save (see ``state.atomic_write``).

To restore: stop the watch service, copy the files you want from ``state/backups/<day>/`` back into ``state/`` (for the
archive: ``state/prices/archive.sqlite``), start the service. See docs/runbook.md.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import sqlite3
import time
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Callable

from .forward_schedule import DailyJobScheduler
from .timezones import IST

log = logging.getLogger(__name__)

KEEP_DAYS = 30
ARCHIVE_KEEP_DAYS = 7   # the price archive is large: its copies go after a week, the JSON copies after 30 days
RUN_AFTER = dtime(16, 0)   # IST, after the close and the forward test
SECRET_WORDS = ("token", "secret", "password", "credential")
_DAY_DIR = re.compile(r"\d{8}")


def _is_secret(name: str) -> bool:
    n = name.lower()
    return n.startswith(".env") or any(w in n for w in SECRET_WORDS)


def _copy_atomic(src: Path, dest: Path) -> None:
    tmp = dest.with_name(dest.name + ".tmp")
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, dest)
    except OSError:
        tmp.unlink(missing_ok=True)   # no half-copied leftovers
        raise


def backup_now(state_dir: Path, today: date, *, keep_days: int = KEEP_DAYS) -> dict[str, Any]:
    """Copy the files for ``today`` and prune folders older than ``keep_days``. Returns what was copied."""
    state_dir = Path(state_dir)
    dest = state_dir / "backups" / today.strftime("%Y%m%d")
    dest.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for f in sorted(state_dir.glob("*.json")):
        if _is_secret(f.name) or not f.is_file():
            continue
        try:
            _copy_atomic(f, dest / f.name)
            copied.append(f.name)
        except OSError as e:
            log.warning("backup of %s failed: %s", f.name, e)
    forward = state_dir / "forward"
    if forward.is_dir():
        (dest / "forward").mkdir(exist_ok=True)
        for f in sorted(forward.glob("*.json")):
            try:
                _copy_atomic(f, dest / "forward" / f.name)
                copied.append("forward/" + f.name)
            except OSError as e:
                log.warning("backup of forward/%s failed: %s", f.name, e)
    archived = False
    archive = state_dir / "prices" / "archive.sqlite"
    if archive.exists():
        tmp = dest / "archive.sqlite.tmp"
        src = dst = None
        try:
            src = sqlite3.connect(str(archive), timeout=30)
            dst = sqlite3.connect(str(tmp))
            src.backup(dst)   # consistent even while another process writes
            dst.close()
            dst = None
            os.replace(tmp, dest / "archive.sqlite")
            archived = True
        except (sqlite3.Error, OSError) as e:
            log.warning("backup of the price archive failed: %s", e)
        finally:
            for c in (src, dst):
                if c is not None:
                    c.close()
            tmp.unlink(missing_ok=True)
    pruned = prune(state_dir, today, keep_days)
    return {"day": today.isoformat(), "files": copied, "archive": archived, "pruned": pruned}


def prune(state_dir: Path, today: date, keep_days: int = KEEP_DAYS, archive_keep_days: int = ARCHIVE_KEEP_DAYS) -> list[str]:
    cutoff = (today - timedelta(days=keep_days)).strftime("%Y%m%d")
    archive_cutoff = (today - timedelta(days=archive_keep_days)).strftime("%Y%m%d")
    gone = []
    for d in sorted((Path(state_dir) / "backups").glob("*")):
        if not (d.is_dir() and _DAY_DIR.fullmatch(d.name)):
            continue
        if d.name < cutoff:
            shutil.rmtree(d, ignore_errors=True)
            gone.append(d.name)
        elif d.name < archive_cutoff:
            (d / "archive.sqlite").unlink(missing_ok=True)
    return gone


class BackupScheduler(DailyJobScheduler):
    """Once per trading day after the close, claim-file guarded like the other daily jobs."""

    claim_prefix = "backup"
    label = "state backup"

    def __init__(self, state_dir: Path, run_fn: Callable[[], Any], *, holidays: Any = None, run_after: dtime = RUN_AFTER,
                 threaded: bool = True, clock: Callable[[], float] = time.monotonic):
        super().__init__(state_dir, run_fn, holidays=holidays, run_after=run_after, threaded=threaded, clock=clock)


def make_scheduler(settings: Any, holidays: Any = None) -> BackupScheduler:
    def job() -> None:
        r = backup_now(Path(settings.state_dir), datetime.now(IST).date())
        log.info("state backup: %d files%s", len(r["files"]), ", price archive" if r["archive"] else "")

    return BackupScheduler(Path(settings.state_dir), job, holidays=holidays)
