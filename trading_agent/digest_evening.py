"""The evening email's HTML: a close report that leads with how your portfolio did today.

Layout (top to bottom): the date and a one-line headline, three figures (today, total P&L, Nifty 50), the "In short"
summary when a model wrote it, your Groww portfolio best to worst with a bar for each day's move and the practice
account in one strip, news for your stocks (negative first) and one line on today's deals, the market bulletin (the
two Nifty charts, levels and momentum in a 2×2 grid, global markets and commodities in one table), and the concept of
the day. A part without data is left out. The plain-text part is still ``digest_render.to_text`` from the same data.

Email clients ignore <style> blocks and classes, so everything is inline-styled tables; the charts stay inline
``cid:`` pictures (``digest_render.inline_data_urls`` turns them into data: URLs for the preview). Every value is escaped.
"""

from __future__ import annotations

import re
from typing import Any

from .bulletin import GAP_FLAT_PCT, rsi_zone, sgn
from .digest import inr, num, num_intl, pct_text
from .digest_morning import (ACCENT, BAD, BODY, CAUTION_INK, EYEBROW, FONT, GOOD, INK, LINE, MUTED, SHORT_BG, SOFT, TABLE,
                             _badge, _day, _note, _section, _short, _tone, frame, stamp, stat, tiles, top)
from .digest_render import _e, _evening_subtitle
from .digest_rules import short_name

STRIP_BG = "#f5f5f8"
FOOTNOTE = "A reading of past prices by rules, never a forecast. Watch levels are earlier turning points, not targets."
BSE_NOTE = "BSE deals arrive after 16:00 and reach tomorrow's morning brief."


def _part(d: dict[str, Any], key: str) -> dict[str, Any] | None:
    """A section of the data when it is there and available, else None."""
    v = d.get(key)
    return v if isinstance(v, dict) and "unavailable" not in v else None


def _nifty(d: dict[str, Any]) -> dict[str, Any] | None:
    b = d.get("bulletin")
    nf = b.get("nifty") if isinstance(b, dict) else None
    return nf if isinstance(nf, dict) and "unavailable" not in nf and isinstance(nf.get("close"), (int, float)) else None


def _qty(q: Any) -> str:
    try:
        n = float(q)
    except (TypeError, ValueError):
        return str(q)
    return f"{n:g} share" + ("" if n == 1 else "s")


# -- top: headline and three figures ----------------------------------------------------------------
def headline(d: dict[str, Any]) -> str:
    """'Down ₹2,140 today while the Nifty rose 0.62%.' / 'No trading today — figures from the Fri 9 Oct close.'"""
    stale = d.get("stale_close")
    if isinstance(stale, dict):
        lead = "No trading today" if stale.get("reason") == "no trading today" else "The market has not closed yet"
        return f"{lead} — figures from the {stale.get('label')} close."
    g, nf = _part(d, "groww"), _nifty(d)
    nifty = None
    if nf and isinstance(nf.get("change_pct"), (int, float)):
        c = nf["change_pct"]
        nifty = ("was flat" if pct_text(c, 2, sign=False) == "0.00%" else
                 f"{'rose' if c > 0 else 'fell'} {pct_text(abs(c), 2, sign=False)}")
    day = g.get("day_pl") if g else None
    if isinstance(day, (int, float)):
        amount = inr(abs(day))
        mine = "Flat today" if amount == "₹0" else f"{'Up' if day > 0 else 'Down'} {amount} today"
        if nifty is None:
            return mine + "."
        if nifty == "was flat":
            return f"{mine}; the Nifty was flat."
        same = mine != "Flat today" and (day > 0) == (nf["change_pct"] > 0)   # type: ignore[index]
        return f"{mine} {'and' if same else 'while'} the Nifty {nifty}."
    if nifty:
        return f"The Nifty {nifty} today."
    return "Today's close report."


def _tiles(d: dict[str, Any]) -> str:
    g, p, nf = _part(d, "groww"), _part(d, "practice"), _nifty(d)
    stale = d.get("stale_close") if isinstance(d.get("stale_close"), dict) else None
    cells: list[tuple[str, str, str, str, str, bool]] = []   # label, big, small, big colour, small colour, bold small
    if g:
        if stale:
            cells.append(("Value", inr(g.get("value")), f"at the {stale.get('label')} close", INK, BODY, False))
        elif isinstance(g.get("day_pl"), (int, float)):
            today = inr(g["day_pl"], 0, True)
            cells.append(("Today", today, f"{pct_text(g.get('day_pct'), 2)} vs previous close", _tone(today), _tone(today), False))
        else:
            cells.append(("Today", "n/a", "no previous close", MUTED, BODY, False))
        if isinstance(g.get("pl"), (int, float)):
            total = inr(g["pl"], 0, True)
            tail = f"invested {inr(g.get('invested'))}" if stale else f"value {inr(g.get('value'))}"
            cells.append(("Total P&L", total, f"{pct_text(g.get('pl_pct'), 2)} · {tail}", _tone(total), BODY, False))
    elif p:   # no Groww figures: the practice account's
        if not stale and isinstance(p.get("day_change"), (int, float)):
            today = inr(p["day_change"], 0, True)
            cells.append(("Practice today", today, f"{pct_text(p.get('day_change_pct'), 2)} vs previous close",
                          _tone(today), _tone(today), False))
        if isinstance(p.get("total_pl"), (int, float)):
            total = inr(p["total_pl"], 0, True)
            cells.append(("Practice total", total, f"{pct_text(p.get('total_pl_pct'), 2)} · equity {inr(p.get('equity'))}",
                          _tone(total), BODY, False))
    if nf:
        move = f"{sgn(nf.get('change') or 0.0)} ({pct_text(nf.get('change_pct'), 2)})"
        cells.append(("Nifty 50", num(nf["close"], 2), move, INK, _tone(move), True))
    if not cells:
        return ""
    widths = {1: ["100%"], 2: ["50%", "50%"], 3: ["34%", "33%", "33%"]}[min(len(cells), 3)]
    cells = cells[:3]
    return tiles([stat(lab, big, small, bc, sc, bold, last=i == len(cells) - 1, width=widths[i])
                  for i, (lab, big, small, bc, sc, bold) in enumerate(cells)])


# -- your Groww portfolio and the practice account ---------------------------------------------------
def _holdings(g: dict[str, Any], stale: bool) -> str:
    rows = list(g.get("holdings") or [])
    if not rows:
        return _note("No priced holdings.", BODY, "13px")
    key = "pl_pct" if stale else "day_pct"
    rows.sort(key=lambda h: (not isinstance(h.get(key), (int, float)), -(h.get(key) or 0.0)))
    biggest = max((abs(h["day_pct"]) for h in rows if isinstance(h.get("day_pct"), (int, float))), default=0.0)
    th = f"{EYEBROW}letter-spacing:.6px;color:{MUTED};font-weight:normal;border-bottom:1px solid {LINE};{FONT}"
    out = [f'<table {TABLE} style="margin-top:10px;border-collapse:collapse"><tr><th align="left" style="{th}padding:6px 0">Stock</th>'
           + ("" if stale else f'<th align="left" style="{th}padding:6px 8px;width:120px">Today</th>')
           + f'<th align="right" style="{th}padding:6px 0 6px 8px">Total</th></tr>']
    for n, h in enumerate(rows):
        edge = f"border-bottom:1px solid {SOFT};" if n < len(rows) - 1 else ""
        td = f"vertical-align:top;{edge}{FONT}"
        name = short_name(h.get("name"))
        sub = " · ".join(x for x in ((name if name and name.upper() != str(h.get("symbol")).upper() else ""),
                                     inr(h.get("price"), 2), _qty(h.get("qty"))) if x)
        cells = [f'<td style="padding:9px 0;{td}"><div style="font-size:14px;font-weight:bold;color:{INK}">{_e(h.get("symbol"))}</div>'
                 f'<div style="font-size:12px;color:{MUTED}">{_e(sub)}</div></td>']
        if not stale:
            dp = h.get("day_pct")
            if isinstance(dp, (int, float)):
                txt = pct_text(dp, 2)
                width = round(abs(dp) / biggest * 100) if biggest else 0
                bar = (f'<div style="height:4px;width:{max(width, 3)}%;background:{_tone(txt) if _tone(txt) != INK else MUTED};'
                       f'border-radius:2px;margin-top:3px;font-size:0;line-height:0">&nbsp;</div>' if width else "")
                cells.append(f'<td style="padding:9px 8px;{td}"><div style="font-size:13px;font-weight:bold;color:{_tone(txt)}">'
                             f'{_e(txt)}</div>{bar}</td>')
            else:
                cells.append(f'<td style="padding:9px 8px;{td}"><div style="font-size:13px;color:{MUTED}">n/a</div></td>')
        pl = inr(h["pl"], 0, True) if isinstance(h.get("pl"), (int, float)) else "n/a"
        cells.append(f'<td align="right" style="padding:9px 0 9px 8px;{td}white-space:nowrap">'
                     f'<div style="font-size:13px;color:{_tone(pl) if pl != "n/a" else MUTED};font-weight:bold">{_e(pl)}</div>'
                     f'<div style="font-size:12px;color:{MUTED}">{_e(pct_text(h.get("pl_pct")))}</div></td>')
        out.append("<tr>" + "".join(cells) + "</tr>")
    out.append("</table>")
    return "".join(out)


def _practice(p: dict[str, Any], stale: bool) -> str:
    if p.get("same_as_groww"):
        left = (f'<b>Practice account</b> <span style="color:{MUTED}">· a copy of the same {_e(p["same_as_groww"])} stocks · '
                f'equity {_e(inr(p.get("equity")))}</span>')
    else:
        left = (f'<b>Practice account</b> <span style="color:{MUTED}">· equity {_e(inr(p.get("equity")))} · '
                f'cash {_e(inr(p.get("cash")))}</span>')
    right: list[str] = []
    if not stale and isinstance(p.get("day_change"), (int, float)):
        t = inr(p["day_change"], 0, True)
        right.append(f'<b style="color:{_tone(t)};white-space:nowrap">{_e(t)} today</b>')
    elif not stale and isinstance(p.get("since_change"), (int, float)) and p.get("since"):
        t = inr(p["since_change"], 0, True)
        right.append(f'<b style="color:{_tone(t)};white-space:nowrap">{_e(t)} since {_e(_day(p["since"]))}</b>')
    if isinstance(p.get("total_pl_pct"), (int, float)):
        right.append(f'<span style="color:{MUTED};white-space:nowrap">{"· " if right else ""}{_e(pct_text(p["total_pl_pct"], 2))} since start</span>')
    fills = [f"Stop hit today: sold {f['qty']:g} {f['symbol']} at {inr(f['price'], 2)}"
             + (f" (stop {inr(f['stop'], 2)})" if isinstance(f.get("stop"), (int, float)) else "") + "."
             for f in p.get("stop_fills_today") or []]
    under = " ".join(fills) if fills else ("" if stale else "No stop-loss sells today.")
    return (f'<table {TABLE} style="margin-top:14px;background:{STRIP_BG};border-radius:10px"><tr>'
            f'<td style="padding:10px 14px;font-size:13px;color:{INK};{FONT}">{left}</td>'
            f'<td align="right" style="padding:10px 14px;font-size:13px;{FONT}">{" ".join(right)}</td></tr>'   # wraps between parts on a phone
            + (f'<tr><td colspan="2" style="padding:0 14px 10px;font-size:12px;color:{BAD if fills else MUTED};{FONT}">{_e(under)}</td></tr>'
               if under else "")
            + "</table>")


def _portfolio(d: dict[str, Any]) -> str:
    g, p = d.get("groww"), _part(d, "practice")
    stale = isinstance(d.get("stale_close"), dict)
    if isinstance(g, dict) and "unavailable" not in g:
        bits = ["Best to worst by total return" if stale else "Best to worst"]
        if isinstance(g.get("invested"), (int, float)):
            bits.append(f"invested {inr(g['invested'])}")
        bits.append("no change for today on this snapshot" if stale else "today's buys show from tomorrow (T+1)")
        out = [_section("Your Groww portfolio" if stale else "Your Groww portfolio today", " · ".join(bits))]
        if g.get("no_price"):
            out.append(_note(f"{len(g['no_price'])} unpriced (bonds or unlisted) not counted: " + ", ".join(g["no_price"]) + "."))
        if g.get("no_prev_close") and not stale:
            out.append(_note("No previous close, left out of today: " + ", ".join(g["no_prev_close"]) + "."))
        out.append(_holdings(g, stale))
    elif isinstance(g, dict):
        out = [_section("Your Groww portfolio"), _note(f"Unavailable: {g['unavailable']}", BAD)]
    elif p:
        out = [_section("Practice account")]
    else:
        return ""
    if p:
        out.append(_practice(p, stale))
    return "".join(out) + "</td></tr>"


# -- news and deals ---------------------------------------------------------------------------------
_BADGES = {"negative": ("NEGATIVE", "#ffffff", BAD), "positive": ("POSITIVE", GOOD, None), "neutral": ("NEUTRAL", MUTED, None)}


def _news(d: dict[str, Any]) -> str:
    n = _part(d, "news")
    items = (n or {}).get("items") or []
    if not items:
        return ""
    order = {"negative": 0, "neutral": 1, "positive": 2}
    items = sorted(items, key=lambda i: order.get(str(i.get("sentiment")), 3))
    sub = "Negative first · tagged by a language model, can be wrong"
    if (n or {}).get("total", len(items)) > len(items):
        sub += f" · {n['total']} headlines, the first {len(items)} shown"   # type: ignore[index]
    out = [_section("News for your stocks", sub), f'<table {TABLE} style="margin-top:10px;border-collapse:collapse">']
    for i in items:
        word, fg, bg = _BADGES.get(str(i.get("sentiment")), (str(i.get("sentiment") or "").upper(), MUTED, None))
        meta = " · ".join(x for x in (str(i.get("source") or ""), str(i.get("when") or "")) if x)
        title = _e(i.get("title"))
        link = str(i.get("link") or "")
        if link.startswith(("http://", "https://")):
            title = f'<a href="{_e(link)}" style="color:{INK};text-decoration:underline;text-decoration-color:#cfc9f3">{title}</a>'
        out.append(f'<tr><td style="padding:10px 0;border-top:1px solid {LINE};{FONT}">'
                   f'<div style="font-size:12px;color:{MUTED}">{_badge(word, fg, bg)} <b style="color:{INK}">{_e(i.get("symbol"))}</b>'
                   f'{" · " + _e(meta) if meta else ""}</div>'
                   f'<div style="font-size:14px;line-height:20px;color:{INK};padding-top:4px">{title}</div></td></tr>')
    out.append("</table>")
    return "".join(out)


def _who(names: list[str]) -> str:
    """'A', 'A or B', 'A, B or C'; more than three are counted."""
    if not names:
        return "the followed investors"
    if len(names) > 3:
        return f"the {len(names)} followed investors"
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]


def deals_line(d: dict[str, Any]) -> str | None:
    """'Deals today: none by A or B.' / 'Deals today: BEL BUY 19,20,000 by A (NSE).' plus the BSE timing note."""
    sec = _part(d, "deals")
    if sec is None:
        return None
    stale = isinstance(d.get("stale_close"), dict)
    rows = sec.get("deals") or []
    if rows:
        bits = [" ".join(x for x in (str(r.get("ticker") or ""), str(r.get("transaction") or ""), str(r.get("size") or "")) if x)
                + f" by {', '.join(r.get('who') or []) or r.get('investor')}"
                + (f" ({r['exchange']})" if r.get("exchange") else "") for r in rows]
        more = int(sec.get("total") or len(rows)) - len(rows)
        text = "; ".join(bits) + (f"; and {more} more" if more > 0 else "") + "."
    else:
        text = f"none by {_who(list(sec.get('following') or []))}."
    return text + ("" if stale else " " + BSE_NOTE)


def _news_and_deals(d: dict[str, Any]) -> str:
    news, line = _news(d), deals_line(d)
    if not news and not line:
        return ""
    deal = (f'<div style="font-size:13px;line-height:19px;color:{BODY};padding-top:{14 if news else 0}px">'
            f'<b style="color:{INK}">Deals today:</b> {_e(line)}</div>' if line else "")
    if news:
        return news + deal + "</td></tr>"
    return f'<tr><td style="padding:22px 24px 4px;{FONT}">{deal}</td></tr>'


# -- the market bulletin ----------------------------------------------------------------------------
def _image(images: list[dict[str, Any]] | None, cid: str) -> str:
    for i in images or []:
        if i.get("cid") == cid:
            return (f'<img src="cid:{_e(cid)}" alt="{_e(i.get("alt") or cid)}" width="592" '
                    'style="display:block;width:100%;max-width:592px;height:auto;border:0;margin-top:10px;border-radius:8px">')
    return ""


def _grid(nf: dict[str, Any]) -> str:
    cells: list[tuple[str, str, str]] = []
    lv = nf.get("levels") or {}
    if lv:
        r, s = lv.get("resistance"), lv.get("support")
        cells.append(("Watch levels", f"{num(r['price']) + ' above' if r else 'none above'} · {num(s['price']) + ' below' if s else 'none below'}",
                      "nearest swing high and low"))
    pv = nf.get("pivots") or {}
    if all(isinstance(pv.get(k), (int, float)) for k in ("P", "R1", "S1")):
        cells.append(("Pivots", f"{num(pv['P'])} · R1 {num(pv['R1'])} · S1 {num(pv['S1'])}", "classic, for the next session"))
    if isinstance(nf.get("adx"), (int, float)):
        band = str(nf.get("adx_band") or "")
        word = "weak or no trend" if band == "weak" else f"{band} trend"
        side = ("" if not all(isinstance(nf.get(k), (int, float)) for k in ("plus_di", "minus_di"))
                else "+DI above −DI" if nf["plus_di"] >= nf["minus_di"] else "−DI above +DI")
        cells.append(("ADX (14)", f"{nf['adx']:.1f} · {word}", side))
    if isinstance(nf.get("rsi"), (int, float)):
        cells.append(("RSI (14)", f"{nf['rsi']:.1f}", rsi_zone(nf["rsi"])))
    if not cells:
        return ""
    rows = [cells[i:i + 2] for i in range(0, len(cells), 2)]
    out = [f'<table {TABLE} style="margin-top:10px;border-collapse:separate;border-spacing:0;border:1px solid {LINE};border-radius:10px">']
    for ri, row in enumerate(rows):
        out.append("<tr>")
        for ci, (lab, big, small) in enumerate(row):
            edges = ("" if ci == len(row) - 1 else f"border-right:1px solid {LINE};") + \
                    ("" if ri == len(rows) - 1 else f"border-bottom:1px solid {LINE};")
            span = ' colspan="2"' if len(row) == 1 and len(rows[0]) == 2 else ""
            out.append(f'<td width="{"100%" if span else "50%"}"{span} style="padding:10px 14px;{edges}vertical-align:top;{FONT}">'
                       f'<div style="{EYEBROW}letter-spacing:.8px;color:{MUTED}">{_e(lab)}</div>'
                       f'<div style="font-size:14px;font-weight:bold;color:{INK};padding-top:3px">{_e(big)}</div>'
                       + (f'<div style="font-size:12px;color:{MUTED}">{_e(small)}</div>' if small else "") + "</td>")
        out.append("</tr>")
    out.append("</table>")
    return "".join(out)


def _markets(b: dict[str, Any]) -> str:
    rows: list[tuple[str, str, Any, Any, str]] = []   # name, last, 1d, 5d, a headline that names it
    g = b.get("global")
    if isinstance(g, dict) and "unavailable" not in g:
        for r in g.get("markets") or []:
            why = r.get("why") if isinstance(r.get("why"), dict) else None
            rows.append((str(r.get("market")), num_intl(r["close"], 2) if isinstance(r.get("close"), (int, float)) else "n/a",
                         r.get("d1_pct"), r.get("d5_pct"),
                         f"In the news: {why['title']} ({why['source']})" if why and why.get("title") else ""))
    c = b.get("commodities")
    if isinstance(c, dict) and "unavailable" not in c:
        for r in c.get("rows") or []:
            rows.append((str(r.get("name")), f"${num_intl(r['last'], 2)}" if isinstance(r.get("last"), (int, float)) else "n/a",
                         r.get("d1_pct"), r.get("d5_pct"), ""))
    if not rows:
        return ""
    th = f"{EYEBROW}letter-spacing:.6px;color:{MUTED};font-weight:normal;border-bottom:1px solid {LINE};{FONT}"
    out = [f'<table {TABLE} style="margin-top:16px;border-collapse:collapse"><tr>'
           f'<th align="left" style="{th}padding:6px 0">Global and commodities</th><th align="right" style="{th}padding:6px 0">Last</th>'
           f'<th align="right" style="{th}padding:6px 0 6px 12px;width:64px">1d</th>'
           f'<th align="right" style="{th}padding:6px 0 6px 12px;width:64px">5d</th></tr>']
    for n, (name, last, d1, d5, why) in enumerate(rows):
        edge = f"border-bottom:1px solid {SOFT};" if n < len(rows) - 1 else ""
        td = f"padding:7px 0;{edge}vertical-align:top;{FONT}"
        one, five = pct_text(d1, 2), pct_text(d5, 2)
        under = f'<div style="font-size:11.5px;line-height:16px;color:{MUTED};padding-top:1px">{_e(why)}</div>' if why else ""
        out.append(f'<tr><td style="{td}font-size:13px;color:{BODY}">{_e(name)}{under}</td>'
                   f'<td align="right" style="{td}font-size:13px;color:{INK};white-space:nowrap">{_e(last)}</td>'
                   f'<td align="right" style="{td}padding-left:12px;font-size:13px;color:{_tone(one) if one != "n/a" else MUTED};white-space:nowrap">{_e(one)}</td>'
                   f'<td align="right" style="{td}padding-left:12px;font-size:12px;color:{MUTED};white-space:nowrap">{_e(five)}</td></tr>')
    out.append("</table>")
    return "".join(out)


def _bulletin(d: dict[str, Any], images: list[dict[str, Any]] | None) -> str:
    b = d.get("bulletin")
    if not isinstance(b, dict):
        return ""
    nf = _nifty(d)
    body: list[str] = []
    if nf:
        stale = d.get("stale_close") if isinstance(d.get("stale_close"), dict) else None
        expected = str(stale["date"]) if stale and stale.get("date") else str(d.get("date") or nf.get("session"))
        on = "" if str(nf.get("session")) == expected else f" on {_day(nf.get('session'))}"
        pct = pct_text(nf.get("change_pct"), 2)
        body.append(f'<div style="font-size:17px;font-weight:bold;color:{INK};padding-top:12px">Nifty 50 closed at '
                    f'{_e(num(nf["close"], 2))}{_e(on)}, <span style="color:{_tone(pct)}">{_e(pct)}</span></div>')
        bits = []
        if isinstance(nf.get("gap_pct"), (int, float)):
            gp = nf["gap_pct"]
            bits.append("Opened near the previous close" if abs(gp) < GAP_FLAT_PCT else
                        f"Opened {abs(gp):.2f}% {'higher' if gp > 0 else 'lower'}")
        names = nf.get("candle")
        if isinstance(names, list):
            bits.append(" and ".join(names) + " candle" if names else "no named candle pattern")
        if bits:
            body.append(f'<div style="font-size:12.5px;color:{BODY};padding-top:2px">{_e(" · ".join(bits))}</div>')
        body.append(_image(images, "nifty15"))
        body.append(_grid(nf))
        body.append(_image(images, "nifty4h"))
        body += [_note(f"Note: {x}") for x in nf.get("notes") or []]
    elif isinstance(b.get("nifty"), dict):
        body.append(_note(f"Nifty 50 unavailable: {b['nifty'].get('unavailable')}", BAD))
    body.append(_markets(b))
    if not any(body):
        return ""
    return (f'<tr><td style="padding:26px 24px 4px;{FONT}"><div style="{EYEBROW}letter-spacing:1.2px;color:{MUTED};font-weight:bold;'
            f'padding-bottom:8px;border-bottom:1px solid {LINE}">Market bulletin</div>' + "".join(body)
            + _note(FOOTNOTE).replace("padding-top:6px", "padding-top:8px") + "</td></tr>")


def _concept(d: dict[str, Any]) -> str:
    b = d.get("bulletin")
    k = b.get("concept") if isinstance(b, dict) else None
    if not isinstance(k, dict) or "unavailable" in k or not k.get("title"):
        return ""
    return (f'<tr><td style="padding:20px 24px 4px"><table {TABLE} style="background:{SHORT_BG};border-radius:10px">'
            f'<tr><td style="padding:14px 16px;{FONT}">'
            f'<div style="{EYEBROW}letter-spacing:.8px;color:{ACCENT};font-weight:bold">{_e(k.get("label") or "Concept of the day")}</div>'
            f'<div style="font-size:16px;font-weight:bold;color:{INK};padding-top:4px">{_e(k["title"])}</div>'
            f'<div style="font-size:13.5px;line-height:20px;color:{INK};padding-top:4px">{_e(k.get("text") or "")}</div>'
            + (f'<div style="font-size:12.5px;color:{BODY};padding-top:6px">Where you see it: {_e(k["uses"])}</div>' if k.get("uses") else "")
            + "</td></tr></table></td></tr>")


# -- the whole email ----------------------------------------------------------------------------------
def to_html(d: dict[str, Any], summary: str | None = None, writer: str = "none",
            images: list[dict[str, Any]] | None = None) -> str:
    stale = isinstance(d.get("stale_close"), dict)
    if stale and summary:   # the headline already says "no trading today"; the summary does not repeat it
        summary = re.sub(r"^(?:No trading today|The market has not closed yet)\.\s*", "", summary) or None
    saved = _evening_subtitle({**d, "stale_close": None})   # 'holdings saved 09 Oct 15:30' when the saved copy is used
    extra = _note(saved[0].upper() + saved[1:] + ".", CAUTION_INK, "12.5px") if saved else ""
    short = _short(summary, writer) if str(writer).startswith("claude:") else ""   # only a model's summary gets the box
    return frame(top("Trading Agent · Close report", stamp(d), headline(d), extra)
                 + _tiles(d) + short + _portfolio(d) + _news_and_deals(d) + _bulletin(d, images) + _concept(d),
                 bool(d.get("delayed")))
