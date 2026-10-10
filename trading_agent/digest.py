"""Daily emails: the morning "what to buy / what to watch" brief and the evening "close" portfolio report.

``morning_brief(ctx)`` and ``evening_report(ctx)`` build the numbers by rules only, from public data and your own
read-only holdings. They never place an order and never raise: a source that is missing becomes
``{"unavailable": "<reason>"}`` in its section. The written summary on top (digest_writer.py), the email layout
(digest_render.py) and the schedule inside the watch service (digest_schedule.py) are separate modules.

Percentages inside the data are plain percent numbers (3.12 means +3.12%), keys end in ``_pct``.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
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
    calendar: Any = None  # NSEHolidays-like (is_trading_day): finds the previous trading day
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
    out = {"regime": r.get("regime"), "score": r.get("score"), "summary": clean_text(r.get("summary") or "", 500),
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
    ideas = []
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
        ideas.append({"symbol": row["symbol"], "name": clean_text(row.get("name") or "", 80), "price": round(float(price), 2),
                      "qty": size["qty"], "notional": size["notional"],
                      "stop": round(stop["level"], 2) if stop["level"] is not None else None,
                      "ret_6m_pct": _pct(row.get("ret_6m")), "ret_12_1_pct": _pct(row.get("ret_12_1")),
                      "rank": row.get("rank")})
    return {"universe": name, "universe_size": res["universe_size"], "scored": res["scored"],
            "eligible": res["eligible"], "errors": res["errors"], "ideas": ideas, "wait": bool(no_new_buys),
            "equity": round(equity, 2), "equity_basis": basis,
            "sizing": "1% of equity at risk on a 2x ATR move, at most 10% of equity per stock"}


_COMMON = {"IDEA", "BANK", "POWER", "STEEL", "GOLD", "LIFE", "OIL", "GAS", "ENERGY", "FINANCE", "CAPITAL", "GLOBAL",
           "INDIA", "INDIAN", "NATIONAL", "STATE", "UNION", "INDUSTRIES", "LIMITED", "FIRST", "GENERAL", "NEXT",
           "SOUTH", "NORTH", "EAST", "WEST", "MARKET", "TRADE", "GROUP", "SERVICES", "TECH", "PHARMA"}


def _remember(ctx: DigestContext, symbol: Any, name: Any) -> None:
    """Note a symbol and the distinctive words of a company name; the summary writer rejects any of them that the
    data does not carry."""
    if symbol:
        ctx.known.add(str(symbol).upper())
    for w in re.findall(r"[A-Za-z&]{4,}", str(name or "")):
        if w.upper() not in _COMMON:
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
                         "estimated": True})
    if no_price:
        notes.append(f"{no_price} Groww holding(s) without a market price (bonds or unlisted) were not checked")
    if ctx.practice is None:
        notes.append("Practice account: unavailable (no practice account yet)")
    else:
        try:
            for p in ctx.practice.positions():
                if p.current_price is None:
                    continue
                holdings.append({"symbol": p.symbol, "name": None, "source": "Practice", "qty": p.qty,
                                 "price": float(p.current_price), "pos": p, "estimated": False})
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
    items, healthy = [], 0
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
                est = " (estimated from your buy price)" if h["estimated"] else ""
                a = atr(bars) if bars else None
                near = min(a, NEAR_STOP * level) if a else NEAR_STOP * level   # 1 ATR, but never more than 3%
                if price <= level:
                    reasons.append(f"price {num(price, 2)} is at or below its stop {num(level, 2)}{est}")
                elif price - level <= near:
                    reasons.append(f"price {num(price, 2)} is within {num(price - level, 2)} "
                                   f"({(price / level - 1) * 100:.1f}%) of its stop {num(level, 2)}{est}")
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
                          "price": round(price, 2), "reasons": reasons})
        else:
            healthy += 1
    return {"items": items, "healthy": healthy, "checked": len(holdings), "notes": [clean_text(n, 300) for n in notes]}


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
             "ticker": clean_text(t.ticker, 20), "transaction": clean_text(t.transaction, 20),
             "size": clean_text(t.size, 60), "reported": t.report_date, "traded": t.transaction_date}
            for t in keep[:MAX_DEALS]]
    return {"since": since.isoformat(), "deals": rows, "total": len(keep), "following": list(s.investors)}


def _section(fn: Callable[..., Any], label: str, *a: Any) -> dict[str, Any]:
    try:
        return fn(*a)
    except Exception as e:  # noqa: BLE001 - a section never takes the email down
        log.warning("digest section %s failed: %s", label, e)
        return _err(label, e)


def _header(ctx: DigestContext, kind: str) -> dict[str, Any]:
    now = ctx.now()
    return {"kind": kind, "date": now.date().isoformat(), "generated_at": now.isoformat(timespec="seconds"),
            "delayed": bool(ctx.delayed), "currency": "₹"}


def morning_brief(ctx: DigestContext) -> dict[str, Any]:
    today = ctx.now().date()
    data = _header(ctx, "morning")
    data["mood"] = _section(_mood, "market mood", ctx)
    no_buys = data["mood"].get("no_new_buys") if "unavailable" not in data["mood"] else None
    data["buy_ideas"] = _section(_buy_ideas, "buy ideas", ctx, no_buys)
    data["watch"] = _section(_watch, "holdings", ctx, today)
    data["deals"] = _section(_deals, "deals", ctx, "morning", today)
    return data


# =============================================================================
# Evening
# =============================================================================
def _groww_close(ctx: DigestContext, today: date) -> dict[str, Any]:
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
            prev = prev_close(_bars(ctx, h["symbol"], "1mo", bse=h.get("exchange") == "BSE"), today)
        except Exception:  # noqa: BLE001
            prev = None
        row = {"symbol": h["symbol"], "name": clean_text(h.get("name") or "", 80), "qty": h["qty"], "price": round(price, 2),
               "value": round(h["qty"] * price, 2), "pl": h.get("pl"), "pl_pct": _pct(h.get("pl_pct")),
               "prev_close": None, "day_pct": None, "day_pl": None}
        if prev:
            row.update(prev_close=round(prev, 2), day_pct=round((price / prev - 1) * 100, 2),
                       day_pl=round(h["qty"] * (price - prev), 2))
            day_pl += h["qty"] * (price - prev)
            day_base += h["qty"] * prev
        else:
            no_prev.append(h["symbol"])
        out_rows.append(row)
    out_rows.sort(key=lambda r: (r["day_pct"] is None, -(r["day_pct"] or 0)))
    g = info["portfolio"]
    priced = [r for r in out_rows]
    out = {"value": g.get("value"), "invested": g.get("invested"), "pl": g.get("pl"), "pl_pct": _pct(g.get("pl_pct")),
           "day_pl": round(day_pl, 2) if priced and len(no_prev) < len(priced) else None,
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


def _practice_close(ctx: DigestContext, today: date) -> dict[str, Any]:
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
    st = _state(ctx)
    change = change_pct = since = since_change = since_pct = None
    fills: list[dict[str, Any]] = []
    if st is not None:
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
            "total_pl": perf.get("pnl"), "total_pl_pct": perf.get("pnl_pct"), "positions": positions,
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
                items.append({"symbol": sym, "title": clean_text(it.get("title"), 160),
                              "source": clean_text(it.get("source"), 40), "sentiment": it["sentiment"],
                              "confidence": it.get("confidence"), "event": it.get("event")})
        except Exception as e:  # noqa: BLE001
            errors.append(f"{sym}: {type(e).__name__}")
    items.sort(key=lambda i: order.get(i["sentiment"], 3))
    out: dict[str, Any] = {"items": items[:MAX_NEWS], "total": len(items), "symbols": len(held)}
    if errors:
        out["notes"] = errors[:5]
    return out


def _is_on(published: Any, day: date) -> bool:
    try:
        dt = datetime.fromisoformat(str(published))
    except ValueError:
        return False
    return (dt if dt.tzinfo else dt.replace(tzinfo=IST)).astimezone(IST).date() == day


def evening_report(ctx: DigestContext) -> dict[str, Any]:
    today = ctx.now().date()
    data = _header(ctx, "evening")
    data["groww"] = _section(_groww_close, "Groww portfolio", ctx, today)
    data["practice"] = _section(_practice_close, "practice account", ctx, today)
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
                 practice: Any = None, groww: Callable[[], dict[str, Any]] | None = None,
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
                practice = LocalPaperBroker(sim, starting_cash=settings.paper_starting_cash, price_fn=prices,
                                            currency="INR", whole_shares=True, cost_model=cost_model_for("in"),
                                            shared=True)
            except Exception as e:  # noqa: BLE001
                log.warning("digest practice account unavailable: %s", e)
    return DigestContext(
        settings=settings, prices=prices, prices_bse=YahooPrices(suffix=".BO", cache_dir=cache), context=context,
        data=data, news=news, practice=practice, calendar=holidays,
        groww=groww or (lambda: read_groww_portfolio(settings, prices, datetime.now(IST).isoformat(timespec="seconds"))),
        universe=load_universe, state_path=Path(settings.state_dir) / "state.json")
