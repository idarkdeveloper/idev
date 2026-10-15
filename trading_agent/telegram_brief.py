"""The Telegram version of the daily emails: a phone-sized HTML message built from the email's own data (no model, no
network). ``telegram_brief(email, allowed_hosts, commands)`` gives {"html", "plain", "button"}; the notifier sends the HTML with
parse_mode HTML and, if Telegram refuses it, the plain text once. Every value that comes from outside is escaped."""

from __future__ import annotations

import html as _html
import ipaddress
import math
import re
from datetime import date
from typing import Any, TypeGuard

from .digest import num, pct_text
from .digest_rules import label, srupee

LIMIT = 4000
TABLE_ROWS = 5
HEAT_CELLS = 5
DAY_STEP = 1.0      # a day's move: one heat cell per 1% (5 cells = 5% or more)
LOSS_STEP = 10.0    # a total loss: one cell per 10% (5 cells = 50% or more)
RULE = "━━━━━━━━━━━━"   # the portfolio card's separator: short enough for the narrowest phone
ROLLOVER_CHARS = 400
SUMMARY_CHARS = 300
CRITERIA = "stop = buy price − 3×ATR · avg = 200-day average"
_ICON = {"risk_on": "🟢", "neutral": "🟡", "risk_off": "🔴"}
_TREND_ICON = {"uptrend": "🟢", "downtrend": "🔴", "mixed": "🟡"}
_WHY = (("risk-off", "regime risk-off"), ("200-day", "Nifty below 200-day avg"), ("downtrend", "downtrend"))


def _e(v: Any) -> str:
    return _html.escape(str(v if v is not None else ""), quote=False)


def _ok(sec: Any) -> TypeGuard[dict[str, Any]]:
    return isinstance(sec, dict) and "unavailable" not in sec


def _header(data: dict[str, Any], kind: str) -> str:
    try:
        d = date.fromisoformat(str(data.get("date")))
        when = f"{d.strftime('%a')} {d.day} {d.strftime('%b')}"
    except ValueError:
        when = ""
    title = "🌅 Morning brief" if kind == "morning" else "🌆 Close"
    return f"<b>{title}{' · ' + _e(when) if when else ''}</b>"


def _short_why(why: Any) -> str:
    out: list[str] = []
    for w in why or []:
        low = str(w).lower()
        short = next((s for k, s in _WHY if k in low), None)
        if short and short not in out:
            out.append(short)
    return ", ".join(out)


def _regime_line(mood: Any) -> str | None:
    if not _ok(mood) or not mood.get("regime"):
        return None
    regime = str(mood["regime"])
    line = f"{_ICON.get(regime, '⚪')} <code>{_e(regime.replace('_', '-').upper())}</code>"
    if isinstance(mood.get("score"), int):
        line += f" ({mood['score']:+d})"
    why = _short_why(mood.get("why"))
    nifty_raw = mood.get("nifty")
    nifty: dict[str, Any] = nifty_raw if isinstance(nifty_raw, dict) else {}
    last = nifty.get("last")
    lvl = f"Nifty {num(last, 0)}" if isinstance(last, (int, float)) else ""
    tail = " · ".join(x for x in (_e(why), lvl) if x)
    return line + (f" · {tail}" if tail else "")


def _action_line(mood: Any, ideas: Any) -> str | None:
    if not _ok(mood):
        return None
    found = (ideas.get("ideas") or []) if _ok(ideas) else []
    if mood.get("no_new_buys"):
        return "⛔ No new buys" + (f" ({len(found)} pass the screen, held back)" if found else "")
    if found:
        top = ", ".join(_e(label(i)) for i in found[:3])
        return f"✅ {len(found)} buy idea{'s' if len(found) != 1 else ''}: {top}"
    return "✅ New buys allowed · none pass the screen" if _ok(ideas) else None


def _macro_line(world: Any, gauges: Any) -> str | None:
    bits: list[str] = []
    if _ok(world):
        for ln in world.get("region_lines") or []:
            m = re.match(r"(US|Asia): (uptrend|downtrend|mixed)", str(ln))
            if m:
                bits.append(f"{m.group(1)} {_TREND_ICON[m.group(2)]}")
    line = "🌍 " + " · ".join(bits) if bits else None
    home: list[str] = []
    if _ok(gauges):
        for r in gauges.get("gauges") or []:
            name = str(r.get("gauge") or "")
            if name not in ("India VIX", "USD/INR") or not isinstance(r.get("value"), (int, float)):
                continue
            reading = str(r.get("reading") or "").lower()
            icon = ("🔴" if r.get("warning") or "weakest" in reading or "near 1-year high" in reading
                    else "🟡" if "rising" in reading else "🟢")
            d20 = r.get("d20_pct")
            extra = f" ({pct_text(d20, 0)} 20d)" if name == "India VIX" and isinstance(d20, (int, float)) else ""
            home.append(f"{_e(name)} {num(r['value'], 1)}{extra} {icon}")
        warn = [str(t).strip().rstrip(".") for t in gauges.get("warning_texts") or [] if str(t).strip()]
        home += [_e(t[0].upper() + t[1:]) for t in warn]
    if home:
        line = (line + "\n" if line else "") + "🇮🇳 " + " · ".join(home)
    return line


def _card(value: str, today: str | None, total: str | None, holdings: Any = None) -> str:
    """The portfolio card: a title, a rule, one line each for Value, Today and Total P&L, a rule. Plain lines (no
    <pre>), so a narrow screen wraps them instead of scrolling a fixed-width block. ``value`` is already HTML."""
    n = f" · {holdings} holdings" if isinstance(holdings, int) and holdings else ""
    rows = [f"💼 <b>Portfolio</b>{n}", RULE, f"Value: <b>{value}</b>"]
    rows += [f"Today: {today}"] if today else []
    rows += [f"Total P&amp;L: {total}"] if total else []
    return "\n".join([*rows, RULE])


def _portfolio_line(watch: Any) -> str | None:
    p = watch.get("portfolio") if _ok(watch) else None
    if not isinstance(p, dict) or not isinstance(p.get("value"), (int, float)):
        return None
    v = float(p["value"])
    value = f"₹{v / 1e5:.2f}L" if v >= 1e5 else f"₹{num(v, 0)}"
    pl, pl_pct = p.get("pl"), p.get("pl_pct")
    parts = ([srupee(pl)] if isinstance(pl, (int, float)) else []) + ([f"({pct_text(pl_pct)})"] if isinstance(pl_pct, (int, float)) else [])
    return _card(value, None, _e(" ".join(parts)) or None, p.get("holdings"))   # morning: no day move before the open


def _events_line(watch: Any) -> str | None:
    if not _ok(watch) or not watch.get("events_checked"):
        return None
    ev = watch.get("events") or []
    if not ev:
        return "📅 Events: none due in the next days"
    parts = []
    for e in ev[:4]:
        try:
            d = date.fromisoformat(str(e.get("date")))
            when = d.strftime("%d %b")
        except ValueError:
            when = str(e.get("date"))
        parts.append(f"{_e(e.get('symbol'))} {_e(e.get('what'))} {when}")
    more = f" +{len(ev) - 4} more" if len(ev) > 4 else ""
    return "📅 Events: " + " · ".join(parts) + more


def _premarket_line(data: dict[str, Any]) -> str | None:
    g = data.get("gauges")
    pm = (g.get("premarket_line") if isinstance(g, dict) else None) or data.get("premarket_line")
    return f"⏰ {_e(pm)}" if isinstance(pm, str) and pm.strip() else None


def _summary_line(summary: Any) -> str | None:
    s = re.sub(r"\s+", " ", str(summary or "")).strip()
    if not s:
        return None
    if len(s) > SUMMARY_CHARS:
        cut = s[:SUMMARY_CHARS]
        end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        s = cut[:end + 1] if end > 40 else cut.rstrip() + "…"
    return f"<i>{_e(s)}</i>"


def _flag(item: dict[str, Any]) -> str:
    reasons = [str(r).lower() for r in item.get("reasons") or []]
    stop = any(r.startswith("below the stop") or "well past any stop" in r for r in reasons)
    near = not stop and any("of the stop level" in r for r in reasons)
    avg = any("200-day average" in r for r in reasons)
    parts = ["stop"] * stop + ["near"] * near + ["avg"] * avg
    if parts:
        return "+".join(parts)
    return "" if isinstance(item.get("loss_pct"), (int, float)) else "news"   # the % is already on the line


def heat_bar(pct: Any, step: float = DAY_STEP, cells: int = HEAT_CELLS) -> str:
    """A proportional bar for a % move: one filled cell per ``step`` percent, rounded up (any move shows one cell),
    at most ``cells``. -2.4 → ▰▰▰▱▱. Always exactly ``cells`` characters."""
    if not isinstance(pct, (int, float)) or math.isnan(pct):
        return "▱" * cells
    filled = min(cells, math.ceil(round(abs(pct) / step, 6)))
    return "▰" * filled + "▱" * (cells - filled)


def _arrow(pct: Any) -> str:
    return "🔻" if isinstance(pct, (int, float)) and pct < 0 else "🔺" if isinstance(pct, (int, float)) and pct > 0 else "▫️"


def _px(v: Any) -> str:
    """A price for a phone line: whole rupees with Indian grouping from 1,000, else up to one decimal (940, 264.4)."""
    if not isinstance(v, (int, float)):
        return "n/a"
    return num(v, 0) if abs(v) >= 1000 else f"{v:.1f}".removesuffix(".0")


def heat_line(code: Any, price: Any, pct: Any, step: float = DAY_STEP, d: int = 1, tag: str = "") -> str:
    """One mover: ``🔻 TATAMOTORS · 940 (−2.4%) ▰▰▰▱▱`` (escaped), plus an optional short tag."""
    line = f"{_arrow(pct)} {_e(str(code or '?')[:20])} · {_px(price)} ({pct_text(pct, d)}) {heat_bar(pct, step)}"
    return line + (f" · {_e(tag)}" if tag else "")


def _watch_block(watch: Any, rollover: bool = True) -> list[str]:
    if not _ok(watch):
        return []
    total = int(watch.get("total", len(watch.get("items") or [])) or 0)
    if not total:
        return []
    key = lambda i: i["loss_pct"] if isinstance(i.get("loss_pct"), (int, float)) else 0.0   # noqa: E731 - worst loss first
    items = sorted(watch.get("items") or [], key=key)
    top, rest = items[:TABLE_ROWS], items[TABLE_ROWS:]
    out = [f"⚠️ <b>Holdings to review ({total} flagged)</b>", f"<i>{_e(CRITERIA)}</i>"]
    for i in top:   # one line each, worst loss first; the bar is the total loss (one cell per 10%)
        out.append(heat_line(i.get("symbol"), i.get("price"), i.get("loss_pct"), LOSS_STEP, 0, _flag(i)))
    more = total - len(top)
    if more > 0:
        names: list[str] = []
        size = 0
        for i in rest if rollover else []:
            nm = _e(str(i.get("symbol") or "?"))   # codes only: the line stays short
            if size + len(nm) + 2 > ROLLOVER_CHARS:
                break
            names.append(nm)
            size += len(nm) + 2
        left = more - len(names)
        if not names:
            out.append(f"<i>+ {more} more</i>")
        else:
            out.append(f"<i>+ {more} more: {', '.join(names)}" + (f" …and {left} others" if left else "") + "</i>")
    return out


def _deals_line(deals: Any) -> str | None:
    if not _ok(deals):
        return None
    rows = deals.get("deals") or []
    total = deals.get("total", len(rows))
    if not total:
        return "🤝 <b>Deals:</b> 0 new"
    lines = []
    for d in rows[:3]:
        who = (d.get("who") or [d.get("investor")])[0] or "An investor"
        tx = str(d.get("transaction") or "").strip().lower()
        verb = "bought" if tx.startswith(("buy", "purch", "acq")) else "sold" if tx.startswith(("sell", "sold")) else (tx or "traded")
        size = f" {_e(d.get('size'))}" if d.get("size") else ""
        ex = f" ({_e(d.get('exchange'))})" if d.get("exchange") else ""
        lines.append(f"{_e(who)} {verb} {_e(label({'symbol': d.get('ticker'), 'name': d.get('name')}))}{size}{ex}")
    return f"🤝 <b>Deals:</b> {total} new\n" + "\n".join(lines)


def _signed(v: float) -> str:
    s = num(v, 0)
    return s if float(s.replace(",", "")) == 0 else ("−" if v < 0 else "+") + s


def _cheat_lines(nf: dict[str, Any]) -> list[str]:
    """Session range and gap, next-session pivots, trend strength: three short lines from the bulletin data, each only
    when its numbers are there."""
    out: list[str] = []
    n = lambda v: isinstance(v, (int, float))   # noqa: E731
    if n(nf.get("low")) and n(nf.get("high")):
        out.append(f"📊 Session: range {num(nf['low'], 0)} – {num(nf['high'], 0)}" + (f" · gap {_signed(nf['gap'])}" if n(nf.get("gap")) else ""))
    piv: dict[str, Any] = nf["pivots"] if isinstance(nf.get("pivots"), dict) else {}
    if all(n(piv.get(k)) for k in ("S1", "P", "R1")):
        out.append(f"📐 Pivots: S1 {num(piv['S1'], 0)} · P {num(piv['P'], 0)} · R1 {num(piv['R1'], 0)}")
    four: dict[str, Any] = nf["four_hour"] if isinstance(nf.get("four_hour"), dict) else {}
    bits = ([f"Daily ADX {nf['adx']:.0f}"] if n(nf.get("adx")) else []) + ([f"4h ADX {four['adx']:.0f}"] if n(four.get("adx")) else []) \
        + ([f"RSI {nf['rsi']:.0f}"] if n(nf.get("rsi")) else [])
    if bits:
        out.append("⚡ " + " · ".join(bits))
    return out


def _evening_lines(data: dict[str, Any]) -> list[str]:
    out: list[str] = []
    stale = data.get("stale_close")
    g = data.get("groww")
    if _ok(g):
        # always shows today: the day's move, or "no trading" on a stale day; then the total
        if stale:
            today = "no trading" if stale.get("reason") == "no trading today" else "not closed yet"
        elif g.get("day_pl") is not None:
            today = f"{_e(srupee(g['day_pl']))} ({_e(pct_text(g.get('day_pct')))})"
        else:
            today = "n/a"
        out.append(_card(f"₹{_e(num(g['value']))}", today, f"{_e(srupee(g.get('pl')))} ({_e(pct_text(g.get('pl_pct')))})"))
    bulletin: dict[str, Any] = data["bulletin"] if isinstance(data.get("bulletin"), dict) else {}
    nf = bulletin.get("nifty")
    if _ok(nf) and nf.get("close") is not None:
        out.append(f"📈 Nifty {_e(num(nf['close'], 2))} ({_e(pct_text(nf.get('change_pct'), 2))})")
    if _ok(nf):
        out += _cheat_lines(nf)
    if stale and not _ok(g):   # the portfolio card already says it when there is one
        out.append("<i>" + _e("No trading today" if stale.get("reason") == "no trading today" else "The market has not closed yet") + "</i>")
    movers = _movers_table(g, stale)
    if movers:
        out.append(movers)
    news = data.get("news")
    if _ok(news):
        neg = [i for i in news.get("items") or [] if i.get("sentiment") == "negative"]
        if neg:
            codes = ", ".join(dict.fromkeys(_e(i.get("symbol")) for i in neg))
            out.append(f"📰 <b>News:</b> {len(neg)} negative for your stocks ({codes}); see the email")
    return out


def _movers_table(g: Any, stale: Any) -> str | None:
    """Today's 3 biggest falls and 3 biggest gains (or, with no trading today, the 5 worst total losses), one heat line
    each: plain lines, no fixed-width table, so nothing scrolls sideways on a narrow screen."""
    if not _ok(g):
        return None
    rows = [h for h in g.get("holdings") or [] if isinstance(h.get("price"), (int, float))]
    field, title, step = ("pl_pct", "Worst total loss", LOSS_STEP) if stale else ("day_pct", "Today's movers", DAY_STEP)
    rows = [h for h in rows if isinstance(h.get(field), (int, float))]
    if not rows:
        return None
    rows.sort(key=lambda h: h[field])
    pick = rows[:5] if stale else (rows[:3] + [h for h in rows[-3:] if h[field] > 0 and h not in rows[:3]][::-1])
    return f"<b>{_e(title)}</b>\n" + "\n".join(heat_line(h.get("symbol"), h["price"], h[field], step) for h in pick)


def dashboard_url(allowed_hosts: Any) -> str | None:
    """https://<first real host name>/ from TA_ALLOWED_HOSTS; never an IP address, a port or plain http."""
    for h in allowed_hosts or []:
        host = str(h).strip().lower()
        if not host or ":" in host or "/" in host or "." not in host:
            continue
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return f"https://{host}/"
    return None


def _assemble(head: str, mid: list[str], summary: str | None, watch: list[str], tail: list[str]) -> str:
    parts = [head, "\n".join(mid), summary, "\n".join(watch), "\n".join(tail)]
    return "\n\n".join(p for p in parts if p)


def html_to_plain(text: str) -> str:
    return _html.unescape(re.sub(r"</?(?:b|i|code|pre)>", "", text))


def telegram_brief(email: dict[str, Any], allowed_hosts: Any = None, commands: bool = False) -> dict[str, Any]:
    """{"html", "plain", "button"} for a built email (morning or evening); ``html`` is at most 4,000 characters.
    ``commands``: the bot answers commands (TELEGRAM_COMMANDS), so the brief gets Portfolio / Stops / Status buttons
    that run those read commands; without the bot they would do nothing, so they are left out."""
    data = email.get("data") or {}
    kind = "evening" if data.get("kind") == "evening" else "morning"
    if kind == "morning":
        mid = [x for x in (_portfolio_line(data.get("watch")), _regime_line(data.get("mood")),
                           _action_line(data.get("mood"), data.get("buy_ideas")),
                           _macro_line(data.get("world"), data.get("gauges")), _premarket_line(data)) if x]
    else:
        mid = _evening_lines(data)
    # the rules summary only repeats the mood / action / world lines above it; keep a model-written one
    writer = str(email.get("writer") or "")
    # morning: the lines above already say it all, so the paragraph is left to the email
    summary = None if kind == "morning" or writer in ("rules", "none") else _summary_line(email.get("summary"))
    deals = _deals_line(data.get("deals"))
    events = _events_line(data.get("watch")) if kind == "morning" else None
    tail = [x for x in (events, deals) if x]
    head = _header(data, kind)
    text = _assemble(head, mid, summary, _watch_block(data.get("watch")), tail)
    if len(text) > LIMIT:   # the written summary goes first, then the rollover shrinks to a count
        text = _assemble(head, mid, None, _watch_block(data.get("watch")), tail)
    if len(text) > LIMIT:
        text = _assemble(head, mid, None, _watch_block(data.get("watch"), rollover=False), tail)
    url = dashboard_url(allowed_hosts)
    rows: list[list[dict[str, str]]] = [[{"text": "📊 Dashboard", "url": url}]] if url else []
    if commands:
        rows.append([{"text": "💼 Portfolio", "callback_data": "go:portfolio"},
                     {"text": "🛡️ Stops", "callback_data": "go:stops"},
                     {"text": "🩺 Status", "callback_data": "go:status"}])
    button = {"inline_keyboard": rows} if rows else None
    return {"html": text, "plain": html_to_plain(text), "button": button}
