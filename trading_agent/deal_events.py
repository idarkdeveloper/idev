"""One event per real-world deal for SIGNALS, across exchanges.

NSE and BSE publish their own bulk and block deals. They are separate trades and stay separate records everywhere a
record is kept (seen-keys, the deals table). But a client who buys the same stock on both exchanges on one day is one
decision, so anything that counts or judges a deal (who-traded counts, investor alerts, the agent's view, the deal
backtest's per-event statistics) works on ``consolidate_deals``: deals with the same (date, symbol, normalised client
name, side) become ONE ``DisclosedTrade`` whose quantity and value are the sums, whose price is the
quantity-weighted average and whose ``exchange`` lists the venues ("NSE + BSE"). A deal with no partner is returned
unchanged (the same object), so nothing else changes for it.

Note: the grouping key has no exchange and no bulk/block label, so a client's BULK and BLOCK deals in the same stock, side
and day (on one exchange or both) are merged into one event too. That is deliberate: it is one decision, and the signal
should count it once. The event keeps the first deal's ``source`` label.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from .quiver import DisclosedTrade

_EXCH_ORDER = {"NSE": 0, "BSE": 1}
# Longest first; only the END of the name is mapped. "ABC LTD" and "ABC PVT LTD" stay different clients.
_SUFFIXES = (("LIMITED LIABILITY PARTNERSHIP", "LLP"), ("PRIVATE LIMITED", "PVT_LTD"), ("PVT LIMITED", "PVT_LTD"),
             ("PVT LTD", "PVT_LTD"), ("LLP", "LLP"), ("LIMITED", "LTD"), ("LTD", "LTD"))


def normalise_client(name: str) -> str:
    """Client name as a grouping key (never stored): upper case, . , - / & ( ) ' as spaces, whitespace collapsed,
    and a company-form suffix at the end mapped to PVT_LTD / LTD / LLP. Everything else stays as words."""
    s = re.sub(r"[.,\-/&()']", " ", (name or "").upper())
    s = " ".join(s.split())
    for suf, tag in _SUFFIXES:
        if s == suf:
            return tag
        if s.endswith(" " + suf):
            return s[:-len(suf)] + tag
    return s


norm_client = normalise_client


def short_client(name: str) -> str:
    """Display name for an alert: the normalised name without its company-form tag."""
    n = normalise_client(name)
    for tag in (" PVT_LTD", " LTD", " LLP"):
        if n.endswith(tag):
            return n[:-len(tag)]
    return n


def _lakh(q: float) -> str:
    return f"{q / 1e5:.1f}L" if q >= 1e5 else f"{q:,.0f}"


def event_text(t: DisclosedTrade) -> str:
    """'HRTI bought 21.1L shares across NSE + BSE (₹256.98 average)' for an alert line."""
    raw = t.raw if isinstance(t.raw, dict) else {}
    qty, price = raw.get("qty"), raw.get("watp")
    verb = "bought" if t.transaction == "Purchase" else "sold" if t.transaction == "Sale" else t.transaction.lower()
    who = short_client(t.investor) or t.investor
    if qty is None:
        return t.summary()
    venue = " + ".join(exchanges_of(t))
    avg = f" (₹{price:,.2f} average)" if price else ""
    return f"{who} {verb} {_lakh(float(qty))} shares of {t.ticker} {'across ' if len(exchanges_of(t)) > 1 else 'on '}{venue}{avg}"


def _num(v: Any) -> float | None:
    try:
        x = float(str(v).replace(",", "").replace("₹", "").strip())
    except (TypeError, ValueError):
        return None
    return x if x == x else None


def _symbol(t: DisclosedTrade) -> str:
    raw = t.raw if isinstance(t.raw, dict) else {}
    isin = str(raw.get("isin") or raw.get("ISIN") or "").strip().upper()
    if isin:
        return "ISIN:" + isin
    return (str(raw.get("nse_symbol") or "") or t.ticker or "").upper().strip()


def event_key(t: DisclosedTrade) -> tuple[str, str, str, str]:
    return (t.transaction_date, _symbol(t), norm_client(t.investor), t.transaction)


def _qty_price(t: DisclosedTrade) -> tuple[float | None, float | None]:
    raw = t.raw if isinstance(t.raw, dict) else {}
    qty = _num(raw.get("qty") or raw.get("BD_QTY_TRD"))
    price = _num(raw.get("watp") or raw.get("BD_TP_WATP"))
    if qty is None:
        m = re.match(r"\s*([\d,\.]+)\s*sh", t.size or "")
        qty = _num(m.group(1)) if m else None
    if price is None:
        m = re.search(r"@\s*₹?\s*([\d,\.]+)", t.size or "")
        price = _num(m.group(1)) if m else None
    return qty, price


def exchanges_of(t: DisclosedTrade) -> list[str]:
    """The exchanges behind a (possibly consolidated) deal, NSE first."""
    raw = t.raw if isinstance(t.raw, dict) else {}
    ex = raw.get("exchanges") or [t.exchange]
    return sorted({str(e) for e in ex}, key=lambda e: (_EXCH_ORDER.get(e, 9), e))


def _merge(group: list[DisclosedTrade]) -> DisclosedTrade:
    first = group[0]
    exchanges = sorted({e for t in group for e in exchanges_of(t)}, key=lambda e: (_EXCH_ORDER.get(e, 9), e))
    parts = [_qty_price(t) for t in group]
    ticker = next((t.ticker for t in group if t.ticker and not t.ticker.upper().endswith(".BO")), first.ticker)
    sources = {t.source for t in group}
    base: dict[str, Any] = {
        "exchanges": exchanges, "members": [t.key for t in group], "consolidated": True,
        "clientName": first.investor, "buySell": first.transaction, "date": first.transaction_date,
        "symbol": ticker,
    }
    if all(q is not None for q, _ in parts):
        qty = sum(q for q, _ in parts if q is not None)
        priced = [(q, p) for q, p in parts if q is not None and p is not None]
        if priced and len(priced) == len(parts) and qty > 0:
            value = sum(q * p for q, p in priced)
            wavg = round(value / qty, 2)
            size = f"{qty:,.0f} sh @ ₹{wavg}"
            base.update(qty=qty, watp=wavg, value=round(value, 2))
        else:
            size = f"{qty:,.0f} sh"
            base.update(qty=qty)
    else:
        size = " + ".join(t.size for t in group)
    return DisclosedTrade(
        source=first.source if len(sources) == 1 else sorted(sources)[0],
        investor=max((t.investor for t in group), key=len),
        ticker=ticker, transaction=first.transaction, transaction_date=first.transaction_date,
        report_date=max(t.report_date for t in group), size=size, raw=base, exchange=" + ".join(exchanges))


def consolidate_deals(trades: Iterable[DisclosedTrade]) -> list[DisclosedTrade]:
    """Signals view of ``trades``: one event per (date, symbol, client, side). Order of first appearance is kept.
    Only exchange deals (bulk / block) are grouped; insider and other disclosures pass through untouched."""
    out: list[Any] = []
    groups: dict[tuple[str, str, str, str], list[DisclosedTrade]] = {}
    for t in trades:
        if t.source not in ("bulk", "block", "deals") or not t.transaction_date or not norm_client(t.investor):
            out.append(t)
            continue
        k = event_key(t)
        if k not in groups:
            groups[k] = []
            out.append(k)
        groups[k].append(t)
    return [(_merge(groups[o]) if len(groups[o]) > 1 else groups[o][0]) if isinstance(o, tuple) else o for o in out]
