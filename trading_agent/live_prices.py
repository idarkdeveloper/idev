"""Groww live prices for the dashboard's holdings: read-only, off unless LIVE_PRICES.

``LivePrices.tick`` (run by its own thread from ``cmd_watch``) asks Groww's Live Data LTP endpoint for the prices of the
holdings the phone feed already knows, once a minute while the NSE is open inside the Groww window, and writes
``state/app/live_prices.json`` atomically. The dashboard reads that file (``load_live`` / ``apply_live_prices``) and never
calls Groww. Nothing here can place an order or create a token request of its own: the one Groww client is built by
the caller with live orders off, and a refused or failing read only backs off (never raises into the watch loop)."""

from __future__ import annotations

import json
import logging
import math
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from . import groww_hours
from .app_feed import _write_private, app_dir, load_feed
from .timezones import IST

log = logging.getLogger(__name__)

LIVE_FILE = "live_prices.json"
SOURCE = "groww_ltp"
FRESH_S = 180            # the dashboard uses the file only while it is this young
FEED_MAX_AGE_S = 300     # the holdings come from the feed's snapshot only while it is this young
MAX_BACKOFF_S = 600
MIN_EVERY_S = 30


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def feed_symbols(state_dir: Path, now: datetime, exchange: str = "NSE") -> list[str]:
    """NSE symbols of the feed's holdings, or [] when there is no fresh Groww snapshot (the tick is then skipped).
    BSE-only holdings (".BO", or any dotted code) stay on Yahoo."""
    if exchange.upper() != "NSE":
        return []
    feed = load_feed(state_dir)
    if not feed or feed.get("source") != "groww":
        return []
    try:
        at = datetime.fromisoformat(str(feed.get("as_of")))
        at = at if at.tzinfo else at.replace(tzinfo=IST)
        if not 0 <= (now - at).total_seconds() <= FEED_MAX_AGE_S:
            return []
    except ValueError:
        return []
    out: list[str] = []
    for h in feed.get("holdings") or []:
        sym = str((h or {}).get("symbol") or "").strip().upper()
        if sym and "." not in sym and sym not in out:
            out.append(sym)
    return out


class LivePrices:
    def __init__(self, settings: Any, ltp_fn: Callable[[list[str]], dict[str, float]], holidays: Any | None = None,
                 clock: Callable[[], datetime] | None = None):
        self.settings = settings
        self.ltp_fn = ltp_fn          # symbols (<= 50) -> {symbol: price}; the caller's read-only Groww client
        self.holidays = holidays
        self.clock = clock or groww_hours.now_ist
        self._next: datetime | None = None   # no attempt before this
        self._fails = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def every(self) -> float:
        return float(max(MIN_EVERY_S, getattr(self.settings, "live_prices_every_s", 60)))

    def _path(self) -> Path:
        return app_dir(self.settings.state_dir) / LIVE_FILE

    def _open(self, now: datetime) -> bool:
        from .safety import market_open
        cal = self.holidays if self.holidays is not None else groww_hours.default_calendar(self.settings)
        return groww_hours.window_open(self.settings, now, cal) and market_open(now, cal)

    def _back_off(self, now: datetime, e: BaseException) -> None:
        self._fails += 1
        delay = min(MAX_BACKOFF_S, self.every * 2 ** (self._fails - 1))
        until = getattr(e, "until", None)   # GrowwTokenUnavailable: the token cool-down
        nxt = now + timedelta(seconds=delay)
        if isinstance(until, datetime):
            nxt = max(nxt, until if until.tzinfo else until.replace(tzinfo=IST))
        self._next = nxt
        status = getattr(getattr(e, "response", None), "status_code", None) or getattr(e, "status", None)
        if self._fails == 1 or self._fails % 10 == 0:   # one line, not one per attempt; never the body or URL
            log.warning("Groww live prices unavailable (%s%s); retrying in %d s", type(e).__name__,
                        f" {status}" if status else "", int(delay))

    def tick(self, now: datetime | None = None) -> dict[str, Any] | None:
        """One attempt when due. Returns the written payload, or None (skipped or failed). Never raises."""
        try:
            now = (now or self.clock()).astimezone(IST)
            if self._next is not None and now < self._next:
                return None
            if not self._open(now):
                return None
            symbols = feed_symbols(self.settings.state_dir, now, getattr(self.settings, "groww_exchange", "NSE"))
            if not symbols:
                return None
            self._next = now + timedelta(seconds=self.every)
            prices: dict[str, float] = {}
            try:
                for i in range(0, len(symbols), 50):
                    got = self.ltp_fn(symbols[i:i + 50])
                    for s, v in (got or {}).items():
                        if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0:
                            prices[str(s).upper()] = float(v)
            except BaseException as e:  # noqa: BLE001 - SystemExit from a missing token too; only the type is logged
                if isinstance(e, (KeyboardInterrupt, GeneratorExit)):
                    raise
                self._back_off(now, e)
                return None
            if not prices:
                self._back_off(now, LookupError("no prices returned"))
                return None
            self._fails = 0
            body = {"as_of": now.astimezone(timezone.utc).isoformat(timespec="seconds"), "source": SOURCE, "prices": prices}
            _write_private(self._path(), body)
            return body
        except Exception as e:  # noqa: BLE001 - never stops the watch
            log.warning("live prices tick failed (%s)", type(e).__name__)
            return None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="live-prices")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(5.0)   # the cadence is enforced by tick() itself; this only polls for "due"


# ---- the dashboard's side: read the file, never call Groww ----

def load_live(state_dir: Path, now: datetime | None = None) -> dict[str, Any] | None:
    """{"as_of": datetime, "prices": {SYM: price}} when the file is fresh (under 180 s), from groww_ltp and well formed."""
    try:
        data = json.loads((app_dir(state_dir) / LIVE_FILE).read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("source") != SOURCE:
            return None
        at = datetime.fromisoformat(str(data.get("as_of")))
        at = at if at.tzinfo else at.replace(tzinfo=timezone.utc)
        if not 0 <= ((now or _now_utc()) - at).total_seconds() <= FRESH_S:
            return None
        raw = data.get("prices")
        if not isinstance(raw, dict):
            return None
        prices = {str(k).upper(): float(v) for k, v in raw.items()
                  if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0}
        return {"as_of": at, "prices": prices} if prices else None
    except (OSError, ValueError, TypeError):
        return None


def apply_live_prices(out: dict[str, Any], live: dict[str, Any] | None) -> dict[str, Any]:
    """A copy of a portfolio answer with Groww prices on the holdings the file covers (the Yahoo price stays on the
    rest), totals recomputed the way runner._assemble does. Marks ``price_src`` per holding and ``prices_as_of``."""
    if not live or not out.get("holdings"):
        return out
    rows, hit = [], False
    for h in out["holdings"]:
        p = live["prices"].get(str(h.get("symbol")).upper()) if h.get("kind", "equity") != "bond" else None
        if p is None:
            rows.append(h)
            continue
        hit = True
        qty, avg = h.get("qty") or 0, h.get("avg_price") or 0
        value = qty * p
        rows.append({**h, "price": p, "value": round(value, 2), "pl": round(value - qty * avg, 2),
                     "pl_pct": (p / avg - 1) if avg else None, "price_src": "groww"})
    if not hit:
        return out
    priced = [r for r in rows if r.get("value") is not None]
    inv_priced = sum(r["invested"] for r in priced)
    value = sum(r["value"] for r in priced)
    return {**out, "holdings": rows, "value": round(value, 2), "pl": round(value - inv_priced, 2),
            "pl_pct": (value / inv_priced - 1) if inv_priced else None,
            "unpriced": [r["symbol"] for r in rows if r.get("value") is None],
            "prices_as_of": live["as_of"].isoformat(timespec="seconds"), "prices_src": "groww"}
