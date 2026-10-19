"""The Look up card's "What this means" text, for places without a browser (the Telegram /lookup command).

A line-for-line port of ``lookupTakeaway`` in ui/common.js, returning plain text (the caller escapes it for HTML).
tests/test_takeaway.py runs both on the same inputs (the JS under node) so the two never drift apart: change both
together.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from .quote import group_indian
from .timezones import IST


def _pct(v: Any, d: int = 1) -> str:
    if v is None:
        return "n/a"
    return ("+" if v >= 0 else "") + f"{v * 100:.{d}f}%"


def _money(v: Any, d: int = 0) -> str:
    """₹ with Indian digit grouping, like the page's money() for INR."""
    if v is None:
        return "n/a"
    x = float(v)
    whole, _, frac = f"{abs(x):.{d}f}".partition(".")
    return ("-" if x < 0 else "") + "₹" + group_indian(int(whole)) + (f".{frac}" if d else "")


def _parse(at: Any) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(at).replace(" ", "T"))
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=IST)   # NSE times carry no zone: they are IST


def _now_ms(now: Any) -> float:
    if now is None:
        return datetime.now(timezone.utc).timestamp() * 1000
    if isinstance(now, datetime):
        return now.timestamp() * 1000
    return float(now)


def days_ago(at: Any, now: Any = None) -> int | None:
    d = _parse(at)
    if d is None:
        return None
    return int((_now_ms(now) - d.timestamp() * 1000) // 86_400_000)


def short_day(at: Any) -> str:
    d = _parse(at)
    if d is None:
        return str(at)[:10]
    d = d.astimezone(IST)
    return f"{d.day} {MONTHS_EN_IN[d.month - 1]}"   # as the browser's en-IN date (September is "Sept")


MONTHS_EN_IN = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sept", "Oct", "Nov", "Dec")


def clip(t: str, n: int) -> str:
    if len(t) <= n:
        return t
    sp = t.rfind(" ", 0, n + 1)
    cut = sp if sp > n * 0.6 else n
    return re.sub(r"[,;:.]$", "", t[:cut]) + "…"


FLAGS = [(r"pledge", "a promoter pledge disclosure"), (r"auditor", "a change of auditor"),
         (r"credit rating", "a credit rating update"), (r"buy ?back", "a share buyback"),
         (r"bonus|split|sub-division", "a bonus issue or share split"),
         (r"penalty|show cause|adjudicat|settlement order|sebi order|levied|fine imposed", "a regulatory order or penalty")]


def lookup_takeaway(r: dict[str, Any], now: Any = None, ctx: dict[str, Any] | None = None) -> str:
    """``r`` is App.lookup()'s answer (plus ``news`` from App.news_for_ticker when known); ``ctx`` the optional
    {"regime", "screen"} the Live page passes. ``now``: a datetime or epoch milliseconds (None = now)."""
    m = r.get("momentum") or {}
    out: list[str] = []
    name = re.sub(r" Limited$", "", r["name"]) if r.get("name") else str(r.get("ticker") or "")
    if m.get("error"):
        return ""
    p6, p12, hi = m.get("ret_6m"), m.get("ret_12_1"), m.get("pct_from_52w_high")
    trend = ("a strong uptrend" if m.get("verdict") == "strong" else
             "a weak, falling trend" if m.get("verdict") == "weak" else "no clear trend")
    out.append(f"{name} is in {trend}: {_pct(p6)} over 6 months and {_pct(p12)} over the past year leaving out the latest month.")
    if m.get("above_200dma") is False:
        out.append("It trades below its 200-day average, so the strategy's rules make no new buy here.")
    if p6 is not None and p6 > 0.5:
        out.append(f"It has already risen {_pct(p6)} in six months: stocks this stretched often fall back sharply on any disappointment.")
    if hi is not None and hi > -0.03:
        out.append("It is right at its 52-week high.")
    elif hi is not None and hi < -0.3:
        out.append(f"It is {abs(hi * 100):.0f}% below its 52-week high.")
    anns = [a for a in (r.get("announcements") or []) if (lambda d: d is None or d <= 60)(days_ago(a.get("at"), now))]

    def find(rx: str) -> dict[str, Any] | None:
        pat = re.compile(rx, re.I)
        return next((a for a in anns if pat.search(f"{a.get('category') or ''} {a.get('text') or ''}")), None)
    tw, res = find(r"trading window"), find(r"financial result|outcome of board meeting")
    res_days = days_ago(res.get("at"), now) if res else None
    if res and res_days is not None and res_days <= 14:
        out.append(f"Results came out on {short_day(res.get('at'))}: the price may still be reacting.")
    elif tw:
        out.append(f"It closed its insider trading window on {short_day(tw.get('at'))}, which usually means quarterly results "
                   "are due within a few weeks: a big move either way is possible around them.")
    deal = find(r"acquisition|amalgamation|merger|demerger|scheme of arrangement|takeover")
    if deal:
        text = re.sub(r"^.*?(informed the Exchange (about|regarding) )", "", str(deal.get("text") or deal.get("category") or ""),
                      count=1, flags=re.I).replace("Hon ble", "Hon'ble")
        out.append(f'Corporate action on {short_day(deal.get("at"))} ("{clip(text, 90)}"): read it before buying.')
    hits = [txt for rx, txt in FLAGS if find(rx)]
    if hits:
        out.append(f"Also in the last two months: {', '.join(hits)}. Worth a look.")
    bad = next((n for n in ((r.get("news") or {}).get("items") or [])
                if n.get("sentiment") == "negative" and n.get("confidence") in ("medium", "high")
                and (lambda d: d is not None and d <= 3)(days_ago(n.get("published"), now))), None)
    if bad:
        out.append(f'Negative news on {short_day(bad.get("published"))} ({bad.get("source")}): "{clip(str(bad.get("title") or ""), 110)}". '
                   "Read it before buying.")
    pos, price = r.get("position"), r.get("price")
    if pos and price:
        pl = price / pos["avg_entry_price"] - 1
        room = 1 - pos["stop"] / price if pos.get("stop") else None
        kind = pos.get("stop_label") or "trailing"
        stop_txt = ("; it has no stop-loss, so nothing sells it automatically" if pos.get("stop_type") == "none"
                    else f"; its {kind} stop at {_money(pos['stop'], 2)} is {room * 100:.1f}% below the price" if room is not None else "")
        qty = pos["qty"]
        qty_txt = f"{int(qty)}" if float(qty).is_integer() else f"{qty}"
        out.append(f"Your practice account holds {qty_txt} at {_money(pos['avg_entry_price'], 2)} ({'+' if pl >= 0 else ''}{pl * 100:.1f}%){stop_txt}.")
    raw_rg = (ctx or {}).get("regime")
    rg: dict[str, Any] = raw_rg if isinstance(raw_rg, dict) and not raw_rg.get("error") else {}
    nifty = (rg.get("markets") or {}).get("nifty50") or {}
    market_wait = bool(rg) and (rg.get("regime") == "risk_off" or nifty.get("above_200dma") is False)
    if market_wait:
        risk_off, below = rg.get("regime") == "risk_off", nifty.get("above_200dma") is False
        why = ("The market is risk-off and the Nifty is below its 200-day average" if risk_off and below
               else "The market is risk-off" if risk_off else "The Nifty is below its 200-day average")
        out.append(f"{why}, so the agent's rules hold off new buys until it recovers; if you buy anyway, keep the position small.")
    strong = m.get("verdict") == "strong" and m.get("above_200dma") is not False
    stock_wait = m.get("verdict") == "weak" or m.get("above_200dma") is False
    risk = (", but there is event risk ahead, so keep any position small" if (tw or res or deal) else "") + \
           (" and expect big swings" if p6 is not None and p6 > 0.5 else "")
    if strong:
        out.append(f"Bottom line: trend-wise it passes, but the market filter says wait{risk}." if market_wait
                   else f"Bottom line: trend-wise it is the kind of stock the screen buys{risk}.")
    elif stock_wait:
        out.append("Bottom line: the momentum rules say wait; a disclosed buy here would be a watch, not a buy.")
    else:
        out.append("Bottom line: nothing decisive either way; it needs a reason beyond the price trend.")
    if ctx is not None and (market_wait or stock_wait):   # as in JS, an empty {} still counts
        screen = ctx.get("screen") or {}
        top = [str(x.get("symbol")) for x in (screen.get("top") or [])[:5]] if screen.get("top") else None
        at = str(screen["at"])[:10] if screen.get("at") else None
        today = datetime.now(timezone.utc).date().isoformat()
        out.append(f"Stocks passing the screen {('as of ' + at) if at and at != today else 'today'}: {', '.join(top)}." if top
                   else "Run Screen to see which stocks pass today.")
    return " ".join(out)
