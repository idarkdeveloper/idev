"""What Ask says for three kinds of question, from fixed templates over the dashboard's own data (never from a model):
stock ideas (the agent's market stance, its latest recommendations, the forward test's top picks), whether a market is open, and
the latest disclosed deals of the followed investors. Pure functions: the data comes in as arguments (ui.App collects it), the
clock is passed in, so everything is testable with fakes. Read-only: nothing here can place or prepare an order."""
from __future__ import annotations

import re
from datetime import date, datetime, time as dtime, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .timezones import IST

NOT_ADVICE = "These are the app's own rule-based screens, not financial advice. I cannot place orders."
MARKETS = ("IN", "US", "UK", "JP")
NSE_HOURS = (dtime(9, 15), dtime(15, 30))
# (name, time zone, open, close); weekdays only, holidays are not known for these markets (and the answer says so)
FOREIGN = {"US": ("The US market", "America/New_York", dtime(9, 30), dtime(16, 0)),
           "UK": ("The London market", "Europe/London", dtime(8, 0), dtime(16, 30)),
           "JP": ("The Tokyo market", "Asia/Tokyo", dtime(9, 0), dtime(15, 30))}


# ---- small formatting helpers ----
def _clock(t: dtime) -> str:
    h = t.hour % 12 or 12
    return f"{h}:{t.minute:02d} {'am' if t.hour < 12 else 'pm'}"


def _day(d: date) -> str:
    return f"{d.strftime('%A')} {d.day} {d.strftime('%B')}"


def _short(d: date) -> str:
    return f"{d.day} {d.strftime('%b')}"


def _span(delta: timedelta) -> str:
    mins = max(1, int(round(delta.total_seconds() / 60)))
    h, m = divmod(mins, 60)
    parts = ([f"{h} hour{'s' if h != 1 else ''}"] if h else []) + ([f"{m} minute{'s' if m != 1 else ''}"] if m else [])
    return " ".join(parts)


def _when(d: date, today: date) -> str:
    if d == today:
        return "today"
    if d == today + timedelta(days=1):
        return f"tomorrow, {_day(d)}"
    return f"on {_day(d)}"


def _spoken_inr(x: float) -> str:
    a = abs(float(x))
    if a >= 1e7:
        return f"{round(a / 1e7, 2):g} crore rupees"
    if a >= 1e5:
        return f"{round(a / 1e5, 2):g} lakh rupees"
    return f"{round(a):,} rupees" if a >= 100 else f"{round(a, 2):g} rupees"


def _skipped(today: date, nxt: date, is_day: Callable[[date], bool], holiday_name: Callable[[date], str | None] | None, weekend_known: bool) -> str:
    """Why the days between today and the next open were skipped: " Saturday and Sunday are weekend days. Tuesday 20 October is a market
    holiday (Diwali)." Names only the skipped weekend and holidays, at most two items. ``weekend_known``: today is a weekday, so a
    coming weekend is news (when today is itself a weekend day it has just been said)."""
    weekend, holidays = [], []
    d = today + timedelta(days=1)
    while d < nxt:
        if d.weekday() >= 5:
            weekend.append(d)
        elif not is_day(d):
            holidays.append(d)
        d += timedelta(days=1)
    items: list[str] = []
    if weekend and weekend_known:
        names = [x.strftime("%A") for x in weekend]
        items.append(" and ".join(names) + (" are weekend days." if len(names) > 1 else " is a weekend day."))
    if holidays:
        days = [_day(x) for x in holidays[:2]]
        reasons = [r for r in (holiday_name(x) if holiday_name else None for x in holidays[:2]) if r]
        text = " and ".join(days) + (" is a market holiday" if len(days) == 1 else " are market holidays")
        items.append(text + (f" ({', '.join(dict.fromkeys(reasons))})." if reasons else "."))
    return (" " + " ".join(items[:2])) if items else ""


# ---- is the market open ----
def compose_market_status(market: str, now: datetime, trading_day: Callable[[date], bool] | None = None,
                          holiday_name: Callable[[date], str | None] | None = None) -> dict[str, Any]:
    """{"say", "facts"}. NSE: the trading calendar (``trading_day``, ``holiday_name``) decides; US, UK and Japan: regular weekday
    hours in their own time zone, holidays not known (said so)."""
    market = market if market in MARKETS else "IN"
    if market == "IN":
        name, tz, (o, c), cal = "The NSE", IST, NSE_HOURS, True
        is_day = trading_day or (lambda d: d.weekday() < 5)
    else:
        label, zone, o, c = FOREIGN[market]
        name, tz, cal = label, ZoneInfo(zone), False
        is_day = lambda d: d.weekday() < 5  # noqa: E731
    n = now.astimezone(tz)
    today = n.date()
    start = datetime.combine(today, o, tzinfo=tz)
    end = datetime.combine(today, c, tzinfo=tz)
    where = "IST" if market == "IN" else {"US": "New York time", "UK": "London time", "JP": "Tokyo time"}[market]
    ist = lambda dt: dt.astimezone(IST)  # noqa: E731
    both = (lambda t: f"{_clock(t)} IST") if market == "IN" else (lambda t: f"{_clock(t)} {where}")

    def next_open(after_today: bool) -> datetime:
        d = today if not after_today else today + timedelta(days=1)
        for _ in range(14):
            if is_day(d):
                return datetime.combine(d, o, tzinfo=tz)
            d += timedelta(days=1)
        return datetime.combine(d, o, tzinfo=tz)

    facts: list[dict[str, Any]] = []
    if is_day(today) and start <= n <= end:
        say = f"{name} is open now. It closes at {both(c)}, in {_span(end - n)}."
        status, tone = "Open", "pos"
        facts.append({"label": "Closes", "value": both(c) if market == "IN" else f"{both(c)} ({_clock(ist(end).time())} IST)", "tone": ""})
    elif is_day(today) and n < start:
        say = f"{name} is closed now. It opens today at {both(o)}, in {_span(start - n)}."
        status, tone = "Closed", "neg"
        facts.append({"label": "Opens", "value": both(o), "tone": ""})
    else:
        nxt = next_open(after_today=True)
        if is_day(today):
            why = f"It closed at {both(c)} today. "
        else:
            hol = holiday_name(today) if (cal and holiday_name) else None
            why = (f"It is closed today for {hol}. " if hol else "It is closed today, it is the weekend. " if today.weekday() >= 5 else "It is closed today. ")
        say = f"{name} is closed now. {why}It opens {_when(nxt.date(), today)}, at {both(o)}." + _skipped(today, nxt.date(), is_day, holiday_name if cal else None, weekend_known=is_day(today) or today.weekday() < 5)
        status, tone = "Closed", "neg"
        facts.append({"label": "Opens", "value": f"{_short(nxt.date())}, {both(o)}", "tone": ""})
    if market != "IN":
        say += f" That is {_clock(ist(start).time())} to {_clock(ist(end).time())} IST. Holidays are not checked for this market."
    facts.insert(0, {"label": {"IN": "NSE", "US": "US market", "UK": "London market", "JP": "Tokyo market"}[market], "value": status, "tone": tone})
    facts.insert(1, {"label": "Time now", "value": f"{n.strftime('%H:%M')} {where}" + ("" if market == "IN" else f" ({ist(n).strftime('%H:%M')} IST)"), "tone": ""})
    return {"say": say, "facts": facts}


def compose_market_when(when: str, now: datetime, trading_day: Callable[[date], bool], holiday_name: Callable[[date], str | None] | None = None,
                        holiday_days: dict[str, Any] | None = None) -> dict[str, Any]:
    """The NSE's answer to "why is the market closed tomorrow", "is it a holiday today" and "when is the next holiday", from the cached
    holiday calendar (``holiday_days``: iso date -> name; None when there is none). Never fetches."""
    n = now.astimezone(IST)
    today = n.date()
    name_of = holiday_name or (lambda d: None)
    o, c = NSE_HOURS
    hours = f"{_clock(o)} to {_clock(c)} IST"
    now_fact = {"label": "Time now", "value": f"{n.strftime('%H:%M')} IST", "tone": ""}

    def next_open(after: date) -> date:
        d = after + timedelta(days=1)
        for _ in range(14):
            if trading_day(d):
                return d
            d += timedelta(days=1)
        return d

    if when == "holidays":
        if holiday_days is None:
            return {"say": "I do not have the NSE holiday list right now.", "facts": [{"label": "Holidays", "value": "not available", "tone": ""}, now_fact]}
        upcoming = []
        for iso, nm in sorted(holiday_days.items()):
            try:
                d = date.fromisoformat(str(iso)[:10])
            except ValueError:
                continue
            if d > today and d.weekday() < 5:
                upcoming.append((d, nm))
        upcoming = upcoming[:3]
        if not upcoming:
            return {"say": "I do not see any further NSE holidays in my holiday list.", "facts": [{"label": "Holidays", "value": "none ahead", "tone": ""}, now_fact]}
        say = "The next NSE holidays: " + "; ".join(f"{_day(d)}" + (f", {nm}" if nm else "") for d, nm in upcoming) + "."
        facts = [{"label": "Holiday", "value": f"{d.strftime('%a')} {_short(d)}" + (f" · {nm}" if nm else ""), "tone": ""} for d, nm in upcoming]
        return {"say": say, "facts": facts + [now_fact]}
    d = today + timedelta(days=1) if when == "tomorrow" else today
    word = "tomorrow" if when == "tomorrow" else "today"
    if d.weekday() >= 5:
        say = f"The NSE is closed {word}, {_day(d)}: it is a weekend."
        reason = "Weekend"
    elif not trading_day(d):
        nm = name_of(d)
        say = f"The NSE is closed {word}, {_day(d)}" + (f", for {nm}." if nm else f". I do not see a reason in my holiday list; {word} may be a special closure.")
        reason = nm or "no reason in the holiday list"
    else:
        extra = ""
        if word == "today":
            extra = " It has closed for the day." if n.time() > c else f" It opens at {_clock(o)} IST." if n.time() < o else " It is open now."
        return {"say": f"The NSE is open {word}, {_day(d)}, {hours}.{extra}",
                "facts": [{"label": "NSE", "value": f"Open {word}", "tone": "pos"}, now_fact, {"label": "Opens", "value": f"{_short(d)}, {_clock(o)} IST", "tone": ""},
                          {"label": "Closes", "value": f"{_clock(c)} IST", "tone": ""}]}
    nxt = next_open(d)
    say += f" It opens {_when(nxt, today)}, at {_clock(o)} IST."
    return {"say": say, "facts": [{"label": "NSE", "value": f"Closed {word}", "tone": "neg"}, now_fact, {"label": "Reason", "value": reason, "tone": ""},
                                   {"label": "Opens", "value": f"{_short(nxt)}, {_clock(o)} IST", "tone": ""}]}


# ---- stock ideas ----
REGIME_LABEL = {"risk_off": "Risk-off", "neutral": "Neutral", "risk_on": "Risk-on"}


def _signed(n: Any) -> str:
    return f"{int(n):+d}"


def compose_ideas(d: dict[str, Any]) -> dict[str, Any]:
    """{"say", "facts"} for "which stock to buy": the agent's stance, its latest recommendations (top 3) and the forward test's
    picks (top 5), always ending with the not-advice line. d = {regime: {regime, score, guidance} | None | {error},
    recs: [{action, ticker, headline, at}], picks: [symbols], picks_as_of: iso date | None}."""
    said: list[str] = []
    facts: list[dict[str, Any]] = []
    r = d.get("regime") or {}
    if r.get("regime") in REGIME_LABEL:
        label = REGIME_LABEL[r["regime"]]
        score = r.get("score")
        said.append(f"For Indian stocks, the agent's market stance is {label}" + (f", score {'minus ' if score < 0 else ''}{abs(int(score))}." if isinstance(score, (int, float)) else "."))
        if r.get("guidance"):
            g = str(r["guidance"]).strip().rstrip(".")
            said.append(g[0].upper() + g[1:] + ".")
        if r["regime"] == "risk_off":
            said.append("The agent itself advises no new buys.")
        facts.append({"label": "Market stance", "value": label + (f" ({_signed(score)})" if isinstance(score, (int, float)) else ""),
                      "tone": "neg" if r["regime"] == "risk_off" else "pos" if r["regime"] == "risk_on" else ""})
    else:
        said.append("For Indian stocks, I do not have the agent's market stance right now.")
        facts.append({"label": "Market stance", "value": "not available", "tone": ""})
    recs = [x for x in (d.get("recs") or []) if x and x.get("ticker") and not x.get("dismissed")][:3]
    if recs:
        items = []
        for x in recs:
            when = _iso_short(x.get("at"))
            act = str(x.get("action") or "").lower()
            items.append(f"{x['ticker']} ({act}{', ' + when if when else ''})")
            facts.append({"label": "Latest idea", "value": items[-1], "tone": "pos" if act == "buy" else ""})
        said.append("The agent's latest ideas: " + "; ".join(items) + ".")
    else:
        said.append("The agent has no recent recommendations.")
        facts.append({"label": "Latest idea", "value": "none yet", "tone": ""})
    picks = [str(p) for p in (d.get("picks") or [])][:5]
    if picks:
        as_of = _iso_short(d.get("picks_as_of"))
        said.append(f"The momentum screen's top picks{' as of ' + as_of if as_of else ''}: " + ", ".join(picks) + ".")
        for i, p in enumerate(picks, 1):
            facts.append({"label": f"Momentum pick {i}", "value": p, "tone": ""})
    said.append(NOT_ADVICE)
    return {"say": " ".join(said), "facts": facts}


def _iso_short(v: Any) -> str:
    try:
        return _short(date.fromisoformat(str(v)[:10]))
    except (TypeError, ValueError):
        return ""


# ---- the followed investors' latest deals ----
_QTY = re.compile(r"^\s*([\d,]+)\s*sh\b(?:\s*@\s*₹?\s*([\d,.]+))?", re.I)


def _indian(n: float) -> str:
    s = f"{int(round(n)):d}"
    if len(s) <= 3:
        return s
    head, tail = s[:-3], s[-3:]
    parts: list[str] = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts + [tail])


def compose_deals(deals: list[dict[str, Any]], today: date, as_of: str = "", days: int = 30) -> dict[str, Any]:
    """{"say", "facts"} for the latest (up to 5) disclosed deals of the followed investors. deals: [{investor, side
    ('buy'|'sell'|other text), ticker, size (as reported, e.g. '50000 sh @ ₹1234.5'), date (iso)}], newest first."""
    cutoff = today - timedelta(days=days)
    rows = []
    for t in deals or []:
        try:
            when = date.fromisoformat(str(t.get("date"))[:10])
        except ValueError:
            continue
        if when >= cutoff:
            rows.append((when, t))
    rows.sort(key=lambda r: r[0], reverse=True)
    if not rows:
        return {"say": f"There are no disclosed deals by the investors you follow in the last {days} days." + (f" As of {as_of}." if as_of else ""), "facts": []}
    said = [f"The latest disclosed deals by the investors you follow, as of {as_of or _short(today)}:"]
    facts: list[dict[str, Any]] = []
    for when, t in rows[:5]:
        side = str(t.get("side") or "").lower()
        verb = "bought" if side == "buy" else "sold" if side == "sell" else "traded"
        who = str(t.get("investor") or "An investor")
        tk = str(t.get("ticker") or "")
        m = _QTY.match(str(t.get("size") or ""))
        qty = int(m.group(1).replace(",", "")) if m else None
        price = float(m.group(2).replace(",", "")) if m and m.group(2) else None
        spoken = f"{who} {verb} {tk}" + (f", {_indian(qty)} shares" if qty else "") + (f" at {_spoken_inr(price)}" if price else "") + f", on {_short(when)}."
        said.append(spoken)
        shown = f"{side.upper() if side in ('buy', 'sell') else 'DEAL'} {tk}" + (f" · {_indian(qty)} sh" if qty else "") + (f" @ ₹{_indian(price)}" if price else "") + f" · {_short(when)}"
        facts.append({"label": who, "value": shown, "tone": "pos" if side == "buy" else "neg" if side == "sell" else "", "long": True})
    return {"say": " ".join(said), "facts": facts}


# ---- how is the market ----
STRIP = (("nifty50", "Nifty 50", "Nifty 50", True, True), ("sp500", "S&P 500", "S&P 500", True, True), ("nasdaq_fut", "Nasdaq futures", "Nasdaq fut", False, True),
         ("nikkei", "Nikkei", "Nikkei", True, True), ("india_vix", "India VIX", "India VIX", True, False), ("usdinr", "USD/INR", "USD/INR", True, False),
         ("brent", "Brent", "Brent", True, False))


def _level(v: float, indian: bool) -> str:
    if v >= 1000:
        return _indian(v) if indian else f"{v:,.0f}"
    return f"{v:.1f}" if v >= 100 else f"{v:.2f}".rstrip("0").rstrip(".") if v < 10 else f"{v:.1f}"


def _move(r: float | None) -> str:
    if r is None:
        return ""
    v = round(abs(r) * 100, 1)
    return "unchanged" if v == 0 else f"{'up' if r > 0 else 'down'} {v:.1f} percent"


def compose_market_summary(d: dict[str, Any]) -> dict[str, Any]:
    """{"say", "facts"} for "how is the market today", from the dashboard's market regime and its Markets strip (quotes as the strip
    has them). d = {regime: {regime, score, guidance} | None, markets: {key: {last, ret_1d, as_of}}, nse_open: bool | None,
    as_of: '21:06 IST' | ''}. A missing quote is left out and named."""
    said: list[str] = []
    facts: list[dict[str, Any]] = []
    r = d.get("regime") or {}
    if r.get("regime") in REGIME_LABEL:
        score = r.get("score")
        said.append(f"The market stance is {REGIME_LABEL[r['regime']]}" + (f", score {'minus ' if score < 0 else ''}{abs(int(score))}" if isinstance(score, (int, float)) else "") +
                    (": " + str(r["guidance"]).strip().rstrip(".").lower() + "." if r.get("guidance") else "."))
        facts.append({"label": "Market stance", "value": REGIME_LABEL[r["regime"]] + (f" ({_signed(score)})" if isinstance(score, (int, float)) else ""),
                      "tone": "neg" if r["regime"] == "risk_off" else "pos" if r["regime"] == "risk_on" else ""})
    else:
        said.append("I do not have the market stance right now.")
        facts.append({"label": "Market stance", "value": "not available", "tone": ""})
    m = d.get("markets") or {}
    missing: list[str] = []
    for key, spoken, _label, _has_level, has_move in STRIP:
        q = m.get(key) or {}
        last, ret = q.get("last"), q.get("ret_1d")
        if last is None:
            missing.append(spoken)
            continue
        indian = key in ("nifty50", "india_vix")
        mv = _move(ret) if has_move else ""
        if key == "nasdaq_fut":
            said.append(f"Nasdaq futures are {mv}." if mv else f"Nasdaq futures are at {_level(last, False)}.")
        elif key == "india_vix":
            said.append(f"India VIX is {_level(last, True)}.")
        elif key == "brent":
            said.append(f"Brent is {_level(last, False)} dollars" + (f", {_move(ret)}." if ret is not None else "."))
        elif key == "usdinr":
            said.append(f"USD/INR is {_level(last, False)}" + (f", {_move(ret)}." if ret is not None else "."))
        else:
            said.append(f"{spoken} is {mv + ' at ' if mv else 'at '}{_level(last, indian)}.")
        val = _level(last, indian) + (f" ({'+' if ret > 0 else '-' if ret < 0 else ''}{abs(ret) * 100:.1f}%)" if ret is not None and has_move else "")
        facts.append({"label": spoken, "value": val, "tone": ("pos" if ret and ret > 0 else "neg" if ret and ret < 0 else "") if has_move and key != "india_vix" else ""})
    if missing:
        said.append("No quote for " + " and ".join([", ".join(missing[:-1]), missing[-1]] if len(missing) > 1 else missing) + ".")
    nd = _iso_short((m.get("nifty50") or {}).get("as_of"))
    if d.get("nse_open") is False and nd:
        said.append(f"The NSE is closed, so Nifty prices are from the {nd} close; US and Asian indices and futures can be live.")
    if d.get("as_of"):
        said.append(f"As of {d['as_of']}.")
        facts.append({"label": "As of", "value": str(d["as_of"]), "tone": ""})
    return {"say": " ".join(said), "facts": facts}


# ---- the top stocks of a market (the screener's tables) ----
# market -> (adjective, currency symbol, decimals, spoken currency, name in the Screener's market tabs, Indian digit grouping)
TOP_MARKETS = {"US": ("US", "$", 2, "dollars", "United States", False), "UK": ("UK", "£", 2, "pounds", "United Kingdom", False),
               "JP": ("Japanese", "¥", 0, "yen", "Japan", False), "IN": ("Indian", "₹", 2, "rupees", "India", True)}
KINDS = ("leaders", "gainers", "losers")


def _money(v: float, symbol: str, dec: int, indian: bool) -> str:
    whole, _, frac = f"{abs(v):.{dec}f}".partition(".")
    grouped = _indian(int(whole)) if indian else f"{int(whole):,}"
    return f"{'-' if v < 0 else ''}{symbol}{grouped}" + (f".{frac}" if frac else "")


def _fmt_pct(v: float, dec: int = 1) -> str:
    return f"{'+' if v > 0 else '-' if v < 0 else ''}{abs(v):.{dec}f}%"


def loading_text(market: str) -> str:
    adj, _s, _d, _w, tab, _i = TOP_MARKETS[market if market in TOP_MARKETS else "IN"]
    return f"The {adj} list is loading, about 2 minutes. Ask again shortly, or open the Screener and pick {tab}."


def compose_market_top(args: dict[str, Any], table: dict[str, Any], now: datetime, regime: dict[str, Any] | None = None) -> dict[str, Any]:
    """{"say", "facts", "loading"} for "top US stocks", "UK gainers today", "biggest losers in Japan": from a Screener table
    (``table``: rows as the Screener serves them, total, pending, fetched_at) only; fixed templates, nothing from a model.
    ``leaders`` are ranked by the Screener's own momentum score (stocks that pass its factor screen), ``gainers`` and ``losers`` by the
    1-day change. A table with no priced row is "loading". ``regime`` is only used for India."""
    mk = str(args.get("market") or "IN").upper()
    mk = mk if mk in TOP_MARKETS else "IN"
    kind = str(args.get("kind") or "leaders")
    kind = kind if kind in KINDS else "leaders"
    try:
        n = max(3, min(10, int(args.get("n") or 5)))
    except (TypeError, ValueError):
        n = 5
    adj, sym, dec, word, _tab, indian = TOP_MARKETS[mk]
    ready = [r for r in (table.get("rows") or []) if r.get("price") is not None and not r.get("error")]
    if not ready:
        return {"say": loading_text(mk), "facts": [], "loading": True}
    if kind == "leaders":
        pool = sorted((r for r in ready if r.get("mom_eligible") and r.get("mom_score") is not None), key=lambda r: -r["mom_score"])
        head = f"The top {{n}} {adj} stocks by momentum right now:"
    else:
        pool = sorted((r for r in ready if r.get("chg_1d") is not None), key=lambda r: -r["chg_1d"] if kind == "gainers" else r["chg_1d"])
        pool = [r for r in pool if (r["chg_1d"] > 0 if kind == "gainers" else r["chg_1d"] < 0)]
        head = f"The top {{n}} {adj} gainers today:" if kind == "gainers" else f"The biggest {{n}} {adj} losers today:"
    stamp = ""
    if table.get("fetched_at"):
        at = datetime.fromtimestamp(float(table["fetched_at"]), IST)
        stamp = f" As of {at.strftime('%H:%M')} IST" + ("" if at.date() == now.astimezone(IST).date() else f" on {_short(at.date())}") + ", prices delayed."
    partial = f" The list is still filling ({table.get('pending')} of {table.get('total')} stocks to come), so this is partial." if table.get("pending") else ""
    if not pool:
        what = "stock passes the Screener's momentum filter yet" if kind == "leaders" else "stock is " + ("up" if kind == "gainers" else "down") + " today"
        return {"say": f"No {adj} {what}.{partial}{stamp}", "facts": [], "loading": False}
    pool = pool[:n]
    said, facts = [], []
    for r in pool:
        nm = str(r.get("name") or "").strip()
        nm = re.sub(r"[,\s]+(?:inc|corp|corporation|plc|ltd|limited|co|company|holdings?)\.?$", "", nm, flags=re.I).strip(" ,") if nm else ""
        who = f"{r['symbol']}" + (f" ({nm})" if nm else "")
        price = r["price"]
        spoken_price = _spoken_inr(price) if indian else f"{price:,.{dec}f} {word}"
        if kind == "leaders":
            y = r.get("ret_1y")
            move = f"up {abs(y):.0f} percent over 12 months" if y is not None and y > 0 else f"down {abs(y):.0f} percent over 12 months" if y is not None and y < 0 else "flat over 12 months" if y is not None else "12-month change not available"
            val = (f"{_fmt_pct(y)} 12m · " if y is not None else "") + _money(price, sym, dec, indian)
            tone = "pos" if y and y > 0 else "neg" if y and y < 0 else ""
        else:
            c = r["chg_1d"]
            move = f"{'up' if c > 0 else 'down'} {abs(c):.1f} percent today"
            val = f"{_fmt_pct(c)} today · {_money(price, sym, dec, indian)}"
            tone = "pos" if c > 0 else "neg"
        said.append(f"{who}, {move}, at {spoken_price}")
        facts.append({"label": f"{r['symbol']} · {nm}" if nm else str(r["symbol"]), "value": val, "tone": tone})
    notes = [head.format(n=len(pool)) + " " + "; ".join(said) + "."]
    if kind == "leaders":
        notes.append("Ranked by momentum, the Screener's own measure; not market size.")
    notes.append("These are the Screener's rankings, not recommendations; prices are delayed." if not stamp else "These are the Screener's rankings, not recommendations.")
    if mk == "IN" and (regime or {}).get("regime") == "risk_off":
        notes.append("The agent's risk-off stance applies to Indian buying only.")
    return {"say": " ".join(notes) + partial + stamp, "facts": facts, "loading": False}
