"""Kill switch: one file, ``state/HALT``, stops every order path until ``kill-switch --release``.

``engage`` runs the spec's five steps in order and never stops half way:

1. write ``state/HALT`` (persistent; a restart keeps it),
2. stop the watch (the dashboard's own watch thread is stopped; a separate watch service sees the file at its next
   tick and idles: it stays alive, so systemd or Docker do not restart it in a loop, but runs nothing),
3. cancel every open agent order at Groww (reference TA-/VT-/SL-); GTT stop-losses are listed and, only with
   ``cancel_gtt``, cancelled too (by default they stay: they protect the holdings while everything else is stopped),
4. delete the cached Groww token (GROWW_ACCESS_TOKEN in .env cannot be deleted from here; the file blocks its use),
5. alert by Telegram and email through the existing notifier.

While the file exists: every order path refuses (live Groww orders and GTT changes, the practice account, the forward
test), the watch loop idles, no Groww token is requested and the CLI refuses with exit 1 (``kill-switch`` itself and
a few offline commands aside). Only ``kill-switch --release`` removes it. Cancelling is never refused: that is what
the switch is for.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .state import atomic_write
from .timezones import IST

log = logging.getLogger(__name__)

FILE = "HALT"
LOG_FILE = "halt_log.jsonl"
AGENT_PREFIXES = ("TA-", "VT-", "SL-")   # the agent's order references (groww_check.AGENT_REFERENCE_PREFIXES)
ORDER_PAGES, ORDER_PAGE_SIZE = 5, 100
RELEASE_HINT = "python -m trading_agent kill-switch --release"


class Halted(PermissionError):
    """An order path refused because ``state/HALT`` exists."""


def path(state_dir: Path) -> Path:
    return Path(state_dir) / FILE


def status(state_dir: Path | None) -> dict[str, Any] | None:
    """The HALT record ({"at", "by", "cancel_gtt"}), or None when trading is not halted. A file that cannot be read
    still halts: anything at that path counts."""
    if state_dir is None:
        return None
    p = path(state_dir)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        return {"at": None, "by": "unknown"}
    try:
        rec = json.loads(text)
    except ValueError:
        rec = None
    return rec if isinstance(rec, dict) else {"at": None, "by": "unknown"}


def reason(state_dir: Path | None, what: str = "") -> str | None:
    """The refusal text while halted, else None."""
    rec = status(state_dir)
    if rec is None:
        return None
    since = _when(rec.get("at"))
    return (f"HALT is set{f' since {since}' if since else ''} (kill switch, by {rec.get('by') or 'unknown'})"
            f"{f': refusing to {what}' if what else ''}. Release with: {RELEASE_HINT}")


def refuse(state_dir: Path | None, what: str) -> None:
    """Raise Halted while ``state/HALT`` exists."""
    text = reason(state_dir, what)
    if text:
        raise Halted(text)


def _when(at: Any) -> str:
    try:
        return datetime.fromisoformat(str(at)).astimezone(IST).strftime("%d %b %H:%M IST")
    except (TypeError, ValueError):
        return ""


def _log(state_dir: Path, rec: dict[str, Any]) -> None:
    try:
        with open(Path(state_dir) / LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError as e:
        log.warning("could not write the HALT log: %s", e)


# --------------------------------------------------------------------------- #
# Engage / release
# --------------------------------------------------------------------------- #
def engage(settings: Any, *, by: str, cancel_gtt: bool = False, broker_fn: Callable[[], Any] | None = None,
           stop_watch: Callable[[], Any] | None = None, notifier: Any | None = None,
           now: datetime | None = None) -> dict[str, Any]:
    """Run the five steps; returns what each did. Steps 2-5 always run, whatever an earlier one hit.

    ``broker_fn()`` builds the live Groww client (it is only called with GROWW_LIVE_ORDERS on: with live orders off
    nothing at Groww can be an agent order). Engaging again while halted keeps the first time and re-runs 2-5."""
    state_dir = Path(settings.state_dir)
    at = (now or datetime.now(IST)).astimezone(IST)
    prev = status(state_dir)
    rec = {"at": (prev or {}).get("at") or at.isoformat(timespec="seconds"), "by": (prev or {}).get("by") or by,
           "cancel_gtt": bool(cancel_gtt)}
    atomic_write(path(state_dir), json.dumps(rec))                      # 1
    report: dict[str, Any] = {"halted_at": rec["at"], "by": rec["by"], "already": prev is not None}
    _log(state_dir, {"event": "engage", "at": at.isoformat(timespec="seconds"), "by": by, "cancel_gtt": bool(cancel_gtt)})

    report["watch"] = _stop_watch(stop_watch)                           # 2
    report.update(_cancel_at_groww(settings, broker_fn, cancel_gtt))    # 3
    report["token"] = _delete_token(settings)                           # 4
    report["alerted"] = _alert(notifier, "[HALT] kill switch engaged", summary(report))   # 5
    return report


def release(settings: Any, *, by: str, notifier: Any | None = None, now: datetime | None = None) -> bool:
    """Remove ``state/HALT``. True if it was there. Alerts like engaging does."""
    state_dir = Path(settings.state_dir)
    prev = status(state_dir)
    if prev is None:
        return False
    try:
        path(state_dir).unlink()
    except FileNotFoundError:
        return False
    at = (now or datetime.now(IST)).astimezone(IST)
    _log(state_dir, {"event": "release", "at": at.isoformat(timespec="seconds"), "by": by})
    _alert(notifier, "[HALT released] trading may resume",
           f"The kill switch was released by {by} at {at:%d %b %H:%M IST} (it was set {_when(prev.get('at')) or 'earlier'}).\n"
           "Order paths, the watch and the CLI work again. GTT stop-losses cancelled by the switch are re-created "
           "by the next GTT sync; a new Groww token is requested when one is next needed.")
    return True


def _stop_watch(stop_watch: Callable[[], Any] | None) -> str:
    note = "a separate watch service idles from its next tick (it stays running and does nothing until released)"
    if stop_watch is None:
        return note
    try:
        stop_watch()
        return "stopped in this process; " + note
    except Exception as e:  # noqa: BLE001 - the remaining steps must still run
        return f"could not stop this process's watch ({type(e).__name__}: {e}); " + note


def _cancel_at_groww(settings: Any, broker_fn: Callable[[], Any] | None, cancel_gtt: bool) -> dict[str, Any]:
    out: dict[str, Any] = {"orders_cancelled": [], "orders_failed": [], "orders_other": [], "gtts": [],
                           "gtts_cancelled": [], "gtts_failed": [], "gtts_other": []}
    if not (getattr(settings, "use_groww", False) and getattr(settings, "groww_live_orders", False)):
        out["groww"] = "live orders are off: no agent order can be open at Groww, nothing to cancel"
        return out
    if broker_fn is None:
        out["groww"] = "no Groww client given: check the Groww app for open orders"
        return out
    try:
        broker = broker_fn()
    except Exception as e:  # noqa: BLE001 - no token, cool-down, network: say so and carry on
        out["groww"] = f"could not reach Groww ({type(e).__name__}: {e}): CHECK THE GROWW APP FOR OPEN ORDERS"
        return out
    rows: list[dict[str, Any]] = []
    try:
        for page in range(ORDER_PAGES):
            got = broker.order_list(page, ORDER_PAGE_SIZE)
            rows += got
            if len(got) < ORDER_PAGE_SIZE:
                break
    except Exception as e:  # noqa: BLE001
        out["groww"] = f"could not list today's orders ({type(e).__name__}: {e}): CHECK THE GROWW APP"
    from .groww import classify_status
    for r in rows:
        ref = str(r.get("order_reference_id") or "")
        oid = r.get("groww_order_id")
        if classify_status(r.get("order_status")) != "open":
            continue
        line = f"{r.get('transaction_type')} {r.get('quantity')} {r.get('trading_symbol')} (id {oid}, ref {ref or 'none'})"
        if not oid or not ref.startswith(AGENT_PREFIXES):   # placed in the app, or no reference: never cancelled here
            out["orders_other"].append(line)
            continue
        try:
            broker.cancel_order(oid)
            out["orders_cancelled"].append(line)
        except Exception as e:  # noqa: BLE001 - one failure never stops the others
            out["orders_failed"].append(f"{line}: {type(e).__name__}: {e}")
    try:
        gtts = [g for g in broker.gtt_list() if str(g.get("status") or "ACTIVE").upper() == "ACTIVE"]
    except Exception as e:  # noqa: BLE001
        gtts = []
        out["gtts_error"] = f"could not list GTTs ({type(e).__name__}: {e})"
    gone: set[str] = set()
    for g in gtts:
        gid = g.get("smart_order_id")
        ref = str(g.get("reference_id") or "")
        line = f"{g.get('trading_symbol')} qty {g.get('quantity')} trigger {g.get('trigger_price')} (id {gid})"
        out["gtts"].append(line)
        if not gid or not ref.startswith(AGENT_PREFIXES):
            out["gtts_other"].append(line)
            continue
        if not cancel_gtt:
            continue
        try:
            broker.cancel_gtt(gid)
            out["gtts_cancelled"].append(line)
            gone.add(str(gid))
        except Exception as e:  # noqa: BLE001
            out["gtts_failed"].append(f"{line}: {type(e).__name__}: {e}")
    if gone:
        try:
            _forget_gtts(settings, gone)
        except Exception as e:  # noqa: BLE001 - a busy GTT lock never stops the switch before steps 4 and 5
            out["gtts_state_error"] = (f"GTT records not updated ({type(e).__name__}: {e}); the next GTT sync after "
                                       "the release finds these GTTs cancelled at Groww")
    return out


def _forget_gtts(settings: Any, ids: set[str]) -> None:
    """GTTs the switch cancelled leave ``gtt_stops`` (moved to ``gtt_history``), so the sync after a release
    creates fresh ones instead of modifying cancelled ids."""
    from .live import GTT_KEYS, gtt_lock
    from .state import State
    with gtt_lock(settings.state_dir, wait=15.0):   # the switch never waits long: steps 4 and 5 must follow
        st = State(Path(settings.state_dir) / "state.json")
        stops = st.data.get("gtt_stops") or {}
        hist = st.data.setdefault("gtt_history", [])
        for sym in list(stops):
            if str(stops[sym].get("smart_order_id")) in ids:
                hist.append({**stops.pop(sym), "symbol": sym, "status": "CANCELLED",
                             "closed_at": datetime.now(IST).isoformat(timespec="seconds"), "reason": "kill switch"})
        st.data["gtt_history"] = hist[-200:]
        st.save_only(GTT_KEYS)


def _delete_token(settings: Any) -> str:
    from .runner import token_cache
    cache = token_cache(settings)
    existed = Path(cache.path).exists()
    try:
        cache.clear()
    except OSError as e:
        return f"could not delete the cached token ({e})"
    text = "cached Groww token deleted" if existed else "no cached Groww token"
    if getattr(settings, "groww_access_token", None):
        text += "; GROWW_ACCESS_TOKEN in .env is still set (remove it by hand) but is not used while halted"
    return text


def _alert(notifier: Any | None, subject: str, body: str) -> list[str]:
    if notifier is None:
        return []
    try:
        return list(notifier.send(subject, body) or [])
    except Exception as e:  # noqa: BLE001 - the switch must work with the alerts down
        log.warning("kill switch alert not delivered: %s", e)
        return []


def summary(report: dict[str, Any]) -> str:
    """Plain text for the alert and the CLI."""
    lines = [f"Trading HALTED (since {_when(report.get('halted_at')) or 'now'}, by {report.get('by')}).",
             f"Watch: {report.get('watch')}."]
    if report.get("groww"):
        lines.append(f"Groww: {report['groww']}.")
    for key, label in (("orders_cancelled", "Cancelled order"), ("orders_failed", "NOT cancelled"),
                       ("gtts_cancelled", "Cancelled GTT"), ("gtts_failed", "GTT NOT cancelled")):
        lines += [f"{label}: {x}" for x in report.get(key) or []]
    if report.get("orders_other"):
        lines.append(f"{len(report['orders_other'])} open order(s) not placed by the agent or without a reference were "
                     "left: check the Groww app: " + "; ".join(report["orders_other"]))
    if report.get("gtts_other"):
        lines.append(f"{len(report['gtts_other'])} GTT(s) not placed by the agent or without a reference were left: "
                     "check the Groww app: " + "; ".join(report["gtts_other"]))
    if not report.get("groww"):
        if not report.get("orders_cancelled") and not report.get("orders_failed") and not report.get("orders_other"):
            lines.append("No open orders at Groww.")
        kept = [g for g in report.get("gtts") or []
                if g not in (report.get("gtts_cancelled") or []) and g not in (report.get("gtts_other") or [])]
        if kept:
            lines.append(f"GTT stop-losses left active ({len(kept)}): " + "; ".join(kept))
    if report.get("gtts_error"):
        lines.append(report["gtts_error"] + ".")
    if report.get("gtts_state_error"):
        lines.append(report["gtts_state_error"] + ".")
    lines.append(f"Token: {report.get('token')}.")
    lines.append(f"Release with: {RELEASE_HINT}")
    return "\n".join(lines)
