"""Device secrets for the read-only phone app API.

A device secret is shown once by ``app-device add`` and stored only as a SHA-256 hash in ``state/app_devices.json``
(owner-only). ``Authenticator.check`` answers 200, 401 (no detail) or 429 (locked out / rate limited). The secret is
never logged or put in a message. In-memory counters only: a restart forgets lockouts."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from collections import deque
from datetime import date
from pathlib import Path
from typing import Any, Callable

from .app_feed import _write_private

DEVICES_FILE = "app_devices.json"
FAIL_LIMIT, FAIL_WINDOW_S, LOCK_S = 5, 600, 900
RATE_LIMIT, RATE_WINDOW_S = 60, 60
MAX_TRACKED = 10_000   # clients remembered for lockout; beyond this expired entries go, then the oldest
_HEX64 = re.compile(r"[0-9a-f]{64}")


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8", errors="surrogatepass")).hexdigest()


def _load(state_dir: Path, *, strict: bool = False) -> list[dict[str, str]]:
    """Well-formed rows only; a missing or garbage file is an empty list (nobody is accepted). ``strict`` (used before
    a write) raises when the file exists but cannot be read, so it is never overwritten by mistake."""
    try:
        data = json.loads((Path(state_dir) / DEVICES_FILE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except OSError as e:
        if strict:
            raise RuntimeError(f"cannot read {DEVICES_FILE} ({type(e).__name__}); not changing it") from None
        return []
    except ValueError:
        return []
    return [r for r in data if isinstance(r, dict) and isinstance(r.get("name"), str) and isinstance(r.get("hash"), str) and _HEX64.fullmatch(r["hash"]) is not None
            and isinstance(r.get("created"), str)] if isinstance(data, list) else []


def add_device(state_dir: Path, name: str) -> str:
    rows = _load(state_dir, strict=True)
    if not name.strip() or any(r["name"] == name for r in rows):
        raise ValueError(f"device name {name!r} is empty or already used")
    secret = secrets.token_urlsafe(32)
    rows.append({"name": name, "hash": _hash(secret), "created": date.today().isoformat()})
    _write_private(Path(state_dir) / DEVICES_FILE, rows)
    return secret


def list_devices(state_dir: Path) -> list[dict[str, Any]]:
    return [{"name": r["name"], "created": r["created"]} for r in _load(state_dir)]


def revoke(state_dir: Path, name: str | None = None, *, all_: bool = False) -> int:
    rows = _load(state_dir, strict=True)
    keep = [] if all_ else [r for r in rows if r["name"] != name]
    if len(keep) != len(rows):
        _write_private(Path(state_dir) / DEVICES_FILE, keep)
    return len(rows) - len(keep)


class Authenticator:
    def __init__(self, state_dir: Path, clock: Callable[[], float] = time.time) -> None:
        self.state_dir = Path(state_dir)
        self.clock = clock
        self._lock = threading.Lock()
        self._fails: dict[str, deque[float]] = {}
        self._blocked: dict[str, float] = {}
        self._hits: dict[str, deque[float]] = {}

    def check(self, secret: str | None, client: str) -> tuple[str | None, int]:
        now = self.clock()
        with self._lock:
            self._sweep(now)
            if self._blocked.get(client, 0) > now:
                return None, 429
            name = self._match(secret)
            if name is None:
                fails = self._fails.setdefault(client, deque())
                fails.append(now)
                while fails and fails[0] <= now - FAIL_WINDOW_S:
                    fails.popleft()
                if len(fails) >= FAIL_LIMIT:
                    self._blocked[client] = now + LOCK_S
                    fails.clear()
                return None, 401
            hits = self._hits.setdefault(name, deque())
            while hits and hits[0] <= now - RATE_WINDOW_S:
                hits.popleft()
            if len(hits) >= RATE_LIMIT:
                return None, 429
            hits.append(now)
            return name, 200

    def _sweep(self, now: float) -> None:
        if max(len(self._fails), len(self._blocked)) <= MAX_TRACKED:
            return
        for c in [c for c, t in self._blocked.items() if t <= now]:
            del self._blocked[c]
        for c in [c for c, d in self._fails.items() if not d or d[-1] <= now - FAIL_WINDOW_S]:
            del self._fails[c]
        for d in (self._fails, self._blocked):
            while len(d) > MAX_TRACKED:
                del d[next(iter(d))]   # oldest first (dicts keep insertion order)

    def _match(self, secret: str | None) -> str | None:
        if not secret or not isinstance(secret, str):
            return None
        given, found = _hash(secret), None
        for r in _load(self.state_dir):   # no early exit: every row is compared
            if hmac.compare_digest(given, r["hash"]):
                found = r["name"]
        return found
