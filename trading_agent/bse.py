"""BSE India bulk and block deals: a second source beside NSE's (nse.py).

Many mid and small-cap deals by the investors we follow happen only on BSE. The public page
``markets/equity/EQReports/BulknBlockDeals.aspx`` (no login) offers a CSV download. It is an
ASP.NET WebForms page, so the flow is the one a browser makes:

1. GET the page (browser-like headers, cookies kept) and read every form field, including the
   hidden ``__VIEWSTATE`` / ``__VIEWSTATEGENERATOR`` / ``__EVENTVALIDATION`` ones;
2. POST the form back with the "history" option, the dates (DD/MM/YYYY), "all markets" and the
   download button; the answer is the CSV.

"Today's deals" are the same page with the "today" option; BSE publishes them after the close, so
they are only asked for after 16:00 IST. Past days never change, so each is cached on disk once.

Nothing here needs a key or an account. If BSE changes the page, ``BSELayoutError`` says which part
is missing; ``BSEClient.deals`` then logs it once a day and returns what is cached, so the watch loop
carries on with NSE alone.

UNVERIFIED: on 10 Oct 2026 the ``www`` host served a JavaScript app shell (no WebForms fields), so the
control names below are from the page's documented layout and are matched by suffix, not exactly;
see ``FIELD_SUFFIXES``. The ``beta`` host is tried when ``www`` does not answer with the form.
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
DEFAULT_HOST = "www.bseindia.com"
FALLBACK_HOST = "beta.bseindia.com"
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
    "mode": "rblDT",              # radio: 0 = today, 1 = history (date range)
    "all_market": "chkAllMarket",  # checkbox: every market segment
    "download": "btnDownload",    # the CSV download postback
}
MODE_TODAY, MODE_HISTORY = "0", "1"
REQUIRED_HIDDEN = ("__VIEWSTATE",)
HIDDEN_SEEN = ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION")


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
        a = {k.lower(): (v if v is not None else "") for k, v in attrs}
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

    def date_inputs(self) -> list[str]:
        """The two date boxes, in page order (from, to)."""
        seen: list[str] = []
        for i in self.inputs:
            n = i["name"]
            if i["type"] in ("text", "date", "") and "date" in n.lower() and "scrip" not in n.lower() and n not in seen:
                seen.append(n)
        return seen

    def kind_control(self) -> tuple[str, dict[str, str]] | None:
        """A select / radio group that chooses bulk or block: (field name, {"bulk": value, "block": value})."""
        mode = self.find(FIELD_SUFFIXES["mode"])
        for name, options in self.selects.items():
            found = {k: v for v, text, _ in options for k in ("bulk", "block") if k in f"{v} {text}".lower()}
            if len(found) == 2:
                return name, found
        groups: dict[str, dict[str, str]] = {}
        for i in self.inputs:
            if i["type"] == "radio" and i["name"] != mode:
                for k in ("bulk", "block"):
                    if k in i["value"].lower():
                        groups.setdefault(i["name"], {})[k] = i["value"]
        for name, found in groups.items():
            if len(found) == 2:
                return name, found
        return None

    def base_data(self) -> dict[str, str]:
        """The values a browser would send untouched: hidden fields, text, checked radios and boxes, selects."""
        data: dict[str, str] = {}
        for i in self.inputs:
            t = i["type"]
            if t in ("submit", "button", "image", "file", "reset"):
                continue
            if t in ("checkbox", "radio") and not i["checked"]:
                continue
            data[i["name"]] = i["value"]
        for name, options in self.selects.items():
            chosen = next((v for v, _, sel in options if sel), options[0][0] if options else "")
            data[name] = chosen
        return data

    def button_value(self, name: str) -> str:
        for i in self.inputs:
            if i["name"] == name and i["value"]:
                return i["value"]
        return "Download"


def parse_form(html: str) -> Form:
    """Read the page's form fields; BSELayoutError when it is not the form we need."""
    p = _FormParser()
    p.feed(html)
    form = Form(p.inputs, p.selects)
    names = set(form.names)
    missing = [h for h in REQUIRED_HIDDEN if h not in names]
    missing += [f"{k} ({v})" for k, v in FIELD_SUFFIXES.items() if form.find(v) is None]
    if len(form.date_inputs()) < 2:
        missing.append("from/to date boxes")
    if missing:
        raise BSELayoutError("BSE deals page layout changed: missing " + ", ".join(missing))
    return form


def build_post(form: Form, mode: str, start: date | None = None, end: date | None = None,
               kind: str | None = None) -> dict[str, str]:
    """The postback body for the CSV download."""
    data = form.base_data()
    data["__EVENTTARGET"] = ""
    data["__EVENTARGUMENT"] = ""
    data[form.find(FIELD_SUFFIXES["mode"])] = mode  # type: ignore[index]
    data[form.find(FIELD_SUFFIXES["all_market"])] = "on"  # type: ignore[index]
    if mode == MODE_HISTORY and start and end:
        from_box, to_box = form.date_inputs()[:2]
        data[from_box] = start.strftime("%d/%m/%Y")
        data[to_box] = end.strftime("%d/%m/%Y")
    ctl = form.kind_control()
    if kind and ctl:
        data[ctl[0]] = ctl[1][kind]
    btn = form.find(FIELD_SUFFIXES["download"])
    data[btn] = form.button_value(btn)  # type: ignore[index,arg-type]
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
    i_name, i_client = col("security name", "scrip name", "company"), col("client")
    i_side, i_qty, i_px = col("deal type", "buy", "b/s"), col("quantity", "qty"), col("price")
    if None in (i_date, i_code, i_client, i_side, i_qty, i_px):
        raise BSELayoutError("BSE deals CSV: unexpected columns " + ", ".join(header)[:200])
    i_type = next((i for i, h in enumerate(header) if "bulk" in h or "block" in h), None)
    rows = []
    for r in reader:
        if len(r) <= max(i_date, i_code, i_client, i_side, i_qty, i_px):  # type: ignore[type-var]
            continue
        d = _date(r[i_date])  # type: ignore[index]
        if not d:
            continue
        row_kind = kind
        if i_type is not None and "block" in r[i_type].lower():
            row_kind = "block"
        rows.append({"date": d, "code": r[i_code].strip(), "name": r[i_name].strip() if i_name is not None else "",  # type: ignore[index]
                     "client": re.sub(r"\s+", " ", r[i_client]).strip(),  # type: ignore[index]
                     "side": r[i_side].strip(), "qty": _number(r[i_qty]), "price": _number(r[i_px]),  # type: ignore[index]
                     "kind": row_kind})
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
        raw={"exchange": "BSE", "bse_code": row["code"], "bse_name": row["name"], "name": row["name"],
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

    def _post_csv(self, url: str, form: Form, mode: str, start: date | None, end: date | None,
                  kind: str | None) -> str:
        self._throttle()
        resp = self.session.post(url, data=build_post(form, mode, start, end, kind),
                                 headers={**HEADERS, "Referer": url}, timeout=self.timeout)
        resp.raise_for_status()
        text = resp.content.decode("utf-8-sig", errors="replace")
        if "<html" in text[:500].lower() or "client" not in text[:400].lower():
            raise BSELayoutError("BSE deals: the download did not return a CSV")
        return text

    def _fetch(self, mode: str, start: date | None, end: date | None) -> list[dict[str, Any]]:
        """GET the form once, then POST the download (once per deal kind when the page has that choice).

        Tries each host in turn and raises BSEError / BSELayoutError when none works."""
        errors: list[BaseException] = []
        for host in self.hosts:
            url = f"https://{host}{PAGE_PATH}"
            try:
                self._throttle()
                page = self.session.get(url, headers=HEADERS, timeout=self.timeout)
                page.raise_for_status()
                form = parse_form(page.content.decode("utf-8-sig", errors="replace"))
                if form.kind_control() is None:
                    # One file holds both; its own deal-type column says block, else it is read as bulk.
                    return parse_deals_csv(self._post_csv(url, form, mode, start, end, None), "bulk")
                rows: list[dict[str, Any]] = []
                for kind in ("bulk", "block"):
                    rows += parse_deals_csv(self._post_csv(url, form, mode, start, end, kind), kind)
                return rows
            except (requests.RequestException, BSEError) as e:
                errors.append(e)
                log.debug("BSE deals via %s failed: %s", host, e)
        cls = BSELayoutError if any(isinstance(e, BSELayoutError) for e in errors) else BSEError
        raise cls("; ".join(str(e) for e in errors) or "no BSE host configured")

    def fetch_range(self, start: date, end: date) -> list[dict[str, Any]]:
        """Rows (both kinds) from the history option for start..end, straight from BSE."""
        return self._fetch(MODE_HISTORY, start, end)

    def fetch_today(self) -> list[dict[str, Any]]:
        """Rows from the "today" option (published after the close)."""
        return self._fetch(MODE_TODAY, None, None)

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
