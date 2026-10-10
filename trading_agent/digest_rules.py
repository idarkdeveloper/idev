"""The "In short" summary written by rules: a few sentences built straight from the email's own data, so every
number and name in it is the same one the tables show. No model is involved, so it cannot be wrong about them.
Missing sections are simply left out."""

from __future__ import annotations

import re
from typing import Any, TypeGuard

from .digest import num, pct_text

_TREND_WORD = {"uptrend": "up", "downtrend": "down", "mixed": "mixed"}


def srupee(v: float | None, d: int = 0) -> str:
    """-21722 as −₹21,722 and 50 as +₹50 (the sign before the rupee sign); Indian grouping."""
    if v is None:
        return "n/a"
    s = num(v, d)
    if float(s.replace(",", "")) == 0:
        return f"₹{s}"
    return f"{'−' if v < 0 else '+'}₹{s}"


def _ok(sec: Any) -> TypeGuard[dict[str, Any]]:
    return isinstance(sec, dict) and "unavailable" not in sec


def short_name(name: Any) -> str:
    """'Vedanta Limited' -> 'Vedanta'; '' when there is no name."""
    n = re.sub(r"\s+", " ", str(name or "")).strip()
    n = re.sub(r"[\s,]+(limited|ltd\.?)$", "", n, flags=re.I).strip()
    return n


def label(item: dict[str, Any]) -> str:
    """'Vedanta (VEDL)' when the company name is known, else the NSE symbol alone."""
    sym = str(item.get("symbol") or "")
    name = short_name(item.get("name"))
    return f"{name} ({sym})" if name and name.upper() != sym.upper() else sym


def _names(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _tags(item: dict[str, Any]) -> list[str]:
    tags: list[str] = []
    loss = item.get("loss_pct")
    if isinstance(loss, (int, float)) and loss <= -10:
        tags.append(pct_text(loss, 0))
    for r in item.get("reasons") or []:
        low = r.lower()
        tag = ("negative news" if low.startswith("negative news") else
               "well past its stop" if "well past any stop" in low else
               "below stop" if low.startswith("below the stop level") else
               "near stop" if "of the stop level" in low else
               "below 200-day average" if "200-day average" in low else
               "results due" if "due" in low else
               "big fall" if low.startswith("fell") else None)
        if tag and tag not in tags:
            tags.append(tag)
    return tags


def _morning(d: dict[str, Any]) -> list[str]:
    out: list[str] = []
    mood = d.get("mood")
    if _ok(mood):
        if mood.get("no_new_buys"):
            why = [re.sub(r"\s*\([^)]*\)", "", w).strip() for w in mood.get("why") or []]
            out.append("No new buys today" + (": " + "; ".join(why) if why else "") + ".")
        else:
            out.append("New buys are allowed today.")
    bits: list[str] = []
    world = d.get("world")
    if _ok(world):
        regions = []
        for ln in world.get("region_lines") or []:
            m = re.match(r"(US|Asia): (uptrend|downtrend|mixed)", str(ln))
            if m:
                regions.append(f"{m.group(1)} {_TREND_WORD[m.group(2)]}")
        if regions:
            bits.append(", ".join(regions))
    gauges = d.get("gauges")
    if _ok(gauges):
        bits += [t[0].upper() + t[1:] for t in gauges.get("warning_texts") or []]
        for r in gauges.get("gauges") or []:
            if r.get("gauge") == "USD/INR" and not r.get("warning") and r.get("reading") == "rupee near its weakest of the year":
                bits.append(r["reading"])
    if bits:
        out.append("; ".join(bits) + ".")
    ideas = d.get("buy_ideas")
    if _ok(ideas) and (ideas.get("ideas") or ideas.get("too_expensive")):
        top = [label(i) for i in ideas.get("ideas") or []][:3]
        if ideas.get("wait"):
            out.append(f"{len(ideas.get('ideas') or [])} stocks would pass the screen, but the market filter says wait.")
        elif top:
            n = len(ideas["ideas"])
            out.append(f"{n} buy idea{'s' if n != 1 else ''} (top: {_names(top)}).")
    watch = d.get("watch")
    if _ok(watch):
        items = watch.get("items") or []
        total = watch.get("total", len(items))
        if total:
            groups: list[tuple[list[str], list[str]]] = []
            for it in items[:5]:
                tags = _tags(it)
                same = None if any(t.endswith("%") for t in tags) else next((g for g in groups if g[1] == tags), None)
                if same is not None:
                    same[0].append(label(it))
                else:
                    groups.append(([label(it)], tags))
            def tagtext(t: list[str]) -> str:   # "−29%, negative news" or "below stop and 200-day average"
                return ", ".join(t) if t and t[0].endswith("%") else _names(t)
            worries = "; ".join(f"{_names(n)} ({tagtext(t)})" if t else _names(n) for n, t in groups[:3])
            out.append(f"{total} holding{'s' if total != 1 else ''} flagged" + (f" — biggest worries: {worries}." if worries else "."))
        elif watch.get("checked"):
            out.append("No holdings flagged.")
    deals = d.get("deals")
    if _ok(deals):
        n = deals.get("total", len(deals.get("deals") or []))
        out.append(f"{n} new deal{'s' if n != 1 else ''} by followed investors." if n else "No new deals.")
    return out


def _evening(d: dict[str, Any]) -> list[str]:
    out: list[str] = []
    stale = d.get("stale_close")
    if stale:
        out.append(("No trading today" if stale.get("reason") == "no trading today" else "The market has not closed yet") + ".")
    g = d.get("groww")
    if _ok(g):
        s = f"Your Groww portfolio is ₹{num(g['value'])}, total {srupee(g.get('pl'))} ({pct_text(g.get('pl_pct'))})"
        if g.get("saved"):
            s += " (from saved holdings)"
        out.append(s + ".")
        if not stale and g.get("day_pl") is not None:
            rows = [h for h in g.get("holdings") or [] if h.get("day_pct") is not None]
            t = f"Today {srupee(g['day_pl'])} ({pct_text(g.get('day_pct'))})"
            if len(rows) >= 2:
                t += f"; best {rows[0]['symbol']} {pct_text(rows[0]['day_pct'])}, worst {rows[-1]['symbol']} {pct_text(rows[-1]['day_pct'])}"
            out.append(t + ".")
    elif isinstance(g, dict):
        out.append("Your Groww portfolio is unavailable.")
    p = d.get("practice")
    if _ok(p):
        t = f"Practice account ₹{num(p['equity'])} ({srupee(p.get('total_pl'))} since the start"
        if not stale and p.get("day_change") is not None:
            t += f", {srupee(p['day_change'])} today"
        t += ")"
        if p.get("same_as_groww"):
            t += ", a copy of your Groww holdings"
        out.append(t + ".")
        fills = p.get("stop_fills_today") or []
        if fills:
            out.append(f"{len(fills)} stop-loss sell{'s' if len(fills) != 1 else ''} today ({_names([f['symbol'] for f in fills])}).")
    nf = (d.get("bulletin") or {}).get("nifty") if isinstance(d.get("bulletin"), dict) else None
    if _ok(nf):
        t = f"Nifty closed at {num(nf['close'], 2)} ({pct_text(nf['change_pct'], 2)})"
        if nf.get("adx") is not None:
            t += f", daily ADX {nf['adx']:.0f} ({nf.get('adx_band')})"
        out.append(t + ".")
    news = d.get("news")
    if _ok(news):
        items = news.get("items") or []
        total = news.get("total", len(items))
        if total:
            neg = [i["symbol"] for i in items if i.get("sentiment") == "negative"]
            uniq = list(dict.fromkeys(neg))
            out.append(f"{total} news item{'s' if total != 1 else ''} for your stocks, {len(neg)} negative"
                       + (f" ({_names(uniq[:3])})." if uniq else "."))
        else:
            out.append("No tagged news for your stocks.")
    deals = d.get("deals")
    if _ok(deals):
        n = deals.get("total", len(deals.get("deals") or []))
        out.append(f"{n} deal{'s' if n != 1 else ''} by followed investors today." if n else "No deals by followed investors today.")
    return out


def rules_summary(kind: str, data: dict[str, Any]) -> str | None:
    """The deterministic summary, or None when there is nothing to say."""
    if kind != "morning" and not any(_ok(data.get(k)) for k in ("groww", "practice", "news", "deals")):
        return None   # nothing to summarise
    try:
        parts = _morning(data) if kind == "morning" else _evening(data)
    except Exception:  # noqa: BLE001 - an odd data shape must never cost the email
        return None
    return " ".join(parts) or None
