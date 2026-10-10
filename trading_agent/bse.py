"""BSE India bulk and block deals: a second source beside NSE's (nse.py).

Many mid and small-cap deals by the investors we follow happen only on BSE. The public page
``beta.bseindia.com/markets/equity/EQReports/BulknBlockDeals.aspx`` (no login) is an ASP.NET WebForms page
(``www`` now serves a JavaScript app shell). Verified by hand on 10 Oct 2026. Per deal type (the
``rblDT`` select: 1 = Bulk Deal, 2 = Block Deal) the flow is the one a browser makes:

1. GET the page (browser-like headers, cookies kept) and read its hidden fields;
2. POST them back with ``rblDT``, ``chkAllMarket=on``, ``txtDate`` / ``txtToDate`` (DD/MM/YYYY) and
   ``btnSubmit=Submit``;
3. take the hidden fields of that answer and POST again with ``__EVENTTARGET=...btnDownload`` (no
   ``btnSubmit``): the body is a plain CSV (sent as application/vnd.ms-excel):
   ``Deal Date,Security Code,Company,Client Name,Deal Type,Quantity,Price`` with DD/MM/YYYY dates,
   Deal Type P / S and Company = the BSE scrip ID (often the NSE symbol).

"Today's deals" are the same flow with today's date as from and to; BSE publishes them after the close,
so they are only asked for after 16:00 IST. Past days never change, so each is cached on disk once.

Nothing here needs a key or an account. If BSE changes the page, ``BSELayoutError`` says which part
is missing; ``BSEClient.deals`` then logs it once a day and returns what is cached, so the watch loop
carries on with NSE alone.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Iterable

import requests

from .quiver import DisclosedTrade, followed_names

log = logging.getLogger(__name__)

PAGE_PATH = "/markets/equity/EQReports/BulknBlockDeals.aspx"
DEFAULT_HOST = "beta.bseindia.com"  # www.bseindia.com now serves a JavaScript shell, not this form
FALLBACK_HOST = None
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
IST = timezone(timedelta(hours=5, minutes=30))
PUBLISHED_AFTER = (16, 0)  # IST: today's deals are on the page after the close
MAX_SPAN_DAYS = 90
MIN_INTERVAL_S = 2.0
RETRY_AFTER_FAILURE_S = 900.0
TODAY_TTL_S = 600.0

# The page's control names, matched by their ending (ASP.NET prefixes them with ctl00$ContentPlaceHolder1$).
FIELD_SUFFIXES = {
    "kind": "rblDT",              # <select>: deal type, "1" = Bulk Deal, "2" = Block Deal
    "all_market": "chkAllMarket",  # checkbox: every market segment
    "from": "txtDate",            # DD/MM/YYYY
    "to": "txtToDate",
    "submit": "btnSubmit",        # step 1 button (value "Submit")
    "download": "btnDownload",    # step 2: a __doPostBack link, so it is the __EVENTTARGET
}
KIND_VALUES = {"bulk": "1", "block": "2"}  # fallback when the option texts cannot be read
REQUIRED_HIDDEN = ("__VIEWSTATE", "__EVENTVALIDATION")


class BSEError(RuntimeError):
    """BSE could not be read (network, refusal)."""


class BSELayoutError(BSEError):
    """The page is not the form we expect, or the answer is not a CSV: BSE changed the layout."""


# ---------------------------------------------------------------------------
# once-a-day warning
# ---------------------------------------------------------------------------
_warned: set[str] = set()


def warn_once_per_day(error: BaseException, today: date | None = None) -> None:
    day = (today or datetime.now(IST).date()).isoformat()
    key = f"{day}:{type(error).__name__}:{str(error)[:80]}"
    if key in _warned:
        return
    _warned.clear() if len(_warned) > 50 else None
    _warned.add(key)
    log.warning("BSE deals unavailable, carrying on with NSE only: %s", error)


# ---------------------------------------------------------------------------
# the page's form
# ---------------------------------------------------------------------------
class _FormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.inputs: list[dict[str, Any]] = []
        self.selects: dict[str, list[tuple[str, str, bool]]] = {}
        self._select: str | None = None
        self._option: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a: dict[str, str] = {}
        for k, v in attrs:  # the first of a repeated attribute wins, as in a browser (the real select has name twice)
            a.setdefault(k.lower(), v if v is not None else "")
        if tag == "input" and a.get("name"):
            self.inputs.append({"name": a["name"], "type": a.get("type", "text").lower(),
                                "value": a.get("value", ""), "checked": "checked" in a, "id": a.get("id", "")})
        elif tag == "select" and a.get("name"):
            self._select = a["name"]
            self.selects[self._select] = []
        elif tag == "option" and self._select is not None:
            self._option = {"value": a.get("value"), "selected": "selected" in a, "text": ""}

    def handle_data(self, data: str) -> None:
        if self._option is not None:
            self._option["text"] += data

    def _close_option(self) -> None:
        if self._option is not None and self._select is not None:
            o = self._option
            value = o["value"] if o["value"] is not None else o["text"].strip()
            self.selects[self._select].append((value, o["text"].strip(), o["selected"]))
        self._option = None

    def handle_endtag(self, tag: str) -> None:
        if tag == "option":
            self._close_option()
        elif tag == "select":
            self._close_option()
            self._select = None


class Form:
    """What the GET answered: every input and select of the page, to be posted back."""

    def __init__(self, inputs: list[dict[str, Any]], selects: dict[str, list[tuple[str, str, bool]]]):
        self.inputs, self.selects = inputs, selects

    @property
    def names(self) -> list[str]:
        return [i["name"] for i in self.inputs] + list(self.selects)

    def find(self, suffix: str) -> str | None:
        for n in self.names:
            if n == suffix or n.endswith("$" + suffix) or n.endswith(suffix):
                return n
        return None

    def hidden(self) -> dict[str, str]:
        """Every hidden field (``__VIEWSTATE``, ``__EVENTVALIDATION``, ...), as a browser would send them."""
        return {i["name"]: i["value"] for i in self.inputs if i["type"] == "hidden"}

    def kind_value(self, kind: str) -> str:
        """The deal-type select's value for "bulk" / "block", read from its option texts."""
        for v, text, _ in self.selects.get(self.find(FIELD_SUFFIXES["kind"]) or "", []):
            if kind in text.lower():
                return v
        return KIND_VALUES[kind]


def parse_form(html: str) -> Form:
    """Read the page's form fields; BSELayoutError when it is not the form we need."""
    p = _FormParser()
    p.feed(html)
    form = Form(p.inputs, p.selects)
    names = set(form.names)
    missing = [h for h in REQUIRED_HIDDEN if h not in names]
    missing += [f"{k} ({v})" for k, v in FIELD_SUFFIXES.items() if k != "download" and form.find(v) is None]
    if missing:
        raise BSELayoutError("BSE deals page layout changed: missing " + ", ".join(missing))
    return form


def _fields(form: Form, kind: str, start: date, end: date) -> dict[str, str]:
    f = FIELD_SUFFIXES
    return {form.find(f["kind"]): form.kind_value(kind),  # type: ignore[dict-item]
            form.find(f["all_market"]): "on",  # type: ignore[dict-item]
            form.find(f["from"]): start.strftime("%d/%m/%Y"),  # type: ignore[dict-item]
            form.find(f["to"]): end.strftime("%d/%m/%Y")}  # type: ignore[dict-item]


def build_submit(form: Form, kind: str, start: date, end: date) -> dict[str, str]:
    """Step 1: the hidden fields plus deal type, all markets, the dates and the Submit button."""
    data = form.hidden()
    data.update(_fields(form, kind, start, end))
    data[form.find(FIELD_SUFFIXES["submit"])] = "Submit"  # type: ignore[index]
    return data


def build_download(result: Form, page: Form, kind: str, start: date, end: date) -> dict[str, str]:
    """Step 2: the hidden fields of the Submit answer, the same choices, and the download postback (no Submit)."""
    data = result.hidden()
    data.update(_fields(page, kind, start, end))
    data["__EVENTTARGET"] = "ctl00$ContentPlaceHolder1$" + FIELD_SUFFIXES["download"]
    return data


# ---------------------------------------------------------------------------
# the CSV
# ---------------------------------------------------------------------------
_DATE_FORMATS = ("%d/%m/%Y", "%d-%b-%Y", "%d %b %Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%y", "%d-%B-%Y")


def _date(text: str) -> str:
    t = (text or "").strip()
    for f in _DATE_FORMATS:
        try:
            return datetime.strptime(t, f).date().isoformat()
        except ValueError:
            continue
    return ""


def _number(text: str) -> str:
    return re.sub(r"[,\s]", "", text or "")


def parse_deals_csv(text: str, kind: str = "bulk") -> list[dict[str, Any]]:
    """BSE's CSV -> rows {date, code, name, client, side, qty, price, kind}.

    Columns are found by their header words (Deal Date, Security Code, Security Name, Client Name,
    Deal Type (B/S), Quantity, Price) so a change of order or of the exact wording still works.
    Raises BSELayoutError when the text is not that CSV."""
    text = text.lstrip("﻿")
    reader = csv.reader(io.StringIO(text))
    header = [h.strip().lower() for h in next(reader, [])]

    def col(*words: str) -> int | None:
        for i, h in enumerate(header):
            if any(w in h for w in words):
                return i
        return None

    i_date, i_code = col("date"), col("security code", "scrip code", "code")
    i_name, i_client = col("company", "security name", "scrip name"), col("client")
    i_side, i_qty, i_px = col("deal type", "buy", "b/s"), col("quantity", "qty"), col("price")
    if None in (i_date, i_code, i_client, i_side, i_qty, i_px):
        raise BSELayoutError("BSE deals CSV: unexpected columns " + ", ".join(header)[:200])
    rows = []
    for r in reader:
        if len(r) <= max(i_date, i_code, i_client, i_side, i_qty, i_px):  # type: ignore[type-var]
            continue
        d = _date(r[i_date])  # type: ignore[index]
        if not d:
            continue
        rows.append({"date": d, "code": r[i_code].strip(), "name": r[i_name].strip() if i_name is not None else "",  # type: ignore[index]
                     "client": re.sub(r"\s+", " ", r[i_client]).strip(),  # type: ignore[index]
                     "side": r[i_side].strip(), "qty": _number(r[i_qty]), "price": _number(r[i_px]),  # type: ignore[index]
                     "kind": kind})
    return rows


def _transaction(side: str) -> str:
    s = side.strip().upper()
    if s.startswith("B") or s == "P":
        return "Purchase"
    if s.startswith("S"):
        return "Sale"
    return side or "Trade"


def to_trade(row: dict[str, Any], ticker: str, nse_symbol: str | None = None) -> DisclosedTrade:
    price = row["price"]
    qty = row["qty"]
    d = row["date"]
    return DisclosedTrade(
        source=row["kind"],
        investor=row["client"],
        ticker=ticker,
        transaction=_transaction(row["side"]),
        transaction_date=d,
        report_date=d,
        size=f"{qty} sh @ ₹{price}" if price else f"{qty} sh",
        raw={"exchange": "BSE", "bse_code": row["code"], "bse_name": row["name"], "bse_scrip_id": row["name"], "name": row["name"],
             "nse_symbol": nse_symbol, "yahoo": f"{row['code']}.BO", "clientName": row["client"],
             "buySell": row["side"], "qty": qty, "watp": price, "date": d},
        exchange="BSE",
    )


# ---------------------------------------------------------------------------
# BSE scrip -> NSE ticker
# ---------------------------------------------------------------------------
class ScripResolver:
    """BSE scrip code (+ name) -> NSE ticker, or None when the company is not listed on NSE.

    Uses the public Groww instrument list (BSE row's token = scrip code -> ISIN -> NSE row), then the
    NSE equity list by exact company name. Both files are public and cached by CompanyNames."""

    def __init__(self, names: Any, load_groww: bool = True):
        self.names = names
        self.load_groww = load_groww
        self._code_isin: dict[str, str] | None = None
        self._isin_sym: dict[str, str] = {}
        self._memo: dict[tuple[str, str], str | None] = {}

    def _load(self) -> None:
        self._code_isin = {}
        if not self.load_groww:
            return
        try:
            from .groww import INSTRUMENT_CSV_URL
            text = self.names._text(INSTRUMENT_CSV_URL, "groww_instruments.csv")
            for r in csv.DictReader(io.StringIO(text)):
                if (r.get("segment") or "").upper() != "CASH":
                    continue
                isin, exch = (r.get("isin") or "").strip().upper(), (r.get("exchange") or "").upper()
                if not isin:
                    continue
                if exch == "BSE":
                    self._code_isin[(r.get("exchange_token") or "").strip()] = isin
                elif exch == "NSE" and (r.get("series") or "EQ").upper() in ("EQ", "BE", "BZ", "SM", "ST"):
                    self._isin_sym.setdefault(isin, (r.get("trading_symbol") or "").strip().upper())
        except Exception as e:  # noqa: BLE001 - mapping is a nicety; names still work
            log.warning("instrument list unavailable for the BSE->NSE map: %s", e)

    def __call__(self, code: str, name: str = "") -> str | None:
        key = (code, name)
        if key in self._memo:
            return self._memo[key]
        if self._code_isin is None:
            self._load()
        sym = None
        isin = (self._code_isin or {}).get(code)
        if isin:
            sym = self._isin_sym.get(isin) or None
        if sym is None and name:  # BSE's Company column is its scrip ID, often the NSE symbol itself
            try:
                if name.upper() in self.names._nse_names():
                    sym = name.upper()
            except Exception as e:  # noqa: BLE001
                log.debug("symbol list unavailable: %s", e)
        if sym is None and name:
            try:
                hits = self.names.search(name, limit=1)
                if hits and hits[0]["score"] >= 95:
                    sym = hits[0]["symbol"]
            except Exception as e:  # noqa: BLE001
                log.debug("name lookup failed for %s: %s", name, e)
        self._memo[key] = sym
        return sym


# ---------------------------------------------------------------------------
# the client
# ---------------------------------------------------------------------------
class BSEClient:
    """Pass ``session``, ``clock``, ``sleep`` and ``resolver`` to inject fakes in tests."""

    def __init__(self, session: Any | None = None, cache_dir: Path | None = None,
                 host: str = DEFAULT_HOST, fallback_host: str | None = FALLBACK_HOST,
                 timeout: float = 30.0, min_interval: float = MIN_INTERVAL_S,
                 resolver: Callable[[str, str], str | None] | None = None,
                 clock: Callable[[], datetime] | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 monotonic: Callable[[], float] = time.monotonic):
        self.session = session or requests.Session()
        self.cache_dir = Path(cache_dir) / "bse_deals" if cache_dir else None
        self.hosts = list(dict.fromkeys(h for h in (host, fallback_host) if h))
        self.timeout = timeout
        self.min_interval = min_interval
        self.resolver = resolver
        self.clock = clock or (lambda: datetime.now(IST))
        self.sleep = sleep
        self.monotonic = monotonic
        self._last_request = -1e9
        self._fail_until = -1e9
        self._today_cache: tuple[float, list[dict[str, Any]]] | None = None
        self.last_error: str | None = None
        self.requests_made = 0

    # -- http ----------------------------------------------------------------
    def _throttle(self) -> None:
        wait = self.min_interval - (self.monotonic() - self._last_request)
        if wait > 0:
            self.sleep(wait)
        self._last_request = self.monotonic()
        self.requests_made += 1

    def _fetch_kind(self, url: str, kind: str, start: date, end: date) -> list[dict[str, Any]]:
        """One deal type: GET the form, POST Submit, POST the download; rows of the CSV."""
        self._throttle()
        page = self.session.get(url, headers=HEADERS, timeout=self.timeout)
        page.raise_for_status()
        form = parse_form(page.content.decode("utf-8-sig", errors="replace"))
        post_headers = {**HEADERS, "Referer": url}
        self._throttle()
        step1 = self.session.post(url, data=build_submit(form, kind, start, end),
                                  headers=post_headers, timeout=self.timeout)
        step1.raise_for_status()
        p = _FormParser()
        p.feed(step1.content.decode("utf-8-sig", errors="replace"))
        result = Form(p.inputs, p.selects)
        if "__VIEWSTATE" not in result.hidden():
            raise BSELayoutError("BSE deals page layout changed: no __VIEWSTATE after Submit")
        self._throttle()
        step2 = self.session.post(url, data=build_download(result, form, kind, start, end),
                                  headers=post_headers, timeout=self.timeout)
        step2.raise_for_status()
        text = step2.content.decode("utf-8-sig", errors="replace")
        if "<html" in text[:500].lower() or "client" not in text[:400].lower():
            raise BSELayoutError("BSE deals: the download did not return a CSV")
        return parse_deals_csv(text, kind)

    def _fetch(self, start: date, end: date) -> list[dict[str, Any]]:
        """Bulk and block deals dated start..end (a request flow per deal type).

        Tries each host in turn and raises BSEError / BSELayoutError when none works."""
        errors: list[BaseException] = []
        for host in self.hosts:
            url = f"https://{host}{PAGE_PATH}"
            try:
                rows: list[dict[str, Any]] = []
                for kind in ("bulk", "block"):
                    rows += self._fetch_kind(url, kind, start, end)
                return rows
            except (requests.RequestException, BSEError) as e:
                errors.append(e)
                log.debug("BSE deals via %s failed: %s", host, e)
        cls = BSELayoutError if any(isinstance(e, BSELayoutError) for e in errors) else BSEError
        raise cls("; ".join(str(e) for e in errors) or "no BSE host configured")

    def fetch_range(self, start: date, end: date) -> list[dict[str, Any]]:
        """Rows (both kinds) for start..end, straight from BSE."""
        return self._fetch(start, end)

    def fetch_today(self) -> list[dict[str, Any]]:
        """Today's deals (published after the close): the same flow with today as from and to."""
        today = self.clock().date()
        return self._fetch(today, today)

    # -- cache ---------------------------------------------------------------
    def _day_path(self, d: date) -> Path | None:
        return self.cache_dir / f"{d.isoformat()}.json" if self.cache_dir else None

    def _cached_day(self, d: date) -> list[dict[str, Any]] | None:
        p = self._day_path(d)
        if p is None or not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))["rows"]
        except (ValueError, KeyError, TypeError, OSError):
            return None  # damaged: fetch again

    def _store_days(self, start: date, end: date, rows: list[dict[str, Any]], today: date) -> None:
        if self.cache_dir is None:
            return
        by_day: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            by_day.setdefault(r["date"], []).append(r)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        d = start
        while d <= end:
            day_rows = by_day.get(d.isoformat(), [])
            recent_empty = not day_rows and (today - d).days < 2  # BSE may not have published it yet
            if d < today and not recent_empty:
                self._day_path(d).write_text(json.dumps({"rows": day_rows}), encoding="utf-8")  # type: ignore[union-attr]
            d += timedelta(days=1)

    # -- public --------------------------------------------------------------
    def raw_rows(self, start: date, end: date) -> list[dict[str, Any]]:
        now = self.clock()
        today = now.date()
        rows: list[dict[str, Any]] = []
        missing: list[date] = []
        d = start
        while d <= min(end, today - timedelta(days=1)):
            cached = self._cached_day(d)
            if cached is not None:
                rows += cached
            elif d.weekday() < 5:
                missing.append(d)
            d += timedelta(days=1)
        if missing and self.monotonic() >= self._fail_until:
            lo = missing[0]
            try:
                while lo <= missing[-1]:
                    hi = min(lo + timedelta(days=MAX_SPAN_DAYS - 1), missing[-1])
                    got = self.fetch_range(lo, hi)
                    self._store_days(lo, hi, got, today)
                    rows += got
                    lo = hi + timedelta(days=1)
                self.last_error = None
            except Exception as e:  # noqa: BLE001 - a second source never breaks the first
                self._fail(e)
        if end >= today and today.weekday() < 5 and (now.hour, now.minute) >= PUBLISHED_AFTER:
            rows += self._today_rows()
        return rows

    def _today_rows(self) -> list[dict[str, Any]]:
        if self._today_cache and self.monotonic() - self._today_cache[0] < TODAY_TTL_S:
            return self._today_cache[1]
        if self.monotonic() < self._fail_until:
            return []
        try:
            rows = self.fetch_today()
        except Exception as e:  # noqa: BLE001
            self._fail(e)
            return []
        self._today_cache = (self.monotonic(), rows)
        return rows

    def _fail(self, error: BaseException) -> None:
        self.last_error = str(error)
        self._fail_until = self.monotonic() + RETRY_AFTER_FAILURE_S
        warn_once_per_day(error, self.clock().date())

    def deals(self, start: date, end: date, kinds: Iterable[str] = ("bulk", "block"),
              investors: Iterable[str] | None = None) -> list[DisclosedTrade]:
        """BSE bulk/block deals dated start..end as DisclosedTrade (exchange "BSE").

        With ``investors``, only deals by those clients are kept (and only those are mapped to NSE tickers)."""
        names = None if investors is None else list(investors)
        kinds = set(kinds)
        seen: set[tuple[str, ...]] = set()
        out: list[DisclosedTrade] = []
        for r in self.raw_rows(start, end):
            if r["kind"] not in kinds or not (start.isoformat() <= r["date"] <= end.isoformat()):
                continue
            if names is not None and not followed_names(r["client"], names):
                continue
            k = (r["date"], r["code"], r["client"], r["side"], r["qty"], r["price"], r["kind"])
            if k in seen:
                continue
            seen.add(k)
            sym = None
            if self.resolver is not None:
                try:
                    sym = self.resolver(r["code"], r["name"])
                except Exception as e:  # noqa: BLE001
                    log.debug("BSE symbol map failed for %s: %s", r["code"], e)
            out.append(to_trade(r, sym or f"{r['code']}.BO", sym))
        out.sort(key=lambda t: (t.report_date, t.transaction_date), reverse=True)
        return out


def make_bse_client(settings: Any) -> BSEClient | None:
    """A BSEClient for Indian-market settings when BSE_DEALS is on, else None."""
    if not getattr(settings, "bse_deals", True) or getattr(settings, "market", "in") != "in":
        return None
    from .instruments import CompanyNames
    cache = Path(settings.state_dir) / "cache"
    return BSEClient(cache_dir=cache, host=getattr(settings, "bse_host", None) or DEFAULT_HOST,
                     resolver=ScripResolver(CompanyNames(cache)))
