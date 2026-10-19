"""Big institutions in the bulk and block deals: mutual funds, foreign funds (FPIs), insurers, banks, pension and
sovereign funds, across every stock on NSE and BSE, not only the followed names.

The watch refreshes ``state/big_money.json`` every 15 minutes beside the insider book (insider_watch.py): the last
``DAYS`` of NSE bulk and block deals plus BSE's, kept when ``investors.classify_client`` calls the client an
institution (a broker's or a foreign bank's trading desk is not one). From it:

* ``notable``: institutional deals at or above ``BIG_MONEY_MIN`` rupees (default ₹10 crore), summed per client, stock
  and side, the biggest first;
* ``trades_in`` / ``summary``: one stock's institutional deals and a per-stock count, for the stock page and the
  portfolio rows;
* ``new_for``: deals not alerted yet in the stocks you hold or that are recommended.

Read-only, public exchange files: no login, no orders.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)

DAYS = 30
MIN_DEFAULT = 1e8          # ₹10 crore
BOOK_FILE = "big_money.json"
EVERY_S = 15 * 60
SEEN_KEEP = 5000
_SIZE = re.compile(r"^\s*([\d,.]+)\s*sh(?:\s*@\s*₹\s*([\d,.]+))?")


def _num(v: Any) -> float | None:
    try:
        x = float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return x if x == x else None


def compact(t: Any) -> dict[str, Any] | None:
    """One bulk/block DisclosedTrade as a plain row, or None when it is not an institution's deal."""
    from .investors import INSTITUTION, classify_client
    client = str(getattr(t, "investor", "") or "").strip()
    sym = str(getattr(t, "ticker", "") or "").upper().strip()
    if not client or not sym or classify_client(client, getattr(t, "source", "")) != INSTITUTION:
        return None
    m = _SIZE.match(str(getattr(t, "size", "") or ""))
    qty = _num(m.group(1)) if m else None
    price = _num(m.group(2)) if m and m.group(2) else None
    raw = t.raw if isinstance(getattr(t, "raw", None), dict) else {}
    name = raw.get("BD_SCRIP_NAME") or raw.get("name") or raw.get("Security Name") or raw.get("security") or None
    return {"symbol": sym, "company": str(name).strip() if name else None, "client": client,
            "side": {"Purchase": "buy", "Sale": "sell"}.get(str(getattr(t, "transaction", "")), "other"),
            "qty": qty, "price": price, "value": round(qty * price, 2) if qty and price else None,
            "date": getattr(t, "transaction_date", "") or None, "kind": getattr(t, "source", "") or None,
            "exchange": getattr(t, "exchange", "NSE") or "NSE"}


def row_key(r: dict[str, Any]) -> str:
    return "|".join(str(r.get(k) or "") for k in ("symbol", "client", "side", "date", "qty", "price", "exchange"))


def notable(rows: Iterable[dict[str, Any]], min_value: float = MIN_DEFAULT) -> list[dict[str, Any]]:
    """Institutional deals summed per client, stock and side, at or above ``min_value`` rupees; the biggest first."""
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for r in rows:
        if r.get("side") not in ("buy", "sell"):
            continue
        g = groups.setdefault((r["symbol"], r["client"], r["side"]), {
            "symbol": r["symbol"], "company": r.get("company"), "client": r["client"], "side": r["side"],
            "value": 0.0, "qty": 0.0, "deals": 0, "first": r.get("date"), "last": r.get("date"), "exchanges": []})
        g["value"] += r.get("value") or 0.0
        g["qty"] += r.get("qty") or 0.0
        g["deals"] += 1
        if r.get("exchange") and r["exchange"] not in g["exchanges"]:
            g["exchanges"].append(r["exchange"])
        d = r.get("date")
        if d:
            g["first"] = min(filter(None, [g["first"], d]))
            g["last"] = max(filter(None, [g["last"], d]))
    out = [g for g in groups.values() if g["value"] >= min_value]
    for g in out:
        g["avg_price"] = round(g["value"] / g["qty"], 2) if g["qty"] else None
    return sorted(out, key=lambda g: -g["value"])


def trades_in(rows: Iterable[dict[str, Any]], symbol: str) -> list[dict[str, Any]]:
    sym = symbol.upper().strip()
    got = [r for r in rows if r.get("symbol") == sym and r.get("side") in ("buy", "sell")]
    return sorted(got, key=lambda r: r.get("date") or "", reverse=True)


def summary(rows: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r.get("side") not in ("buy", "sell"):
            continue
        s = out.setdefault(r["symbol"], {"buys": 0, "sells": 0, "buy_value": 0.0, "sell_value": 0.0})
        s["buys" if r["side"] == "buy" else "sells"] += 1
        s["buy_value" if r["side"] == "buy" else "sell_value"] += r.get("value") or 0.0
    return out


class BigMoneyBook:
    """``state/big_money.json``: {"updated", "days", "rows", "seen", "error"}."""

    def __init__(self, state_dir: Path, min_value: float = MIN_DEFAULT):
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
        """Read the last DAYS of NSE bulk and block deals (and BSE's, when the client has BSE on) and keep the
        institutions' deals. Recorded as the "big_money" run. Raises when NSE's files cannot be read."""
        from .health import note_run
        d = self.load()
        try:
            trades = list(client.historical_deals(DAYS, "bulk")) + list(client.historical_deals(DAYS, "block"))
        except Exception as e:
            note_run(self.state_dir, "big_money", ok=False, error=f"{type(e).__name__}: {e}")
            d.update({"error": f"{type(e).__name__}: {e}", "error_at": now.isoformat(timespec="seconds")})
            self._save(d)
            raise
        bse = getattr(client, "_bse_rows", None)
        if bse is not None:
            trades += list(bse("deals", DAYS, None))   # never raises: BSE problems leave NSE's deals
        rows = [r for r in (compact(t) for t in trades) if r is not None]
        cutoff = (now.date() - timedelta(days=DAYS)).isoformat()
        rows = [r for r in rows if not r.get("date") or r["date"] >= cutoff]
        seen_rows: dict[str, dict[str, Any]] = {}
        for r in rows:   # NSE can list one large trade as both a bulk and a block deal
            seen_rows.setdefault(row_key(r), r)
        rows = list(seen_rows.values())
        d.update({"updated": now.isoformat(timespec="seconds"), "days": DAYS, "rows": rows, "error": None, "error_at": None})
        self._save(d)
        note_run(self.state_dir, "big_money", ok=True, rows=len(rows))
        return d

    def new_for(self, symbols: Iterable[str]) -> list[dict[str, Any]]:
        """Deals in ``symbols`` not alerted yet, marking them alerted; the first call only marks what is there."""
        d = self.load()
        first = "seen" not in d
        seen = set(d.get("seen") or [])
        want = {s.upper() for s in symbols}
        fresh: list[dict[str, Any]] = []
        for r in d.get("rows") or []:
            k = row_key(r)
            if k in seen or r.get("side") not in ("buy", "sell"):
                continue
            if first or r.get("symbol") in want:
                seen.add(k)
                if not first:
                    fresh.append(r)
        d["seen"] = sorted(seen)[-SEEN_KEEP:]
        self._save(d)
        return fresh

    def view(self, symbol: str | None = None) -> dict[str, Any]:
        d = self.load()
        rows = d.get("rows") or []
        base = {"updated": d.get("updated"), "days": d.get("days") or DAYS, "error": d.get("error"),
                "min_value": self.min_value}
        if symbol:
            return {**base, "symbol": symbol.upper(), "trades": trades_in(rows, symbol)[:50]}
        return {**base, "notable": notable(rows, self.min_value)[:60], "summary": summary(rows)}


def book_for(settings: Any) -> BigMoneyBook:
    return BigMoneyBook(settings.state_dir, getattr(settings, "big_money_min", MIN_DEFAULT) or MIN_DEFAULT)


def alert_text(rows: list[dict[str, Any]]) -> tuple[str, str]:
    def money(v: Any) -> str:
        return "value not stated" if not v else f"₹{v / 1e7:,.2f} cr" if v >= 1e7 else f"₹{v / 1e5:,.1f} lakh"
    syms = sorted({r["symbol"] for r in rows})
    subject = f"[INSTITUTION] {len(rows)} fund deal(s) in {', '.join(syms[:4])}{'…' if len(syms) > 4 else ''}"
    lines = [f"{r['symbol']}: {r['client']} {'bought' if r['side'] == 'buy' else 'sold'} "
             f"{(r.get('qty') or 0):,.0f} shares at ₹{(r.get('price') or 0):,.2f} ({money(r.get('value'))}), "
             f"{r.get('kind') or 'deal'} deal on {r.get('exchange') or 'NSE'}, {r.get('date') or ''}" for r in rows]
    lines += ["", "From the exchanges' bulk and block deal files (institutions: mutual funds, foreign funds, insurers, "
                  "banks). A fund buying a large block is usually a researched view; it can also be rebalancing. Not advice."]
    return subject, "\n".join(lines)
