"""HTML cards for the alert emails, in the dark style of the news card (news_story.render_html):

* ``recommendation_card``: a [DEAL] / [WATCH ...] recommendation. Badges (action, confidence, Nifty trend), metric
  tiles for the deal (shares, average price, value, current price and how far it has moved since the deal), the
  model's rationale, and a market-filter callout when the rules hold off new buys. Every figure in the tiles and the
  callout is computed from the deal rows, the price and the regime, never read out of the model's prose.
* ``announcements_card``: new NSE announcements as a timeline: a coloured tag per company, the category as the title,
  the text without its "X Limited has informed the Exchange about" boilerplate, and a dot whose colour says how much
  the kind of filing usually matters.

The plain-text body of each email is unchanged (it is also what webhooks and Telegram get); the card is the HTML part.
Every value from outside is escaped.
"""

from __future__ import annotations

import hashlib
import html as _html
import re
from datetime import datetime
from typing import Any, Iterable

from .timezones import IST

BG, PANEL, BORDER, FG, TEXT, MUTED = "#111827", "#1f2937", "#1f2937", "#ffffff", "#e5e7eb", "#9ca3af"
GREEN, AMBER, RED, BLUE, GRAY = "#22c55e", "#f59e0b", "#ef4444", "#3b82f6", "#9ca3af"
FONT = "-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif"
TAGS = ("#22c55e", "#3b82f6", "#f59e0b", "#a855f7", "#14b8a6", "#ec4899", "#eab308", "#6366f1")
ACTION = {"buy": ("BUY", GREEN), "sell": ("SELL", RED), "hold": ("HOLD", GRAY), "watch": ("WATCH ONLY", AMBER)}
CONFIDENCE = {"high": GREEN, "medium": AMBER, "low": RED}
# how much a kind of filing usually matters: high (red), notable (amber), routine (gray)
HIGH = re.compile(r"result|acqui|amalgam|merger|demerger|scheme of arrangement|takeover|sale or disposal|disposal|"
                  r"pledge|credit rating|default|penalt|show cause|sebi order|insolvency|fraud|resign|auditor|buy ?back|"
                  r"delist", re.I)
NOTABLE = re.compile(r"trading window|board meeting|outcome|dividend|bonus|split|allotment|press release|updates?$|"
                     r"order|contract|investor meet|analyst|restructur|subsidiar|production|business update|"
                     r"fund raising|qip|preferential", re.I)
BOILERPLATE = re.compile(r"^.*?\bhas\s+(?:informed|intimated|submitted(?:\s+to)?)\s+the\s+exchange(?:\s+(?:a\s+)?copy(?:\s+of)?)?"
                         r"(?:\s+(?:about|regarding|that|on))?\s*", re.I)


def _e(v: Any) -> str:
    return _html.escape(str(v if v is not None else ""), quote=True)


def _group(n: float) -> str:
    """Indian digit grouping: 1234567 -> 12,34,567."""
    whole = f"{abs(n):.0f}"
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts: list[str] = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        whole = ",".join(([head] if head else []) + parts + [tail])
    return ("-" if n < 0 else "") + whole


def money(v: float, currency: str = "INR") -> str:
    if currency != "INR":
        return f"${v:,.2f}" if abs(v) < 1000 else f"${v:,.0f}"
    return f"₹{_group(v)}" if abs(v) >= 100 else f"₹{v:,.2f}"


def big_money(v: float, currency: str = "INR") -> str:
    """₹235 Cr, ₹4.5 L, $1.2M: the tile's short form."""
    if currency != "INR":
        return f"${v / 1e9:.1f}B" if v >= 1e9 else f"${v / 1e6:.1f}M" if v >= 1e6 else money(v, currency)
    return f"₹{v / 1e7:,.0f} Cr" if v >= 1e9 else f"₹{v / 1e7:.1f} Cr" if v >= 1e7 else \
        f"₹{v / 1e5:.1f} L" if v >= 1e5 else money(v)


def shares(q: float) -> str:
    return f"{q / 1e6:.2f}M" if q >= 1e6 else f"{q / 1e3:.1f}K" if q >= 1e4 else _group(q)


# --------------------------------------------------------------------------- #
# facts (computed, never from the model's text)
# --------------------------------------------------------------------------- #
def deal_facts(trades: Iterable[Any], ticker: str, investor: str | None) -> dict[str, Any] | None:
    """The deal(s) behind a recommendation: {shares, price, value, entities, date, side}, from the bulk / block rows
    of this run for ``ticker`` (and the followed ``investor`` when named). None when no row has a quantity."""
    from .deal_events import _qty_price
    from .quiver import followed_names
    base = ticker.upper().split(".")[0]
    rows = [t for t in trades if str(getattr(t, "ticker", "")).upper().split(".")[0] == base
            and (not investor or followed_names(t.investor, [investor]))]
    qty = value = 0.0
    priced = True
    names: set[str] = set()
    for t in rows:
        q, p = _qty_price(t)
        if q is None:
            continue
        qty += q
        names.add(str(t.investor).strip().upper())
        if p is None:
            priced = False
        else:
            value += q * p
    if qty <= 0:
        return None
    sides = {str(t.transaction) for t in rows}
    return {"shares": qty, "price": round(value / qty, 2) if priced else None, "value": value if priced else None,
            "entities": len(names), "date": max(str(t.transaction_date) for t in rows),
            "side": sides.pop() if len(sides) == 1 else "Trade"}


def market_facts(regime: Any) -> dict[str, Any]:
    """{regime, nifty_up (True/False/None), nifty_20d, wait} from GlobalContext.fetch()."""
    r = regime if isinstance(regime, dict) and not regime.get("error") else {}
    nifty = ((r.get("markets") or {}).get("nifty50") or {}) if r else {}
    up = nifty.get("above_200dma")
    return {"regime": r.get("regime"), "nifty_up": up if isinstance(up, bool) else None,
            "nifty_20d": nifty.get("ret_20d") if isinstance(nifty.get("ret_20d"), (int, float)) else None,
            "wait": r.get("regime") == "risk_off" or up is False}


# --------------------------------------------------------------------------- #
# the recommendation card
# --------------------------------------------------------------------------- #
def _badge(text: str, colour: str) -> str:
    return (f'<span style="display:inline-block;font-size:11px;font-weight:700;letter-spacing:0.5px;color:{colour};'
            f'border:1px solid {colour};border-radius:4px;padding:2px 6px;margin:0 6px 6px 0;">{_e(text)}</span>')


def _tile(value: str, label: str, note: str = "", colour: str = FG, note_colour: str = MUTED) -> str:
    return (f'<td valign="top" style="background:{PANEL};border-radius:6px;padding:10px 8px;text-align:center;">'
            f'<div style="font-size:18px;font-weight:700;color:{colour};line-height:1.2;">{_e(value)}</div>'
            f'<div style="font-size:10px;font-weight:700;letter-spacing:0.6px;color:{MUTED};margin-top:3px;">{_e(label)}</div>'
            + (f'<div style="font-size:10px;color:{note_colour};margin-top:2px;">{_e(note)}</div>' if note else "")
            + "</td>")


def _tiles(tiles: list[str]) -> str:
    """Two tiles a row: four in one row are too narrow on a phone."""
    if not tiles:
        return ""
    rows = []
    for k in range(0, len(tiles), 2):
        pair = tiles[k:k + 2] + ([] if len(tiles[k:k + 2]) == 2 else ['<td style="width:50%"></td>'])
        rows.append(f"<tr>{pair[0]}<td style=\"width:8px\"></td>{pair[1]}</tr><tr><td style=\"height:8px\"></td></tr>")
    return ('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:14px 0 4px 0;'
            'table-layout:fixed;">' + "".join(rows) + "</table>")


def _day(iso: str) -> str:
    try:
        d = datetime.fromisoformat(str(iso)[:10])
    except ValueError:
        return str(iso)[:10]
    return f"{d.day} {d:%b}"


def recommendation_card(rec: dict[str, Any], *, deal: dict[str, Any] | None = None, market: dict[str, Any] | None = None,
                        price: float | None = None, holding: str | None = None, currency: str = "INR") -> str:
    action = str(rec.get("action") or "").lower()
    label, colour = ACTION.get(action, (action.upper() or "NOTE", GRAY))
    m = market or {}
    if action == "watch" and m.get("wait"):
        label = "WATCH ONLY (NO NEW BUYS)"
    conf = str(rec.get("confidence") or "").lower()
    badges = [_badge(f"⛔ {label}" if "NO NEW BUYS" in label else label, colour)]
    if conf in CONFIDENCE:
        badges.append(_badge(f"CONFIDENCE: {conf.upper()}", CONFIDENCE[conf]))
    if str(rec.get("ticker") or "").upper().endswith(".BO"):
        badges.append(_badge("BSE ONLY · NOT TRADABLE HERE", GRAY))
    if m.get("nifty_up") is not None:
        badges.append(_badge(f"NIFTY TREND: {'UP' if m['nifty_up'] else 'DOWN'}", GREEN if m["nifty_up"] else RED))
    tiles = []
    d = deal or {}
    if d.get("shares"):
        tiles.append(_tile(shares(d["shares"]), "SHARES", f"across {d['entities']} entities" if d.get("entities", 0) > 1
                           else str(d.get("side") or "")))
    if d.get("price"):
        tiles.append(_tile(money(d["price"], currency), "DEAL PRICE", f"on {_day(d['date'])}" if d.get("date") else ""))
    if d.get("value"):
        tiles.append(_tile(big_money(d["value"], currency), "DEAL VALUE"))
    move = price / d["price"] - 1 if price and d.get("price") else None
    if price:
        tiles.append(_tile(money(price, currency), "CURRENT PRICE",
                           f"{move * 100:+.1f}% vs deal" if move is not None else "",
                           colour=(GREEN if move is not None and move < 0 else RED if move is not None and move > 0.05 else FG),
                           note_colour=MUTED))
    callout = ""
    if m.get("wait") and action in ("buy", "watch", "hold"):
        why = []
        if m.get("regime") == "risk_off":
            why.append("the market regime is risk-off")
        if m.get("nifty_up") is False:
            why.append("the Nifty is below its 200-day average"
                       + (f" ({m['nifty_20d'] * 100:+.1f}% over 20 days)" if m.get("nifty_20d") is not None else ""))
        extra = (f" The price is {abs(move) * 100:.0f}% {'above' if move > 0 else 'below'} the deal price of "
                 f"{money(d['price'], currency)}." if move is not None and abs(move) >= 0.03 else "")
        reason = " and ".join(why)
        callout = (f'<div style="background:#3b1d1d;border:1px solid {RED};border-radius:6px;padding:10px 12px;'
                   f'margin:14px 0;font-size:13px;line-height:1.5;color:{TEXT};">⚠️ <b>Market filter:</b> '
                   f'{_e(reason[:1].upper() + reason[1:])}, so the rules make no new buys.{_e(extra)}</div>')
    size = rec.get("suggested_notional_usd") or 0
    size_line = (f'<div style="font-size:12px;color:{MUTED};margin-top:6px;">Suggested size: {_e(money(float(size), currency))}</div>'
                 if size else "")
    foot = " · ".join(x for x in (holding, f"Investor: {rec.get('investor')}" if rec.get("investor") else None) if x)
    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{BG};'
        f'border:1px solid {BORDER};border-radius:10px;font-family:{FONT};margin-bottom:14px;"><tr><td style="padding:16px;">'
        f'<div style="font-size:11px;font-weight:700;letter-spacing:0.8px;color:{MUTED};">TRADING AGENT · DEAL ALERT</div>'
        f'<div style="font-size:20px;font-weight:700;color:{FG};margin:8px 0 8px 0;">{_e(rec.get("ticker"))} · '
        f'{_e(ACTION.get(action, (action.upper(), GRAY))[0].split(" ")[0])}</div>'
        f'<div>{"".join(badges)}</div>'
        f'{_tiles(tiles)}'
        f'<div style="font-size:15px;font-weight:700;color:{FG};line-height:1.35;margin-top:10px;">{_e(rec.get("headline"))}</div>'
        f'<div style="font-size:13px;line-height:1.55;color:#cbd5e1;margin-top:6px;">{_e(rec.get("rationale"))}</div>'
        f'{size_line}{callout}'
        + (f'<div style="border-top:1px solid {BORDER};padding-top:10px;margin-top:12px;font-size:12px;color:{MUTED};">'
           f'{_e(foot)}</div>' if foot else "")
        + '<div style="font-size:11px;color:#6b7280;margin-top:10px;">Figures are from exchange data and prices; the '
          'summary is the model\'s. Not advice: check before acting.</div>'
        "</td></tr></table>")


# --------------------------------------------------------------------------- #
# the announcements timeline
# --------------------------------------------------------------------------- #
def _tag_colour(symbol: str) -> str:
    return TAGS[int(hashlib.sha1(symbol.encode()).hexdigest(), 16) % len(TAGS)]


def priority(category: str, text: str = "") -> str:
    """high / notable / routine, from the category (and the text when the category is vague)."""
    c = f"{category} {text}" if str(category).strip().lower() in ("updates", "general updates", "") else str(category)
    return "high" if HIGH.search(c) else "notable" if NOTABLE.search(c) else "routine"


def short_text(text: str, limit: int = 220) -> str:
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    t = (BOILERPLATE.sub("", t) or t).strip(" '\"‘’“”")
    t = re.sub(r"^(.*?)['\"‘’“”]+(\.?)$", r"\1\2", t)   # a quote left over from "regarding ' Title'."
    t = t[:1].upper() + t[1:]
    return t if len(t) <= limit else t[:limit].rsplit(" ", 1)[0] + "…"


def _when(at: str) -> str:
    try:
        d = datetime.fromisoformat(str(at).replace(" ", "T"))
    except ValueError:
        return str(at)[:16]
    if d.tzinfo:
        d = d.astimezone(IST)
    return f"{d.day:02d} {d:%b} {d:%H:%M}"


def _body(category: str, text: str) -> str:
    """'<b>Category:</b> text', or just the category when the text only repeats it."""
    t = short_text(text)
    norm = lambda v: re.sub(r"[^a-z0-9]", "", v.lower())   # noqa: E731
    if not t or norm(t).startswith(norm(category)) and len(norm(t)) - len(norm(category)) < 12:
        return f"<b>{_e(category)}</b>"
    return f"<b>{_e(category)}:</b> {_e(t)}"


def tag_name(a: dict[str, Any]) -> str:
    """The company tag: the NSE code, or for a BSE-only stock (a bare scrip code) its name and BSE."""
    sym = str(a.get("symbol") or "?")
    company = re.sub(r"\s+(Limited|Ltd\.?)$", "", str(a.get("company") or "").strip(), flags=re.I)
    return f"{company[:32]} · BSE" if sym.upper().endswith(".BO") and company else sym


def announcements_subject(rows: list[dict[str, Any]]) -> str:
    syms = list(dict.fromkeys(tag_name(a) for a in rows))
    head = ", ".join(syms[:4]) + (f" +{len(syms) - 4}" if len(syms) > 4 else "")
    return f"[NEWS] {len(rows)} new announcement{'s' if len(rows) != 1 else ''}: {head}"


def announcements_card(rows: list[dict[str, Any]]) -> str:
    dot = {"high": RED, "notable": AMBER, "routine": GRAY}
    items = []
    for a in sorted(rows, key=lambda a: str(a.get("at") or ""), reverse=True):
        sym = str(a.get("symbol") or "?")
        c = _tag_colour(sym)
        pr = priority(str(a.get("category") or ""), str(a.get("text") or ""))
        items.append(
            '<tr><td valign="top" style="width:18px;padding-top:4px;">'
            f'<div style="width:10px;height:10px;border-radius:5px;background:{dot[pr]};"></div></td>'
            f'<td style="padding:0 0 14px 6px;border-left:1px solid {BORDER};">'
            f'<span style="display:inline-block;font-size:11px;font-weight:700;color:{c};border:1px solid {c};'
            f'border-radius:4px;padding:1px 6px;">{_e(tag_name(a))}</span>'
            f'<span style="font-size:11px;color:{MUTED};"> · {_e(_when(str(a.get("at") or "")))}</span>'
            f'<div style="font-size:13px;color:{TEXT};line-height:1.45;margin-top:4px;">'
            f'{_body(str(a.get("category") or "Announcement"), str(a.get("text") or ""))}</div></td></tr>')
    legend = " · ".join(f'<span style="color:{dot[k]};">●</span> {k}' for k in ("high", "notable", "routine"))
    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{BG};'
        f'border:1px solid {BORDER};border-radius:10px;font-family:{FONT};"><tr><td style="padding:16px;">'
        f'<div style="font-size:11px;font-weight:700;letter-spacing:0.8px;color:{MUTED};">TRADING AGENT · EXCHANGE FILINGS</div>'
        f'<div style="font-size:17px;font-weight:700;color:{FG};margin:8px 0 12px 0;">🗓️ {len(rows)} new '
        f'announcement{"s" if len(rows) != 1 else ""}</div>'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{"".join(items)}</table>'
        f'<div style="font-size:11px;color:{MUTED};margin-top:4px;">{legend} (by the kind of filing)</div>'
        "</td></tr></table>")
