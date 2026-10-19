"""Insider trades across every NSE company: SEBI PIT disclosures (promoters, directors, key managers buying or selling
their own company's shares), read whatever WATCH_SOURCE says.

The watch service refreshes the book every ``EVERY_S`` on its own worker thread (the first run reads up to the NSE
client's per-run cap of filings; each later run only the new ones, as parsed filings are cached by file name) and
stores it in ``state/insider_book.json``. From it:

* ``notable_buys``: promoter, promoter-group, director and key-manager **open-market purchases** at or above
  ``INSIDER_MIN_BUY`` rupees (default ₹1 crore), summed per person and stock: insiders paying market price for more of
  their own company is the classic signal. ESOP allotments, gifts, inter-se transfers and pledges are left out.
* ``for_symbol`` / ``summary``: every insider buy or sell in one stock (pledges and their release left out), for the
  stock page and the portfolio rows.
* ``new_for``: trades not alerted yet in the stocks you hold or that are recommended, for the watch's alert.

Read-only and public: NSE's own filings, no login, no orders.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)

EVERY_S = 15 * 60          # how often the watch refreshes the book
DAYS = 30                  # how far back the book reaches
MIN_BUY_DEFAULT = 1e7      # ₹1 crore: the default size of a "notable" insider purchase
BOOK_FILE = "insider_book.json"
SEEN_KEEP = 5000           # alerted trade keys kept (a month of filings is a few thousand)

_INSIDER = re.compile(r"promoter|director|key\s*manag|\bkmp\b|chairman|managing|ceo|cfo|whole[- ]?time", re.I)
_PROMOTER = re.compile(r"promoter", re.I)
_MARKET = re.compile(r"market", re.I)                       # "Market Purchase", "Market Sale", "On Market"
_OFF_MARKET = re.compile(r"off[- ]?market|inter[- ]?se|gift|esop|esos|allot|transmission|bonus|rights", re.I)
_PLEDGE = re.compile(r"pledge|encumb|invoc|revoc", re.I)
_SIZE = re.compile(r"^\s*([\d,.]+)\s*sh(?:\s*\(₹\s*([\d,.]+)\))?")


def _num(v: Any) -> float | None:
    try:
        x = float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return x if x == x else None   # not NaN


def compact(t: Any) -> dict[str, Any] | None:
    """One insider DisclosedTrade as a plain row for the book, or None when it is not a usable stock trade."""
    raw = t.raw if isinstance(getattr(t, "raw", None), dict) else {}
    sym = str(getattr(t, "ticker", "") or "").upper().strip()
    person = str(getattr(t, "investor", "") or "").strip()
    if not sym or not person:
        return None
    m = _SIZE.match(str(getattr(t, "size", "") or ""))
    qty = _num(raw.get("qty")) if raw.get("qty") is not None else (_num(m.group(1)) if m else None)
    value = _num(raw.get("value")) if raw.get("value") is not None else (_num(m.group(2)) if m and m.group(2) else None)
    category = str(raw.get("category") or raw.get("personCategory") or "").strip()
    mode = str(raw.get("mode") or raw.get("acqMode") or "").strip()
    instrument = str(raw.get("instrument") or raw.get("secType") or "").strip()
    return {"symbol": sym, "company": raw.get("company") or raw.get("company_name") or None, "person": person,
            "category": category or None, "mode": mode or None, "instrument": instrument or None,
            "side": {"Purchase": "buy", "Sale": "sell"}.get(str(getattr(t, "transaction", "")), "other"),
            "qty": qty, "value": value, "price": round(value / qty, 2) if value and qty else None,
            "date": getattr(t, "transaction_date", "") or None, "reported": getattr(t, "report_date", "") or None,
            "filed_at": raw.get("filed_at"), "held_after": _num(raw.get("held_after"))}


def row_key(r: dict[str, Any]) -> str:
    return "|".join(str(r.get(k) or "") for k in ("symbol", "person", "side", "date", "qty", "mode"))


def is_insider(r: dict[str, Any]) -> bool:
    return bool(_INSIDER.search(str(r.get("category") or "")))


def is_pledge(r: dict[str, Any]) -> bool:
    return bool(_PLEDGE.search(f"{r.get('mode') or ''} {r.get('instrument') or ''}"))


def is_market(r: dict[str, Any]) -> bool:
    mode = str(r.get("mode") or "")
    return bool(_MARKET.search(mode)) and not _OFF_MARKET.search(mode)


def is_equity(r: dict[str, Any]) -> bool:
    inst = str(r.get("instrument") or "").lower()
    return not inst or "equity" in inst or "share" in inst


def who(r: dict[str, Any]) -> str:
    """A short label for the person's role: Promoter, Director, Key manager or Insider."""
    c = str(r.get("category") or "")
    if _PROMOTER.search(c):
        return "Promoter"
    if re.search(r"director", c, re.I):
        return "Director"
    if re.search(r"key\s*manag|\bkmp\b", c, re.I):
        return "Key manager"
    return c.strip() or "Insider"


def notable_buys(rows: Iterable[dict[str, Any]], min_value: float = MIN_BUY_DEFAULT) -> list[dict[str, Any]]:
    """Insider open-market purchases of equity, summed per stock and person, at or above ``min_value`` rupees; the
    biggest first."""
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        if r.get("side") != "buy" or not is_insider(r) or not is_market(r) or is_pledge(r) or not is_equity(r):
            continue
        g = groups.setdefault((r["symbol"], r["person"]), {
            "symbol": r["symbol"], "company": r.get("company"), "person": r["person"], "who": who(r),
            "value": 0.0, "qty": 0.0, "trades": 0, "first": r.get("date"), "last": r.get("date")})
        g["value"] += r.get("value") or 0.0
        g["qty"] += r.get("qty") or 0.0
        g["trades"] += 1
        d = r.get("date")
        if d:
            g["first"] = min(filter(None, [g["first"], d]))
            g["last"] = max(filter(None, [g["last"], d]))
    out = [g for g in groups.values() if g["value"] >= min_value]
    for g in out:
        g["avg_price"] = round(g["value"] / g["qty"], 2) if g["qty"] else None
    return sorted(out, key=lambda g: -g["value"])


def trades_in(rows: Iterable[dict[str, Any]], symbol: str) -> list[dict[str, Any]]:
    """Every insider buy or sell in one stock (pledges and their release left out), newest first."""
    sym = symbol.upper().strip()
    got = [r for r in rows if r.get("symbol") == sym and r.get("side") in ("buy", "sell") and not is_pledge(r)]
    return sorted(got, key=lambda r: (r.get("date") or "", r.get("filed_at") or ""), reverse=True)


def summary(rows: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """symbol -> {"buys", "sells", "buy_value", "sell_value", "insider_buys"} over the book, for the portfolio rows."""
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r.get("side") not in ("buy", "sell") or is_pledge(r):
            continue
        s = out.setdefault(r["symbol"], {"buys": 0, "sells": 0, "buy_value": 0.0, "sell_value": 0.0, "market_buys": 0})
        s["buys" if r["side"] == "buy" else "sells"] += 1
        s["buy_value" if r["side"] == "buy" else "sell_value"] += r.get("value") or 0.0
        if r["side"] == "buy" and is_insider(r) and is_market(r):
            s["market_buys"] += 1
    return out


class InsiderBook:
    """``state/insider_book.json``: {"updated", "days", "rows": [compact rows], "seen": [alerted keys], "error"}."""

    def __init__(self, state_dir: Path, min_value: float = MIN_BUY_DEFAULT):
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / BOOK_FILE
        self.min_value = float(min_value)

    def load(self) -> dict[str, Any]:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, d: dict[str, Any]) -> None:
        from .state import atomic_write
        self.state_dir.mkdir(parents=True, exist_ok=True)
        atomic_write(self.path, json.dumps(d, sort_keys=True))

    def due(self, now: datetime) -> bool:
        """EVERY_S after the last read, whether it worked or failed (a failing NSE is not asked every minute)."""
        d = self.load()
        times = []
        for k in ("updated", "error_at"):
            try:
                times.append(datetime.fromisoformat(str(d.get(k))))
            except ValueError:
                pass
        return not times or (now - max(times)).total_seconds() >= EVERY_S

    def refresh(self, client: Any, now: datetime) -> dict[str, Any]:
        """Read the last DAYS of insider filings from ``client`` (an NSEClient) and store them. Records the outcome
        as the "insider_market" run for the Health page. Raises on failure (the book keeps its last rows)."""
        from .health import note_run
        d = self.load()
        try:
            trades = client.insider_trades(DAYS, end=now.date())
        except Exception as e:
            note_run(self.state_dir, "insider_market", ok=False, error=f"{type(e).__name__}: {e}")
            d["error"] = f"{type(e).__name__}: {e}"
            d["error_at"] = now.isoformat(timespec="seconds")
            self._save(d)
            raise
        rows = [r for r in (compact(t) for t in trades) if r is not None]
        cutoff = (now.date() - timedelta(days=DAYS)).isoformat()
        rows = [r for r in rows if not r.get("date") or r["date"] >= cutoff]
        d.update({"updated": now.isoformat(timespec="seconds"), "days": DAYS, "rows": rows, "error": None, "error_at": None})
        self._save(d)
        note_run(self.state_dir, "insider_market", ok=True, rows=len(rows))
        return d

    def new_for(self, symbols: Iterable[str]) -> list[dict[str, Any]]:
        """Trades in ``symbols`` not alerted yet, marking them alerted. On the very first call every trade already in
        the book is marked without being returned, so turning this on never sends a month of old filings."""
        d = self.load()
        rows = d.get("rows") or []
        first = "seen" not in d
        seen = set(d.get("seen") or [])
        want = {s.upper() for s in symbols}
        fresh: list[dict[str, Any]] = []
        for r in rows:
            if r.get("side") not in ("buy", "sell") or is_pledge(r):
                continue
            k = row_key(r)
            if k in seen:
                continue
            if first or r.get("symbol") in want:
                seen.add(k)
                if not first:
                    fresh.append(r)
        d["seen"] = sorted(seen)[-SEEN_KEEP:]
        self._save(d)
        return fresh

    def view(self, symbol: str | None = None) -> dict[str, Any]:
        """What GET /api/insider returns: the notable buys and the per-stock summary, or one stock's trades."""
        d = self.load()
        rows = d.get("rows") or []
        base = {"updated": d.get("updated"), "days": d.get("days") or DAYS, "error": d.get("error"),
                "min_value": self.min_value}
        if symbol:
            return {**base, "symbol": symbol.upper(), "trades": trades_in(rows, symbol)[:50]}
        return {**base, "notable": notable_buys(rows, self.min_value)[:40], "summary": summary(rows)}


def book_for(settings: Any) -> InsiderBook:
    return InsiderBook(settings.state_dir, getattr(settings, "insider_min_buy", MIN_BUY_DEFAULT) or MIN_BUY_DEFAULT)


def alert_text(rows: list[dict[str, Any]]) -> tuple[str, str]:
    """(subject, body) for an alert about insider trades in your stocks."""
    def money(v: Any) -> str:
        if not v:
            return "value not stated"
        return f"₹{v / 1e7:,.2f} cr" if v >= 1e7 else f"₹{v / 1e5:,.1f} lakh" if v >= 1e5 else f"₹{v:,.0f}"
    syms = sorted({r["symbol"] for r in rows})
    subject = f"[INSIDER] {len(rows)} insider trade(s) in {', '.join(syms[:4])}{'…' if len(syms) > 4 else ''}"
    lines = []
    for r in rows:
        qty = f"{r['qty']:,.0f} shares" if r.get("qty") else "shares"
        how = f", {r['mode']}" if r.get("mode") else ""
        lines.append(f"{r['symbol']}: {r['person']} ({who(r)}) {'bought' if r['side'] == 'buy' else 'sold'} {qty}, "
                     f"{money(r.get('value'))}{how}, on {r.get('date') or 'date not stated'}")
    lines.append("")
    lines.append("From NSE insider-trading (PIT) filings. A promoter buying in the market is usually read as a good "
                 "sign and a sale as a mild one; a sale can also be for tax or personal reasons. Not advice.")
    return subject, "\n".join(lines)
