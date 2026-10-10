"""What the dashboard says about safety and freshness: is the watch service alive, is the market open, and which
stop actually protects each real holding. Pure functions over plain data (no network, no orders, no .env), so the
page, the tests and a fake clock all use the same rules.

The watch service writes ``state/watch_alive.json`` ({"at", "tick_started", "tick_finished", "last_error", "every",
"market_window"}); this module only reads it, and treats a missing or unreadable file as "not seen".
"""

from __future__ import annotations

import json
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any

from .timezones import IST

WATCH_FILE = "watch_alive.json"
OPEN, CLOSE = dtime(9, 15), dtime(15, 30)   # NSE cash market, IST
WARN_AFTER_S, BAD_AFTER_S = 5 * 60, 15 * 60   # watch age in market hours: amber, then red

# GTT states that no longer protect anything.
_DEAD_GTT = {"CANCELLED", "CANCELED", "TRIGGERED", "EXPIRED", "FAILED", "REJECTED", "DISABLED", "COMPLETED"}


def _inr(v: float) -> str:
    return "₹" + f"{float(v):,.2f}"


def parse_time(value: Any) -> datetime | None:
    """An ISO timestamp as an aware datetime (a bare one is taken as IST, the watch service's clock)."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=IST)


def is_trading_day(day: Any, holidays: Any | None = None) -> bool:
    if day.weekday() >= 5:
        return False
    if holidays is not None:
        try:
            return bool(holidays.is_trading_day(day))
        except Exception:  # noqa: BLE001 - a holiday list that cannot be read counts weekdays as open
            return True
    return True


def market_open(now: datetime, holidays: Any | None = None) -> bool:
    """09:15 to 15:30 IST on an NSE trading day."""
    n = now.astimezone(IST)
    return is_trading_day(n.date(), holidays) and OPEN <= n.time() <= CLOSE


def last_close(now: datetime, holidays: Any | None = None) -> datetime:
    """The most recent 15:30 IST close at or before ``now`` (skips weekends and holidays)."""
    n = now.astimezone(IST)
    day = n.date()
    for _ in range(14):
        close = datetime.combine(day, CLOSE, tzinfo=IST)
        if is_trading_day(day, holidays) and close <= n:
            return close
        day -= timedelta(days=1)
    return datetime.combine(day, CLOSE, tzinfo=IST)


def read_watch_alive(state_dir: Path) -> dict[str, Any] | None:
    """The watch service's heartbeat file, or None when it does not exist or cannot be read."""
    try:
        data = json.loads((Path(state_dir) / WATCH_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def watch_info(state_dir: Path, now: datetime, is_open: bool) -> dict[str, Any]:
    """{"seen", "at", "age_s", "every", "last_error", "tick_finished", "level"}. ``level`` is "ok", "warn" or "bad"
    from the heartbeat's age during market hours, "idle" outside them, and "unseen" when there is no heartbeat."""
    raw = read_watch_alive(state_dir)
    at = parse_time((raw or {}).get("at"))
    if raw is None or at is None:
        return {"seen": False, "at": None, "age_s": None, "every": None, "last_error": None, "tick_finished": None,
                "level": "unseen"}
    age = max(0, int((now - at).total_seconds()))
    if not is_open:
        level = "idle"
    elif age > BAD_AFTER_S:
        level = "bad"
    elif age > WARN_AFTER_S:
        level = "warn"
    else:
        level = "ok"
    err = raw.get("last_error")
    return {"seen": True, "at": at.isoformat(), "age_s": age, "every": raw.get("every"),
            "last_error": str(err) if err else None, "tick_finished": raw.get("tick_finished"), "level": level}


def freshness(state_dir: Path, now: datetime, *, holidays: Any | None = None, live_orders: bool = False,
              bar_at: str | None = None, deals_at: float | None = None) -> dict[str, Any]:
    """The pieces of the freshness chip: the watch heartbeat, whether the market is open, the last close the
    prices can be from, the newest price bar the page used, and when the deals were last fetched."""
    is_open = market_open(now, holidays)
    deals_age = None if not deals_at else max(0, int(now.timestamp() - deals_at))
    return {
        "now": now.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "market_open": is_open,
        "live_orders": bool(live_orders),
        "watch": watch_info(state_dir, now, is_open),
        "prices": {"last_close": last_close(now, holidays).isoformat(), "bar_at": bar_at},
        "deals": {"age_s": deals_age,
                  "fetched_at": datetime.fromtimestamp(deals_at, timezone.utc).isoformat(timespec="seconds")
                  if deals_at else None},
    }


def _age_text(age_s: int) -> str:
    return f"{age_s // 3600} h" if age_s >= 7200 else f"{max(1, round(age_s / 60))} min" if age_s >= 90 else f"{age_s} s"


def protection(gtt: dict[str, Any] | None, stop_level: float | None, *, live_orders: bool,
               watch: dict[str, Any] | None = None) -> dict[str, Any]:
    """Which stop actually protects one real holding.

    ``gtt`` is the holding's ``gtt_stops`` record (or None), ``stop_level`` its trailing-stop price (or None),
    ``watch`` the ``watch_info`` dict. Returns {"kind": "gtt" | "server" | "none", "tone": "solid" | "neutral" |
    "warn" | "bad", "text": str, "warning": str | None}. The tone is shown with words and an icon, never as colour
    alone."""
    watch = watch or {"level": "unseen", "age_s": None}
    warning = None
    if gtt and gtt.get("last_error"):
        warning = f"GTT problem: {gtt['last_error']}"
    status = str((gtt or {}).get("status") or "ACTIVE").upper()
    if gtt and gtt.get("smart_order_id") and gtt.get("trigger") is not None and status not in _DEAD_GTT:
        return {"kind": "gtt", "tone": "solid", "warning": warning,
                "text": f"GTT at Groww {_inr(gtt['trigger'])} (#{gtt['smart_order_id']})"}
    if not live_orders:
        return {"kind": "none", "tone": "neutral", "warning": warning,
                "text": "Not protected (live orders off — stop is advisory)"}
    if stop_level is None:
        return {"kind": "none", "tone": "bad", "warning": warning,
                "text": "No stop set — nothing sells this holding"}
    text = f"Server stop {_inr(stop_level)} — sells only while the watch service runs"
    level, age = watch.get("level"), watch.get("age_s")
    tone = "neutral"
    if level in ("warn", "bad"):
        tone = level
        text += f" · server not seen for {_age_text(age or 0)}"
    elif level == "unseen":
        tone = "warn"
        text += " · watch service not seen"
    return {"kind": "server", "tone": tone, "text": text, "warning": warning}


def protection_map(positions: list[dict[str, Any]], gtt_stops: dict[str, Any], *, live_orders: bool,
                   watch: dict[str, Any]) -> dict[str, Any]:
    """Protection for every holding the dashboard can name: {"live_orders", "default", "by_symbol"}. A real holding
    with no entry of its own falls back to ``default``."""
    by_symbol: dict[str, Any] = {}
    stops = {str(p.get("symbol", "")).upper(): p.get("stop") for p in positions}
    syms = set(stops) | {str(s).upper() for s in (gtt_stops or {})}
    for sym in sorted(syms):
        if not sym:
            continue
        has_position = sym in stops
        if not live_orders and not (gtt_stops or {}).get(sym):
            continue   # live orders off and no GTT recorded: the default already says it
        by_symbol[sym] = protection((gtt_stops or {}).get(sym), stops.get(sym) if has_position else None,
                                    live_orders=live_orders, watch=watch)
    default = protection(None, None, live_orders=live_orders, watch=watch)
    if live_orders:
        default = {"kind": "none", "tone": "neutral", "warning": None,
                   "text": "No stop recorded (not tradable here, or no price)"}
    return {"live_orders": bool(live_orders), "default": default, "by_symbol": by_symbol}
