"""The screener for the United States, United Kingdom and Japan: view only, read only, from Yahoo daily history.

Lists (current members; none of these is stored in the repo, each is read from its public source and kept on disk for a day):

* ``SP500_US``  - S&P 500, Wikipedia "List of S&P 500 companies" (the same table format the replication study reads for
  the S&P 400, ``research_replication._Tables``);
* ``FTSE100_UK`` - FTSE 100, Wikipedia "FTSE 100 Index" (the same Ticker column parser as the FTSE 250);
* ``TOPIX100_JP`` - TOPIX Core30 + Large70 from JPX's weight file (the file the replication study reads for the Mid400).
  The Nikkei 225 list is not offered: there is no free official file of it, and none is checked in here.

A stock is keyed by its qualified code (``AAPL.US``, ``VOD.L``, ``7203.T``, see ``markets.py``), so its drawer link opens the
existing view-only Look up for that market. UK prices from Yahoo's ``.L`` symbols are in pence and are divided by 100.
Nothing here can place an order; the India-only columns (price band, ASM/GSM, pledges, held, followed investors) are absent.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from . import markets

log = logging.getLogger(__name__)

LIST_TTL = 24 * 3600
FRESH_TTL = 30 * 60           # the market's own open hours: a stock's figures are re-read after this
CLOSED_TTL = 6 * 3600         # outside them: after this
TOPIX_SERIES = ("TOPIX Core30", "TOPIX Large70")

UNIVERSES: dict[str, dict[str, Any]] = {
    "SP500_US": {"market": "US", "label": "S&P 500 (US)", "index": "S&P 500", "symbol": "$", "locale": "en-US", "dec": 2, "benchmark": "^GSPC",
                 "url": "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"},
    "FTSE100_UK": {"market": "UK", "label": "FTSE 100 (UK)", "index": "FTSE 100", "symbol": "£", "locale": "en-GB", "dec": 2, "benchmark": "^FTSE",
                   "url": "https://en.wikipedia.org/wiki/FTSE_100_Index"},
    "TOPIX100_JP": {"market": "JP", "label": "TOPIX 100 (Japan)", "index": "TOPIX 100", "symbol": "¥", "locale": "ja-JP", "dec": 0, "benchmark": "^N225",
                    "url": "https://www.jpx.co.jp/automation/english/markets/indices/topix/files/topixweight_e.csv"},
}
HOURS: dict[str, tuple[str, dtime, dtime]] = {   # market -> (time zone, open, close); weekdays only, no holiday list
    "US": ("America/New_York", dtime(9, 30), dtime(16, 0)),
    "UK": ("Europe/London", dtime(8, 0), dtime(16, 30)),
    "JP": ("Asia/Tokyo", dtime(9, 0), dtime(15, 30)),
}
UK_PENCE = 100.0


def is_foreign_universe(key: str) -> bool:
    return str(key or "").upper() in UNIVERSES


def market_of(key: str) -> str | None:
    u = UNIVERSES.get(str(key or "").upper())
    return u["market"] if u else None


def market_open(market: str, now: datetime | None = None) -> bool:
    tz, o, c = HOURS[market]
    n = (now or datetime.now(ZoneInfo("UTC"))).astimezone(ZoneInfo(tz))
    return n.weekday() < 5 and o <= n.time() <= c


def market_info(key: str, now: datetime | None = None) -> dict[str, Any]:
    """What the page shows about the market of a universe: name, currency symbol, open or closed, local time and IST."""
    u = UNIVERSES[key.upper()]
    m = markets.MARKETS[u["market"]]
    tz = HOURS[u["market"]][0]
    n = now or datetime.now(ZoneInfo("UTC"))
    return {"code": u["market"], "name": m["name"], "index": u["index"], "symbol": u["symbol"], "locale": u["locale"], "dec": u["dec"],
            "open": market_open(u["market"], n), "tz": tz, "local": n.astimezone(ZoneInfo(tz)).strftime("%H:%M"),
            "ist": n.astimezone(ZoneInfo("Asia/Kolkata")).strftime("%H:%M"), "benchmark": u["benchmark"],
            "pence_note": "UK prices are converted from pence to pounds." if u["market"] == "UK" else None,
            "note": f"{m['name']} market, view-only; prices delayed. No orders here."}


# ---- constituent lists ----
def _header_index(header: list[str], *names: str) -> int | None:
    low = [h.lower() for h in header]
    for n in names:
        for i, h in enumerate(low):
            if h.startswith(n):
                return i
    return None


def parse_sp500(page: str) -> list[dict[str, str]]:
    from .research_replication import _Tables, us_symbol
    p = _Tables()
    p.feed(page)
    rows = p.tables.get("constituents") or []
    if not rows:
        return []
    h = rows[0]
    sym, name, sector = _header_index(h, "symbol"), _header_index(h, "security", "company"), _header_index(h, "gics sector", "sector")
    out, seen = [], set()
    for r in rows[1:]:
        if sym is None or len(r) <= sym:
            continue
        t = us_symbol(r[sym])
        if t and t not in seen:
            seen.add(t)
            out.append({"symbol": markets.qualify(t, "US"), "name": r[name] if name is not None and len(r) > name else "",
                        "industry": r[sector] if sector is not None and len(r) > sector else ""})
    return sorted(out, key=lambda m: m["symbol"])


def parse_ftse100(page: str) -> list[dict[str, str]]:
    from .research_replication import _Tables, uk_symbol
    p = _Tables()
    p.feed(page)
    rows = p.tables.get("constituents") or []
    if not rows:
        return []
    h = rows[0]
    tick, name, sector = _header_index(h, "ticker", "epic"), _header_index(h, "company"), _header_index(h, "ftse industry", "sector", "industry")
    out, seen = [], set()
    for r in rows[1:]:
        if tick is None or len(r) <= tick:
            continue
        t = uk_symbol(r[tick])
        if t and t not in seen:
            seen.add(t)
            out.append({"symbol": t + ".L", "name": r[name] if name is not None and len(r) > name else "",
                        "industry": r[sector] if sector is not None and len(r) > sector else ""})
    return sorted(out, key=lambda m: m["symbol"])


def parse_topix100(text: str) -> list[dict[str, str]]:
    import csv
    import io
    out, seen = [], set()
    for r in csv.DictReader(io.StringIO(text)):
        if (r.get("New Index Series Code") or "").strip() not in TOPIX_SERIES:
            continue
        code = (r.get("Code") or "").strip().upper()
        if not code or code in seen:
            continue
        seen.add(code)
        name = next((r[k] for k in ("Name (English)", "Issue Name", "Name") if r.get(k)), "")
        sector = next((r[k] for k in ("Sector(33)", "Industry", "33 Sector(name)", "Sector") if r.get(k)), "")
        out.append({"symbol": code + ".T", "name": name.strip(), "industry": sector.strip()})
    return sorted(out, key=lambda m: m["symbol"])


PARSERS: dict[str, Callable[[str], list[dict[str, str]]]] = {"SP500_US": parse_sp500, "FTSE100_UK": parse_ftse100, "TOPIX100_JP": parse_topix100}


def _coded(members: list[dict[str, str]]) -> list[dict[str, str]]:
    """Every member carries its qualified ``code`` (the same as its symbol): the page links to the view-only Look up with it."""
    return [{**m, "code": m.get("code") or m["symbol"]} for m in members]


def load_members(key: str, cache_dir: Path | None = None, session: Any = None, clock: Callable[[], float] = time.time) -> list[dict[str, str]]:
    """Members of a foreign universe: the day-old copy on disk, else the public page / file (parsed), else a stale copy."""
    key = key.upper()
    u = UNIVERSES[key]
    path = Path(cache_dir) / f"{key}.json" if cache_dir else None

    def stored() -> list[dict[str, str]] | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path else None
            return data["members"] if isinstance(data, dict) and isinstance(data.get("members"), list) else None
        except (OSError, ValueError, KeyError):
            return None
    if path and path.exists() and clock() - path.stat().st_mtime < LIST_TTL and (got := stored()):
        return _coded(got)
    try:
        import requests
        from .research_replication import HEADERS
        r = (session or requests.Session()).get(u["url"], headers=HEADERS, timeout=40)
        r.raise_for_status()
        members = PARSERS[key](r.content.decode("utf-8-sig", errors="replace"))
        if len(members) < 20:
            raise ValueError(f"{u['index']}: only {len(members)} members found in the source")
    except Exception as e:  # noqa: BLE001
        old = stored()
        if old:
            log.warning("%s list unavailable (%s); using the stored copy", key, type(e).__name__)
            return _coded(old)
        raise
    if path:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            from .prices import atomic_write_text
            atomic_write_text(path, json.dumps({"members": members, "source": u["url"], "fetched": datetime.now().isoformat(timespec="seconds")}))
        except OSError:
            pass
    return _coded(members)


# ---- prices and fundamentals in the form the ScreenerService expects ----
class ForeignPrices:
    """``history(code)`` for qualified codes: the Yahoo symbol is derived (AAPL.US -> AAPL), UK pence become pounds."""

    def __init__(self, inner: Any, divisor: float = 1.0):
        self.inner, self.divisor = inner, divisor

    def history(self, symbol: str, range_: str = "2y") -> list[dict[str, Any]]:
        bars = self.inner.history(markets.yahoo_symbol(symbol), range_)
        if self.divisor == 1.0:
            return bars
        out = []
        for b in bars:
            nb = dict(b)
            for k in ("close", "adj_close", "open", "high", "low"):
                if nb.get(k) is not None:
                    nb[k] = float(nb[k]) / self.divisor
            out.append(nb)
        return out


def fund_fields(symbol: str, parsed: dict[str, Any], now: float) -> dict[str, Any]:
    """P/E and dividend yield (percent) from a Yahoo fundamentals snapshot; market cap is left out (it would be in the
    local currency, not rupees crore). {"error"} when Yahoo had none."""
    from .quote import build_quote
    q = build_quote(symbol, parsed, fetched_at=now)
    if q["error"]:
        return {"error": q["error"]}
    dy = q.get("dividend_yield")
    return {"pe": q.get("pe"), "market_cap_cr": None, "div_yield": None if dy is None else round(dy * 100.0, 2)}


def make_service(key: str, state_dir: Path, *, holidays_unused: Any = None, session: Any = None, fund_session: Any = None) -> Any:
    """A ScreenerService for one foreign universe: its own Yahoo caches under ``state/cache/screener_<market>``, at most
    3 history requests (then 2 fundamentals requests) in flight, jittered pauses, no India-only data."""
    from .fundamentals import YahooFundamentals
    from .prices import YahooPrices
    from .screener import ScreenerService
    key = key.upper()
    mk = UNIVERSES[key]["market"]
    cache = Path(state_dir) / "cache" / f"screener_{mk.lower()}"
    yp = YahooPrices(suffix=markets.MARKETS[mk]["yahoo_suffix"], session=session, cache_dir=cache, cache_ttl=CLOSED_TTL)
    yp.ttl_fn = lambda d: min(d, FRESH_TTL) if market_open(mk) else d   # 30 min during the market's own hours, 6 h otherwise
    prices = ForeignPrices(yp, UK_PENCE if mk == "UK" else 1.0)
    funds = YahooFundamentals(cache, session=fund_session, cache_ttl=24 * 3600)
    return ScreenerService(prices=prices, fundamentals=funds, load_universe=lambda k: load_members(k, cache / "lists"),
                           workers=3, fund_workers=2, pace=0.15, jitter=True, fund_fn=fund_fields, turnover_floor=0.0,
                           metrics_ttl=lambda: FRESH_TTL if market_open(mk) else CLOSED_TTL, meta=market_info)
