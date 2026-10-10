"""Daily emails: the morning "what to buy / what to watch" brief and the evening "close" portfolio report.

``morning_brief(ctx)`` and ``evening_report(ctx)`` build the numbers by rules only, from public data and your own
read-only holdings. They never place an order and never raise: a source that is missing becomes
``{"unavailable": "<reason>"}`` in its section. The written summary on top (digest_writer.py), the email layout
(digest_render.py) and the schedule inside the watch service (digest_schedule.py) are separate modules.

Percentages inside the data are plain percent numbers (3.12 means +3.12%), keys end in ``_pct``.
"""

from __future__ import annotations

import functools
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Callable

from .notify import clean_text
from .quiver import fetch_followed, followed_names
from .risk import atr, position_size, position_stop
from .state import State
from .stops import FILLS_KEY
from .timezones import IST

log = logging.getLogger(__name__)

KINDS = ("morning", "evening")
DIGEST_STATE_FILE = "digest_state.json"  # once-a-day marks live apart from state.json, which check() rewrites whole
NEAR_STOP = 0.03  # near the stop: within the smaller of 1 ATR and 3% of it
BIG_DROP = -5.0  # percent in the last session
RESULTS_DAYS = 7
NEWS_DAYS = 2
MAX_DEALS = 15
MAX_NEWS = 20
MAX_WATCH = 15


@dataclass
class DigestContext:
    """The objects the digest reads. Every field except ``settings`` may be None (that section then says
    "unavailable"). ``prices`` has ``history(symbol, range)``; ``context`` is a regime.GlobalContext; ``data`` the
    NSE client (``announcements``, ``trades_for_investors``); ``news`` a NewsService-like object with
    ``for_symbol(symbol, name, background)``; ``practice`` the practice LocalPaperBroker; ``groww`` a function
    returning the read-only holdings (runner.read_groww_portfolio's shape); ``universe`` loads an index list."""

    settings: Any
    prices: Any = None
    prices_bse: Any = None
    context: Any = None
    data: Any = None
    news: Any = None
    practice: Any = None
    groww: Callable[[], dict[str, Any]] | None = None
    universe: Callable[[str], list[dict[str, str]]] | None = None
    state_path: Path | None = None
    now: Callable[[], datetime] = lambda: datetime.now(IST)  # noqa: E731
    delayed: bool = True  # prices come from Yahoo
    screen_budget_s: float = 240.0
    world_prices: Any = None  # history(symbol, range) with no exchange suffix (Yahoo), for the world indices
    names: Any = None  # CompanyNames: the full NSE equity list (cached), for the summary's known-name check
    calendar: Any = None  # NSEHolidays-like (is_trading_day): finds the previous trading day
    name_of: dict = field(default_factory=dict)  # symbol -> company name (from the NSE list), for the summary check
    known_symbols: set = field(default_factory=set)
    known: set = field(default_factory=set)  # every symbol / company word seen, so a summary cannot name others
    cancel: threading.Event | None = None  # set when the build has timed out
    deadline: float | None = None  # time.monotonic() after which the build stops asking for more
    _groww_memo: list = field(default_factory=list, repr=False)

    def expired(self) -> bool:
        return bool((self.cancel is not None and self.cancel.is_set())
                    or (self.deadline is not None and time.monotonic() > self.deadline))

    def read_groww(self) -> dict[str, Any]:
        """The Groww holdings, read once per build (one Groww call, one snapshot write)."""
        if not self._groww_memo:
            try:
                self._groww_memo.append((True, self.groww()))
            except Exception as e:  # noqa: BLE001
                self._groww_memo.append((False, e))
        ok, val = self._groww_memo[0]
        if not ok:
            raise val
        return val


def unavailable(reason: object) -> dict[str, Any]:
    return {"unavailable": clean_text(reason, 300)}


def _err(label: str, e: BaseException) -> dict[str, Any]:
    return unavailable(f"{label}: {type(e).__name__}: {e}")


def _pct(x: float | None) -> float | None:
    return None if x is None else round(x * 100, 2)


def _state(ctx: DigestContext) -> State | None:
    try:
        return State(ctx.state_path or Path(ctx.settings.state_dir) / "state.json")
    except Exception:  # noqa: BLE001
        return None


# -- price history ---------------------------------------------------------------
class _Budget:
    """History source that stops asking after a time budget, so a cold cache cannot make the screen run for ever."""

    def __init__(self, source: Any, seconds: float, ctx: "DigestContext | None" = None):
        self.source = source
        self.deadline = time.monotonic() + seconds
        self.ctx = ctx

    def history(self, symbol: str, range_: str = "2y") -> list[dict[str, Any]]:
        if time.monotonic() > self.deadline or (self.ctx is not None and self.ctx.expired()):
            raise TimeoutError("time budget for the screen used up")
        return self.source.history(symbol, range_)


def _bars(ctx: DigestContext, symbol: str, range_: str = "1y", bse: bool = False) -> list[dict[str, Any]]:
    if ctx.prices is None:
        raise LookupError("no price source")
    try:
        if bse and ctx.prices_bse is not None:
            return ctx.prices_bse.history(symbol, range_)
        return ctx.prices.history(symbol, range_)
    except Exception:  # noqa: BLE001
        if ctx.prices_bse is not None and not bse:
            return ctx.prices_bse.history(symbol, range_)  # BSE-only listing
        raise


def prev_close(bars: list[dict[str, Any]], today: date) -> float | None:
    """The close before today's session: the bar before today's, or the last bar when today's is not in yet."""
    if not bars:
        return None
    if bars[-1].get("date") == today.isoformat():
        return float(bars[-2]["close"]) if len(bars) >= 2 else None
    return float(bars[-1]["close"])


def last_session_move(bars: list[dict[str, Any]], today: date) -> float | None:
    """Percent move of the last completed session (before ``today``)."""
    done = [b for b in bars if str(b.get("date")) < today.isoformat()]
    if len(done) < 2 or not done[-2]["close"]:
        return None
    return (done[-1]["close"] / done[-2]["close"] - 1) * 100


# -- dates in announcements ----------------------------------------------------------
_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_DATE_PATTERNS = (
    re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?[\s\-/.]+([A-Za-z]{3,9})\.?[\s\-/.,]+(\d{4})\b"),
    re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b"),
    re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"),
    re.compile(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})\b"),
)
_RESULTS_RE = re.compile(r"board meeting|financial results|results|earnings", re.I)


def dates_in(text: str) -> list[date]:
    out: list[date] = []
    for pat_i, pat in enumerate(_DATE_PATTERNS):
        for m in pat.finditer(text or ""):
            try:
                a, b, c = m.groups()
                if pat_i == 0:
                    out.append(date(int(c), _MONTHS[b[:3].lower()], int(a)))
                elif pat_i == 1:
                    out.append(date(int(c), _MONTHS[a[:3].lower()], int(b)))
                elif pat_i == 2:
                    out.append(date(int(a), int(b), int(c)))
                else:
                    out.append(date(int(c), int(b), int(a)))
            except (KeyError, ValueError):
                continue
    return out


def results_due(announcements: list[dict[str, Any]], today: date, days: int = RESULTS_DAYS) -> tuple[date, str] | None:
    """(date, what) of a results / board-meeting announcement whose stated date falls in the next ``days`` days."""
    for a in announcements or []:
        blob = f"{a.get('category') or ''} {a.get('text') or ''}"
        if not _RESULTS_RE.search(blob):
            continue
        soon = sorted(d for d in dates_in(blob) if today <= d <= today + timedelta(days=days))
        if soon:
            what = "board meeting" if re.search(r"board meeting", blob, re.I) else "results"
            return soon[0], what
    return None


# -- money formatting for the data (used by the renderers too) --------------------
def num(v: float, d: int = 0) -> str:
    """12345678.5 as 1,23,45,678 (Indian grouping) with ``d`` decimals; the sign is dropped."""
    ip, _, fp = f"{abs(v):.{d}f}".partition(".")
    if len(ip) > 3:
        head, tail = ip[:-3], ip[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        ip = ",".join(([head] if head else []) + parts + [tail])
    return ip + ("." + fp if fp else "")


def num_intl(v: float, d: int = 0) -> str:
    """Foreign index levels and dollar prices use international grouping (123,456); rupee amounts stay Indian."""
    return f"{abs(v):,.{d}f}"


def inr(v: float | None, d: int = 0, sign: bool = False) -> str:
    if v is None:
        return "n/a"
    s = num(v, d)
    zero = float(s.replace(",", "")) == 0
    if v < 0 and not zero:
        return f"₹−{s}"
    return f"₹+{s}" if sign and not zero else f"₹{s}"


def pct_text(v: float | None, d: int = 1, sign: bool = True) -> str:
    if v is None:
        return "n/a"
    s = f"{abs(v):.{d}f}"
    if v < 0 and float(s) != 0:
        return f"−{s}%"
    return f"+{s}%" if sign and float(s) != 0 else f"{s}%"


# =============================================================================
# Morning
# =============================================================================
def _mood(ctx: DigestContext) -> dict[str, Any]:
    if ctx.context is None:
        return unavailable("global market context is not configured")
    r = ctx.context.fetch()
    nifty = (r.get("markets") or {}).get("nifty50") or {}
    below = nifty.get("above_200dma") is False
    risk_off = r.get("regime") == "risk_off"
    down = r.get("trend") == "down"
    why = []   # which of the rules switched buying off
    if risk_off:
        why.append(f"the market regime is risk-off (score {r.get('score'):+d})" if isinstance(r.get("score"), int)
                   else "the market regime is risk-off")
    if below:
        why.append("Nifty is below its 200-day average")
    if down:
        why.append("Nifty is in a downtrend (50-day average below the 200-day, price below both)")
    risk_off = risk_off or down
    out = {"trend": r.get("trend"), "nifty": {"last": nifty.get("last"), "ret_1d_pct": _pct(nifty.get("ret_1d")),
                                              "ret_20d_pct": _pct(nifty.get("ret_20d")), "above_200dma": nifty.get("above_200dma")},
           "regime": r.get("regime"), "score": r.get("score"), "summary": clean_text(r.get("summary") or "", 500),
           "guidance": r.get("guidance"), "nifty_above_200dma": nifty.get("above_200dma"),
           "no_new_buys": bool(risk_off or below), "why": why,
           "rules": "no new buys when the regime is risk-off, Nifty is below its 200-day average, or Nifty is in a downtrend"}
    if r.get("errors"):
        out["notes"] = [clean_text(f"{k}: {v}", 160) for k, v in r["errors"].items()]
    return out


def _buy_ideas(ctx: DigestContext, no_new_buys: bool | None) -> dict[str, Any]:
    from .screen import load_universe, run_screen
    s = ctx.settings
    if ctx.prices is None:
        return unavailable("no price source for the screen")
    name = s.digest_universe
    members = (ctx.universe or load_universe)(name)
    if not members:
        return unavailable(f"universe {name} is empty")
    for m in members:
        _remember(ctx, m["symbol"], m.get("name"))
    res = run_screen(members, _Budget(ctx.prices, ctx.screen_budget_s, ctx), top=int(s.digest_top))
    equity, basis = float(s.paper_starting_cash), "starting cash"
    if ctx.practice is not None:
        try:
            equity, basis = float(ctx.practice.account().equity), "practice account equity"
        except Exception:  # noqa: BLE001
            pass
    ideas, too_expensive = [], []
    for row in res["top"][:int(s.digest_top)]:
        price = row.get("last_close")
        if not price:
            continue
        try:
            bars = ctx.prices.history(row["symbol"], "2y")
        except Exception:  # noqa: BLE001
            bars = []
        a = atr(bars) if bars else None
        size = position_size(equity, float(price), a)
        stop = position_stop({"stop_type": "trailing", "avg_entry_price": price, "current_price": price,
                              "high_water": price}, bars)
        if not size["qty"]:
            too_expensive.append(row["symbol"])   # one share is more than the per-stock cap on this account
            continue
        ideas.append({"symbol": row["symbol"], "name": clean_text(row.get("name") or "", 80), "price": round(float(price), 2),
                      "qty": size["qty"], "notional": size["notional"],
                      "stop": round(stop["level"], 2) if stop["level"] is not None else None,
                      "ret_6m_pct": _pct(row.get("ret_6m")), "ret_12_1_pct": _pct(row.get("ret_12_1")),
                      "rank": row.get("rank")})
    return {"universe": name, "universe_size": res["universe_size"], "scored": res["scored"],
            "eligible": res["eligible"], "errors": res["errors"], "ideas": ideas, "too_expensive": too_expensive, "wait": bool(no_new_buys),
            "equity": round(equity, 2), "equity_basis": basis,
            "sizing": "1% of equity at risk on a 2x ATR move, at most 10% of equity per stock"}


COMMON_WORDS = {
    "WALL",
    "IDEA", "BANK", "POWER", "STEEL", "GOLD", "LIFE", "OIL", "GAS", "ENERGY", "FINANCE", "CAPITAL", "GLOBAL", "INDIA",
    "INDIAN", "NATIONAL", "STATE", "UNION", "INDUSTRIES", "LIMITED", "FIRST", "GENERAL", "NEXT", "SOUTH", "NORTH", "EAST",
    "WEST", "MARKET", "TRADE", "GROUP", "SERVICES", "TECH", "PHARMA", "CHEMICALS", "TEXTILES", "FOODS", "MOTORS", "HOUSING",
    "INFRA", "ENGINEERING", "TECHNOLOGIES", "SOLUTIONS", "SYSTEMS", "PRODUCTS", "MATERIALS", "LABORATORIES", "HOLDINGS",
    "ENTERPRISES", "VENTURES", "INTERNATIONAL", "COMPANY", "CORPORATION", "COMMUNICATIONS", "ELECTRIC", "ELECTRICALS",
    "PETROLEUM", "RESOURCES", "MINING", "SHIPPING", "LOGISTICS", "HEALTHCARE", "INSURANCE", "SECURITIES", "PROPERTIES",
    "REALTY", "MEDIA", "AUTO", "PAPER", "CEMENT", "SUGAR", "FERTILISERS", "PORTS", "TRADING", "INVESTMENTS", "INDUSTRIAL",
    "MANUFACTURING", "PRICE", "PRICES", "STOCK", "STOCKS", "SHARE", "SHARES", "TODAY", "NEWS", "WATCH", "CASH", "VALUE",
    "NEW", "BEST", "TOP", "HIGH", "LOW", "OPEN", "CLOSE", "PRIME", "ROYAL", "STAR", "BLUE", "GREEN", "SMART", "PURE"}
_COMMON = COMMON_WORDS


@functools.lru_cache(maxsize=1)
def english_words() -> frozenset:
    """Everyday English (a few thousand words, shipped as common_words.txt): these are never company names here."""
    try:
        from importlib import resources
        text = (resources.files("trading_agent") / "common_words.txt").read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        text = ""
    return frozenset(w.strip().lower() for w in text.splitlines() if w.strip()) | {w.lower() for w in COMMON_WORDS}


def _remember(ctx: DigestContext, symbol: Any, name: Any) -> None:
    """Note a symbol (always) and the distinctive, non-English words of a company name; the summary writer rejects
    any of them that the data does not carry."""
    if symbol:
        ctx.known.add(str(symbol).upper())
        ctx.known_symbols.add(str(symbol).upper())   # always: an upper-case OIL is still checked
    eng = english_words()
    for w in re.findall(r"[A-Za-z&]{4,}", str(name or "")):
        if w.upper() not in COMMON_WORDS and w.lower() not in eng:
            ctx.known.add(w.upper())


def _groww_rows(ctx: DigestContext) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """(Groww holdings, info) where info has notes / source / saved_at / unavailable."""
    info: dict[str, Any] = {}
    if ctx.groww is None:
        info["unavailable"] = "Groww holdings are not configured"
        return [], info
    try:
        g = ctx.read_groww()
    except Exception as e:  # noqa: BLE001
        info["unavailable"] = f"Groww holdings: {type(e).__name__}: {e}"
        return [], info
    if not g.get("linked"):
        info["unavailable"] = "Groww is not linked"
        return [], info
    if g.get("holdings") is None:
        info["unavailable"] = _groww_down_text(g)
        return [], info
    for h in g["holdings"]:
        _remember(ctx, h.get("symbol"), h.get("name"))
    if g.get("source") == "saved":
        info["source"], info["saved_at"] = "saved", g.get("saved_at")
        info["reason"] = _groww_down_text(g)
        info["age"], info["note"] = g.get("age_trading_days"), g.get("source_note")
    return list(g["holdings"]), {**info, "portfolio": g}


def _groww_down_text(g: dict[str, Any]) -> str:
    until = g.get("blocked_until")
    if until:
        try:
            return f"Groww unavailable until {datetime.fromisoformat(until).astimezone(IST).strftime('%H:%M')} IST"
        except ValueError:
            pass
    return clean_text(g.get("error") or g.get("reason") or "Groww unavailable", 200)


def _saved_label(info: dict[str, Any]) -> str | None:
    if info.get("source") != "saved":
        return None
    try:
        when = datetime.fromisoformat(info["saved_at"]).astimezone(IST).strftime("%d %b %H:%M")
    except (ValueError, TypeError, KeyError):
        when = str(info.get("saved_at"))
    age = info.get("age")
    old = f" — {age} trading days old; buys or sells since then are missing" if isinstance(age, int) and age > 1 else ""
    note = f" ({info['note']})" if info.get("note") else ""
    return f"saved holdings from {when} IST{old} ({info.get('reason')}){note}"


def _watch(ctx: DigestContext, today: date) -> dict[str, Any]:
    from .momentum import momentum_stats
    from .news import is_alert
    notes: list[str] = []
    holdings: list[dict[str, Any]] = []
    g_rows, g_info = _groww_rows(ctx)
    if g_info.get("unavailable"):
        notes.append(str(g_info["unavailable"]))
    label = _saved_label(g_info)
    if label:
        notes.append("Using " + label)
    no_price = 0
    for h in g_rows:
        if h.get("kind") == "bond" or h.get("price") is None:
            no_price += 1
            continue
        holdings.append({"symbol": h["symbol"], "name": h.get("name"), "source": "Groww", "qty": h["qty"],
                         "price": float(h["price"]), "exchange": h.get("exchange"),
                         "pos": {"stop_type": "trailing", "avg_entry_price": h["avg_price"],
                                 "current_price": h["price"], "high_water": h["avg_price"]},
                         "estimated": True, "avg": h["avg_price"]})
    if no_price:
        notes.append(f"{no_price} Groww holding(s) without a market price (bonds or unlisted) were not checked")
    if ctx.practice is None:
        notes.append("Practice account: unavailable (no practice account yet)")
    else:
        try:
            for p in ctx.practice.positions():
                if p.current_price is None:
                    continue
                # A copy from Groww, or an old mirror, was never bought here: its stored high-water mark is not "the
                # highest price since entry", so a stop is estimated from the buy price like a Groww holding.
                est = p.source == "groww" or p.opened_at is None
                pos: Any = ({"stop_type": p.stop_type, "stop_value": p.stop_value, "avg_entry_price": p.avg_entry_price,
                             "current_price": p.current_price, "high_water": p.avg_entry_price} if est else p)
                holdings.append({"symbol": p.symbol, "name": ctx.name_of.get(p.symbol.upper()), "source": "Practice",
                                 "qty": p.qty, "price": float(p.current_price), "pos": pos, "estimated": est,
                                 "avg": p.avg_entry_price})
        except Exception as e:  # noqa: BLE001
            notes.append(f"Practice account: unavailable ({type(e).__name__}: {e})")
    if g_info.get("unavailable") and ctx.practice is None:
        return {**unavailable("no holdings source: " + "; ".join(notes)), "notes": notes}
    news_err: list[str] = []
    ann_err: list[str] = []
    if ctx.news is None:
        notes.append("News: unavailable (not configured)")
    if ctx.data is None or not hasattr(ctx.data, "announcements"):
        notes.append("Results dates: unavailable (no NSE announcements source)")
    now = ctx.now()
    items = []
    for h in holdings:
        if ctx.expired():
            notes.append("Stopped early: the build ran out of time, so some holdings were not checked")
            break
        reasons: list[str] = []
        sym, price = h["symbol"], h["price"]
        _remember(ctx, sym, h.get("name"))
        bars: list[dict[str, Any]] = []
        try:
            bars = _bars(ctx, sym, "1y", bse=h.get("exchange") == "BSE")
        except Exception as e:  # noqa: BLE001
            notes.append(f"{sym}: price history unavailable ({type(e).__name__})")
        try:
            st = position_stop(h["pos"], bars)
            level = st["level"]
            if level is not None and price is not None:
                a = atr(bars) if bars else None
                near = min(a, NEAR_STOP * level) if a else NEAR_STOP * level   # 1 ATR, but never more than 3%
                avg = h["avg"]
                what = "the stop level"
                if h["estimated"]:
                    rule = "buy price minus 15%" if abs(level - avg * 0.85) < 0.005 * avg else "buy price minus 3×ATR"
                else:
                    rule = st["label"]
                if price <= level:
                    if h["estimated"] and price < level * 0.75 and price < avg:
                        reasons.append(f"down {(1 - price / avg) * 100:.0f}% from your buy price (well past any stop)")
                    else:
                        reasons.append(f"below {what} {num(level, 2)} ({rule})")
                elif price - level <= near:
                    reasons.append(f"within {num(price - level, 2)} ({(price / level - 1) * 100:.1f}%) of {what} {num(level, 2)} ({rule})")
        except Exception as e:  # noqa: BLE001
            notes.append(f"{sym}: stop level unavailable ({type(e).__name__})")
        if bars:
            ms = momentum_stats(bars)
            if ms.get("above_200dma") is False:
                reasons.append(f"below its 200-day average ({num(ms['ma200'], 2)})")
            drop = last_session_move(bars, today)
            if drop is not None and drop < BIG_DROP:
                reasons.append(f"fell {abs(drop):.1f}% in the last session")
        if ctx.news is not None and not ctx.expired():
            try:
                for it in ctx.news.for_symbol(sym, h.get("name"), background=True).get("items", []):
                    if is_alert(it) and _within_days(it.get("published"), now, NEWS_DAYS):
                        reasons.append(f"negative news: {clean_text(it.get('title'), 160)} ({clean_text(it.get('source'), 40)})")
            except Exception as e:  # noqa: BLE001
                if not news_err:
                    news_err.append(f"{type(e).__name__}: {e}")
                    notes.append(f"News: unavailable ({news_err[0]})")
        if ctx.data is not None and hasattr(ctx.data, "announcements") and not ctx.expired():
            try:
                due = results_due(ctx.data.announcements(sym, limit=20), today)
                if due:
                    reasons.append(f"{due[1]} due {due[0].strftime('%d %b')}")
            except Exception as e:  # noqa: BLE001
                if not ann_err:
                    ann_err.append(f"{type(e).__name__}: {e}")
                    notes.append(f"Results dates: unavailable ({ann_err[0]})")
        if reasons:
            items.append({"symbol": sym, "name": clean_text(h.get("name") or "", 80), "source": h["source"],
                          "price": round(price, 2), "reasons": reasons, "loss_pct": round((price / h["avg"] - 1) * 100, 2)})
    # one line per stock and place; a practice position with the same reasons as its Groww twin is shown once
    groww_price = {h["symbol"]: h["price"] for h in holdings if h["source"] == "Groww"}
    groww = {i["symbol"]: i for i in items if i["source"] == "Groww"}
    kept = []
    for i in items:
        if i["source"] != "Practice" or i["symbol"] not in groww_price:
            kept.append(i)
            continue
        twin = groww.get(i["symbol"])   # the same stock in both places is one card, with the reasons of both
        if twin is None:
            twin = {**i, "source": "Groww", "price": round(groww_price[i["symbol"]], 2), "reasons": []}
            groww[i["symbol"]] = twin
            kept.append(twin)
        twin["also_practice"] = True
        twin["reasons"] += [r for r in i["reasons"] if r not in twin["reasons"]]
    kept.sort(key=lambda i: (-len(i["reasons"]), i["loss_pct"]))   # most reasons first, then the biggest loss
    stocks = {h["symbol"] for h in holdings}   # a stock held in both places counts once
    flagged = {i["symbol"] for i in items}
    return {"items": kept[:MAX_WATCH], "total": len(kept), "more": max(0, len(kept) - MAX_WATCH),
            "healthy": len(stocks - flagged), "checked": len(stocks), "places": sorted({h["source"] for h in holdings}), "notes": [clean_text(n, 300) for n in notes]}


def _within_days(published: Any, now: datetime, days: float) -> bool:
    try:
        dt = datetime.fromisoformat(str(published))
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt >= now - timedelta(days=days)


def read_digest_state(state_dir: Any) -> dict[str, Any]:
    try:
        data = json.loads((Path(state_dir) / DIGEST_STATE_FILE).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _last_digest_date(ctx: DigestContext) -> date | None:
    sent = read_digest_state(ctx.settings.state_dir).get("sent") or {}
    days = []
    for v in sent.values():
        try:
            days.append(date.fromisoformat(str(v)))
        except ValueError:
            pass
    return max(days) if days else None


def _deals(ctx: DigestContext, kind: str, today: date) -> dict[str, Any]:
    if ctx.data is None:
        return unavailable("no deals source")
    s = ctx.settings
    trades = fetch_followed(ctx.data, s.investors, s.watch_source)
    if kind == "evening":
        keep = [t for t in trades if today.isoformat() in (t.report_date, t.transaction_date)]
        since = today
    else:
        since = _last_digest_date(ctx) or today - timedelta(days=3)
        keep = [t for t in trades if t.report_date >= since.isoformat()]
    keep.sort(key=lambda t: (t.report_date, t.transaction_date), reverse=True)
    rows = [{"investor": clean_text(t.investor, 80), "who": [clean_text(n, 60) for n in followed_names(t.investor, s.investors)],
             "ticker": clean_text(t.ticker, 20), "name": clean_text(ctx.name_of.get(t.ticker.upper()) or "", 80),
             "transaction": clean_text(t.transaction, 20),
             "size": clean_text(t.size, 60), "reported": t.report_date, "traded": t.transaction_date}
            for t in keep[:MAX_DEALS]]
    return {"since": since.isoformat(), "deals": rows, "total": len(keep), "following": list(s.investors)}


def _section(fn: Callable[..., Any], label: str, *a: Any) -> dict[str, Any]:
    try:
        return fn(*a)
    except Exception as e:  # noqa: BLE001 - a section never takes the email down
        log.warning("digest section %s failed: %s", label, e)
        return _err(label, e)


# =============================================================================
# World markets (morning)
# =============================================================================
# (yahoo symbol, short name, region, country). Futures are the overnight read; VIX is shown on its own.
WORLD_INDICES = [
    ("^GSPC", "S&P 500", "us", "US"), ("^IXIC", "Nasdaq", "us", "US"), ("^DJI", "Dow", "us", "US"),
    ("^N225", "Nikkei", "asia", "Japan"), ("^HSI", "Hang Seng", "asia", "Hong Kong"), ("000001.SS", "Shanghai", "asia", "China"),
    ("^KS11", "KOSPI", "asia", "Korea"), ("^TWII", "Taiwan", "asia", "Taiwan"), ("^STI", "Straits Times", "asia", "Singapore"),
    ("^AXJO", "ASX 200", "asia", "Australia")]
WORLD_FUTURES = [("ES=F", "S&P 500 fut"), ("NQ=F", "Nasdaq 100 fut")]
WORLD_VIX = ("^VIX", "VIX")
_REGIME_TREND = {"up": "UP", "down": "DOWN", "mixed": "mixed"}


def _chg(closes: list[float], n: int) -> float | None:
    if len(closes) <= n or not closes[-1 - n]:
        return None
    return round((closes[-1] / closes[-1 - n] - 1) * 100, 2)


def _avg(closes: list[float], n: int) -> float | None:
    return sum(closes[-n:]) / n if len(closes) >= n else None


def trend_label(closes: list[float]) -> str:
    """UP when close > 50-day average > 200-day average, DOWN when close < 50-day < 200-day, else mixed ("n/a" with
    fewer than 200 closes). A description of the current trend, not a forecast."""
    ma50, ma200 = _avg(closes, 50), _avg(closes, 200)
    if ma50 is None or ma200 is None:
        return "n/a"
    if closes[-1] > ma50 > ma200:
        return "UP"
    if closes[-1] < ma50 < ma200:
        return "DOWN"
    return "mixed"


ASIA_CLOSE_IST = {"^N225": dtime(11, 30), "^HSI": dtime(13, 30), "000001.SS": dtime(12, 30), "^KS11": dtime(12, 0),
                  "^TWII": dtime(11, 0), "^STI": dtime(14, 30), "^AXJO": dtime(11, 30)}


def _world_row(src: Any, symbol: str, name: str, drop_today: date | None = None) -> dict[str, Any] | None:
    """``drop_today``: that day's bar is left out (an Asian market still trading at 09:00 IST has only a partial bar;
    the row then describes the last completed session)."""
    try:
        bars = [b for b in src.history(symbol, "1y") if b.get("close") is not None]
        if drop_today is not None and bars and str(bars[-1].get("date")) == drop_today.isoformat():
            bars = bars[:-1]
        closes = [float(b["close"]) for b in bars]
    except Exception:  # noqa: BLE001 - an index that cannot be read is skipped (and counted)
        return None
    if len(closes) < 2:
        return None
    return {"index": name, "close": round(closes[-1], 2), "d1_pct": _chg(closes, 1), "d5_pct": _chg(closes, 5),
            "d20_pct": _chg(closes, 20), "trend": trend_label(closes), "vs_50d": (
                None if _avg(closes, 50) is None else ("above" if closes[-1] > _avg(closes, 50) else "below"))}


def _region_line(region: str, rows: list[dict[str, Any]], countries: dict[str, str]) -> str | None:
    if not rows:
        return None
    # by the TREND label of each index (the same one the table shows), never by the day's move; indices only
    up = [r for r in rows if r["trend"] == "UP"]
    down = [r for r in rows if r["trend"] == "DOWN"]
    mixed = [r for r in rows if r["trend"] not in ("UP", "DOWN")]
    label = "uptrend" if len(up) * 2 > len(rows) else "downtrend" if len(down) * 2 > len(rows) else "mixed"
    who = (lambda rs: [r["index"] for r in rs]) if region == "US" else (lambda rs: [countries[r["index"]] for r in rs])
    if region == "US":
        parts = [f"{len(up)} of {len(rows)} up"] + [", ".join(who(rs)) + f" {w}" for rs, w in ((down, "down"), (mixed, "mixed")) if rs]
        return f"US: {label} ({', '.join(parts)})"
    parts = [", ".join(who(rs)) + f" {w}" for rs, w in ((up, "up"), (down, "down"), (mixed, "mixed")) if rs]
    return f"Asia: {label} ({'; '.join(parts)})"


def _world(ctx: DigestContext) -> dict[str, Any]:
    src = ctx.world_prices or getattr(ctx.context, "source", None)
    if src is None:
        return unavailable("no price source for world markets")
    countries = {name: country for _s, name, _r, country in WORLD_INDICES}
    rows: dict[str, list[dict[str, Any]]] = {"us": [], "asia": []}
    skipped = 0
    for sym, name, region, _c in WORLD_INDICES:
        if ctx.expired():
            skipped += 1
            continue
        now = ctx.now()
        still_open = region == "asia" and now.time() < ASIA_CLOSE_IST.get(sym, dtime(0, 0))
        row = _world_row(src, sym, name, now.date() if still_open else None)
        if row is None:
            skipped += 1
        else:
            rows[region].append(row)
    futures = []
    for sym, name in WORLD_FUTURES:
        if ctx.expired():
            skipped += 1
            continue
        row = _world_row(src, sym, name)
        if row is None:
            skipped += 1
        else:
            futures.append(row)
    vix = None if ctx.expired() else _world_row(src, *WORLD_VIX)
    if vix is None:
        skipped += 1
    else:
        vix["direction"] = "rising" if vix["vs_50d"] == "above" else "falling" if vix["vs_50d"] == "below" else None
    india = []   # Nifty only: India VIX, USD/INR and Brent are in the risk gauges
    nifty = _world_row(src, "^NSEI", "Nifty")
    if nifty is not None:
        india.append(nifty)
    else:
        try:
            m = (ctx.context.fetch().get("markets", {}) if ctx.context is not None else {}).get("nifty50") or {}
            if m.get("last") is not None:
                india.append({"index": "Nifty", "close": round(m["last"], 2), "d1_pct": _pct(m.get("ret_1d")),
                              "d5_pct": _pct(m.get("ret_5d")), "d20_pct": _pct(m.get("ret_20d")),
                              "trend": _REGIME_TREND.get(str(m.get("trend")), "mixed")})
        except Exception:  # noqa: BLE001
            pass
    if not (rows["us"] or rows["asia"] or futures or vix or india):
        return unavailable("none of the world indices could be read")
    lines = [x for x in (_region_line("US", rows["us"], countries), _region_line("Asia", rows["asia"], countries)) if x]
    out: dict[str, Any] = {
        "us": rows["us"], "asia": rows["asia"], "futures": futures, "vix": vix, "india": india, "skipped": skipped,
        "region_lines": lines,
        "trends": {r["index"]: r["trend"] for r in rows["us"] + rows["asia"] + india},
        "futures_line": ("Overnight futures: " + ", ".join(f"{r['index'].replace(' fut', '')} {pct_text(r['d1_pct'])}" for r in futures)
                         if futures else None),
        "vix_line": (f"VIX {num_intl(vix['close'], 2)}, {vix['direction']} against its 50-day average" if vix and vix.get("direction") else None),
        "note": "Current trends from daily closes (UP: close above the 50-day average above the 200-day; DOWN: the reverse), not a forecast."}
    return out


# =============================================================================
# Risk gauges (morning)
# =============================================================================
# (yahoo symbol, name, kind). Readings come from fixed rules over daily closes; they describe, they do not forecast.
GAUGES = [("^VIX", "US VIX", "vix_us"), ("^INDIAVIX", "India VIX", "vix_in"), ("^TNX", "US 10-year yield %", "high"),
          ("DX-Y.NYB", "Dollar index", "high"), ("INR=X", "USD/INR", "inr"), ("BZ=F", "Brent $", "brent"),
          ("GC=F", "Gold $", "plain"), ("^NSEBANK", "Nifty Bank", "sector"), ("^CNXIT", "Nifty IT", "sector")]


WARN_TEXT = {"India VIX": "India VIX is above 20", "US VIX": "US VIX is above 25",
             "USD/INR": "the rupee is at a new 1-year low", "Brent $": "Brent is above $110"}


def _range_text(close: float, lo: float, hi: float) -> str:
    if hi <= lo:
        return "flat over the year"
    pos = (close - lo) / (hi - lo)
    if close >= hi * 0.98 and pos >= 0.9:
        return "near 1-year high"
    if pos <= 0.1:
        return "near 1-year low"
    return f"{pos * 100:.0f}% of the way up its 1-year range"


def gauge_reading(kind: str, closes: list[float]) -> tuple[str, bool]:
    """(plain-language reading, warning) for one gauge from its daily closes, by fixed rules."""
    c, hi = closes[-1], max(closes)
    ma50 = _avg(closes, 50)
    d20 = _chg(closes, 20)
    if kind in ("vix_us", "vix_in"):
        word = "calm" if c < 15 else "normal" if c < 20 else "elevated" if c < 25 else "stress"
        if kind == "vix_in" and d20 is not None and d20 >= 15:
            word += ", rising fast"
        warn = c > 20 if kind == "vix_in" else c > 25
        return word, warn
    if kind == "high":
        return ("near 1-year high: pressure on emerging markets" if c >= hi * 0.98 else "not near its 1-year high"), False
    if kind == "inr":
        near = c >= hi * 0.995
        return ("rupee near its weakest of the year" if near else "rupee not near its weakest of the year"), c >= hi
    if kind == "brent":
        elevated = ma50 is not None and c > ma50 * 1.05
        return ("oil elevated (costly for India)" if elevated else "oil not elevated"), c > 110
    if kind == "sector":
        return ("weak (below its 50-day average)" if ma50 is not None and c < ma50 else "holding above its 50-day average"), False
    return ("below its 50-day average" if ma50 is not None and c < ma50 else "above its 50-day average"), False


def _gauges(ctx: DigestContext) -> dict[str, Any]:
    src = ctx.world_prices or getattr(ctx.context, "source", None)
    if src is None:
        return unavailable("no price source for the risk gauges")
    rows, skipped = [], 0
    for sym, name, kind in GAUGES:
        if ctx.expired():
            skipped += 1
            continue
        try:
            closes = [float(b["close"]) for b in src.history(sym, "1y") if b.get("close") is not None]
        except Exception:  # noqa: BLE001
            closes = []
        if len(closes) < 21:
            skipped += 1
            continue
        reading, warn = gauge_reading(kind, closes)
        ma50 = _avg(closes, 50)
        rows.append({"gauge": name, "value": round(closes[-1], 2), "d20_pct": _chg(closes, 20),
                     "vs_50d_pct": None if ma50 is None else round((closes[-1] / ma50 - 1) * 100, 2),
                     "range": _range_text(closes[-1], min(closes), max(closes)), "reading": reading, "warning": warn})
    if not rows:
        return unavailable("none of the risk gauges could be read")
    return {"gauges": rows, "warnings": [r["gauge"] for r in rows if r["warning"]],
            "warning_texts": [WARN_TEXT[r["gauge"]] for r in rows if r["warning"] and r["gauge"] in WARN_TEXT], "skipped": skipped,
            "note": "Readings from fixed rules over daily closes, not a forecast."}


def _header(ctx: DigestContext, kind: str) -> dict[str, Any]:
    now = ctx.now()
    return {"kind": kind, "date": now.date().isoformat(), "generated_at": now.isoformat(timespec="seconds"),
            "delayed": bool(ctx.delayed), "currency": "₹"}


def load_known(ctx: DigestContext) -> None:
    """Every NSE symbol and distinctive company word, from the cached equity list, so a summary cannot name a stock
    the data does not list. Falls back to what the run sees (universe, holdings) when the list is unavailable."""
    if ctx.names is None:
        return
    try:
        for sym, name in ctx.names._nse_names().items():
            _remember(ctx, sym, name)
            ctx.name_of[sym] = name
    except Exception as e:  # noqa: BLE001
        log.warning("digest: the NSE equity list is unavailable for the name check: %s", e)


def morning_brief(ctx: DigestContext) -> dict[str, Any]:
    load_known(ctx)
    today = ctx.now().date()
    data = _header(ctx, "morning")
    data["mood"] = _section(_mood, "market mood", ctx)
    no_buys = data["mood"].get("no_new_buys") if "unavailable" not in data["mood"] else None
    data["world"] = _section(_world, "world markets", ctx)
    data["gauges"] = _section(_gauges, "risk gauges", ctx)
    data["buy_ideas"] = _section(_buy_ideas, "buy ideas", ctx, no_buys)
    data["watch"] = _section(_watch, "holdings", ctx, today)
    data["deals"] = _section(_deals, "deals", ctx, "morning", today)
    return data


# =============================================================================
# Evening
# =============================================================================
def _groww_close(ctx: DigestContext, today: date, closed: bool = True) -> dict[str, Any]:
    rows, info = _groww_rows(ctx)
    if info.get("unavailable"):
        return unavailable(info["unavailable"])
    out_rows, no_price, no_prev = [], [], []
    day_pl = day_base = 0.0
    for h in rows:
        price = h.get("price")
        if price is None:
            no_price.append(h["symbol"])
            continue
        prev = None
        try:
            if closed:
                prev = prev_close(_bars(ctx, h["symbol"], "1mo", bse=h.get("exchange") == "BSE"), today)
        except Exception:  # noqa: BLE001
            prev = None
        row = {"symbol": h["symbol"], "name": clean_text(h.get("name") or "", 80), "qty": h["qty"], "price": round(price, 2),
               "avg": h.get("avg_price"),
               "value": round(h["qty"] * price, 2), "pl": h.get("pl"), "pl_pct": _pct(h.get("pl_pct")),
               "prev_close": None, "day_pct": None, "day_pl": None}
        if prev:
            row.update(prev_close=round(prev, 2), day_pct=round((price / prev - 1) * 100, 2),
                       day_pl=round(h["qty"] * (price - prev), 2))
            day_pl += h["qty"] * (price - prev)
            day_base += h["qty"] * prev
        elif closed:
            no_prev.append(h["symbol"])
        out_rows.append(row)
    out_rows.sort(key=lambda r: (r["day_pct"] is None, -(r["day_pct"] or 0)))
    g = info["portfolio"]
    priced = [r for r in out_rows]
    out = {"value": g.get("value"), "invested": g.get("invested"), "pl": g.get("pl"), "pl_pct": _pct(g.get("pl_pct")),
           "day_pl": round(day_pl, 2) if closed and priced and len(no_prev) < len(priced) else None,
           "day_pct": round(day_pl / day_base * 100, 2) if day_base else None,
           "holdings": out_rows, "no_price": no_price, "no_prev_close": no_prev}
    label = _saved_label(info)
    if label:
        out["saved"] = label
    return out


def _previous_trading_day(ctx: DigestContext, today: date) -> date:
    d = today - timedelta(days=1)
    for _ in range(14):
        if d.weekday() < 5 and (ctx.calendar is None or ctx.calendar.is_trading_day(d)):
            return d
        d -= timedelta(days=1)
    return d


def _practice_close(ctx: DigestContext, today: date, closed: bool = True,
                    groww: dict[str, Any] | None = None) -> dict[str, Any]:
    if ctx.practice is None:
        return unavailable("no practice account yet")
    acct = ctx.practice.account()
    perf = ctx.practice.performance() if hasattr(ctx.practice, "performance") else {}
    positions = []
    for p in ctx.practice.positions():
        positions.append({"symbol": p.symbol, "qty": p.qty, "avg": round(p.avg_entry_price, 2),
                          "price": round(p.current_price, 2) if p.current_price is not None else None,
                          "pl": round(p.unrealized_pl, 2) if p.unrealized_pl is not None else None,
                          "pl_pct": round((p.current_price / p.avg_entry_price - 1) * 100, 2)
                          if p.current_price is not None and p.avg_entry_price else None})
    same = None
    if groww and positions and "holdings" in groww:
        g_rows = {h["symbol"]: h for h in groww["holdings"]}
        unpriced = set(groww.get("no_price") or [])
        p_rows = {x["symbol"]: x for x in positions}
        if g_rows and set(g_rows) <= set(p_rows) and set(p_rows) <= set(g_rows) | unpriced and all(
                float(g_rows[k]["qty"]) == float(p_rows[k]["qty"]) and g_rows[k].get("avg")
                and abs(float(g_rows[k]["avg"]) - float(p_rows[k]["avg"])) <= 0.001 * float(g_rows[k]["avg"]) for k in g_rows):
            same = len(positions)
    st = _state(ctx)
    change = change_pct = since = since_change = since_pct = None
    fills: list[dict[str, Any]] = []
    if st is not None and closed:
        before, before_day = None, None
        for pt in st.data.get("practice_equity", []):
            try:
                d = datetime.fromisoformat(pt["at"])
                d = (d if d.tzinfo else d.replace(tzinfo=IST)).astimezone(IST).date()
            except (KeyError, ValueError):
                continue
            if d < today:
                before, before_day = pt["equity"], d
        if before:
            diff = round(acct.equity - before, 2)
            pct = round(diff / before * 100, 2)
            if before_day == _previous_trading_day(ctx, today):
                change, change_pct = diff, pct
            else:   # no point from the previous trading day: say what the change is measured from
                since, since_change, since_pct = before_day.isoformat(), diff, pct
        for f in st.data.get(FILLS_KEY, []):
            try:
                d = datetime.fromisoformat(str(f.get("at")))
                d = (d if d.tzinfo else d.replace(tzinfo=IST)).astimezone(IST).date()
            except ValueError:
                continue
            if d == today:
                fills.append({"symbol": f["symbol"], "qty": f["qty"], "price": f["price"], "stop": f.get("stop"),
                              "label": f.get("label")})
    return {"equity": round(acct.equity, 2), "cash": round(acct.cash, 2), "day_change": change, "day_change_pct": change_pct,
            "since": since, "since_change": since_change, "since_change_pct": since_pct,
            "same_as_groww": same,
            "total_pl": perf.get("pnl"), "total_pl_pct": perf.get("pnl_pct"), "positions": [] if same else positions,
            "stop_fills_today": fills}


def _news_today(ctx: DigestContext, today: date) -> dict[str, Any]:
    if ctx.news is None:
        return unavailable("news is not configured")
    held: dict[str, str | None] = {}
    rows, _info = _groww_rows(ctx)
    for h in rows:
        if h.get("kind") != "bond":
            held.setdefault(h["symbol"], h.get("name"))
    if ctx.practice is not None:
        try:
            for p in ctx.practice.positions():
                held.setdefault(p.symbol, None)
        except Exception:  # noqa: BLE001
            pass
    if not held:
        return {"items": [], "note": "no holdings to look up"}
    order = {"negative": 0, "neutral": 1, "positive": 2}
    items, errors = [], []
    for sym, name in held.items():
        if ctx.expired():
            errors.append("stopped early: out of time")
            break
        try:
            for it in ctx.news.for_symbol(sym, name, background=True).get("items", []):
                if not it.get("sentiment") or not _is_on(it.get("published"), today):
                    continue
                link = str(it.get("link") or "")
                items.append({"symbol": sym, "when": _day_label(it.get("published")),
                              "link": link if link.startswith(("http://", "https://")) else "",
                              "name": clean_text(name or ctx.name_of.get(sym.upper()) or "", 80),
                              "title": clean_text(it.get("title"), 160),
                              "source": clean_text(it.get("source"), 40), "sentiment": it["sentiment"],
                              "confidence": it.get("confidence"), "event": it.get("event")})
        except Exception as e:  # noqa: BLE001
            errors.append(f"{sym}: {type(e).__name__}")
    items.sort(key=lambda i: order.get(i["sentiment"], 3))
    out: dict[str, Any] = {"items": items[:MAX_NEWS], "total": len(items), "symbols": len(held)}
    if errors:
        out["notes"] = errors[:5]
    return out


def _day_label(published: Any) -> str:
    try:
        dt = datetime.fromisoformat(str(published))
        return f"{dt.day} {dt:%b}"
    except ValueError:
        return ""


def _is_on(published: Any, day: date) -> bool:
    try:
        dt = datetime.fromisoformat(str(published))
    except ValueError:
        return False
    return (dt if dt.tzinfo else dt.replace(tzinfo=IST)).astimezone(IST).date() == day


def _trading_day(ctx: DigestContext, d: date) -> bool:
    return d.weekday() < 5 and (ctx.calendar is None or bool(ctx.calendar.is_trading_day(d)))


def evening_report(ctx: DigestContext) -> dict[str, Any]:
    load_known(ctx)
    now = ctx.now()
    today = now.date()
    data = _header(ctx, "evening")
    trading = _trading_day(ctx, today)
    closed = trading and now.time() >= dtime(15, 30)
    if not closed:   # a weekend, a holiday, or before the close: the figures are those of the last close
        last = _previous_trading_day(ctx, today)
        data["stale_close"] = {"date": last.isoformat(), "reason": "before the close" if trading else "no trading today",
                               "label": f"{last:%a} {last.day} {last:%b}"}
    data["groww"] = _section(_groww_close, "Groww portfolio", ctx, today, closed)
    data["practice"] = _section(_practice_close, "practice account", ctx, today, closed,
                                (data["groww"] if "unavailable" not in data["groww"] else None))
    data["news"] = _section(_news_today, "news", ctx, today)
    data["deals"] = _section(_deals, "deals", ctx, "evening", today)
    return data


def build_data(kind: str, ctx: DigestContext) -> dict[str, Any]:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    return morning_brief(ctx) if kind == "morning" else evening_report(ctx)


# =============================================================================
# Real context
# =============================================================================
def make_context(settings: Any, *, data: Any = None, prices: Any = None, news: Any = None, holidays: Any = None,
                 prices_bse: Any = None, practice: Any = None, groww: Callable[[], dict[str, Any]] | None = None,
                 context: Any = None) -> DigestContext:
    """The digest's inputs for a real run. Anything that cannot be built is left None (its section then says
    "unavailable"); nothing here places an order or writes to Groww."""
    from .broker import LocalPaperBroker
    from .costs import cost_model_for
    from .prices import YahooPrices
    from .runner import free_prices, read_groww_portfolio
    from .screen import load_universe

    cache = Path(settings.state_dir) / "cache"
    prices = prices or free_prices(settings)
    if context is None:
        from .regime import GlobalContext
        context = GlobalContext(YahooPrices(suffix="", cache_dir=cache, cache_ttl=900))
    if news is None:
        try:
            from .instruments import CompanyNames
            from .news import NewsService
            news = NewsService(settings, names=CompanyNames(cache))
        except Exception as e:  # noqa: BLE001
            log.warning("digest news unavailable: %s", e)
    if practice is None:
        sim = Path(settings.state_dir) / "paper_broker.json"
        if sim.exists():
            try:
                from .instruments import nse_then_bse
                bse = prices_bse or YahooPrices(suffix=".BO", cache_dir=cache)
                practice = LocalPaperBroker(sim, starting_cash=settings.paper_starting_cash, price_fn=nse_then_bse(prices, bse),
                                            currency="INR", whole_shares=True, cost_model=cost_model_for("in"),
                                            shared=True)
            except Exception as e:  # noqa: BLE001
                log.warning("digest practice account unavailable: %s", e)
    names = None
    try:
        from .instruments import CompanyNames
        names = CompanyNames(cache)
    except Exception:  # noqa: BLE001
        pass
    return DigestContext(
        names=names, world_prices=YahooPrices(suffix="", cache_dir=cache, cache_ttl=6 * 3600), settings=settings, prices=prices, prices_bse=prices_bse or YahooPrices(suffix=".BO", cache_dir=cache), context=context,
        data=data, news=news, practice=practice, calendar=holidays,
        groww=groww or (lambda: read_groww_portfolio(settings, prices, datetime.now(IST).isoformat(timespec="seconds"))),
        universe=load_universe, state_path=Path(settings.state_dir) / "state.json")
