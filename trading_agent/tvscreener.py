"""Optional extra screener columns from TradingView's public scanner endpoint. Display only, off by default.

TradingView offers no official API for this: the scanner endpoint behind its website screener is unofficial, and its
terms restrict automated access. So this module is deliberately timid:

* Off unless the user turns ``TRADINGVIEW_SCREENER`` on (Settings says plainly what that means).
* Refused outright when ``TA_ROLE=server``: the cloud server's IP is registered with Groww and must never be seen
  doing something a site could object to. The check is the first thing ``fetch`` does, before any network object is
  touched.
* One request per universe load, at most once every 15 minutes per universe, cached.
* Any error, or HTTP 403 / 429 (or anything but 200, or an answer it cannot read), starts a 24 hour back-off that is
  saved to disk, so a restart does not retry early. Columns show "n/a" meanwhile.
* No login, no cookies, no account: a fresh session per request that sends only a User-Agent and the JSON body.

What comes back is shown in the table, labelled as from TradingView. It never feeds the agent's trading rules, the
daily emails or Claude; nothing outside ``ui.py`` imports this module.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import requests

log = logging.getLogger(__name__)

URL = "https://scanner.tradingview.com/india/scan"
COLUMNS = ["Recommend.All", "sector", "industry", "earnings_per_share_diluted_yoy_growth_ttm"]
CACHE_SECONDS = 15 * 60
BACKOFF_SECONDS = 24 * 3600
TIMEOUT_SECONDS = 15.0
BACKOFF_FILE = "tradingview_backoff.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
           "Accept": "application/json", "Content-Type": "application/json"}


def summary_label(value: Any) -> str | None:
    """The scanner's -1..1 recommendation figure as words. None when it is missing or not a number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        return None
    if value >= 0.5:
        return "Strong buy"
    if value >= 0.1:
        return "Buy"
    if value > -0.1:
        return "Neutral"
    if value > -0.5:
        return "Sell"
    return "Strong sell"


def tv_symbol(symbol: str) -> str:
    """NSE:SYMBOL as the scanner names it (a hyphen in an NSE code is an underscore there)."""
    return "NSE:" + symbol.strip().upper().replace("-", "_")


def parse_scan(payload: Any, symbols: Iterable[str]) -> dict[str, dict[str, Any]]:
    """{our symbol: {summary, summary_label, sector, industry, eps_growth}} from a scan answer. Raises ValueError for
    an answer that is not the expected shape (the caller backs off)."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("unexpected TradingView answer")
    by_tv = {tv_symbol(s): s.strip().upper() for s in symbols}
    out: dict[str, dict[str, Any]] = {}
    for item in payload["data"]:
        if not isinstance(item, dict) or item.get("s") not in by_tv or not isinstance(item.get("d"), list):
            continue
        d = item["d"] + [None] * (len(COLUMNS) - len(item["d"]))
        rec, sector, industry, eps = d[0], d[1], d[2], d[3]
        num = lambda v: round(float(v), 2) if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v else None  # noqa: E731
        out[by_tv[item["s"]]] = {
            "summary": num(rec), "summary_label": summary_label(rec),
            "sector": sector.strip() if isinstance(sector, str) and sector.strip() else None,
            "industry": industry.strip() if isinstance(industry, str) and industry.strip() else None,
            "eps_growth": num(eps)}
    return out


class TradingViewColumns:
    """``fetch(universe, symbols)`` -> {symbol: columns} or None (off, refused, backing off, or failed).

    ``enabled`` is a callable so the Settings toggle takes effect at once; ``role`` likewise (read from the settings
    the server was started with). ``session`` is only for tests: a real run builds a fresh cookie-less session per
    request."""

    def __init__(self, state_dir: Path, *, enabled: Callable[[], bool], role: Callable[[], str],
                 session: Any | None = None, clock: Callable[[], float] = time.time):
        self.state_dir = Path(state_dir)
        self.enabled, self.role, self._session, self.clock = enabled, role, session, clock
        self._cache: dict[str, tuple[float, dict[str, dict[str, Any]]]] = {}
        self._tried: dict[str, float] = {}
        self._lock = threading.Lock()
        self.requests_made = 0

    # -- state ----------------------------------------------------------------
    def _backoff_until(self) -> float:
        try:
            return float(json.loads((self.state_dir / BACKOFF_FILE).read_text(encoding="utf-8")).get("until") or 0)
        except (OSError, ValueError, AttributeError, TypeError):
            return 0.0

    def _start_backoff(self, why: str) -> None:
        until = self.clock() + BACKOFF_SECONDS
        log.warning("TradingView columns paused for 24 hours: %s", why)
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            (self.state_dir / BACKOFF_FILE).write_text(json.dumps({"until": until, "why": why[:200]}), encoding="utf-8")
        except OSError:
            pass

    def status(self) -> dict[str, Any]:
        """{"state": "off" | "refused_server" | "backoff" | "on", "until": ISO time or None}, for the page."""
        if (self.role() or "").strip().lower() == "server":
            return {"state": "refused_server", "until": None}
        if not self.enabled():
            return {"state": "off", "until": None}
        until = self._backoff_until()
        if until > self.clock():
            return {"state": "backoff", "until": datetime.fromtimestamp(until, tz=timezone.utc).isoformat(timespec="seconds")}
        return {"state": "on", "until": None}

    # -- the one request --------------------------------------------------------
    def fetch(self, universe: str, symbols: list[str]) -> dict[str, dict[str, Any]] | None:
        if (self.role() or "").strip().lower() == "server":   # first, before anything else: never from the Groww server
            return None
        if not self.enabled() or not symbols:
            return None
        now = self.clock()
        if self._backoff_until() > now:
            return None
        key = universe.upper()
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None and now - hit[0] < CACHE_SECONDS:
                return hit[1]
            if now - self._tried.get(key, -1e18) < CACHE_SECONDS:   # asked recently (and it failed or was empty): not again
                return hit[1] if hit else None
            self._tried[key] = now
            self.requests_made += 1
        body = {"symbols": {"tickers": [tv_symbol(s) for s in symbols]}, "columns": COLUMNS}
        session = self._session or requests.Session()
        try:
            resp = session.post(URL, json=body, headers=HEADERS, timeout=TIMEOUT_SECONDS)
            if resp.status_code != 200:   # 403 / 429 above all: stop for a day, whatever the code
                self._start_backoff(f"HTTP {resp.status_code}")
                return None
            data = parse_scan(resp.json(), symbols)
        except Exception as e:  # noqa: BLE001 - any failure at all is a reason to leave them alone for a day
            self._start_backoff(f"{type(e).__name__}")
            return None
        finally:
            cookies = getattr(session, "cookies", None)
            if cookies is not None and hasattr(cookies, "clear"):
                cookies.clear()   # no cookies kept, ever
            if self._session is None and hasattr(session, "close"):
                session.close()
        with self._lock:
            self._cache[key] = (self.clock(), data)
        return data
