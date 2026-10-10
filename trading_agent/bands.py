"""NSE price bands: stocks that can only move 2% or 5% in a day are not bought.

Source: NSE's daily price band list, ``sec_list.csv`` on the public archive host (columns Symbol, Series, Security Name,
Band, Remarks; Band is 2, 5, 10, 20 or "No Band", the last for F&O stocks and others with dynamic bands). Fetched once
per trading day (aimed at before 09:00 IST, but taken whenever today's list is missing) and stored as ``<state_dir>/price_bands.json`` (symbol -> band).

Rules (``BandBook.rule``):
* 2% or 5% band: skipped for buys ("price band 5%: liquidity can vanish in a fall; not bought");
* 10% band: shown as a caution, not blocked;
* 20%, no band, or a symbol not in the list: nothing.
Sells and stop exits are never blocked: this module only ever answers about buying. A missing or unreadable file, or a list more than
2 trading days old (weekdays), means no filtering at all, with one log line a day.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .state import atomic_write
from .timezones import IST

log = logging.getLogger(__name__)

URL = "https://nsearchives.nseindia.com/content/equities/sec_list.csv"
MAX_AGE_TRADING_DAYS = 2
MAX_TRIES = 6
RETRY_GAP = timedelta(minutes=20)
SKIP_BANDS = (2, 5)
CAUTION_BANDS = (10,)
_LOCK = threading.Lock()
_warned: set[str] = set()


def parse_sec_list(text: str) -> dict[str, int | None]:
    """symbol -> band percent (None for "No Band"). A symbol listed under several series keeps its tightest band."""
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    if not rows:
        raise ValueError("empty price band file")
    head = [h.strip().lower() for h in rows[0]]
    try:
        i_sym, i_band = head.index("symbol"), head.index("band")
    except ValueError:
        raise ValueError("price band file lacks Symbol / Band columns") from None
    out: dict[str, int | None] = {}
    for r in rows[1:]:
        if len(r) <= max(i_sym, i_band):
            continue
        sym, raw = r[i_sym].strip().upper(), r[i_band].strip()
        if not sym:
            continue
        try:
            band: int | None = int(float(raw))
        except ValueError:
            band = None   # "No Band"
        if sym not in out or (band is not None and (out[sym] is None or band < out[sym])):
            out[sym] = band
    if not out:
        raise ValueError("price band file has no rows")
    return out


class BandBook:
    def __init__(self, state_dir: Path, enabled: bool = True, now_fn: Any = None):
        self.path = Path(state_dir) / "price_bands.json"
        self.enabled = enabled
        self.now_fn = now_fn or (lambda: datetime.now(IST))
        self._tries: dict[str, tuple[int, datetime]] = {}
        self._cache: tuple[float, dict[str, Any]] | None = None

    # -- storage --------------------------------------------------------------
    def _load(self) -> dict[str, Any] | None:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return None
        if self._cache and self._cache[0] == mtime:
            return self._cache[1]
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(d.get("bands"), dict):
                return None
        except (OSError, ValueError):
            return None
        self._cache = (mtime, d)
        return d

    def save(self, bands: dict[str, int | None], day: date) -> None:
        with _LOCK:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(self.path, json.dumps({"date": day.isoformat(), "bands": bands}))
        self._cache = None

    def fetched_on(self) -> str | None:
        d = self._load()
        return d.get("date") if d else None

    def due(self, now: datetime, calendar: Any | None = None) -> bool:
        """A trading day on which no list is stored for today yet, at any time of day (the watch loop gets there before
        09:00 on a normal day); after a failure at most MAX_TRIES tries a day, RETRY_GAP apart."""
        now = now.astimezone(IST) if now.tzinfo else now
        if calendar is not None and hasattr(calendar, "is_trading_day"):
            if not calendar.is_trading_day(now.date()):
                return False
        elif now.date().weekday() >= 5:
            return False
        if self.fetched_on() == now.date().isoformat():
            return False
        n, last = self._tries.get(now.date().isoformat(), (0, None))
        return n < MAX_TRIES and (last is None or now - last >= RETRY_GAP)

    def fetch(self, session: Any, day: date, timeout: float = 30.0) -> int:
        """Download and store the list. ``session`` is a requests session (NSEClient's). Returns the symbol count."""
        from .nse import HEADERS
        resp = session.get(URL, headers={**HEADERS, "Accept": "*/*"}, timeout=timeout)
        resp.raise_for_status()
        bands = parse_sec_list(resp.content.decode("utf-8-sig", errors="replace"))
        self.save(bands, day)
        return len(bands)

    def tick(self, client: Any, now: datetime, calendar: Any | None = None) -> int | None:
        """For the watch loop; never raises."""
        if client is None or not self.enabled or not self.due(now, calendar):
            return None
        n, _ = self._tries.get(now.date().isoformat(), (0, None))
        self._tries[now.date().isoformat()] = (n + 1, now if now.tzinfo else now.replace(tzinfo=IST))
        try:
            return self.fetch(client.session, now.date())
        except Exception as e:  # noqa: BLE001
            self._warn_once(f"price band list unavailable: {e}", now.date())
            return None

    # -- answers --------------------------------------------------------------
    def _warn_once(self, msg: str, day: date | None = None) -> None:
        key = f"{day or self.now_fn().date()}:{msg[:40]}"
        if key not in _warned:
            _warned.add(key)
            log.warning("%s", msg)

    def _current(self) -> dict[str, Any] | None:
        """The stored list unless it is missing or more than MAX_AGE_TRADING_DAYS trading days (weekdays) old."""
        d = self._load()
        if d is None:
            self._warn_once("price band list missing: not filtering by price band today")
            return None
        try:
            listed = date.fromisoformat(str(d.get("date")))
        except ValueError:
            return None
        today = self.now_fn().date()
        age = sum(1 for i in range(1, (today - listed).days + 1) if (listed + timedelta(days=i)).weekday() < 5)
        if age > MAX_AGE_TRADING_DAYS:
            self._warn_once(f"price band list is from {listed} ({age} trading days old): not filtering by price band today")
            return None
        return d

    def band(self, symbol: str) -> int | None | str:
        """Band percent, None for "No Band", or "unknown" (no list, a stale one, or the symbol is not in it)."""
        d = self._current() if self.enabled else None
        if d is None:
            return "unknown"
        sym = str(symbol or "").upper().strip()
        return d["bands"][sym] if sym in d["bands"] else "unknown"

    def active(self) -> bool:
        """True when the filter is on AND a usable (present, fresh) list is stored, i.e. buys are really being filtered."""
        return bool(self.enabled and self._current() is not None)

    def rule(self, symbol: str) -> dict[str, Any]:
        """{"band", "skip", "caution", "label", "reason"} for a BUY of ``symbol``. Nothing is ever blocked when the
        filter is off or the list is missing."""
        if not self.enabled:
            return {"band": "unknown", "skip": False, "caution": False, "label": None, "reason": None}
        if self._current() is None:
            return {"band": "unknown", "skip": False, "caution": False, "label": None, "reason": None}
        b = self.band(symbol)
        if isinstance(b, int) and b in SKIP_BANDS:
            return {"band": b, "skip": True, "caution": False, "label": f"{b}%",
                    "reason": f"price band {b}%: liquidity can vanish in a fall; not bought"}
        if isinstance(b, int) and b in CAUTION_BANDS:
            return {"band": b, "skip": False, "caution": True, "label": f"{b}%",
                    "reason": f"price band {b}%: a fall can be hard to exit; caution"}
        return {"band": b, "skip": False, "caution": False,
                "label": f"{b}%" if isinstance(b, int) else ("no band" if b is None else None), "reason": None}

    def refuse_buy(self, symbol: str) -> str | None:
        """The message for a buy that must not happen (2% / 5% band), else None."""
        r = self.rule(symbol)
        return f"{symbol.upper()}: {r['reason']}" if r["skip"] else None


def book_for(settings: Any) -> BandBook:
    return BandBook(Path(settings.state_dir), enabled=bool(getattr(settings, "price_band_filter", True))
                    and getattr(settings, "market", "in") == "in")
