"""One look for every Telegram alert: the DEAL card, the NEWS card, the grouped filings and the daily briefs.

Rules (phones, a glance, read-only): the first line is the whole story in at most 70 characters, bold, with a tone marker that
always sits next to a word (▲ BOUGHT, ▼ SOLD, ● WATCH: never a colour or an emoji alone); a facts block of three or four short
lines with the numbers in <code>; one "Why" line of at most two sentences; everything visible without opening anything fits about
twelve lines and 900 characters; the rest goes into an expandable quote (Telegram renders ``<blockquote expandable>``: collapsed
by default) and is cut first when the 4,096-character limit asks for it. Buttons are links only (the dashboard's stock page, all
deals, the article): nothing that acts. Only Telegram's own HTML tags are used and every outside value is escaped. ``plain`` holds
the same message without markup and WITH the quote's text, for the notifier's fallback when Telegram refuses the HTML.
"""

from __future__ import annotations

import html as _html
import re
from typing import Any
from urllib.parse import quote, urlparse

TOTAL_LIMIT = 4096        # Telegram's limit for one text message
VISIBLE_LIMIT = 900       # what shows before anything is expanded
FIRST_LINE = 70           # the whole story, in characters of text
WHY_CHARS = 220
UP, DOWN, FLAT = "▲", "▼", "●"
_QUOTE_OPEN, _QUOTE_CLOSE = "<blockquote expandable>", "</blockquote>"


def esc(v: Any) -> str:
    return _html.escape(str(v if v is not None else ""), quote=False)


def code(v: Any) -> str:
    return f"<code>{esc(v)}</code>"


def clip(text: Any, limit: int) -> str:
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    return t if len(t) <= limit else t[:max(1, limit - 1)].rsplit(" ", 1)[0].rstrip(" ,;:-") + "…"


def sentences(text: Any, n: int = 2, limit: int = WHY_CHARS) -> str:
    """The first ``n`` sentences, at most ``limit`` characters (cut at a word, with …)."""
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9₹\"'(])", t)
    return clip(" ".join(parts[:n]), limit)


def marker(tone: str) -> str:
    return {"up": UP, "down": DOWN}.get(tone, FLAT)


def first_line(tone: str, word: str, subject: str, who: str = "") -> str:
    """'<b>▼ SOLD XPROINDIA · Kacholia</b>', at most FIRST_LINE characters of text."""
    head = f"{marker(tone)} {word.upper()} {subject}"
    if who:
        room = FIRST_LINE - len(head) - 3
        if room >= 4:
            head += f" · {clip(who, room)}"
    return f"<b>{esc(head[:FIRST_LINE])}</b>"


# ---- money, the Indian way ----
def _trim(x: float, dec: int) -> str:
    t = f"{x:.{dec}f}"
    return t.rstrip("0").rstrip(".") if "." in t else t


def rs(v: float) -> str:
    """Rs21.5 cr, Rs2.4 lakh, Rs970, Rs1,234: short, with Rs (no symbol font needed)."""
    a = abs(float(v))
    sign = "-" if v < 0 else ""
    if a >= 1e7:
        return f"{sign}Rs{_trim(a / 1e7, 1)} cr"
    if a >= 1e5:
        return f"{sign}Rs{_trim(a / 1e5, 2)} lakh"
    if a >= 1000:
        head, tail = f"{a:.0f}"[:-3], f"{a:.0f}"[-3:]
        parts: list[str] = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        return f"{sign}Rs" + ",".join(([head] if head else []) + parts + [tail])
    return f"{sign}Rs{_trim(a, 2 if a < 100 else 0)}"


def qty(q: float) -> str:
    a = abs(float(q))
    if a >= 1e7:
        return f"{_trim(a / 1e7, 2)} cr sh"
    if a >= 1e5:
        return f"{_trim(a / 1e5, 2)} lakh sh"
    return f"{a:,.0f} sh"


def day_short(at: Any) -> str:
    """'19 Oct' from an ISO date or time."""
    from datetime import date
    try:
        d = date.fromisoformat(str(at)[:10])
    except ValueError:
        return ""
    return f"{d.day} {d.strftime('%b')}"


def when_ist(at: Any) -> str:
    """'19 Oct 17:53 IST' from an ISO time (UTC or IST) or a '2026-10-19 17:53:33' string taken as IST."""
    from datetime import datetime
    from .timezones import IST
    try:
        d = datetime.fromisoformat(str(at).replace(" ", "T"))
    except ValueError:
        return ""
    d = d.astimezone(IST) if d.tzinfo else d.replace(tzinfo=IST)
    return f"{d.day} {d.strftime('%b')} {d.strftime('%H:%M')} IST"


# ---- links and buttons (read-only) ----
def valid_url(url: Any) -> str | None:
    if not isinstance(url, str) or len(url) > 2000:
        return None
    p = urlparse(url)
    return url if p.scheme in ("http", "https") and p.netloc and " " not in url else None


def dashboard_base(settings: Any) -> str | None:
    """DASHBOARD_URL if set, else https://<first host of TA_ALLOWED_HOSTS> (the brief's own rule), else None: no buttons."""
    from .telegram_brief import dashboard_url
    own = valid_url(getattr(settings, "dashboard_url", "") or "")
    if own:
        return own.rstrip("/")
    d = dashboard_url(getattr(settings, "allowed_hosts", None))
    return d.rstrip("/") if d else None


def stock_url(base: str | None, symbol: str) -> str | None:
    sym = str(symbol or "").strip().upper().split(".")[0]
    return f"{base}/?stock={quote(sym)}" if base and re.fullmatch(r"[A-Z0-9&-]{1,20}", sym) else None


def button_row(*links: tuple[str, str | None]) -> dict[str, Any] | None:
    """{"inline_keyboard": [[{"text", "url"}, ...]]}: one row of up to three link buttons; links that are not http(s) are dropped."""
    row = [{"text": clip(t, 24), "url": u} for t, u in links if t and valid_url(u)][:3]
    return {"inline_keyboard": [row]} if row else None


# ---- putting the message together ----
def _expandable(raw: str, room: int) -> tuple[str, str]:
    """(html, plain) of the quote's text cut to fit ``room`` characters of HTML; ('', '') when nothing fits."""
    raw = raw.strip()
    overhead = len(_QUOTE_OPEN) + len(_QUOTE_CLOSE) + 2
    if not raw or room <= overhead + 20:
        return "", raw
    text = raw
    while len(esc(text)) + overhead > room and len(text) > 20:
        text = clip(text[:int(len(text) * 0.9)], len(text) - 1)
    if len(esc(text)) + overhead > room:
        return "", raw
    return f"{_QUOTE_OPEN}{esc(text)}{_QUOTE_CLOSE}", raw


def assemble(visible: list[str], detail: str = "") -> tuple[str, str]:
    """(html, plain). ``visible``: ready HTML lines (blank string = an empty line); ``detail``: raw text for the expandable quote.
    The visible part is kept within VISIBLE_LIMIT (its last lines go first, never the first two); the quote is cut to fit the 4,096."""
    lines = [x for x in visible]
    while len("\n".join(lines)) > VISIBLE_LIMIT and len(lines) > 2:
        lines.pop()
    head = "\n".join(lines).strip()
    quote_html, plain_detail = _expandable(detail, TOTAL_LIMIT - len(head) - 2)
    html = head + ("\n" + quote_html if quote_html else "")
    plain_head = html_to_plain("\n".join(lines)).strip()
    plain = plain_head + ("\n\n" + plain_detail.strip() if plain_detail.strip() else "")
    return html[:TOTAL_LIMIT], plain[:TOTAL_LIMIT]


def html_to_plain(text: str) -> str:
    t = re.sub(r"<a [^>]*>|</a>", "", text)
    t = re.sub(r"</?(?:b|i|u|s|code|pre|blockquote|tg-spoiler)(?: [^>]*)?>", "", t)
    return _html.unescape(t)


# ---- the DEAL card ----
def deal_card(rec: dict[str, Any], *, deal: dict[str, Any] | None = None, holding_qty: float | None = None, holding_known: bool = True,
              market_wait: bool = False, regime: str | None = None, base_url: str | None = None) -> dict[str, Any]:
    """{"html", "plain", "button"} for a [DEAL] recommendation: who did what, the figures, the agent's call, one Why line, the
    full reasoning collapsed. Figures come from the deal rows (``deal``: shares, price, value, date, side), never the model's text."""
    sym = str(rec.get("ticker") or "?").upper()
    action = str(rec.get("action") or "").lower()
    d = deal or {}
    side = str(d.get("side") or "").lower()
    if side.startswith(("purchase", "buy")):
        tone, word = "up", "bought"
    elif side.startswith(("sale", "sell")):
        tone, word = "down", "sold"
    else:
        tone, word = {"buy": ("up", "buy"), "sell": ("down", "sell")}.get(action, ("flat", action or "note"))
    who = str(rec.get("investor") or "").strip()
    who_short = who.split()[-1].title() if len(who.split()) > 1 else who.title()
    lines = [first_line(tone, word, sym, who_short), ""]
    figs = []
    if d.get("value"):
        figs.append(code(rs(d["value"])))
    if d.get("shares"):
        figs.append(code(qty(d["shares"]) + (f" @ {rs(d['price'])}" if d.get("price") else "")))
    if figs:
        lines.append(" · ".join(figs))
    if d.get("date"):
        lines.append(f"deal disclosed · {esc(day_short(d['date']) or d['date'])}")
    conf = str(rec.get("confidence") or "").lower()
    agent = f"Agent: {code(action.upper() + (f' ({conf})' if conf else ''))}"
    hold = ("You hold: none" if holding_qty in (None, 0) and holding_known else f"You hold: {code(f'{holding_qty:g} sh')}" if holding_qty else "")
    lines.append(" · ".join(x for x in (hold, agent) if x))
    if market_wait and action in ("buy", "watch", "hold"):
        lines.append("Market: " + esc("risk-off" if regime == "risk_off" else "weak") + " · no new buys")
    why = sentences(rec.get("rationale") or rec.get("headline"), 2)
    if why:
        lines += ["", f"<b>Why</b> {esc(why)}"]
    size = rec.get("suggested_notional_usd")
    nl = chr(10)
    detail = nl.join([clip(rec.get("headline"), 300), "", str(rec.get("rationale") or "").strip()]
                     + ([nl + f"Investor: {who}"] if who else []) + ([f"Suggested size: {rs(float(size))}"] if size and rec.get("currency", "INR") == "INR" else []))
    html, plain = assemble(lines, detail)
    button = button_row((f"Open {sym}", stock_url(base_url, sym)), ("All deals", f"{base_url}/#deals" if base_url else None))
    return {"html": html, "plain": plain, "button": button}


# ---- the NEWS card ----
_IMPACT_TONE = {"NEGATIVE": "down", "POSITIVE": "up"}


def news_card(card: dict[str, Any], base_url: str | None = None) -> dict[str, Any]:
    """{"html", "plain", "button"} for one headline alert: marker, symbol and impact in words; the headline; source and time in IST;
    two lines of summary; "For you" only when the card has a portfolio context; the rest of the story collapsed."""
    sym = str(card.get("symbol") or "?")
    impact = str(card.get("impact") or card.get("sentiment") or "NEUTRAL").upper()
    tone = _IMPACT_TONE.get(impact, "flat")
    lines = [f"<b>{marker(tone)} NEWS · {esc(sym)} · {esc(impact.title())}</b>", f"<b>{esc(clip(card.get('headline'), 120))}</b>"]
    when = when_ist(card.get("published"))
    src = " · ".join(x for x in (clip(card.get("source"), 40), when) if x)
    if src:
        lines.append(f"<i>{esc(src)}</i>")
    takeaways = [clip(t, 110) for t in (card.get("takeaways") or []) if str(t).strip()]
    story = str(card.get("story") or "").strip()
    summary = takeaways[:2] or ([sentences(story.split(chr(10) * 2)[0], 2, 200)] if story else [])
    if summary:
        lines.append("")
        lines += [f"• {esc(t)}" for t in summary]
    if card.get("context"):
        lines += ["", f"<b>For you</b> {esc(clip(card['context'], 160))}"]
    nl = chr(10)
    extra = [f"• {t}" for t in takeaways[2:]]
    detail = nl.join(([nl.join(extra)] if extra else []) + ([story] if story else []))
    html, plain = assemble(lines, detail)
    button = button_row((f"Open {sym}", stock_url(base_url, sym)), (f"Read on {card.get('source')}" if card.get("source") else "Read article", valid_url(card.get("url"))))
    return {"html": html, "plain": plain, "button": button}


# ---- grouped exchange filings ----
_RANK = ((re.compile(r"result|financial results|earnings", re.I), 0), (re.compile(r"board meeting|outcome of board", re.I), 1),
         (re.compile(r"dividend|bonus|split|buy ?back", re.I), 2), (re.compile(r"\b(?<!sebi )(?<!court )orders?\b|contract|award|bagg", re.I), 3), (re.compile(r"pledge|encumbrance", re.I), 4),
         (re.compile(r"penalt|\bfine[sd]?\b|show cause|disciplinary", re.I), 5), (re.compile(r"\bsebi\b", re.I), 6),
         (re.compile(r"appointment|resignation|cessation|director|key managerial|auditor", re.I), 7))
_LOW = re.compile(r"newspaper|publication|trading window|copy of", re.I)
MAX_DETAIL_ROWS = 25


def filing_rank(category: Any, text: Any = "") -> int:
    """0 (results) .. 7 (appointments) matter first, 8 is anything else, 9 is newspaper publications and trading-window notices."""
    c = f"{category} {text}"
    if _LOW.search(str(category)):
        return 9
    for rx, n in _RANK:
        if rx.search(c):
            return n
    return 8


def _span(rows: list[dict[str, Any]]) -> str:
    from datetime import date
    days = []
    for r in rows:
        try:
            days.append(date.fromisoformat(str(r.get("at") or "")[:10]))
        except ValueError:
            continue
    if not days:
        return ""
    a, b = min(days), max(days)
    if a == b:
        return f"{b.day} {b.strftime('%b')}"
    return f"{a.day}-{b.day} {b.strftime('%b')}" if (a.month, a.year) == (b.month, b.year) else f"{a.day} {a.strftime('%b')}-{b.day} {b.strftime('%b')}"


def filings_card(rows: list[dict[str, Any]], base_url: str | None = None) -> dict[str, Any]:
    """{"html", "plain", "button"} for the new exchange announcements of one poll: the three that matter most, one line each; the rest
    (at most 25 lines) in the collapsed quote. Newspaper publications, trading-window notices and repeats rank last."""
    from .alert_cards import short_text
    rows = [r for r in rows if isinstance(r, dict)]
    syms = list(dict.fromkeys(str(r.get("symbol") or "?") for r in rows))
    n = len(rows)
    span = _span(rows)
    who = syms[0] if len(syms) == 1 else ", ".join(syms[:3]) + (f" +{len(syms) - 3}" if len(syms) > 3 else "")
    head = f"<b>{FLAT} {esc(who)} · {n} new filing{'s' if n != 1 else ''}{esc(f' ({span})') if span else ''}</b>"
    seen: set[tuple[str, str]] = set()
    ranked = []
    for r in sorted(rows, key=lambda r: str(r.get("at") or ""), reverse=True):   # newest first within a rank
        cat = str(r.get("category") or "Announcement")
        gist = short_text(r.get("text") or "", 60) if r.get("text") else ""
        key = (str(r.get("symbol")), (cat + gist).lower())
        rank = 9 if key in seen else filing_rank(cat, r.get("text"))
        seen.add(key)
        ranked.append((rank, r, cat, gist))
    ranked.sort(key=lambda x: x[0])   # stable: the newest stays first inside each rank

    def line(r: dict[str, Any], cat: str, gist: str, with_sym: bool) -> str:
        t = when_ist(r.get("at")).replace(" IST", "")
        return " · ".join(x for x in ([str(r.get("symbol") or "?")] if with_sym else []) + [t, clip(cat, 40)] + ([clip(gist, 60)] if gist and gist.lower() != cat.lower() else []) if x)

    multi = len(syms) > 1
    top = ranked[:3]
    lines = [head, ""] + [esc(line(r, c, g, multi)) for _, r, c, g in top]
    if n > len(top):
        lines.append(f"<i>+{n - len(top)} more</i>")
    nl = chr(10)
    rest = ranked[3:3 + MAX_DETAIL_ROWS]
    detail = nl.join(line(r, c, g, multi) for _, r, c, g in rest) + (f"{nl}…and {len(ranked) - 3 - len(rest)} more" if len(ranked) - 3 > len(rest) else "")
    html, plain = assemble(lines, detail)
    button = button_row((f"Open {syms[0]}" if len(syms) == 1 else "Open dashboard", stock_url(base_url, syms[0]) if len(syms) == 1 else base_url))
    return {"html": html, "plain": plain, "button": button}
