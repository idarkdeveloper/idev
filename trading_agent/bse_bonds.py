"""Bond prices from BSE's free daily debt bhavcopy, for Groww holdings that no other free source prices.

Groww's live quotes are not in the free API plan, Yahoo lists no BSE bonds, and BSE's quote API refuses cloud servers.
BSE publishes, for every trading day, ``DEBTBHAVCOPY<ddmmyyyy>.zip`` with three files:

* ``fgroup<date>.csv``: bonds traded in the cash segment (most retail NCDs): code, name, open/high/low/close in rupees
  per bond, trades, ISIN, face value, coupon;
* ``icdm<date>.csv``: corporate debt on the reporting platform: LTP and weighted price as a PERCENT of face value;
* ``wdm<date>.csv``: government securities (not used here).

Bonds trade thinly, so a bond's price is its LAST traded close within ``LOOKBACK_DAYS``, shown with that date. This is
an end-of-day price, never a live one. Every day's file is cached on disk (``cache/bse_debt/``); a past weekday with
no file (a holiday) is remembered as empty, today's absence is not (the file appears after the close). Read-only.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import requests

from .timezones import IST

log = logging.getLogger(__name__)

URL = "https://www.bseindia.com/download/Bhavcopy/Debt/DEBTBHAVCOPY{ddmmyyyy}.zip"
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.bseindia.com/"}
LOOKBACK_DAYS = 30            # calendar days searched for a bond's last trade
SOURCE = "BSE close"


def _num(v: Any) -> float | None:
    try:
        x = float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return x if x > 0 else None


def _row(r: dict[str, Any], *names: str) -> Any:
    """A column by any of its spellings (BSE's headers carry stray spaces and odd case)."""
    norm = {str(k).strip().lower(): v for k, v in r.items() if k is not None}
    for n in names:
        if n.lower() in norm:
            return norm[n.lower()]
    return None


def parse_bhavcopy(blob: bytes) -> dict[str, dict[str, Any]]:
    """{key: {"price", "name", "isin", "code", "face"}} from one day's zip. Keys are the BSE code, the short name
    (the trading symbol Groww shows, e.g. 1225PCL29) and the ISIN, upper-case. The cash-segment close (fgroup) wins;
    the reporting platform's LTP (icdm, % of face) fills in bonds that only traded there."""
    out: dict[str, dict[str, Any]] = {}
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        names = z.namelist()
        groups = sorted(names, key=lambda n: 0 if n.lower().startswith("fgroup") else 1)
        for name in groups:
            low = name.lower()
            if not (low.startswith("fgroup") or low.startswith("icdm")):
                continue
            # some days' files carry stray carriage returns inside lines (seen 21 and 28 Sep 2026): drop them
            text = z.read(name).decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "")
            for r in csv.DictReader(io.StringIO(text, newline="")):
                isin = str(_row(r, "ISIN No.", "ISIN") or "").strip().upper()
                face = _num(_row(r, "FACE VALUE", "Face Value"))
                if low.startswith("fgroup"):
                    code = str(_row(r, "Security_cd") or "").strip().upper()
                    short = str(_row(r, "sc_name") or "").strip().upper()
                    price = _num(_row(r, "Close Price"))
                else:
                    code = ""
                    short = str(_row(r, "Security Code") or "").strip().upper()
                    ltp = _num(_row(r, "LTP"))
                    price = round(ltp * face / 100, 4) if ltp is not None and face else None
                if price is None:
                    continue
                q = {"price": price, "name": short or code, "isin": isin or None, "code": code or None, "face": face}
                for k in (code, short, isin):
                    if k and k not in out:   # fgroup first: its close is the rupee price per bond
                        out[k] = q
    return out


class BondPrices:
    """``lookup(symbol)`` -> {"price", "date", "source"} or None. Pass ``session`` and ``today`` to inject fakes."""

    def __init__(self, cache_dir: Path | None, session: Any | None = None, timeout: float = 20.0,
                 today: Callable[[], date] | None = None, lookback_days: int = LOOKBACK_DAYS):
        self.dir = Path(cache_dir) / "bse_debt" if cache_dir else None
        self.session = session or requests.Session()
        self.timeout = timeout
        self._today = today or (lambda: datetime.now(IST).date())
        self.lookback_days = lookback_days
        self._mem: dict[str, dict[str, dict[str, Any]] | None] = {}
        self.last_error: str | None = None

    def _day(self, d: date) -> dict[str, dict[str, Any]] | None:
        """One day's quotes (an empty dict for a day with no file), or None when the file could not be read now."""
        key = d.isoformat()
        if key in self._mem:
            return self._mem[key]
        path = self.dir / f"{key}.json" if self.dir else None
        if path is not None and path.exists():
            try:
                saved = json.loads(path.read_text())
                self._mem[key] = saved
                return dict(saved)
            except (OSError, ValueError):
                pass
        try:
            r = self.session.get(URL.format(ddmmyyyy=d.strftime("%d%m%Y")), headers=HEADERS, timeout=self.timeout)
        except Exception as e:  # noqa: BLE001 - no network now: try again next time, nothing cached
            self.last_error = f"{type(e).__name__}: {e}"[:200]
            self._mem[key] = None
            return None
        status = getattr(r, "status_code", 200)
        data: dict[str, dict[str, Any]] | None
        if status == 404:
            data = {} if d < self._today() else None   # a holiday; today: not yet
        elif status != 200:
            self.last_error = f"BSE debt bhavcopy HTTP {status}"
            data = None
        else:
            try:
                data = parse_bhavcopy(r.content)
            except zipfile.BadZipFile:   # BSE answers a day with no file with an HTML page, not a 404
                data = {} if d < self._today() else None
            except (KeyError, ValueError, csv.Error) as e:   # a real file we could not read: never cached as empty
                self.last_error = f"BSE debt bhavcopy {key} unreadable: {e}"[:200]
                data = None
        self._mem[key] = data
        if data is not None and path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data))
        return data

    def lookup(self, symbol: str, isin: str | None = None) -> dict[str, Any] | None:
        """The bond's last traded price within the lookback, newest day first, with its date. None when it did not
        trade in that time (or BSE could not be read)."""
        keys = [k for k in (str(symbol or "").strip().upper().removesuffix(".BO"), (isin or "").strip().upper()) if k]
        if not keys:
            return None
        today = self._today()
        for back in range(self.lookback_days + 1):
            d = today - timedelta(days=back)
            if d.weekday() >= 5:
                continue
            quotes = self._day(d)
            if not quotes:
                continue
            for k in keys:
                q = quotes.get(k)
                if q is not None:
                    return {"price": q["price"], "date": d.isoformat(), "source": SOURCE, "face": q.get("face")}
        return None
