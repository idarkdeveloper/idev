"""Point-in-time fundamentals from NSE's quarterly results filings (XBRL).

Yahoo only gives today's numbers, which can't be backtested. NSE publishes every listed
company's quarterly results as XBRL, with the moment each filing was broadcast, so the
numbers known on any past date can be rebuilt:

* ``api/corporates-financial-results`` lists filings for quarters up to Dec 2024
  (XBRL from about Dec 2018).
* ``api/integrated-filing-results`` (type "Integrated Filing- Financials") lists them
  from 2025, after SEBI's switch to integrated filing.

Each filing's XBRL is downloaded once and reduced to one small JSON record: the
quarter's profit and revenue, and, when the filing carries a balance sheet (half-years
and years), equity and borrowings. Contexts are matched by their dates, not their ids,
so only the current quarter and the current balance-sheet date are read, never the
comparatives. Banks report profit and equity under other tags, handled by fallbacks.

``point_in_time(records, day)`` then uses only filings broadcast on or before ``day``.
Downloads are sequential with a short pause and stop for the run if NSE starts
refusing; the cache makes a rerun pick up where the last one stopped.
"""

from __future__ import annotations

import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

import requests

log = logging.getLogger(__name__)

BASE = "https://www.nseindia.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept": "application/json, text/plain, */*", "Accept-Language": "en-US,en;q=0.9"}
OLD_REFERER = BASE + "/companies-listing/corporate-filings-financial-results"
NEW_REFERER = BASE + "/companies-listing/corporate-integrated-filing"

PROFIT_TAGS = ("ProfitOrLossAttributableToOwnersOfParent", "ProfitLossForPeriod",
               "ProfitLossAfterTaxesMinorityInterestAndShareOfProfitLossOfAssociates",
               "NetProfitLossForThePeriod", "ProfitLossForThePeriod")
REVENUE_TAGS = ("RevenueFromOperations", "Income")
EQUITY_TAGS = ("EquityAttributableToOwnersOfParent", "Equity")
EPS_TAGS = ("BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
            "BasicEarningsLossPerShareFromContinuingOperations", "BasicEarningsPerShareAfterExtraordinaryItems")
MONTHS = {m: i for i, m in enumerate(("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), 1)}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _date(s: str | None) -> date | None:
    """'31-Dec-2024', '30-JUN-2026', '2026-03-31' or with a time after the date."""
    if not s:
        return None
    s = s.strip()
    try:
        if re.match(r"\d{4}-\d{2}-\d{2}", s):
            return date.fromisoformat(s[:10])
        d, m, y = s[:11].split("-")
        return date(int(y), MONTHS[m.upper()[:3]], int(d))
    except (ValueError, KeyError):
        return None


def _stamp(s: str | None) -> str | None:
    """Broadcast time as ISO, e.g. '23-Jan-2025 18:43:50' -> '2025-01-23T18:43:50'."""
    d = _date(s)
    if d is None:
        return None
    t = re.search(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", s[11:] if s else "")
    return datetime(d.year, d.month, d.day, int(t.group(1)), int(t.group(2)), int(t.group(3) or 0)).isoformat() \
        if t else d.isoformat() + "T00:00:00"


def parse_results_xbrl(xml_text: str, period_end: date) -> dict[str, Any]:
    """The current quarter, year-to-date and balance sheet out of one results filing.

    NSE's results taxonomy uses fixed context ids: ``OneD`` is the quarter, ``FourD`` the
    financial year to date (the full year in a March filing) and ``OneI`` the balance-sheet
    date. The dates declared on those contexts are not reliable in older filings (some
    give FourD the quarter's dates; some don't declare OneD at all), so the ids are used.
    """
    if re.search(r"<!(DOCTYPE|ENTITY)", xml_text, re.I):  # no DTDs: blocks entity expansion
        raise ValueError("refusing XML with a DOCTYPE/ENTITY declaration")
    root = ET.fromstring(xml_text.encode("utf-8"))
    facts: dict[tuple[str, str], float] = {}
    for el in root.iter():
        cid = el.get("contextRef")
        if cid not in ("OneD", "FourD", "OneI") or el.text is None:
            continue
        try:
            facts.setdefault((_local(el.tag), cid), float(el.text.strip()))
        except ValueError:
            continue

    def pick(tags: Iterable[str], *ids: str) -> float | None:
        for t in tags:
            for cid in ids:
                if (t, cid) in facts:
                    return facts[(t, cid)]
        return None

    equity = pick(EQUITY_TAGS, "OneI")
    if equity is None:  # banks: capital + reserves
        cap, res = pick(("Capital", "EquityShareCapital"), "OneI"), pick(("ReservesAndSurplus", "OtherEquity"), "OneI")
        equity = cap + res if cap is not None and res is not None else None
    bc, bn = pick(("BorrowingsCurrent",), "OneI"), pick(("BorrowingsNoncurrent",), "OneI")
    borrowings = (bc or 0.0) + (bn or 0.0) if bc is not None or bn is not None else pick(("Borrowings",), "OneI")
    paid_up = pick(("PaidUpValueOfEquityShareCapital",), "OneD", "FourD")
    face = pick(("FaceValueOfEquityShareCapital",), "OneD", "FourD")
    profit_q = pick(PROFIT_TAGS, "OneD")
    profit_ytd = pick(PROFIT_TAGS, "FourD")
    if profit_ytd is None and period_end.month == 6:  # first quarter: year to date is the quarter
        profit_ytd = profit_q
    return {"period_end": period_end.isoformat(), "profit_q": profit_q, "profit_ytd": profit_ytd,
            "revenue_q": pick(REVENUE_TAGS, "OneD"), "eps_q": pick(EPS_TAGS, "OneD"),
            "equity": equity, "borrowings": borrowings,
            "shares": paid_up / face if paid_up and face else None}


class ResultsHistory:
    """Lists and caches quarterly results filings per symbol, then answers point-in-time."""

    def __init__(self, cache_dir: Path, *, session: requests.Session | None = None, pause: float = 0.25,
                 max_new_downloads: int = 400, listing_ttl: float = 20 * 3600, sleep: Any = time.sleep,
                 timeout: float = 20.0):
        self.dir = Path(cache_dir) / "nse_results"
        self.session = session or requests.Session()
        self.pause = pause
        self.max_new_downloads = max_new_downloads
        self.listing_ttl = listing_ttl
        self.sleep = sleep
        self.timeout = timeout
        self.downloads = 0
        self.refused = 0
        self._warm = False

    # -- listings ---------------------------------------------------------------
    def _get_json(self, path: str, params: dict[str, Any], referer: str) -> Any:
        if not self._warm:
            try:
                self.session.get(BASE + "/", headers=HEADERS, timeout=self.timeout)
            except requests.RequestException:
                pass
            self._warm = True
        r = self.session.get(f"{BASE}/{path}", params=params, headers={**HEADERS, "Referer": referer},
                             timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def filings(self, symbol: str) -> list[dict[str, Any]]:
        """Both feeds, normalised: period_end, available (broadcast), consolidated, xbrl."""
        symbol = symbol.upper()
        path = self.dir / "listings" / f"{re.sub(r'[^A-Z0-9_.&-]', '_', symbol)}.json"
        if path.exists() and time.time() - path.stat().st_mtime < self.listing_ttl:
            return json.loads(path.read_text(encoding="utf-8"))
        out: list[dict[str, Any]] = []
        old = self._get_json("api/corporates-financial-results",
                             {"index": "equities", "symbol": symbol, "period": "Quarterly"}, OLD_REFERER)
        for r in old if isinstance(old, list) else old.get("data", []):
            if (r.get("xbrl") or "").endswith(".xml"):
                out.append({"period_end": _date(r.get("toDate")), "available": _stamp(r.get("broadCastDate") or r.get("filingDate")),
                            "consolidated": (r.get("consolidated") or "").lower() == "consolidated", "xbrl": r["xbrl"]})
        new = self._get_json("api/integrated-filing-results",
                             {"index": "equities", "symbol": symbol, "period_ended": "all",
                              "type": "Integrated Filing- Financials"}, NEW_REFERER)
        for r in new.get("data", []) if isinstance(new, dict) else new:
            if (r.get("xbrl") or "").endswith(".xml"):
                out.append({"period_end": _date(r.get("qe_Date")), "available": _stamp(r.get("broadcast_Date")),
                            "consolidated": (r.get("consolidated") or "").lower() == "consolidated", "xbrl": r["xbrl"]})
        out = [{**f, "period_end": f["period_end"].isoformat()} for f in out if f["period_end"] and f["available"]]
        out.sort(key=lambda f: (f["period_end"], f["available"]))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out), encoding="utf-8")
        return out

    # -- one filing ---------------------------------------------------------------
    def _record_path(self, url: str) -> Path:
        name = re.sub(r"[^A-Za-z0-9_.-]", "_", url.rsplit("/", 1)[-1])
        return self.dir / "filings_v2" / (re.sub(r"\.xml$", "", name, flags=re.I) + ".json")

    def record(self, filing: dict[str, Any]) -> dict[str, Any] | None:
        """Parsed filing from cache, else downloaded (unless this run's budget is spent)."""
        path = self._record_path(filing["xbrl"])
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                pass
        if self.refused >= 2 or self.downloads >= self.max_new_downloads:
            return None
        if self.downloads and self.pause:
            self.sleep(self.pause)
        self.downloads += 1
        try:
            r = self.session.get(filing["xbrl"], headers={"User-Agent": UA, "Accept": "application/xml,*/*"},
                                 timeout=self.timeout)
            r.raise_for_status()
            text = r.content.decode("utf-8-sig", errors="replace")
            rec = parse_results_xbrl(text, date.fromisoformat(filing["period_end"]))
        except (requests.HTTPError, requests.Timeout, requests.ConnectionError) as e:
            # NSE's archive throttles by refusing (403/429) or by stalling the connection
            # until it times out. Either way: not cached, retried on a later run; back off
            # once with a fresh connection, and stop this run's downloads on a second refusal.
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status is None or status in (403, 429) or status >= 500:
                self.refused += 1
                if self.refused < 2:
                    self.sleep(60)
                    self.session = requests.Session()
                    self._warm = False
                return None
            # 404 and other permanent answers: remember it, so it isn't fetched again every run
            rec = {"period_end": filing["period_end"], "error": f"HTTP {status}"}
        except Exception as e:  # noqa: BLE001 - an unparseable filing is recorded as empty
            log.debug("results filing %s unreadable: %s", filing["xbrl"], e)
            rec = {"period_end": filing["period_end"], "error": f"{type(e).__name__}: {e}"}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rec), encoding="utf-8")
        return rec

    def history(self, symbol: str, *, since: str = "2018-01-01") -> list[dict[str, Any]]:
        """One record per quarter: consolidated when filed, else standalone; the first
        version broadcast (later revisions are ignored, as a trader then wouldn't have them)."""
        chosen: dict[str, dict[str, Any]] = {}
        for f in self.filings(symbol):
            if f["period_end"] < since:
                continue
            cur = chosen.get(f["period_end"])
            better = cur is None or (f["consolidated"] and not cur["consolidated"]) or \
                (f["consolidated"] == cur["consolidated"] and f["available"] < cur["available"])
            if better:
                chosen[f["period_end"]] = f
        out = []
        for pe in sorted(chosen):
            f = chosen[pe]
            rec = self.record(f)
            if rec and "error" not in rec:
                out.append({**rec, "available": f["available"], "consolidated": f["consolidated"]})
        return out


def _fy_end(period_end: date) -> date:
    """31 March closing the Indian financial year that contains this quarter end."""
    return date(period_end.year + (1 if period_end.month > 3 else 0), 3, 31)


def _ttm(by_end: dict[str, dict[str, Any]], end: date) -> float | None:
    """Trailing twelve months of profit ending at ``end``, from year-to-date figures:
    YTD now + last full year - YTD a year earlier (the full year itself in March).
    Falls back to the sum of four quarterly figures."""
    r = by_end.get(end.isoformat())
    if r is None:
        return None
    ytd = r.get("profit_ytd")
    if end.month == 3 and ytd is not None:
        return ytd
    prev_same = by_end.get(date(end.year - 1, end.month, end.day).isoformat())
    prev_fy = by_end.get(date(_fy_end(end).year - 1, 3, 31).isoformat())
    if ytd is not None and prev_same and prev_fy and prev_same.get("profit_ytd") is not None             and prev_fy.get("profit_ytd") is not None:
        return ytd + prev_fy["profit_ytd"] - prev_same["profit_ytd"]
    ends = [date(end.year - (1 if end.month - 3 * k <= 0 else 0), (end.month - 3 * k - 1) % 12 + 1, 1) for k in range(4)]
    qs = []
    for d in ends:  # quarter ends: last day of the month
        last = (date(d.year + (d.month == 12), d.month % 12 + 1, 1) - date.resolution)
        q = by_end.get(last.isoformat())
        if q is None or q.get("profit_q") is None:
            return None
        qs.append(q["profit_q"])
    return sum(qs)


def point_in_time(records: list[dict[str, Any]], day: str | date) -> dict[str, Any] | None:
    """Fundamentals as known at the end of ``day``, from filings broadcast by then."""
    cutoff = (day.isoformat() if isinstance(day, date) else str(day)[:10]) + "T23:59:59"
    known = [r for r in records if r["available"] <= cutoff]
    if not known:
        return None
    by_end = {r["period_end"]: r for r in known}
    latest = date.fromisoformat(known[-1]["period_end"])
    profit = _ttm(by_end, latest)
    prev = _ttm(by_end, date(latest.year - 1, latest.month, latest.day))
    bs = next((r for r in reversed(known) if r.get("equity")), None)
    shares = next((r["shares"] for r in reversed(known) if r.get("shares")), None)
    age = (date.fromisoformat(cutoff[:10]) - latest).days
    equity = bs["equity"] if bs else None
    return {"as_of": cutoff[:10], "latest_quarter": latest.isoformat(), "stale": age > 200,
            "ttm_profit": profit, "equity": equity,
            "roe": profit / equity if profit is not None and equity and equity > 0 else None,
            "debt_to_equity": (bs["borrowings"] / equity) if bs and bs.get("borrowings") is not None and equity and equity > 0 else None,
            "earnings_growth": (profit / prev - 1) if profit is not None and prev and prev > 0 else None,
            "shares": shares}


def with_price(m: dict[str, Any] | None, price: float | None) -> dict[str, Any]:
    """Add the price-based value fields (same names as the Yahoo snapshot)."""
    if not m or m.get("stale"):
        return {"error": "no recent results filing"}
    cap = price * m["shares"] if price and m.get("shares") else None
    ey = m["ttm_profit"] / cap if cap and m.get("ttm_profit") is not None else None
    bp = m["equity"] / cap if cap and m.get("equity") else None
    return {"roe": m.get("roe"), "debt_to_equity": m.get("debt_to_equity"), "earnings_growth": m.get("earnings_growth"),
            "earnings_yield": ey, "book_to_price": bp, "pe": 1 / ey if ey and ey > 0 else None,
            "pb": 1 / bp if bp and bp > 0 else None}
