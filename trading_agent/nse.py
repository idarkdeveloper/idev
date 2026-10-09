"""NSE India disclosed-trade data: bulk deals, block deals and insider (PIT) filings.

Indian equivalents of the US "politician / insider trades" feed:

* **Bulk deals**  - any client trading > 0.5% of a company's shares in a day (published same day).
* **Block deals** - single trades >= 5 lakh shares or >= Rs 5 crore.
* **Insider (PIT) disclosures** - SEBI-mandated promoter / director / KMP transactions.

Well-known investors (Ashish Kacholia, Mukul Agrawal, Vijay Kedia, the Jhunjhunwala
family, Dolly Khanna, ...) and institutions show up in bulk/block deals under their
client name, which is what ``WATCH_INVESTOR`` is matched against.

NSE's JSON endpoints are not officially documented and need browser-like headers;
none of the ones used here needs cookies.

Insider (PIT) disclosures moved in May 2026: the old ``api/corporates-pit`` JSON stops
on 2 May 2026, and newer filings are XBRL documents listed by ``api/corporates-pit-gg``
(company, symbol, regulation and a link to the filing's XML). The person, category,
quantity, value and buy/sell are inside each XML, so each filing is downloaded once and
its parsed rows cached on disk as small JSON (the XML itself is ~100 KB and is not
kept); a routine run only fetches filings it hasn't seen. NSE's archive
host blocks bursts (HTTP 403 for about a minute), so downloads are sequential over one
connection with a short pause, capped per run, and stop early if NSE starts refusing.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from pathlib import Path
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

_TRADE_FIELDS = ("source", "investor", "ticker", "transaction", "transaction_date", "report_date",
                 "size", "raw")

# First day served only by the XBRL insider feed; earlier days come from the old JSON.
PIT_XBRL_SINCE = date(2026, 5, 1)

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


def _norm_announcement(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row.get("seq_id") or row.get("dt") or ""),
        "symbol": str(row.get("symbol") or "").upper(),
        "company": str(row.get("sm_name") or ""),
        "at": str(row.get("sort_date") or row.get("an_dt") or ""),
        "category": str(row.get("desc") or ""),
        "text": str(row.get("attchmntText") or "").strip(),
        "file": row.get("attchmntFile") or "",
    }


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


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_pit_xbrl(xml_text: str, filing: dict[str, Any] | None = None) -> list[DisclosedTrade]:
    """One DisclosedTrade per ``DisclosureN`` context in an NSE insider XBRL filing."""
    filing = filing or {}
    # XBRL filings never declare a DTD; refusing one blocks entity-expansion and
    # external-entity tricks without needing defusedxml.
    if re.search(r"<!(DOCTYPE|ENTITY)", xml_text, re.I):
        raise ValueError("refusing XML with a DOCTYPE/ENTITY declaration")
    root = ET.fromstring(xml_text.encode("utf-8"))
    main: dict[str, str] = {}
    disclosures: dict[str, dict[str, str]] = {}
    for el in root.iter():
        ctx = el.get("contextRef")
        if not ctx or el.text is None:
            continue
        name, value = _local(el.tag), el.text.strip()
        if ctx.lower().startswith("disclosure"):
            disclosures.setdefault(ctx, {})[name] = value
        else:
            main.setdefault(name, value)
    symbol = (main.get("Symbol") or filing.get("symbol") or "").upper().strip()
    out = []
    for ctx in sorted(disclosures, key=lambda c: int(re.sub(r"[^0-9]", "", c) or 0)):
        d = disclosures[ctx]
        person = (d.get("NameOfThePerson") or "").strip()
        if not person:
            continue
        ttype = (d.get("SecuritiesAcquiredOrDisposedTransactionType") or "").strip()
        qty = d.get("SecuritiesAcquiredOrDisposedNumberOfSecurity") or ""
        val = d.get("SecuritiesAcquiredOrDisposedValueOfSecurity") or ""
        out.append(DisclosedTrade(
            source="insider",
            investor=person,
            ticker=symbol,
            transaction={"BUY": "Purchase", "SELL": "Sale"}.get(ttype.upper(), ttype or "Trade"),
            transaction_date=_iso(d.get("DateOfAllotmentAdviceOrAcquisitionOfSharesOrSaleOfSharesSpecifyFromDate")),
            report_date=_iso(d.get("DateOfIntimationToCompany") or main.get("DateOfFiling")),
            size=f"{qty} sh" + (f" (₹{val})" if val else ""),
            raw={"category": d.get("CategoryOfPerson"), "mode": d.get("ModeOfAcquisitionOrDisposal"),
                 "instrument": d.get("TypeOfInstrument"), "company": main.get("NameOfTheCompany"),
                 "regulation": main.get("DisclosureUnderRegulation") or filing.get("regulation"),
                 "held_after": d.get("SecuritiesHeldPostAcquistionOrDisposalNumberOfSecurity"),
                 "filed_at": filing.get("broadcastDateTime"), "xbrl": filing.get("xmlFileName")},
        ))
    return out


_TICKER = re.compile(r"^[A-Z0-9&.\-]{1,20}$")


def check_ticker(symbol: str) -> str:
    """Upper-cased NSE ticker, or ValueError: it ends up in file names, so keep it plain."""
    sym = str(symbol or "").strip().upper()
    if not _TICKER.match(sym) or sym.startswith("."):
        raise ValueError(f"{symbol!r} is not a valid ticker (letters, digits, & . - only, up to 20)")
    return sym


class NSEClient:
    """Pass ``session`` to inject a fake in tests."""

    def __init__(self, session: requests.Session | None = None, timeout: float = 30.0,
                 base_url: str = BASE_URL, cache_dir: Path | None = None,
                 max_insider_filings: int = 1500, max_new_downloads: int = 400,
                 pause: float = 0.25, sleep: Any = time.sleep):
        self.session = session or requests.Session()
        self.timeout = timeout
        self.base_url = base_url.rstrip("/")
        self._warm = False
        # Insider filings never change once filed, so their parsed rows are cached by file name.
        self.pit_cache = Path(cache_dir) / "nse_pit" if cache_dir else None
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self._pit_mem: dict[str, list[DisclosedTrade]] = {}
        self.max_insider_filings = max_insider_filings
        self.max_new_downloads = max_new_downloads  # the rest wait for the next run
        self.pause = pause
        self.sleep = sleep

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
    def insider_trades(self, days: int = 30, end: date | None = None,
                       symbol: str | None = None) -> list[DisclosedTrade]:
        end = end or date.today()
        start = end - timedelta(days=days)
        rows: list[DisclosedTrade] = []
        if start < PIT_XBRL_SINCE:  # the old JSON feed, which stops in early May 2026
            params = {"index": "equities", "from_date": _nse_date(start),
                      "to_date": _nse_date(min(end, PIT_XBRL_SINCE))}
            if symbol:
                params["symbol"] = symbol.upper()
            data = self._get("api/corporates-pit", params=params, referer=self._pit_referer)
            legacy = data.get("data", []) if isinstance(data, dict) else data
            rows += [t for t in (_norm_insider(r) for r in legacy) if t.investor]
        if end >= PIT_XBRL_SINCE:
            rows += self.insider_xbrl_trades(max(start, PIT_XBRL_SINCE), end, symbol=symbol)
        return _dedupe(rows)

    @property
    def _pit_referer(self) -> str:
        return f"{self.base_url}/companies-listing/corporate-filings-insider-trading"

    def insider_filings(self, start: date, end: date, symbol: str | None = None) -> list[dict[str, Any]]:
        """XBRL insider filings (newest first) between two dates, without their contents."""
        params = {"index": "equities", "from_date": _nse_date(start), "to_date": _nse_date(end)}
        if symbol:
            params["symbol"] = symbol.upper()
        data = self._get("api/corporates-pit-gg", params=params, referer=self._pit_referer)
        rows = data.get("data", []) if isinstance(data, dict) else data
        return [r for r in rows if r.get("xmlFileName")]

    def insider_xbrl_trades(self, start: date, end: date, symbol: str | None = None) -> list[DisclosedTrade]:
        filings = self.insider_filings(start, end, symbol)
        if len(filings) > self.max_insider_filings:
            log.warning("NSE listed %d insider filings; reading the newest %d", len(filings),
                        self.max_insider_filings)
            filings = filings[:self.max_insider_filings]

        out: list[DisclosedTrade] = []
        downloads = refused = skipped = unreadable = 0
        for f in filings:
            url = f["xmlFileName"]
            cached = self._pit_cached(url)
            if cached is None and (refused >= 2 or downloads >= self.max_new_downloads):
                skipped += 1
                continue
            try:
                if cached is None:
                    downloads += 1
                    if downloads > 1 and self.pause:
                        self.sleep(self.pause)
                    cached = self._pit_fetch(url, f)
            except (requests.HTTPError, requests.Timeout, requests.ConnectionError) as e:
                status = getattr(getattr(e, "response", None), "status_code", None)
                if status is None or status in (403, 429) or status >= 500:  # refused, or stalled
                    refused += 1
                    if refused < 2:
                        self.sleep(60)  # NSE's archive block lifts after about a minute
                skipped += 1
                continue
            except Exception as e:  # noqa: BLE001 - one bad filing must not sink the rest
                log.debug("insider filing %s unreadable: %s", url, e)
                unreadable += 1
                continue
            out += cached
        if skipped or unreadable:
            log.warning("insider filings: %d read, %d left for the next run%s, %d unreadable",
                        len(filings) - skipped - unreadable, skipped,
                        " (NSE refused downloads)" if refused >= 2 else "", unreadable)
        return out

    @staticmethod
    def _pit_name(url: str) -> str:
        base = re.sub(r"[^A-Za-z0-9_.-]", "_", url.rsplit("/", 1)[-1]) or "filing"
        return re.sub(r"\.xml$", "", base, flags=re.I) + ".json"

    def _pit_cached(self, url: str) -> list[DisclosedTrade] | None:
        name = self._pit_name(url)
        if name in self._pit_mem:
            return self._pit_mem[name]
        path = self.pit_cache / name if self.pit_cache else None
        if path is None or not path.exists():
            return None
        try:
            rows = [DisclosedTrade(**{k: r[k] for k in _TRADE_FIELDS}) for r in json.loads(path.read_text("utf-8"))]
        except (ValueError, KeyError, TypeError):
            return None  # damaged cache entry: download again
        self._pit_mem[name] = rows
        return rows

    def _pit_fetch(self, url: str, filing: dict[str, Any]) -> list[DisclosedTrade]:
        """Download and parse one filing, caching the parsed rows."""
        resp = self.session.get(url, headers={**HEADERS, "Accept": "application/xml,*/*"},
                                timeout=self.timeout)
        resp.raise_for_status()
        text = resp.content.decode("utf-8-sig", errors="replace")
        if not text.lstrip().startswith("<"):
            raise ValueError("not an XML document")
        rows = parse_pit_xbrl(text, filing)
        name = self._pit_name(url)
        if self.pit_cache is not None:
            self.pit_cache.mkdir(parents=True, exist_ok=True)
            (self.pit_cache / name).write_text(
                json.dumps([{k: getattr(t, k) for k in _TRADE_FIELDS} for t in rows], ensure_ascii=False),
                encoding="utf-8")
        self._pit_mem[name] = rows
        return rows

    # -- corporate announcements ---------------------------------------------
    def announcements(self, symbol: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        """Recent NSE corporate announcements, newest first, for one symbol or all equities."""
        params: dict[str, Any] = {"index": "equities"}
        if symbol:
            params["symbol"] = symbol.upper()
        data = self._get("api/corporate-announcements", params=params,
                         referer=f"{self.base_url}/companies-listing/corporate-filings-announcements")
        rows = data if isinstance(data, list) else data.get("data", [])
        out = [_norm_announcement(r) for r in rows]
        out.sort(key=lambda a: a["at"], reverse=True)
        return out[:limit]

    def announcement_history(self, symbol: str, max_age_s: float = 86400.0) -> list[dict[str, Any]]:
        """Every NSE announcement for one company, newest first (back to 2004 for old listings).

        NSE's date-range query times out, but the per-symbol query returns the whole history,
        so Replay fetches it once a day and slices it at its clock."""
        sym = check_ticker(symbol)
        path = self.cache_dir / "nse_ann" / f"{sym}.json" if self.cache_dir else None
        if path and path.exists() and time.time() - path.stat().st_mtime < max_age_s:
            return json.loads(path.read_text(encoding="utf-8"))
        data = self._get("api/corporate-announcements", params={"index": "equities", "symbol": sym},
                         referer=f"{self.base_url}/companies-listing/corporate-filings-announcements")
        rows = data if isinstance(data, list) else data.get("data", [])
        out = sorted((_norm_announcement(r) for r in rows), key=lambda a: a["at"], reverse=True)
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(out), encoding="utf-8")
        return out

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
