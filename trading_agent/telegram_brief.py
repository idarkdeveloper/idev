"""The Telegram version of the daily emails: a phone-sized HTML message built from the email's own data (no model, no
network). ``telegram_brief(email, allowed_hosts)`` gives {"html", "plain", "button"}; the notifier sends the HTML with
parse_mode HTML and, if Telegram refuses it, the plain text once. Every value that comes from outside is escaped."""

from __future__ import annotations

import html as _html
import ipaddress
import re
from datetime import date
from typing import Any, TypeGuard

from .digest import num, pct_text
from .digest_rules import label, srupee

LIMIT = 4000
TABLE_ROWS = 5
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
    why = _short_why(mood.get("why"))
    return line + (f" · {_e(why)}" if why else "")


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
    if _ok(gauges):
        for t in gauges.get("warning_texts") or []:
            t = str(t).strip().rstrip(".")
            if t:
                bits.append(_e(t[0].upper() + t[1:]))
    return "🌍 " + " · ".join(bits) if bits else None


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
    loss = item.get("loss_pct")
    return pct_text(loss, 0, sign=False) if isinstance(loss, (int, float)) else "news"


def _compact(v: Any) -> str:
    if not isinstance(v, (int, float)):
        return "n/a"
    return f"{v:.0f}" if abs(v) >= 1000 else f"{v:.1f}"


def _line(code: str, pl: str, price: str, flag: str) -> str:
    """CODE (9) P&L (6) PRICE (6) FLAG (8), single spaces: at most 32 characters."""
    return f"{code[:9]:<9} {pl[:6]:>6} {price[:6]:>6} {flag[:8]}".rstrip()


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
    if top:
        rows = [_line("CODE", "P&L", "PRICE", "FLAG")]
        rows += [_line(str(i.get("symbol") or "?"), _compact(i.get("loss_pct")), _compact(i.get("price")), _flag(i)) for i in top]
        out.append("<pre>" + "\n".join(_e(r) for r in rows) + "</pre>")
    more = total - len(top)
    if more > 0:
        names: list[str] = []
        size = 0
        for i in rest if rollover else []:
            nm = _e(label(i))
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


def _evening_lines(data: dict[str, Any]) -> list[str]:
    out: list[str] = []
    stale = data.get("stale_close")
    g = data.get("groww")
    if _ok(g):
        s = f"💼 <b>₹{_e(num(g['value']))}</b> · total {_e(srupee(g.get('pl')))} ({_e(pct_text(g.get('pl_pct')))})"
        if not stale and g.get("day_pl") is not None:
            s += f" · today {_e(srupee(g['day_pl']))} ({_e(pct_text(g.get('day_pct')))})"
        out.append(s)
    bulletin: dict[str, Any] = data["bulletin"] if isinstance(data.get("bulletin"), dict) else {}
    nf = bulletin.get("nifty")
    if _ok(nf) and nf.get("close") is not None:
        out.append(f"📈 Nifty {_e(num(nf['close'], 2))} ({_e(pct_text(nf.get('change_pct'), 2))})")
    if stale:
        out.append("<i>" + _e("No trading today" if stale.get("reason") == "no trading today" else "The market has not closed yet") + "</i>")
    return out


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


def telegram_brief(email: dict[str, Any], allowed_hosts: Any = None) -> dict[str, Any]:
    """{"html", "plain", "button"} for a built email (morning or evening); ``html`` is at most 4,000 characters."""
    data = email.get("data") or {}
    kind = "evening" if data.get("kind") == "evening" else "morning"
    if kind == "morning":
        mid = [x for x in (_regime_line(data.get("mood")), _action_line(data.get("mood"), data.get("buy_ideas")),
                           _macro_line(data.get("world"), data.get("gauges")), _premarket_line(data)) if x]
    else:
        mid = _evening_lines(data)
    # the rules summary only repeats the mood / action / world lines above it; keep a model-written one
    writer = str(email.get("writer") or "")
    summary = None if writer in ("rules", "none") else _summary_line(email.get("summary"))
    deals = _deals_line(data.get("deals"))
    tail = [deals] if deals else []
    head = _header(data, kind)
    text = _assemble(head, mid, summary, _watch_block(data.get("watch")), tail)
    if len(text) > LIMIT:   # the written summary goes first, then the rollover shrinks to a count
        text = _assemble(head, mid, None, _watch_block(data.get("watch")), tail)
    if len(text) > LIMIT:
        text = _assemble(head, mid, None, _watch_block(data.get("watch"), rollover=False), tail)
    url = dashboard_url(allowed_hosts)
    button = {"inline_keyboard": [[{"text": "📊 Dashboard", "url": url}]]} if url else None
    return {"html": text, "plain": html_to_plain(text), "button": button}
