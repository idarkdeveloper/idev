"""The morning email's HTML: a short brief that leads with what to do today.

Layout (top to bottom): the date and a one-line headline, three figures (market, your portfolio, holdings that need a
look), the "In short" summary, the holdings to watch, the buy ideas (or the held-back candidates on a no-buy day), new
deals by followed investors, then the market context (Nifty, world indices, risk gauges, flows and breadth) in one
compact table. The plain-text part is still made by ``digest_render.to_text`` from the same data, so both say the same.

Email clients ignore <style> blocks and classes, so everything is inline-styled tables. Every value is escaped.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from .digest import WARN_TEXT, inr, num, num_intl, pct_text
from .digest_render import DELAYED, FOOTER, STOP_RULE, _breached, _e, _flags, _meta_line, _plural
from .digest_rules import short_name

FONT = "font-family:Arial,Helvetica,sans-serif;"
INK, BODY, MUTED = "#1a1c28", "#474b5e", "#6a6e82"
ACCENT, GOOD, BAD = "#5546b8", "#0a7a3c", "#c1272d"
LINE, SOFT = "#e4e6ee", "#f1f2f7"
CAUTION_BG, CAUTION_INK = "#f6e7c1", "#7d5200"
SHORT_BG = "#f5f4ff"
TABLE = 'role="presentation" width="100%" cellpadding="0" cellspacing="0"'
EYEBROW = "font-size:11px;text-transform:uppercase;"
REGIME = {"risk_on": "Risk-on", "risk_off": "Risk-off", "neutral": "Neutral"}


def _tone(text: str) -> str:
    t = str(text)
    return GOOD if t.startswith("+") else BAD if t.startswith("−") else INK


def _cls(text: str) -> str:
    t = str(text)
    return ' class="pl-profit"' if t.startswith("+") else ' class="pl-loss"' if t.startswith("−") else ""


def _day(iso: Any) -> str:
    """'2026-10-10' -> '10 Oct'; anything else as given."""
    try:
        d = date.fromisoformat(str(iso)[:10])
    except ValueError:
        return str(iso or "")
    return f"{d.day} {d.strftime('%b')}"


def _section(title: str, sub: str | None = None, colour: str = INK, pad: str = "22px 24px 4px") -> str:
    head = f'<tr><td style="padding:{pad};{FONT}"><div style="font-size:17px;font-weight:bold;color:{colour}">{_e(title)}</div>'
    return head + (f'<div style="font-size:12px;color:{MUTED};padding-top:2px">{_e(sub)}</div>' if sub else "")


def _note(text: str, colour: str = MUTED, size: str = "12px") -> str:
    return f'<div style="font-size:{size};line-height:18px;color:{colour};padding-top:6px">{_e(text)}</div>'


def _badge(text: str, fg: str, bg: str | None = None) -> str:
    look = f"color:{fg};background:{bg};" if bg else f"color:{fg};border:1px solid {fg};"
    return (f'<span style="display:inline-block;font-size:10.5px;font-weight:bold;letter-spacing:.6px;{look}'
            f'border-radius:4px;padding:{"2px 6px" if bg else "1px 5px"}">{_e(text)}</span>')


# -- top: headline, three figures, summary ---------------------------------------------------
def _counts(d: dict[str, Any]) -> tuple[bool, int, int | None]:
    """(no new buys today, buy ideas shown, holdings to watch or None when the watch list is unavailable)."""
    mood, ideas, watch = d.get("mood") or {}, d.get("buy_ideas") or {}, d.get("watch") or {}
    wait = "unavailable" not in mood and bool(mood.get("no_new_buys"))
    n_buy = 0 if ("unavailable" in ideas or wait or ideas.get("wait")) else len(ideas.get("ideas") or [])
    n_watch = None if "unavailable" in watch else int(watch.get("total", len(watch.get("items") or [])))
    return wait, n_buy, n_watch


def headline(d: dict[str, Any]) -> str:
    """'Risk-on. 3 buy ideas, 2 holdings to watch.' / 'No new buys today. 2 holdings to watch.'"""
    wait, n_buy, n_watch = _counts(d)
    mood = d.get("mood") or {}
    watch = ("holdings not checked" if n_watch is None else "nothing to watch" if not n_watch
             else _plural(n_watch, "holding") + " to watch")
    if wait:
        return f"No new buys today. {watch[0].upper() + watch[1:]}."
    lead = "Market data unavailable" if "unavailable" in mood else REGIME.get(str(mood.get("regime")), "Mixed market")
    return f"{lead}. {_plural(n_buy, 'buy idea')}, {watch}."


def _stat(label: str, big: str, small: str, big_colour: str = INK, small_colour: str = BODY, bold_small: bool = False,
          last: bool = False) -> str:
    edge = "" if last else f"border-right:1px solid {LINE};"
    return (f'<td width="{"34%" if label == "Market" else "33%"}" style="padding:12px 14px;{edge}vertical-align:top;{FONT}">'
            f'<div style="{EYEBROW}letter-spacing:.8px;color:{MUTED}">{_e(label)}</div>'
            f'<div style="font-size:17px;font-weight:bold;color:{big_colour};padding-top:3px">{_e(big)}</div>'
            f'<div style="font-size:12px;color:{small_colour};{"font-weight:bold;" if bold_small else ""}padding-top:2px">{_e(small)}</div></td>')


def _stats(d: dict[str, Any]) -> str:
    mood, watch = d.get("mood") or {}, d.get("watch") or {}
    wait = "unavailable" not in mood and bool(mood.get("no_new_buys"))
    nf = mood.get("nifty") or {}
    if "unavailable" in mood:
        market = _stat("Market", "n/a", "market data unavailable", MUTED)
    else:
        regime = str(mood.get("regime") or "")
        word = REGIME.get(regime, "Mixed")
        colour = BAD if wait or regime == "risk_off" else GOOD if regime == "risk_on" else INK
        if wait and nf.get("above_200dma") is False:
            sub = "Nifty below 200-day avg"
        elif wait and mood.get("trend") == "down":
            sub = "Nifty in a downtrend"
        elif isinstance(nf.get("last"), (int, float)):
            sub = f"Nifty {num(nf['last'])} · {pct_text(nf.get('ret_1d_pct'), 2)}"
        else:
            sub = "Nifty n/a"
        market = _stat("Market", word, sub, colour)
    p = watch.get("portfolio") if isinstance(watch.get("portfolio"), dict) else None
    if p and isinstance(p.get("value"), (int, float)):
        if isinstance(p.get("pl"), (int, float)):
            pct = f" ({pct_text(p['pl_pct'], 2)})" if isinstance(p.get("pl_pct"), (int, float)) else ""
            pl = f"{'−' if p['pl'] < 0 else '+'}{inr(abs(p['pl']))}{pct}"
            port = _stat("Your portfolio", inr(p["value"]), pl, small_colour=_tone(pl), bold_small=True)
        else:
            port = _stat("Your portfolio", inr(p["value"]), "P&L n/a")
    else:
        port = _stat("Your portfolio", "n/a", "no portfolio figures", MUTED)
    if "unavailable" in watch:
        look = _stat("Needs a look", "n/a", "holdings not checked", MUTED, last=True)
    else:
        items = watch.get("items") or []
        total, checked = int(watch.get("total", len(items))), int(watch.get("checked") or 0)
        below = sum(1 for i in items if _breached(i))
        big = f"{total} of {checked} holdings" if checked else _plural(total, "holding")
        look = _stat("Needs a look", big, f"{below} below {'its' if below == 1 else 'their'} stop" if below else "none below a stop",
                     small_colour=BAD if below else BODY, bold_small=bool(below), last=True)
    return (f'<tr><td style="padding:12px 24px 4px"><table {TABLE} style="border-collapse:separate;border-spacing:0;'
            f'border:1px solid {LINE};border-radius:10px"><tr>{market}{port}{look}</tr></table></td></tr>')


def _short(summary: str | None, writer: str) -> str:
    if not summary:
        return ""
    who = "the rules" if writer == "rules" else ("Claude Haiku" if "claude-haiku" in writer else writer) + ", check the numbers below"
    return (f'<tr><td style="padding:14px 24px 2px"><table {TABLE} style="background:{SHORT_BG};border-radius:10px">'
            f'<tr><td style="padding:12px 14px;{FONT}">'
            f'<div style="{EYEBROW}letter-spacing:.8px;color:{ACCENT};font-weight:bold">In short <span style="font-weight:normal;color:{MUTED};'
            f'text-transform:none;letter-spacing:0">· written by {_e(who)}</span></div>'
            f'<div style="font-size:14px;line-height:21px;color:{INK};padding-top:5px">{_e(summary)}</div></td></tr></table></td></tr>')


# -- holdings to watch -------------------------------------------------------------------------
def _detail(i: dict[str, Any], breached: bool) -> str:
    """'Review for exit · below 200-day avg (₹1,018.40) · results due' — the flags in words, the badge says the rest."""
    bits = ["Review for exit"] if breached else []
    for f in _flags(i).split(", "):
        if not f or f == "below stop":
            continue
        if f == "below avg":
            avg = f" (₹{num(i['ma200'], 2)})" if isinstance(i.get("ma200"), (int, float)) else ""
            f = "below 200-day avg" + avg
        elif f.startswith("−"):
            f = f"{f} from your buy price"
        bits.append(f)
    p, st = i.get("price"), i.get("stop")
    if not breached and isinstance(p, (int, float)) and isinstance(st, (int, float)) and p >= st:
        bits.append(f"₹{num(p - st, 0)} above its stop")
    if i.get("also_practice"):
        bits.append("also in practice")
    text = " · ".join(bits) or "flagged"
    return text[0].upper() + text[1:]


def _watch(d: dict[str, Any]) -> str:
    watch = d.get("watch") or {}
    if "unavailable" in watch:
        return _section("Holdings to watch") + _note(f"Unavailable: {watch['unavailable']}", BAD) + "</td></tr>"
    items = watch.get("items") or []
    meta = _meta_line(watch)
    if not items:
        lines = (["Nothing to watch today."] if watch.get("checked") else []) + ([meta] if meta else [])
        return _section("Holdings to watch") + "".join(_note(x, BODY, "13px") for x in lines) + "</td></tr>"
    key = lambda i: i["loss_pct"] if isinstance(i.get("loss_pct"), (int, float)) else 0.0   # noqa: E731
    ordered = sorted([i for i in items if _breached(i)], key=key) + sorted([i for i in items if not _breached(i)], key=key)
    out = [_section("Holdings to watch", " · ".join(x for x in (meta, STOP_RULE) if x)),
           f'<table {TABLE} style="margin-top:10px;border-collapse:collapse">']
    for i in ordered:
        breached = _breached(i)
        flags = _flags(i)
        badge = (_badge("BELOW STOP", "#ffffff", BAD) if breached else
                 _badge("TREND CAUTION" if "below avg" in flags else "WATCH", CAUTION_INK, CAUTION_BG))
        name = short_name(i.get("name"))
        name_html = (f' <span style="font-size:12.5px;color:{MUTED}">{_e(name)}</span>'
                     if name and name.upper() != str(i.get("symbol") or "").upper() else "")
        tip = "; ".join(r if len(r) <= 90 else r[:87].rstrip() + "..." for r in map(str, i.get("reasons") or []))
        priced = isinstance(i.get("price"), (int, float))
        loss = i.get("loss_pct")
        pl = pct_text(loss) if priced and isinstance(loss, (int, float)) else ("n/a" if priced else "")
        stop = f"stop ₹{num(i['stop'], 2)}" if isinstance(i.get("stop"), (int, float)) else ""
        right = " · ".join(x for x in (pl, stop) if x)
        cell = f"padding:12px 0;border-top:1px solid {LINE};vertical-align:top;{FONT}"
        tip_attr = f' title="{_e(tip)}"' if tip else ""   # the full reasons on hover
        out.append(f'<tr><td style="{cell}">{badge}'
                   f'<span style="font-size:15px;font-weight:bold;color:{INK};padding-left:6px">{_e(i.get("symbol"))}</span>{name_html}'
                   f'<div{tip_attr} style="font-size:12.5px;line-height:18px;color:{BODY};'
                   f'padding-top:4px">{_e(_detail(i, breached))}</div></td>'
                   f'<td align="right" style="{cell}padding-left:10px;white-space:nowrap">'
                   f'<div style="font-size:15px;font-weight:bold;color:{INK}">{_e("₹" + num(i["price"], 2) if priced else "no price")}</div>'
                   + (f'<div{_cls(pl)} style="font-size:12.5px;color:{_tone(pl)};font-weight:bold">{_e(right)}</div>' if right else "")
                   + "</td></tr>")
    out.append("</table></td></tr>")
    return "".join(out)


# -- buy ideas -----------------------------------------------------------------------------------
def _ideas(d: dict[str, Any]) -> str:
    ideas, mood = d.get("buy_ideas") or {}, d.get("mood") or {}
    wait = "unavailable" not in mood and bool(mood.get("no_new_buys"))
    title = "Would pass, held back by the market filter" if wait else "Buy ideas"
    colour = MUTED if wait else INK
    if "unavailable" in ideas:
        return _section(title, colour=colour) + _note(f"Unavailable: {ideas['unavailable']}", BAD) + "</td></tr>"
    rows = ideas.get("ideas") or []
    sub = (f"Momentum screen of {ideas['universe']}: {ideas['eligible']} of {ideas['universe_size']} pass"
           + (f" ({ideas['scored']} scored; {ideas['errors']} could not be read)" if ideas.get("errors") else "")
           + f" · sized to {ideas['sizing']}, on {inr(ideas['equity'])} ({ideas['equity_basis']})")
    out = [_section(title, sub, colour)]
    if wait:
        why = " and ".join(mood.get("why") or ["the market filter says wait"])
        out.append(_note(f"No new buys today: {why}. Rule: {mood.get('rules') or 'the market filter'}.", BODY, "12.5px"))
    pricey = ideas.get("too_expensive") or []
    if pricey:
        out.append(_note("Too expensive for this account size (one share > 10% of equity): " + ", ".join(pricey) + "."))
    for b in ideas.get("band_skipped") or []:
        out.append(_note(f"{b['symbol']}: {b['reason']}."))
    cautions = [i["symbol"] + " (" + i["band_note"] + ")" for i in rows if i.get("band_note")]
    if cautions:
        out.append(_note("Caution: " + "; ".join(cautions) + ".", CAUTION_INK))
    if not rows:
        if not pricey:
            out.append(_note("Nothing passes the screen today.", BODY, "13px"))
        return "".join(out) + "</td></tr>"
    th = f"{EYEBROW}letter-spacing:.6px;color:{MUTED};font-weight:normal;border-bottom:1px solid {LINE};{FONT}"
    out.append(f'<table {TABLE} style="margin-top:10px;border-collapse:collapse"><tr>'
               f'<th align="left" style="{th}padding:6px 0">Stock</th><th align="right" style="{th}padding:6px 0 6px 8px">Buy</th>'
               f'<th align="right" style="{th}padding:6px 0 6px 8px">Stop</th><th align="right" style="{th}padding:6px 0 6px 8px">6m</th></tr>')
    for n, i in enumerate(rows):
        edge = f"border-bottom:1px solid {SOFT};" if n < len(rows) - 1 else ""
        td = f"padding:10px 0 10px 8px;{edge}vertical-align:top;{FONT}"
        name = short_name(i.get("name"))
        six = pct_text(i.get("ret_6m_pct"), 0)
        six_colour = MUTED if wait else _tone(six)
        out.append(f'<tr><td style="padding:10px 0;{edge}vertical-align:top;{FONT}">'
                   f'<div style="font-size:14px;font-weight:bold;color:{INK}">{_e(i["symbol"])}</div>'
                   f'<div style="font-size:12px;color:{MUTED}">{_e(" · ".join(x for x in (name, inr(i["price"], 2)) if x))}</div></td>'
                   f'<td align="right" style="{td}white-space:nowrap"><div style="font-size:14px;color:{INK}">{_e(_plural(int(i["qty"]), "share"))}</div>'
                   f'<div style="font-size:12px;color:{MUTED}">{_e(inr(i["notional"]))}</div></td>'
                   f'<td align="right" style="{td}font-size:14px;color:{INK};white-space:nowrap">{_e(inr(i["stop"], 2) if i.get("stop") else "n/a")}</td>'
                   f'<td align="right" style="{td}font-size:14px;color:{six_colour};font-weight:bold;white-space:nowrap">{_e(six)}</td></tr>')
    out.append("</table></td></tr>")
    return "".join(out)


# -- deals ---------------------------------------------------------------------------------------
def _size(s: Any) -> str:
    t = str(s or "")
    return t + " shares" if re.fullmatch(r"[\d,]+", t) else t


def _deals(d: dict[str, Any]) -> str:
    sec = d.get("deals") or {}
    title = "New deals by followed investors"
    if "unavailable" in sec:
        return _section(title) + _note(f"Unavailable: {sec['unavailable']}", BAD) + "</td></tr>"
    if not sec.get("deals"):
        who = sec.get("following") or []
        named = ", ".join(who) if len(who) <= 3 else f"the {len(who)} tracked investors"
        return _section(title) + _note(f"No disclosures by {named} since {_day(sec.get('since'))}.", BODY, "13px") + "</td></tr>"
    rows = sec["deals"]
    more = sec.get("total", len(rows)) - len(rows)
    out = [_section(title, f"{sec['total']} since {_day(sec.get('since'))}" + (f"; the first {len(rows)} are shown" if more > 0 else "")),
           f'<table {TABLE} style="margin-top:10px;border-collapse:collapse">']
    for x in rows:
        side = str(x.get("transaction") or "").upper()
        who = ", ".join(x.get("who") or []) or x.get("investor") or ""
        rest = " · ".join(v for v in (_size(x.get("size")), who) if v)
        when = " · ".join(v for v in (_day(x.get("reported")), str(x.get("exchange") or "")) if v)
        cell = f"padding:9px 0;border-top:1px solid {LINE};{FONT}"
        out.append(f'<tr><td style="{cell}font-size:14px;color:{INK}">{_badge(side or "DEAL", BAD if side == "SELL" else ACCENT)} '
                   f'<b>{_e(x.get("ticker"))}</b> <span style="color:{MUTED};font-size:12.5px">· {_e(rest)}</span></td>'
                   f'<td align="right" style="{cell}padding-left:10px;font-size:12.5px;color:{MUTED};white-space:nowrap">{_e(when)}</td></tr>')
    out.append("</table></td></tr>")
    return "".join(out)


# -- market context ------------------------------------------------------------------------------
def _ctx_row(name: str, value: str, mid: str, mid_colour: str, d20: str, sub: str = "", sub_colour: str = MUTED, last: bool = False) -> str:
    edge = "" if last else f"border-bottom:1px solid {SOFT};"
    td = f"padding:7px 0;{edge}vertical-align:top;{FONT}"
    under = f'<div style="font-size:11.5px;color:{sub_colour};padding-top:1px">{_e(sub)}</div>' if sub else ""
    return (f'<tr><td style="{td}font-size:13px;color:{BODY}">{_e(name)}{under}</td>'
            f'<td align="right" style="{td}font-size:13px;color:{INK};white-space:nowrap">{_e(value)}</td>'
            f'<td align="right" style="{td}padding-left:12px;font-size:13px;color:{mid_colour};white-space:nowrap">{_e(mid)}</td>'
            f'<td align="right" style="{td}padding-left:12px;font-size:12px;color:{MUTED};white-space:nowrap">{_e(d20)}</td></tr>')


def _context(d: dict[str, Any], pm: str | None) -> str:
    mood, world, gauges = d.get("mood") or {}, d.get("world"), d.get("gauges")
    rows: list[tuple[Any, ...]] = []
    nf = mood.get("nifty") or {}
    if "unavailable" not in mood and isinstance(nf.get("last"), (int, float)):
        above = ("above its 200-day average" if nf.get("above_200dma") is True else
                 "below its 200-day average" if nf.get("above_200dma") is False else "")
        one = pct_text(nf.get("ret_1d_pct"), 2)
        rows.append(("Nifty 50", num(nf["last"], 2), one, _tone(one), pct_text(nf.get("ret_20d_pct")) + " in 20d", above))
    notes: list[tuple[str, str]] = []
    if isinstance(world, dict) and "unavailable" not in world:
        for r in world.get("us", []) + world.get("asia", []) + world.get("futures", []) + ([world["vix"]] if world.get("vix") else []):
            one = pct_text(r.get("d1_pct"), 2)
            trend = f"trend {r['trend']}" if r.get("trend") else ""
            rows.append((r["index"], num_intl(r["close"], 2), one, _tone(one), pct_text(r.get("d20_pct")) + " in 20d", trend))
        notes += [(x, BODY) for x in world.get("region_lines") or []]
        if world.get("skipped"):
            notes.append((f"{world['skipped']} index(es) could not be read and are left out.", MUTED))
    elif isinstance(world, dict):
        notes.append((f"World markets unavailable: {world['unavailable']}", BAD))
    if isinstance(gauges, dict) and "unavailable" not in gauges:
        if gauges.get("warnings"):
            notes.insert(0, ("⚠ Warning: " + "; ".join(WARN_TEXT.get(w, w) for w in gauges["warnings"]) + ". A reason to size smaller.", BAD))
        for g in gauges.get("gauges") or []:
            reading = ("⚠ warning: " if g.get("warning") else "") + str(g.get("reading") or "")
            short = len(reading) <= 14
            colour = BAD if g.get("warning") else BODY
            value = (num if str(g["gauge"]).startswith("Nifty") else num_intl)(g["value"], 2)
            sub = "; ".join(x for x in ([] if short else [reading]) + [str(g.get("range") or "")] if x)
            rows.append((g["gauge"], value, reading if short else "", colour, pct_text(g.get("d20_pct")) + " in 20d", sub,
                         BAD if g.get("warning") and not short else MUTED))
        notes += [(str(gauges[k]), BODY) for k in ("flows_line", "breadth_line") if gauges.get(k) and gauges.get(k) != pm]
        if gauges.get("skipped"):
            notes.append((f"{gauges['skipped']} gauge(s) could not be read and are left out.", MUTED))
    elif isinstance(gauges, dict):
        notes.append((f"Risk gauges unavailable: {gauges['unavailable']}", BAD))
    if "unavailable" in mood:
        notes.insert(0, (f"Market mood unavailable: {mood['unavailable']}", BAD))
    notes += [(f"Note: {n}", MUTED) for n in mood.get("notes") or []]
    if not rows and not notes:
        return ""
    out = [f'<tr><td style="padding:24px 24px 4px;{FONT}"><div style="{EYEBROW}letter-spacing:1.2px;color:{MUTED};font-weight:bold;'
           f'padding-bottom:8px;border-bottom:1px solid {LINE}">Market context</div>']
    if rows:
        out.append(f'<table {TABLE} style="margin-top:4px;border-collapse:collapse">')
        for n, r in enumerate(rows):
            out.append(_ctx_row(r[0], r[1], r[2], r[3], r[4], sub=r[5] if len(r) > 5 else "", sub_colour=r[6] if len(r) > 6 else MUTED,
                                last=n == len(rows) - 1))
        out.append("</table>")
    for text, colour in notes:
        out.append(f'<div style="font-size:12.5px;line-height:19px;color:{colour};padding-top:8px">{_e(text)}</div>')
    readings = [x for x in ((gauges or {}).get("note") if isinstance(gauges, dict) else None,
                            (world or {}).get("note") if isinstance(world, dict) else None,
                            "Cross-market moves are used for sizing, never for direction." if isinstance(world, dict) and "unavailable" not in world else None) if x]
    if readings:
        out.append(_note(" ".join(readings)))
    out.append("</td></tr>")
    return "".join(out)


# -- the whole email -------------------------------------------------------------------------------
def to_html(d: dict[str, Any], summary: str | None = None, writer: str = "none") -> str:
    try:
        when = datetime.fromisoformat(d["generated_at"])
        stamp = when.strftime("%a ") + str(when.day) + when.strftime(" %b %Y · %H:%M IST")
    except (KeyError, TypeError, ValueError):
        stamp = str(d.get("date") or "")
    pm = (d.get("gauges") or {}).get("premarket_line") if isinstance(d.get("gauges"), dict) else None
    w = d.get("watch")
    watch: dict[str, Any] = w if isinstance(w, dict) else {}
    saved = next((n for n in watch.get("notes", []) if str(n).startswith("Using ")), None)
    top = [f'<tr><td style="padding:22px 24px 6px;{FONT}"><table {TABLE}><tr>'
           f'<td style="{EYEBROW}letter-spacing:1.2px;color:{ACCENT};font-weight:bold;{FONT}">Trading Agent · Morning brief</td>'
           f'<td align="right" style="font-size:12px;color:{MUTED};white-space:nowrap;{FONT}">{_e(stamp)}</td></tr></table>'
           f'<div style="font-size:24px;line-height:30px;font-weight:bold;color:{INK};padding-top:10px">{_e(headline(d))}</div>']
    if pm:   # the pre-market canary result, first thing under the headline
        bad = "FAILED" in str(pm)
        top.append(f'<div style="margin-top:10px;padding:8px 12px;border-radius:8px;font-size:13px;line-height:19px;'
                   f'color:{BAD if bad else INK};background:{"#fbeaea" if bad else SHORT_BG}">{_e(pm)}</div>')
    if saved:
        top.append(_note(str(saved).rstrip(".") + ".", CAUTION_INK, "12.5px"))
    top.append("</td></tr>")
    foot = "<br>".join(_e(x) for x in [FOOTER] + ([DELAYED] if d.get("delayed") else []))
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{SOFT};{FONT}">'
            '<tr><td align="center" style="padding:16px">'
            f'<table role="presentation" cellpadding="0" cellspacing="0" style="max-width:640px;width:100%;background:#ffffff;'
            f'border-radius:12px;border:1px solid {LINE}">'
            + "".join(top) + _stats(d) + _short(summary, writer) + _watch(d) + _ideas(d) + _deals(d) + _context(d, pm)
            + f'<tr><td style="padding:22px 24px 22px;{FONT}"><div style="border-top:1px solid {LINE};padding-top:14px;font-size:12px;'
            f'line-height:18px;color:{MUTED}">{foot}</div></td></tr></table></td></tr></table>')
