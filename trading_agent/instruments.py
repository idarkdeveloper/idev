"""Company names (and the exchange a symbol trades on) for tickers like VAML or VOGL.

* NSE's equity list (EQUITY_L.csv) gives the official full name of every NSE-listed
  company, e.g. VAML -> "Vedanta Aluminium Metal Limited".
* Holdings that trade only on BSE (some bonds, unlisted-turned-BSE-listed shares such
  as NSE Ltd itself) are not in it, so Groww's public instrument list fills the gap and
  says which exchange they trade on. That list is about 20 MB, so it is downloaded only
  when a symbol is missing from NSE's list, and shared with the tick-size cache.

Both files are public (no credentials) and cached on disk for most of a day.
"""

from __future__ import annotations

import csv
import io
import logging
import time
from pathlib import Path
from typing import Any, Iterable

import requests

from .groww import INSTRUMENT_CSV_URL

log = logging.getLogger(__name__)

NSE_EQUITY_URL = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"
# Where both public lists only repeat the ticker as the name.
KNOWN_NAMES = {"NSE": "National Stock Exchange of India Limited"}
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"}


class CompanyNames:
    def __init__(self, cache_dir: Path, *, session: requests.Session | None = None, max_age_s: float = 20 * 3600):
        self.cache_dir = Path(cache_dir)
        self.session = session or requests.Session()
        self.max_age_s = max_age_s
        self._nse: dict[str, str] | None = None
        self._groww: dict[str, dict[str, str]] | None = None

    def _text(self, url: str, name: str) -> str:
        path = self.cache_dir / name
        if path.exists() and time.time() - path.stat().st_mtime < self.max_age_s:
            return path.read_text(encoding="utf-8")
        r = self.session.get(url, headers=UA, timeout=120)
        r.raise_for_status()
        text = r.content.decode("utf-8-sig", errors="replace")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return text

    def _nse_names(self) -> dict[str, str]:
        if self._nse is None:
            try:
                rows = csv.DictReader(io.StringIO(self._text(NSE_EQUITY_URL, "nse_equity_list.csv")))
                self._nse = {(r.get("SYMBOL") or "").strip().upper(): (r.get("NAME OF COMPANY") or "").strip()
                             for r in rows if r.get("SYMBOL")}
            except Exception as e:  # noqa: BLE001 - names are a nicety, never a failure
                log.warning("NSE equity list unavailable: %s", e)
                self._nse = {}
        return self._nse

    def _groww_names(self) -> dict[str, dict[str, str]]:
        """symbol -> {name, exchange}; NSE wins when a symbol trades on both."""
        if self._groww is None:
            out: dict[str, dict[str, str]] = {}
            try:
                for r in csv.DictReader(io.StringIO(self._text(INSTRUMENT_CSV_URL, "groww_instruments.csv"))):
                    if (r.get("segment") or "").upper() != "CASH":
                        continue
                    sym, exch = (r.get("trading_symbol") or "").upper(), (r.get("exchange") or "").upper()
                    if sym and (sym not in out or exch == "NSE"):
                        out[sym] = {"name": (r.get("name") or "").strip(), "exchange": exch}
            except Exception as e:  # noqa: BLE001
                log.warning("Groww instrument list unavailable: %s", e)
            self._groww = out
        return self._groww

    def lookup(self, symbols: Iterable[str]) -> dict[str, dict[str, Any]]:
        """{symbol: {"name": str | None, "exchange": "NSE" | "BSE" | None}}."""
        symbols = [s.upper() for s in symbols]
        nse = self._nse_names()
        out = {s: {"name": nse[s], "exchange": "NSE"} for s in symbols if nse.get(s)}
        missing = [s for s in symbols if s not in out]
        if missing:
            g = self._groww_names()
            for s in missing:
                info = g.get(s) or {}
                name = info.get("name")
                out[s] = {"name": name if name and name.upper() != s else KNOWN_NAMES.get(s),
                          "exchange": info.get("exchange")}
        return out


def nse_then_bse(nse_prices: Any, bse_prices: Any):
    """Price function for holdings: the NSE quote, else the BSE quote (BSE-only listings)."""
    def price(symbol: str) -> float:
        try:
            return float(nse_prices.latest_price(symbol))
        except Exception:  # noqa: BLE001
            return float(bse_prices.latest_price(symbol))
    return price
