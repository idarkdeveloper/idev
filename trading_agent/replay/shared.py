"""One in-process price-history cache shared by every replay trial and tool (see ReplayApp).

Trade-off: a degraded load (archive-only after a Yahoo failure, Yahoo-only after a locked archive) does not raise, so
it is cached like any other for up to ``ttl`` seconds (default 10 minutes, deliberately short for that reason).

Cached lists are handed out as-is, never copied: every consumer (ClockedPrices.history slices, screen, momentum) only
reads them. Do not mutate what ``history`` returns.
"""

from __future__ import annotations

import threading
import time
from typing import Any


class SharedHistory:
    def __init__(self, source: Any, ttl: float = 600.0):
        self._source, self._ttl = source, ttl
        self._cache: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}
        self._locks: dict[tuple[str, str], threading.Lock] = {}
        self._lock = threading.Lock()

    def _fresh(self, key: tuple[str, str]) -> list[dict[str, Any]] | None:
        hit = self._cache.get(key)
        return hit[1] if hit and time.monotonic() - hit[0] < self._ttl else None

    def history(self, symbol: str, range_: str = "2y") -> list[dict[str, Any]]:
        key = (symbol.upper(), range_)
        with self._lock:
            bars = self._fresh(key)
            if bars is not None:
                return bars
            now = time.monotonic()   # evict expired entries and their locks, so memory is bounded by the TTL
            for k in [k for k, (t, _) in self._cache.items() if now - t >= self._ttl]:
                del self._cache[k]
                self._locks.pop(k, None)
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:   # one loader per key; other keys load in parallel
            with self._lock:
                bars = self._fresh(key)
            if bars is not None:
                return bars
            bars = self._source.history(symbol, range_)   # any error propagates, nothing cached
            with self._lock:
                self._cache[key] = (time.monotonic(), bars)
            return bars

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__") or name in ("_source", "_ttl"):   # no recursion before __init__ / on copy+pickle probes
            raise AttributeError(name)
        return getattr(self._source, name)
