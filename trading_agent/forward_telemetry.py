"""Forward-test telemetry and the friction gate.

The forward test only earns live money if the screen beats its index fund *after* what trading it costs. At each
monthly rebalance this measures that friction, against fixed targets:

* **Turnover a year**: half of what each rebalance bought and sold, as a share of the account, averaged over the
  monthly rebalances and times 12. The first rebalance builds the portfolio from cash and does not count. Target
  under 250% a year (1.5x the 168% the adopted rules measured in research).
* **Active share (approximate)**: how different the holdings are from the fund, ``1 - sum(min(w_portfolio,
  w_fund))``. The fund's real weights are not published daily, so they are approximated from the index members'
  market caps (full, not free-float) when at least 80% of them have one, else by equal weights (and labelled so).
  Target 75-90%: below 75% it is a costly copy of the fund, above 90% it barely resembles it.
* **Slippage**: the paper account fills at the close, so it has none. Real Groww fills are measured against the last
  price when the order was placed (buys paying more and sells getting less count against you). Until there are
  real fills, the cost model's assumption is shown. Target 15 bps or less a side.
* **Charges as a share of the return above the fund**: what the strategy paid in charges divided by its return over
  the fund before those charges. Target under 15%.

**Friction gate.** Once two monthly rebalances have followed the start, charges above 20% of the return above the
fund (or no return above the fund at all to pay for them) trip the gate: one Telegram and email alert a month, an
amber banner on the Live and Demo pages, and ``groww-check --live-test`` refuses without
``--confirm-friction-override``. It never changes a setting by itself.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable, Iterable

log = logging.getLogger(__name__)

# 1.5x the 168% a year the adopted rules measured in research (B2-B4); the India test ran 244%. Set 22 Oct 2026: the
# earlier 120% predated the research rules and would have flagged a strategy behaving as designed.
TURNOVER_MAX = 2.50
ACTIVE_SHARE_RANGE = (0.75, 0.90)
SLIPPAGE_MAX_BPS = 15.0
CHARGE_SHARE_WARN = 0.15
CHARGE_SHARE_GATE = 0.20
ARM_AFTER = 2            # monthly rebalances after the start before the gate can trip
CAP_COVERAGE = 0.80      # share of members needing a market cap before cap weights are used


def active_share(values: dict[str, float], equity: float, members: Iterable[str],
                 caps: dict[str, float | None] | None = None) -> dict[str, Any] | None:
    """``values``: market value per holding; ``equity``: the whole account (cash is a position the fund doesn't hold).
    Returns {"value", "basis"} or None without members or equity."""
    names = sorted({str(m).upper() for m in members})
    if not names or not equity or equity <= 0:
        return None
    known = {str(k).upper(): float(v) for k, v in (caps or {}).items()
             if isinstance(v, (int, float)) and v > 0 and str(k).upper() in names}
    if len(known) >= CAP_COVERAGE * len(names):
        total = sum(known.values())
        fund = {k: v / total for k, v in known.items()}
        basis = f"market-cap weights ({len(known)} of {len(names)} members)"
    else:
        fund = {k: 1 / len(names) for k in names}
        basis = "equal weights (too few market caps)"
    overlap = sum(min(max(v, 0.0) / equity, fund.get(s.upper(), 0.0)) for s, v in values.items())
    return {"value": round(1 - overlap, 4), "basis": basis}


def member_caps(symbols: Iterable[str], provider: Any) -> dict[str, float | None]:
    """Market caps from a fundamentals provider (``get(symbol)`` -> {"market_cap": ...}); a failure is a gap."""
    out: dict[str, float | None] = {}
    for s in symbols:
        try:
            out[s.upper()] = (provider.get(s) or {}).get("market_cap")
        except Exception as e:  # noqa: BLE001 - one missing cap only weakens the approximation
            log.debug("no market cap for %s: %s", s, e)
            out[s.upper()] = None
    return out


def rebalance_turnover(entry: dict[str, Any], equity: float | None) -> float | None:
    """One-way turnover of one rebalance: (bought + sold) / 2 / account value."""
    eq = entry.get("equity") or equity
    if not eq:
        return None
    traded = sum(abs(float(t["qty"]) * float(t["price"])) for t in entry.get("trades", [])
                 if "error" not in t and t.get("price") and t.get("qty"))
    return traded / 2 / float(eq)


def slippage(live_orders: Iterable[dict[str, Any]], model_bps: float = 15.0) -> dict[str, Any]:
    """Average one-way slippage of real filled orders against the last price when placed, in bps (positive = cost)."""
    rows = []
    for o in live_orders:
        try:
            fill, ref, qty = float(o.get("average_fill_price") or 0), float(o.get("ltp") or 0), float(o.get("filled_quantity") or 0)
        except (TypeError, ValueError):
            continue
        if fill <= 0 or ref <= 0 or qty <= 0:
            continue
        sign = 1.0 if str(o.get("side", "")).lower() == "buy" else -1.0
        rows.append(sign * (fill - ref) / ref * 10_000)
    if not rows:
        return {"bps": None, "orders": 0, "model_bps": model_bps,
                "basis": f"no real fills yet; the paper account fills at the close and the cost model assumes {model_bps:g} bps"}
    return {"bps": round(sum(rows) / len(rows), 1), "orders": len(rows), "model_bps": model_bps,
            "basis": f"{len(rows)} real Groww fill{'s' if len(rows) != 1 else ''} against the last price when placed"}


def _check(name: str, value: Any, target: str, status: str, text: str) -> dict[str, Any]:
    return {"name": name, "value": value, "target": target, "status": status, "text": text}


def telemetry(data: dict[str, Any], charges_paid: float, live_orders: Iterable[dict[str, Any]] = (),
              model_slippage_bps: float = 15.0) -> dict[str, Any]:
    """Every figure above from the forward state (``ForwardTest.data``) plus what the paper account paid."""
    rebs, hist = data.get("rebalances", []), data.get("history", [])
    by_date = {p["date"]: p.get("equity") for p in hist}
    per = []
    for i, e in enumerate(rebs):
        t = rebalance_turnover(e, by_date.get(e.get("date")) or data.get("capital"))
        per.append({"date": e.get("date"), "build": i == 0, "turnover": None if t is None else round(t, 4),
                    "charges": e.get("charges"), "active_share": (e.get("active_share") or {}).get("value")})
    monthly = [p["turnover"] for p in per if not p["build"] and p["turnover"] is not None]
    turnover_year = round(sum(monthly) / len(monthly) * 12, 4) if monthly else None
    act = next((e["active_share"] for e in reversed(rebs) if e.get("active_share")), None)
    slip = slippage(live_orders, model_slippage_bps)

    last = hist[-1] if hist else None
    share = gross = None
    no_excess = False
    if last and last.get("bench") and last.get("equity"):
        gross = round(last["equity"] - last["bench"] + charges_paid, 2)
        if gross > 0:
            share = round(charges_paid / gross, 4)
        else:
            no_excess = charges_paid > 0
    after = max(len(rebs) - 1, 0)

    checks = []
    if turnover_year is None:
        checks.append(_check("Turnover a year", None, "under 250%", "n/a", "after the first monthly rebalance"))
    else:
        checks.append(_check("Turnover a year", turnover_year, "under 250%",
                             "ok" if turnover_year <= TURNOVER_MAX else "warn",
                             f"{turnover_year * 100:.0f}% a year from {len(monthly)} monthly rebalance"
                             f"{'s' if len(monthly) != 1 else ''}"))
    if act is None:
        checks.append(_check("Active share (approx.)", None, "75-90%", "n/a", "measured at the next rebalance"))
    else:
        lo, hi = ACTIVE_SHARE_RANGE
        checks.append(_check("Active share (approx.)", act["value"], "75-90%",
                             "ok" if lo <= act["value"] <= hi else "warn",
                             f"{act['value'] * 100:.0f}% vs {data.get('benchmark', 'the fund')}, {act['basis']}"))
    if slip["bps"] is None:
        checks.append(_check("Slippage a side", None, "15 bps or less", "n/a", slip["basis"]))
    else:
        checks.append(_check("Slippage a side", slip["bps"], "15 bps or less",
                             "ok" if slip["bps"] <= SLIPPAGE_MAX_BPS else "warn", f"{slip['bps']:+.1f} bps, {slip['basis']}"))
    if share is not None:
        checks.append(_check("Charges / return above the fund", share, "under 15%",
                             "ok" if share <= CHARGE_SHARE_WARN else "warn",
                             f"₹{charges_paid:,.0f} of ₹{gross:,.0f} above the fund before charges"))
    elif no_excess:
        checks.append(_check("Charges / return above the fund", None, "under 15%", "warn",
                             f"no return above the fund before charges (₹{gross:,.0f}) to pay ₹{charges_paid:,.0f} of charges"))
    else:
        checks.append(_check("Charges / return above the fund", None, "under 15%", "n/a", "needs a fund price"))

    armed = after >= ARM_AFTER
    over = no_excess or (share is not None and share > CHARGE_SHARE_GATE)
    tripped = armed and over
    if tripped:
        why = (f"charges are {share * 100:.0f}% of the return above {data.get('benchmark', 'the fund')} (limit 20%)"
               if share is not None else f"there is no return above {data.get('benchmark', 'the fund')} to pay the charges")
        text = (f"Friction gate: {why} after {after} monthly rebalances. The live test needs "
                "--confirm-friction-override. Nothing was changed.")
    elif not armed:
        text = f"Friction gate arms after {ARM_AFTER} monthly rebalances ({after} so far)."
    else:
        text = "Friction gate clear: charges are within 20% of the return above the fund."
    return {"rebalances_after_start": after, "turnover_year": turnover_year, "per_rebalance": per,
            "active_share": act, "slippage": slip, "charge_share": share, "gross_excess": gross,
            "charges_paid": round(charges_paid, 2), "checks": checks,
            "gate": {"armed": armed, "tripped": tripped, "threshold": CHARGE_SHARE_GATE, "text": text}}


def format_telemetry(t: dict[str, Any] | None) -> str:
    if not t:
        return ""
    mark = {"ok": "ok  ", "warn": "WARN", "n/a": "n/a "}
    lines = ["Friction telemetry (targets in brackets):"]
    for c in t["checks"]:
        lines.append(f"  {mark.get(c['status'], c['status'])} {c['name']} [{c['target']}]: {c['text']}")
    lines.append(("  " if not t["gate"]["tripped"] else "  !!! ") + t["gate"]["text"])
    return "\n".join(lines)


def live_orders_from(state_dir: Path) -> list[dict[str, Any]]:
    """Real Groww orders from state.json, read-only (empty when unreadable)."""
    try:
        return list(json.loads((Path(state_dir) / "state.json").read_text(encoding="utf-8")).get("live_orders", []))
    except (OSError, ValueError, AttributeError):
        return []


def gate(state_dir: Path, *, loader: Callable[[Path], Any] | None = None) -> dict[str, Any] | None:
    """The friction gate as stored on disk now (no network), or None without a forward account."""
    d = Path(state_dir) / "forward"
    files = sorted(f for f in d.glob("*.json") if not f.stem.endswith("_broker")) if d.exists() else []
    if not files:
        return None
    try:
        if loader is not None:
            s = loader(files[0])
        else:
            from .forward import ForwardTest
            s = ForwardTest(state_dir, universe=files[0].stem.upper(), price_fn=None).summary()
        return (s.get("telemetry") or {}).get("gate")
    except Exception as e:  # noqa: BLE001 - an unreadable account cannot vouch for itself
        return {"armed": True, "tripped": True, "threshold": CHARGE_SHARE_GATE,
                "text": f"Friction gate: the forward account is unreadable ({type(e).__name__}: {e})."}
