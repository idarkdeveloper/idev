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
import re
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
        self._face: dict[str, float] | None = None

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

    def face_value(self, symbol: str) -> float | None:
        """Face value (rupees per share) from NSE's equity list (its FACE VALUE column), or None when the list is
        unavailable or has no usable number for the symbol."""
        if getattr(self, "_face", None) is None:
            face: dict[str, float] = {}
            try:
                for r in csv.DictReader(io.StringIO(self._text(NSE_EQUITY_URL, "nse_equity_list.csv"))):
                    row = {(k or "").strip().upper(): (v or "").strip() for k, v in r.items()}   # the header has leading spaces
                    try:
                        fv = float(row.get("FACE VALUE", ""))
                    except ValueError:
                        continue
                    if row.get("SYMBOL") and fv > 0:
                        face[row["SYMBOL"].upper()] = fv
            except Exception as e:  # noqa: BLE001 - a nicety, never a failure
                log.warning("NSE equity list unavailable for face values: %s", e)
            self._face = face
        return (self._face or {}).get(symbol.upper())

    def _groww_names(self) -> dict[str, dict[str, str]]:
        """symbol -> {name, exchange, series}; NSE wins when a symbol trades on both."""
        if self._groww is None:
            out: dict[str, dict[str, str]] = {}
            try:
                for r in csv.DictReader(io.StringIO(self._text(INSTRUMENT_CSV_URL, "groww_instruments.csv"))):
                    if (r.get("segment") or "").upper() != "CASH":
                        continue
                    sym, exch = (r.get("trading_symbol") or "").upper(), (r.get("exchange") or "").upper()
                    if sym and (sym not in out or exch == "NSE"):
                        out[sym] = {"name": (r.get("name") or "").strip(), "exchange": exch,
                                    "series": (r.get("series") or "").strip().upper()}
            except Exception as e:  # noqa: BLE001
                log.warning("Groww instrument list unavailable: %s", e)
            self._groww = out
        return self._groww

    def nse_names(self, symbols: Iterable[str]) -> dict[str, str]:
        """{symbol: company name} from NSE's published equity list only (no Groww file); unknown symbols are left out."""
        nse = self._nse_names()
        return {s.upper(): nse[s.upper()] for s in symbols if nse.get(s.upper())}

    def lookup(self, symbols: Iterable[str]) -> dict[str, dict[str, Any]]:
        """{symbol: {"name", "exchange": "NSE" | "BSE" | None, "kind": "equity" | "bond", "maturity"}}."""
        symbols = [s.upper() for s in symbols]
        nse = self._nse_names()
        out = {s: {"name": nse[s], "exchange": "NSE", "kind": "equity", "maturity": None} for s in symbols if nse.get(s)}
        missing = [s for s in symbols if s not in out]
        if missing:
            g = self._groww_names()
            for s in missing:
                info = g.get(s) or {}
                name = info.get("name")
                name = name if name and name.upper() != s else KNOWN_NAMES.get(s)
                kind = "bond" if is_debt(info.get("exchange"), info.get("series")) else "equity"
                maturity = None
                if kind == "bond" and name:
                    m = MATURITY.search(name)  # Groww writes bond names like "Prachay Capital Limited Mar'31"
                    if m:
                        name, maturity = name[:m.start()].strip(), f"{m.group(1)} 20{m.group(2)}"
                out[s] = {"name": name, "exchange": info.get("exchange"), "kind": kind, "maturity": maturity}
        return out


    # -- search by company name ---------------------------------------------------
    @staticmethod
    def _norm(text: str) -> str:
        t = re.sub(r"[^a-z0-9& ]+", " ", text.lower())
        t = re.sub(r"\b(limited|ltd|the|india|of|and|co|company|corporation|corp)\b", " ", t)
        return re.sub(r"\s+", " ", t).strip()

    def search(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        """NSE-listed companies matching a ticker or company name, best first."""
        q = (query or "").strip()
        if len(q) < 2:
            return []
        qu, qn = q.upper(), self._norm(q)
        words = qn.split()
        scored = []
        for sym, name in self._nse_names().items():
            nn = self._norm(name)
            if sym == qu:
                score = 100
            elif nn == qn and qn:
                score = 95
            elif sym.startswith(qu.replace(" ", "")):
                score = 80
            elif qn and nn.startswith(qn):
                score = 75
            elif words and all(re.search(r"\b" + re.escape(w), nn) for w in words):
                score = 60
            elif qn and qn in nn:
                score = 40
            else:
                continue
            scored.append((score, len(name), sym, name))
        scored.sort(key=lambda x: (-x[0], x[1], x[2]))
        return [{"symbol": sym, "name": name, "score": score} for score, _, sym, name in scored[:limit]]

    def resolve(self, text: str) -> tuple[str, str | None]:
        """(ticker, company name) for a ticker or a company name typed by a person.

        A known NSE ticker is kept as is; otherwise the best name match is used when it is
        a clear one (all the words appear in the name). Falls back to the text in capitals."""
        raw = (text or "").strip()
        nse = self._nse_names()
        if raw.upper() in nse:
            return raw.upper(), nse[raw.upper()]
        hits = self.search(raw, limit=1)
        if hits and hits[0]["score"] >= 60:
            return hits[0]["symbol"], hits[0]["name"]
        return raw.upper().replace(" ", ""), None


MATURITY = re.compile(r"\s+([A-Z][a-z]{2})'(\d{2})$")


def is_debt(exchange: str | None, series: str | None) -> bool:
    """Bonds and other debt by their exchange series (BSE F/G; NSE N*, Y*, Z*, GS, GB, SG)."""
    ex, se = (exchange or "").upper(), (series or "").upper()
    if ex == "BSE":
        return se in ("F", "G")
    if ex == "NSE":
        return se[:1] in ("N", "Y", "Z") or se in ("GS", "GB", "SG")
    return False


def nse_then_bse(nse_prices: Any, bse_prices: Any):
    """Price function for holdings: the NSE quote, else the BSE quote (BSE-only listings)."""
    def price(symbol: str) -> float:
        try:
            return float(nse_prices.latest_price(symbol))
        except Exception:  # noqa: BLE001
            return float(bse_prices.latest_price(symbol))
    return price
