"""NSE India disclosed-trade data: bulk deals, block deals and insider (PIT) filings.

Indian equivalents of the US "politician / insider trades" feed:

* **Bulk deals**  - any client trading > 0.5% of a company's shares in a day (published same day).
* **Block deals** - single trades >= 5 lakh shares or >= Rs 5 crore.
* **Insider (PIT) disclosures** - SEBI-mandated promoter / director / KMP transactions.

Well-known investors (Ashish Kacholia, Mukul Agrawal, Vijay Kedia, the Jhunjhunwala
family, Dolly Khanna, ...) and institutions show up in bulk/block deals under their
client name, which is what ``WATCH_INVESTOR`` is matched against.

NSE's JSON endpoints are not officially documented and need browser-like headers.
Bulk/block endpoints answer without cookies; the insider endpoint is best-effort.
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import date, timedelta
from typing import Any, Iterable

import requests

from .quiver import DisclosedTrade, filter_by_investor

log = logging.getLogger(__name__)

BASE_URL = "https://www.nseindia.com"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/market-data/large-deals",
}

_MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


def _iso(d: str | None) -> str:
    """'08-Oct-2026' / '08-OCT-2026' / '08-10-2026' -> '2026-10-08'."""
    if not d:
        return ""
    parts = str(d).strip().split("-")
    if len(parts) == 3 and len(parts[0]) <= 2:
        dd, mm, yy = parts
        if mm.upper() in _MONTHS:
            mm = f"{_MONTHS[mm.upper()]:02d}"
        return f"{yy}-{mm}-{dd.zfill(2)}"
    return str(d)[:10]


def _nse_date(d: date) -> str:
    return d.strftime("%d-%m-%Y")


def _norm_deal(row: dict[str, Any], kind: str) -> DisclosedTrade:
    """Snapshot rows (buySell/clientName/...) and historical rows (BD_*) both land here."""
    side = row.get("buySell") or row.get("BD_BUY_SELL") or ""
    qty = row.get("qty") or row.get("BD_QTY_TRD") or ""
    price = row.get("watp") or row.get("BD_TP_WATP") or ""
    d = _iso(row.get("date") or row.get("BD_DT_DATE"))
    return DisclosedTrade(
        source=kind,
        investor=str(row.get("clientName") or row.get("BD_CLIENT_NAME") or "").strip(),
        ticker=str(row.get("symbol") or row.get("BD_SYMBOL") or "").upper().strip(),
        transaction={"BUY": "Purchase", "SELL": "Sale"}.get(str(side).upper(), str(side) or "Trade"),
        transaction_date=d,
        report_date=d,
        size=f"{qty} sh @ ₹{price}" if price else f"{qty} sh",
        raw=row,
    )


def _parse_deals_csv(text: str) -> list[dict[str, Any]]:
    """CSV columns: Date, Symbol, Security Name, Client Name, Buy / Sell, Quantity Traded,
    Trade Price / Wght. Avg. Price, Remarks (headers carry trailing spaces; quantities use
    Indian digit grouping like 3,50,000)."""
    reader = csv.reader(io.StringIO(text))
    header = [h.strip().lower() for h in next(reader, [])]

    def col(name: str) -> int:
        for i, h in enumerate(header):
            if h.startswith(name):
                return i
        raise KeyError(name)

    i_date, i_sym, i_name, i_client = col("date"), col("symbol"), col("security"), col("client")
    i_side, i_qty, i_px = col("buy"), col("quantity"), col("trade price")
    i_rem = col("remarks") if any(h.startswith("remarks") for h in header) else None
    rows = []
    for r in reader:
        if len(r) <= i_px:
            continue
        rows.append({
            "date": r[i_date].strip(), "symbol": r[i_sym].strip(), "name": r[i_name].strip(),
            "clientName": r[i_client].strip(), "buySell": r[i_side].strip(),
            "qty": r[i_qty].replace(",", "").strip(), "watp": r[i_px].replace(",", "").strip(),
            "remarks": r[i_rem].strip() if i_rem is not None else "",
        })
    return rows


def _norm_insider(row: dict[str, Any]) -> DisclosedTrade:
    ttype = str(row.get("tdpTransactionType") or row.get("transactionType") or "").strip()
    transaction = {"BUY": "Purchase", "SELL": "Sale"}.get(ttype.upper(), ttype or "Trade")
    qty = row.get("secAcq") or row.get("secAcqDis") or ""
    val = row.get("secVal") or ""
    return DisclosedTrade(
        source="insider",
        investor=str(row.get("acqName") or "").strip(),
        ticker=str(row.get("symbol") or "").upper().strip(),
        transaction=transaction,
        transaction_date=_iso(row.get("acqfromDt") or row.get("date")),
        report_date=_iso(row.get("intimDt") or row.get("date")),
        size=f"{qty} sh" + (f" (₹{val})" if val else ""),
        raw=row,
    )


class NSEClient:
    """Pass ``session`` to inject a fake in tests."""

    def __init__(self, session: requests.Session | None = None, timeout: float = 30.0,
                 base_url: str = BASE_URL):
        self.session = session or requests.Session()
        self.timeout = timeout
        self.base_url = base_url.rstrip("/")
        self._warm = False

    def _get(self, path: str, params: dict[str, Any] | None = None,
             referer: str | None = None) -> Any:
        headers = dict(HEADERS)
        if referer:
            headers["Referer"] = referer
        if not self._warm:
            # Best effort cookie bootstrap; NSE sometimes 403s the homepage, which is fine.
            try:
                self.session.get(self.base_url + "/", headers=headers, timeout=self.timeout)
            except requests.RequestException:
                pass
            self._warm = True
        resp = self.session.get(f"{self.base_url}/{path.lstrip('/')}", headers=headers,
                                params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def _get_text(self, path: str, params: dict[str, Any], referer: str) -> str:
        headers = {**HEADERS, "Accept": "*/*", "Referer": referer}
        resp = self.session.get(f"{self.base_url}/{path.lstrip('/')}", headers=headers,
                                params=params, timeout=self.timeout)
        resp.raise_for_status()
        text = resp.content.decode("utf-8-sig", errors="replace")
        if not text.lstrip().startswith('"Date'):
            raise ValueError("response is not the deals CSV")
        return text

    # -- large deals (today) -------------------------------------------------
    def large_deals(self) -> list[DisclosedTrade]:
        data = self._get("api/snapshot-capital-market-largedeal")
        out = [_norm_deal(r, "bulk") for r in data.get("BULK_DEALS_DATA", [])]
        out += [_norm_deal(r, "block") for r in data.get("BLOCK_DEALS_DATA", [])]
        return [t for t in out if t.investor]

    # -- historical bulk / block deals --------------------------------------
    def historical_deals(self, days: int = 30, kind: str = "bulk",
                         end: date | None = None) -> list[DisclosedTrade]:
        """Deals over the last ``days`` days.

        NSE's JSON endpoint silently caps the answer at ~70 rows, so the CSV download
        (which returns the whole range) is used first and JSON only as a fallback.
        """
        end = end or date.today()
        if days > 92:  # NSE serves long ranges unreliably; stitch 90-day windows
            out: list[DisclosedTrade] = []
            cursor = end
            remaining = days
            while remaining > 0:
                span = min(90, remaining)
                out += self.historical_deals(span, kind, end=cursor)
                cursor = cursor - timedelta(days=span + 1)
                remaining -= span + 1
            return _dedupe(out)
        start = end - timedelta(days=days)
        params = {"optionType": f"{kind}_deals", "from": _nse_date(start), "to": _nse_date(end)}
        path = "api/historicalOR/bulk-block-short-deals"
        referer = f"{self.base_url}/report-detail/display-bulk-and-block-deals"
        rows: list[dict[str, Any]]
        try:
            rows = _parse_deals_csv(self._get_text(path, {**params, "csv": "true"}, referer))
        except Exception as e:  # noqa: BLE001
            log.warning("NSE CSV download failed (%s); falling back to capped JSON", e)
            data = self._get(path, params=params, referer=referer)
            rows = data.get("data", []) if isinstance(data, dict) else data
        return [t for t in (_norm_deal(r, kind) for r in rows) if t.investor]

    # -- insider (PIT) disclosures ------------------------------------------
    def insider_trades(self, days: int = 30, end: date | None = None) -> list[DisclosedTrade]:
        end = end or date.today()
        start = end - timedelta(days=days)
        data = self._get("api/corporates-pit",
                         params={"index": "equities", "from_date": _nse_date(start),
                                 "to_date": _nse_date(end)},
                         referer=f"{self.base_url}/companies-listing/corporate-filings-insider-trading")
        rows = data.get("data", []) if isinstance(data, dict) else data
        return [t for t in (_norm_insider(r) for r in rows) if t.investor]

    # -- public --------------------------------------------------------------
    def trades_for_investor(self, investor: str, source: str = "deals",
                            days: int = 30) -> list[DisclosedTrade]:
        """Recent disclosed trades whose client / acquirer name contains ``investor``.

        ``source``: ``deals`` (bulk + block, default), ``bulk``, ``block`` or ``insider``.
        """
        rows: list[DisclosedTrade] = []
        if source in {"deals", "bulk"}:
            rows += self.historical_deals(days, "bulk")
        if source in {"deals", "block"}:
            rows += self.historical_deals(days, "block")
        if source == "insider":
            rows += self.insider_trades(days)
        return _dedupe(filter_by_investor(rows, investor))

    def history_for_ticker(self, investor: str, ticker: str, days: int = 365) -> list[DisclosedTrade]:
        rows = self.historical_deals(days, "bulk") + self.historical_deals(days, "block")
        return _dedupe(filter_by_investor([t for t in rows if t.ticker == ticker.upper()], investor))


def _dedupe(rows: Iterable[DisclosedTrade]) -> list[DisclosedTrade]:
    """Drop repeats, ignoring the source label: a large trade can be listed by NSE as
    both a bulk deal and a block deal, and it should reach the agent once."""
    seen: set[tuple[str, ...]] = set()
    out = []
    for t in rows:
        k = (t.investor, t.ticker, t.transaction, t.transaction_date, t.size)
        if k not in seen:
            seen.add(k)
            out.append(t)
    return out
