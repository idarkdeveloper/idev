"""Disclosed deals as they were KNOWN on a replay day (point in time, no lookahead).

Deals come from the sources the dashboard already uses: NSE bulk and block deals (``NSEClient.historical_deals``),
BSE's (``NSEClient.bse``) and, when the dashboard follows insider filings, SEBI PIT disclosures
(``NSEClient.insider_trades``). Fetched ranges are cached on disk by calendar month, so stepping a replay does
not fetch again.

The one visibility rule is ``visible_on``: a deal can be shown on replay day ``D`` only when ``backtest.visible_after``
says it became public strictly before ``D``. ``visible_after`` is the date the first usable close must be strictly
after (a bulk deal's own date, because it is published that evening; an insider filing's broadcast time through the
15:00 IST cut-off in ``filing_time``). Replay fills at the day's close, so a deal published after the close of ``D``
cannot be known on ``D``; the first day it can be seen is the next one. Everything this module returns passes
through ``visible_on``, whatever the month files hold.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable

from ..backtest import visible_after
from ..deal_events import consolidate_deals
from ..filing_time import parse_ts
from ..investors import classify_client
from ..quiver import DisclosedTrade, followed_names
from ..untrusted import wrap_json

log = logging.getLogger(__name__)

KINDS_BY_SOURCE = {"deals": ("bulk", "block"), "bulk": ("bulk",), "block": ("block",), "insider": ("insider",)}
LOOKBACK_FETCH_DAYS = 45      # filings made a few weeks before the window can still be the ones that became public in it
MAX_WINDOW_DAYS = 120
_TRADE_FIELDS = ("source", "investor", "ticker", "transaction", "transaction_date", "report_date", "size", "raw",
                 "exchange")


def _day(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def visible_on(deal: DisclosedTrade, day: str) -> bool:
    """True when ``deal`` was public before the close of replay day ``day``. A deal whose dates cannot be read is
    never shown: the unknown is treated as the future."""
    if _day(day) is None:
        return False
    try:
        seen = visible_after(deal)
    except Exception:  # noqa: BLE001 - an unreadable deal is hidden, not shown
        return False
    return _day(seen) is not None and seen < day


# -- fetching -------------------------------------------------------------------------------------------------
class NSEFetcher:
    """Adapts an ``NSEClient`` (and its optional ``bse``) to ``fetch(kind, start, end) -> (rows, complete)``."""

    def __init__(self, nse: Any):
        self.nse = nse

    def fetch(self, kind: str, start: date, end: date) -> tuple[list[DisclosedTrade], bool]:
        days = (end - start).days
        if kind == "insider":
            return list(self.nse.insider_trades(days, end=end)), True
        rows = list(self.nse.historical_deals(days, kind, end=end))
        bse, complete = getattr(self.nse, "bse", None), True
        if bse is not None:
            try:
                rows += bse.deals(start, end, kinds=(kind,), investors=None)
                complete = not getattr(bse, "last_error", None)   # BSE trouble: show what there is, ask again next time
            except Exception as e:  # noqa: BLE001 - BSE never breaks the NSE read
                log.warning("BSE deals unavailable for the replay: %s", e)
                complete = False
        return rows, complete


def _to_row(t: DisclosedTrade) -> dict[str, Any]:
    return {k: getattr(t, k) for k in _TRADE_FIELDS}


def _from_row(r: dict[str, Any]) -> DisclosedTrade:
    return DisclosedTrade(**{k: r[k] for k in _TRADE_FIELDS if k in r})


def _months(start: date, end: date) -> list[tuple[date, date]]:
    out, d = [], start.replace(day=1)
    while d <= end:
        nxt = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
        out.append((d, nxt - timedelta(days=1)))
        d = nxt
    return out


class DealsService:
    """Visible deals for a replay day, from a fetcher (anything with ``fetch(kind, start, end)``)."""

    def __init__(self, fetcher: Any | None, cache_dir: Path | None, today_fn: Callable[[], str],
                 kinds_fn: Callable[[], Iterable[str]]):
        self.fetcher, self.cache_dir, self.today_fn, self.kinds_fn = fetcher, cache_dir, today_fn, kinds_fn
        self._mem: dict[tuple[str, str], list[DisclosedTrade]] = {}
        self._lock = threading.Lock()   # one fetch at a time: a second caller reuses the first one's chunks

    @property
    def available(self) -> bool:
        return self.fetcher is not None

    def _path(self, kind: str, first: date) -> Path | None:
        return self.cache_dir / f"{kind}-{first:%Y-%m}.json" if self.cache_dir else None

    def _chunk(self, kind: str, first: date, last: date, errors: list[str]) -> list[DisclosedTrade]:
        key = (kind, first.isoformat())
        if key in self._mem:
            return self._mem[key]
        path = self._path(kind, first)
        if path is not None and path.exists():
            try:
                rows = [_from_row(r) for r in json.loads(path.read_text(encoding="utf-8"))["rows"]]
                self._mem[key] = rows
                return rows
            except (OSError, ValueError, KeyError, TypeError):
                pass   # damaged: fetch again
        today = _day(self.today_fn()) or date.today()
        end = min(last, today)
        if first > end:
            return []
        try:
            rows, complete = self.fetcher.fetch(kind, first, end)  # type: ignore[union-attr]
        except Exception as e:  # noqa: BLE001 - NSE throttles; missing deals must show as a note, not break the page
            errors.append(f"{kind} deals for {first:%b %Y} could not be loaded ({type(e).__name__}: {e})")
            return []
        rows = [t for t in rows if t.investor]
        # A month that is still running (or whose data was partial) is not final: use it now, do not keep it.
        if complete and last < today - timedelta(days=2):
            self._mem[key] = rows
            if path is not None:
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps({"rows": [_to_row(t) for t in rows]}, default=str), encoding="utf-8")
                except OSError as e:
                    log.warning("could not cache replay deals: %s", e)
        elif not complete:
            errors.append(f"some {kind} deals for {first:%b %Y} were unavailable; reload to try again")
        return rows

    def known_on(self, day: str, days: int = 30, ticker: str | None = None,
                 cap: int = MAX_WINDOW_DAYS) -> tuple[list[DisclosedTrade], list[str]]:
        """Deals public before the close of ``day`` and first made public within the last ``days`` days
        (newest first), plus notes for anything that could not be loaded. Nothing newer than ``day`` comes back."""
        errors: list[str] = []
        end_day = _day(day)
        if not self.available or end_day is None:
            return [], errors
        days = max(1, min(int(days), cap))
        since = (end_day - timedelta(days=days)).isoformat()
        fetch_from = end_day - timedelta(days=days + LOOKBACK_FETCH_DAYS)
        seen: set[tuple[str, ...]] = set()
        out: list[DisclosedTrade] = []
        with self._lock:
            for kind in self.kinds_fn():
                for first, last in _months(fetch_from, end_day):
                    for t in self._chunk(kind, first, last, errors):
                        k = (t.source, t.investor, t.ticker, t.transaction, t.transaction_date, t.report_date,
                             t.size, t.exchange)
                        if k in seen:
                            continue
                        seen.add(k)
                        if ticker and t.ticker != ticker:
                            continue
                        if visible_on(t, day) and visible_after(t) >= since:   # the gate: never anything newer than day
                            out.append(t)
        out.sort(key=lambda t: (reported_date(t, day), t.transaction_date), reverse=True)
        return out, sorted(set(errors))


# -- shaping for the page and for Claude ----------------------------------------------------------------------
def reported_date(t: DisclosedTrade, day: str) -> str:
    """The date the deal was reported (an insider filing's broadcast day when known), never later than ``day``."""
    rep = (t.report_date or t.transaction_date or "")[:10]
    if t.source == "insider":
        raw = t.raw if isinstance(t.raw, dict) else {}
        stamp = parse_ts(raw.get("filed_at") or raw.get("broadcastDateTime") or raw.get("brdCstDt"))
        if stamp is not None:
            rep = stamp.isoformat()[:10]   # a datetime and a bare date both start with YYYY-MM-DD
    return min(rep, day) if _day(rep) else day


def _num(text: str) -> float | None:
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


_QTY = re.compile(r"([\d,]+(?:\.\d+)?)\s*sh")
_PRICE = re.compile(r"@\s*₹\s*([\d,]+(?:\.\d+)?)")
_VALUE = re.compile(r"\(\s*₹\s*([\d,]+(?:\.\d+)?)\s*\)")


def size_parts(t: DisclosedTrade) -> dict[str, float | None]:
    """Shares, price per share and rupee value from the trade's size text ("350000 sh @ ₹412.5" or "9000 sh (₹3,70,000)")."""
    size = t.size or ""
    q, p, v = _QTY.search(size), _PRICE.search(size), _VALUE.search(size)
    qty = _num(q.group(1)) if q else None
    price = _num(p.group(1)) if p else None
    value = _num(v.group(1)) if v else None
    if value is None and qty is not None and price is not None:
        value = round(qty * price, 2)
    if price is None and qty and value:
        price = round(value / qty, 2)
    return {"qty": qty, "price": price, "value": value}


def _company(t: DisclosedTrade) -> str:
    raw = t.raw if isinstance(t.raw, dict) else {}
    return str(raw.get("name") or raw.get("company") or raw.get("bse_name") or "").strip()


def deal_row(t: DisclosedTrade, day: str, followed: Iterable[str]) -> dict[str, Any]:
    side = "buy" if t.transaction == "Purchase" else "sell" if t.transaction == "Sale" else "other"
    return {"key": t.key, "reported": reported_date(t, day), "traded": t.transaction_date, "side": side,
            "ticker": t.ticker, "name": _company(t), "exchange": t.exchange, "kind": t.source,
            "who": t.investor, "client_type": classify_client(t.investor, t.source),
            "followed": followed_names(t.investor, followed), **size_parts(t)}


def claude_deals(rows: list[dict[str, Any]], limit: int = 25) -> str:
    """The deals for Claude's message: third-party names, so inside the untrusted block, newest ``limit`` only."""
    keep = ("reported", "side", "ticker", "name", "qty", "price", "value", "exchange", "who", "client_type", "followed")
    return wrap_json([{k: r.get(k) for k in keep} for r in rows[:limit]], "disclosed_deals")


# -- the end-of-replay line ------------------------------------------------------------------------------------
def _entry(bars: list[dict[str, Any]], after: str, end: str) -> tuple[int, float] | None:
    """Index and price of the first open strictly after ``after`` (an adjusted open, so it compares with adj_close)."""
    for i, b in enumerate(bars):
        if b["date"] > end:
            return None
        if b["date"] > after:
            close, adj = float(b["close"]), float(b.get("adj_close", b["close"]))
            return i, float(b.get("open") or close) * (adj / close if close else 1.0)
    return None


def followed_summary(deals: list[DisclosedTrade], followed: list[str], prices: Any, start: str, end: str,
                     benchmark: str = "^NSEI") -> dict[str, Any]:
    """What buying each followed investor's BUY deal at the next open after it was reported would have returned by
    ``end``, against the Nifty over the same days. Only for an ended replay: it looks from each deal to the end date,
    never past it. ``deals`` are the ones public by ``end``."""
    mine = consolidate_deals([t for t in deals if t.transaction == "Purchase" and followed_names(t.investor, followed)
                              and visible_on(t, end) and visible_after(t) >= start])
    try:
        bench = [b for b in prices.history(benchmark, "10y") if b["date"] <= end]
    except Exception:  # noqa: BLE001
        bench = []
    rets: list[float] = []
    bench_rets: list[float] = []
    rows: list[dict[str, Any]] = []
    for t in mine:
        try:
            bars = [b for b in prices.history(t.ticker, "10y") if b["date"] <= end]
        except Exception:  # noqa: BLE001 - not listed / no price (a BSE-only code): left out, counted below
            continue
        hit = _entry(bars, visible_after(t), end)
        if hit is None or not bars:
            continue
        i, px = hit
        ret = float(bars[-1].get("adj_close", bars[-1]["close"])) / px - 1.0
        rets.append(ret)
        b_hit = _entry(bench, visible_after(t), end) if bench else None
        if b_hit is not None:
            bench_rets.append(float(bench[-1].get("adj_close", bench[-1]["close"])) / b_hit[1] - 1.0)
        rows.append({"ticker": t.ticker, "who": t.investor, "reported": t.report_date, "entry": bars[i]["date"],
                     "ret": ret, "bench": (bench_rets[-1] if b_hit is not None else None)})
    n = len(rets)
    out: dict[str, Any] = {"deals": len(mine), "priced": n, "start": start, "end": end, "followed": followed,
                           "rows": rows[:50]}
    if not n:
        out["text"] = ("Deals you could have followed: "
                       + ("none of the followed investors' buys were public during this replay."
                          if not mine else f"{len(mine)} buy(s) by followed investors, but none could be priced."))
        return out
    mean = sum(rets) / n
    paired = [r for r in rows if r["bench"] is not None]
    bmean = sum(r["bench"] for r in paired) / len(paired) if paired else None
    out.update(mean_return=mean, mean_bench=bmean, hit_rate=sum(1 for r in rets if r > 0) / n)
    vs = (f", against the Nifty's {bmean * 100:+.1f}% over the same days" if bmean is not None else "")
    skipped = f" ({len(mine) - n} more could not be priced)" if len(mine) > n else ""
    out["text"] = (f"Deals you could have followed: {n} buy{'s' if n != 1 else ''} by followed investors became public "
                   f"during this replay; buying each at the next open after it was reported would have returned "
                   f"{mean * 100:+.1f}% on average by {end}{vs}, before charges{skipped}.")
    return out
