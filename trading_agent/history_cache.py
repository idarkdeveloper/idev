"""One in-process price-history cache for the READ-ONLY research tools (and the replay): ``SharedHistory``.

The factor screen, the portfolio backtest, the signal lab, the deals backtest, the screener page and every replay trial ask
for the same (symbol, range) series again and again; each ask reads the disk cache and the price archive and builds a
list of dicts. This keeps the answer for ``ttl`` seconds (default 10 minutes), thread-safe, one loader per key, and bounded in
memory (``max_entries``; the oldest entries go first). It must never wrap the agent's, the watch service's or the paper
broker's price source, and not the dashboard's latest-price reads (the order-form line, Look up): those keep the unwrapped
source and its 60 s market-hours cache.

Trade-off: a degraded load (archive-only after a Yahoo failure, Yahoo-only after a locked archive) does not raise, so it is
cached like any other for up to ``ttl`` seconds (deliberately short for that reason).

Cached lists are handed out as-is, never copied: every consumer (ClockedPrices.history slices, screen, momentum) only reads
them. Do not mutate what ``history`` returns.

Memory: a 2-year series is about 150 KB as Python dicts and a 10-year one (what a 5-year backtest reads) about 900 KB, so a full cache is several hundred MB (600 series of 900 KB would be 540 MB, the worst case); ``max_entries`` (env RESEARCH_CACHE_MAX) is the knob.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

log = logging.getLogger(__name__)

DEFAULT_MAX_ENTRIES = 600


class SharedHistory:
    """``history(symbol, range)`` cached. ``ttl`` is the normal lifetime; a warmed entry (``warm``) lives ``warm_ttl`` longer
    only while ``market_open()`` is false: while the NSE is open every entry expires after ``ttl``."""

    def __init__(self, source: Any, ttl: float = 600.0, max_entries: int = DEFAULT_MAX_ENTRIES,
                 clock: Callable[[], float] = time.monotonic, market_open: Callable[[], bool] | None = None):
        self._source, self._ttl, self._max, self._clock, self._open = source, ttl, max(1, int(max_entries)), clock, market_open
        self._cache: dict[tuple[str, str], tuple[float, list[dict[str, Any]], float]] = {}   # key -> (stored at, bars, its ttl)
        self._locks: dict[tuple[str, str], threading.Lock] = {}
        self._lock = threading.Lock()
        self.hits = 0
        self.loads = 0

    def _ttl_of(self, entry_ttl: float) -> float:
        if entry_ttl > self._ttl and self._open is not None:
            try:
                if self._open():
                    return self._ttl
            except Exception:  # noqa: BLE001 - an unreadable calendar: the normal lifetime
                return self._ttl
        return entry_ttl

    def _fresh(self, key: tuple[str, str]) -> list[dict[str, Any]] | None:
        hit = self._cache.get(key)
        return hit[1] if hit and self._clock() - hit[0] < self._ttl_of(hit[2]) else None

    def __len__(self) -> int:
        return len(self._cache)

    def history(self, symbol: str, range_: str = "2y") -> list[dict[str, Any]]:
        return self._get(symbol, range_, self._ttl)

    def warm(self, symbol: str, range_: str = "2y", ttl: float | None = None) -> None:
        """Load a series ahead of use and keep it for ``ttl`` (default: the normal lifetime; longer only outside market
        hours, see ``_ttl_of``). Errors propagate; an entry that is already fresh is left as it is."""
        self._get(symbol, range_, self._ttl if ttl is None else float(ttl))

    def _get(self, symbol: str, range_: str, ttl: float) -> list[dict[str, Any]]:
        key = (symbol.upper(), range_)
        with self._lock:
            bars = self._fresh(key)
            if bars is not None:
                self.hits += 1
                return bars
            now = self._clock()   # evict expired entries and their locks, so memory is bounded by the TTLs
            for k in [k for k, (t, _, e) in self._cache.items() if now - t >= self._ttl_of(e)]:
                del self._cache[k]
                self._locks.pop(k, None)
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:   # one loader per key; other keys load in parallel
            with self._lock:
                bars = self._fresh(key)
            if bars is not None:
                self.hits += 1
                return bars
            bars = self._source.history(symbol, range_)   # any error propagates, nothing cached
            with self._lock:
                self.loads += 1
                self._cache[key] = (self._clock(), bars, ttl)
                over = len(self._cache) - self._max
                if over > 0:   # a full cache drops its oldest entries (and the locks of keys not being loaded)
                    for k, _ in sorted(self._cache.items(), key=lambda kv: kv[1][0])[:over]:
                        if k != key:
                            del self._cache[k]
                            self._locks.pop(k, None)
            return bars

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__") or name in ("_source", "_ttl", "_max", "_clock", "_open"):   # no recursion before __init__ / on copy+pickle probes
            raise AttributeError(name)
        return getattr(self._source, name)


WARM_TTL = 30 * 60   # a warmed series outlives the warm-up run (outside market hours; see SharedHistory._ttl_of)


class ResearchWarm:
    """Loads the default universes' 2-year histories (and NIFTY200's 10-year range, what a 5-year backtest reads) a little
    after the dashboard starts. Honest about what it does: it mainly prepares the price archive and the disk cache (the next
    screen then reads files instead of fetching), and keeps each series in memory for ``warm_ttl`` (30 min) outside market
    hours. It fetches from Yahoo, so it never runs while the NSE is open (``is_open`` true: skipped, one INFO line, and a
    run that is underway when the market opens stops). A daemon thread with a pause between symbols (the GIL goes to page
    requests); ``stop()`` ends it. It never raises and never blocks."""

    def __init__(self, history: Any, members_of: Callable[[str], list[str]], universes: tuple[str, ...] = ("NIFTY200", "NIFTY500", "NIFTYMIDCAP150", "NIFTY50"),
                 five_year: tuple[str, ...] = ("NIFTY200",),   # universes whose 10y range is warmed too
                 delay: float = 30.0, pause: float = 0.5, is_open: Callable[[], bool] | None = None, warm_ttl: float = WARM_TTL):
        self.history, self.members_of, self.universes, self.five_year = history, members_of, universes, five_year
        self.delay, self.pause, self.is_open, self.warm_ttl = delay, pause, is_open, warm_ttl
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.loaded = 0

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True, name="research-warm")
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _open_now(self) -> bool:
        try:
            return bool(self.is_open and self.is_open())
        except Exception:  # noqa: BLE001 - an unreadable calendar counts as open: no network warm-up on a guess
            return True

    def _run(self) -> None:
        try:
            if self._stop.wait(self.delay):
                return
            if self._open_now():
                log.info("research warm-up skipped: the NSE is open (it would fetch from Yahoo)")
                return
            t0 = time.monotonic()
            for name in self.universes:
                try:
                    syms = self.members_of(name)
                except Exception as e:  # noqa: BLE001
                    log.debug("research warm-up: universe %s skipped: %s", name, e)
                    continue
                ranges = ("2y", "10y") if name in self.five_year else ("2y",)   # a 5-year backtest asks for 10y (one year of lookback)
                for sym in syms:
                    for rng in ranges:
                        if self._stop.is_set():
                            return
                        if self._open_now():
                            log.info("research warm-up stopped: the NSE opened (%d series loaded)", self.loaded)
                            return
                        try:
                            self.history.warm(sym, rng, self.warm_ttl)
                            self.loaded += 1
                        except Exception as e:  # noqa: BLE001
                            log.debug("research warm-up: %s skipped: %s", sym, e)
                        if self._stop.wait(self.pause):
                            return
            log.info("research warm-up: %d series loaded in %.1fs", self.loaded, time.monotonic() - t0)
        except Exception:  # noqa: BLE001 - a warm-up must never take anything down
            log.debug("research warm-up failed", exc_info=True)
