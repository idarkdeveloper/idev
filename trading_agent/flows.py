"""FII/DII daily cash-market flows from NSE's FII/DII trade report.

Source: the JSON behind https://www.nseindia.com/reports/fii-dii, ``api/fiidiiTradeReact`` (public, same cookie/session
handling as every other NSE call: it goes through ``NSEClient._get``). It returns the LATEST session only (two rows,
"FII/FPI" and "DII", values in rupees crore as strings); it takes no date parameter, so a history can only be built
from now on, one fetch per trading day. NSE publishes in the evening (about 18:00 to 19:00 IST) and the figures are
provisional.

The series is kept in ``<state_dir>/flows.json`` as ``{"days": {date: row}, "attempts": {date: ...}}``; ``attempts``
is the once-per-day claim, so a failing fetch is not retried every watch tick.

Derived: 5-day cumulative net for FII, DII and combined, and the streak of consecutive negative combined days. This is
information for the morning email only; it is NOT a trading rule (see the registered B3 variant in the research spec).
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from .state import atomic_write
from .timezones import IST

log = logging.getLogger(__name__)

REFERER = "https://www.nseindia.com/reports/fii-dii"
PATH = "api/fiidiiTradeReact"
FETCH_AFTER = time(19, 0)   # NSE posts between about 18:00 and 19:00 IST
MAX_TRIES = 6
RETRY_GAP = timedelta(minutes=20)
_LOCK = threading.Lock()
_MONTHS = {m: i for i, m in enumerate(("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), 1)}


def _num(v: Any) -> float | None:
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _day(s: str) -> str | None:
    try:
        d, m, y = str(s).strip().split("-")
        return date(int(y), _MONTHS[m.upper()[:3]], int(d)).isoformat()
    except (ValueError, KeyError):
        return None


def parse_fii_dii(payload: Any) -> dict[str, Any]:
    """One session's row: {date, fii_buy, fii_sell, fii_net, dii_buy, dii_sell, dii_net} in rupees crore.
    Raises ValueError when the payload is not the FII/DII report (so a changed or blocked response is not stored)."""
    if not isinstance(payload, list):
        raise ValueError("FII/DII response is not a list")
    row: dict[str, Any] = {}
    for r in payload:
        cat = str(r.get("category") or "").upper()
        who = "fii" if cat.startswith("FII") or "FPI" in cat else "dii" if cat.startswith("DII") else None
        d = _day(r.get("date"))
        buy, sell = _num(r.get("buyValue")), _num(r.get("sellValue"))
        if who is None or d is None or buy is None or sell is None:
            continue
        net = _num(r.get("netValue"))
        row.setdefault("date", d)
        if row["date"] != d:
            raise ValueError("FII/DII rows are for different dates")
        row[f"{who}_buy"], row[f"{who}_sell"] = buy, sell
        row[f"{who}_net"] = round(net if net is not None else buy - sell, 2)
    need = {"date", "fii_buy", "fii_sell", "fii_net", "dii_buy", "dii_sell", "dii_net"}
    if not need <= row.keys():
        raise ValueError("FII/DII response lacks an FII or a DII row")
    return row


class FlowStore:
    """The stored daily series plus the fetch."""

    def __init__(self, state_dir: Path):
        self.path = Path(state_dir) / "flows.json"

    def _load(self) -> dict[str, Any]:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(d, dict) and isinstance(d.get("days"), dict):
                d.setdefault("attempts", {})
                return d
        except (OSError, ValueError):
            pass
        return {"days": {}, "attempts": {}}

    def _save(self, d: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        d["attempts"] = dict(sorted(d["attempts"].items())[-30:])
        d["days"] = dict(sorted(d["days"].items())[-800:])
        atomic_write(self.path, json.dumps(d, indent=1))

    def rows(self) -> list[dict[str, Any]]:
        """Stored sessions, oldest first."""
        return [v for _, v in sorted(self._load()["days"].items())]

    def add(self, row: dict[str, Any]) -> bool:
        """Store a session; True if it was new. A day already stored is not overwritten (the first published figure
        is what a trader saw)."""
        with _LOCK:
            d = self._load()
            if row["date"] in d["days"]:
                return False
            d["days"][row["date"]] = row
            self._save(d)
            return True

    def fetch(self, client: Any) -> dict[str, Any]:
        """Ask NSE for the latest session and store it. ``client`` is an NSEClient. Raises on a bad response."""
        row = parse_fii_dii(client._get(PATH, referer=REFERER))
        self.add(row)
        return row

    def due(self, now: datetime, calendar: Any | None = None) -> bool:
        """True when the fetch should run: a trading day, at or after 19:00 IST, not done today, with at most MAX_TRIES
        tries a day, RETRY_GAP apart (NSE sometimes posts late; until it does the API still serves the last session)."""
        now = now.astimezone(IST) if now.tzinfo else now
        day = now.date()
        if now.time() < FETCH_AFTER:
            return False
        if calendar is not None and hasattr(calendar, "is_trading_day"):
            if not calendar.is_trading_day(day):
                return False
        elif day.weekday() >= 5:
            return False
        a = self._load()["attempts"].get(day.isoformat())
        if not a:
            return True
        if a.get("done") or a.get("tries", 0) >= MAX_TRIES:
            return False
        try:
            last = datetime.fromisoformat(a["last"])
            last = last if last.tzinfo else last.replace(tzinfo=IST)
        except (KeyError, ValueError):
            return True
        return now - last >= RETRY_GAP

    def claim(self, now: datetime, outcome: str, done: bool) -> None:
        with _LOCK:
            d = self._load()
            key = now.date().isoformat()
            a = d["attempts"].get(key, {})
            d["attempts"][key] = {"tries": a.get("tries", 0) + 1, "done": done, "last": now.isoformat(timespec="seconds"),
                                  "outcome": outcome}
            self._save(d)

    def tick(self, client: Any, now: datetime, calendar: Any | None = None) -> dict[str, Any] | None:
        """For the watch loop: fetch once per trading day after 19:00 IST. Never raises. The day counts as done only when
        NSE returns today's session; otherwise it is retried a few times."""
        if client is None or not self.due(now, calendar):
            return None
        try:
            row = self.fetch(client)
            fresh = row["date"] == (now.astimezone(IST) if now.tzinfo else now).date().isoformat()
            self.claim(now, "ok" if fresh else "still " + row["date"], fresh)
            return row if fresh else None
        except Exception as e:  # noqa: BLE001 - a flows failure must never stop the watch
            log.warning("FII/DII flows unavailable: %s", e)
            self.claim(now, "error: " + type(e).__name__, False)
            return None


def summarize(rows: list[dict[str, Any]], window: int = 5) -> dict[str, Any] | None:
    """Latest session, the cumulative net of the last ``window`` stored sessions and the negative-combined streak."""
    if not rows:
        return None
    last = rows[-1]
    recent = rows[-window:]
    streak = 0
    for r in reversed(rows):
        if r["fii_net"] + r["dii_net"] < 0:
            streak += 1
        else:
            break
    return {"date": last["date"], "fii_net": last["fii_net"], "dii_net": last["dii_net"],
            "combined_net": round(last["fii_net"] + last["dii_net"], 2),
            "days": len(recent),
            "fii_net_5d": round(sum(r["fii_net"] for r in recent), 2),
            "dii_net_5d": round(sum(r["dii_net"] for r in recent), 2),
            "combined_net_5d": round(sum(r["fii_net"] + r["dii_net"] for r in recent), 2),
            "negative_streak": streak}


def _cr(v: float) -> str:
    sign = "−" if v < 0 else "+"
    return f"{sign}₹{abs(round(v)):,.0f} cr"


def flows_line(rows: list[dict[str, Any]], today: date | None = None, max_age_days: int = 5) -> str | None:
    """'FII −₹3,569 cr, DII +₹4,743 cr on Fri 9 Oct (provisional NSE figures); 5-day net FII −₹8,100 cr', or None
    when nothing is stored or the newest session is too old to call current."""
    s = summarize(rows)
    if s is None:
        return None
    d = date.fromisoformat(s["date"])
    if today is not None and (today - d).days > max_age_days:
        return None
    when = f"{d.strftime('%a')} {d.day} {d.strftime('%b')}"
    tail = f"; {s['days']}-day net FII {_cr(s['fii_net_5d'])}" if s["days"] >= 2 else ""
    return f"FII {_cr(s['fii_net'])}, DII {_cr(s['dii_net'])} on {when} (provisional NSE figures){tail}"
