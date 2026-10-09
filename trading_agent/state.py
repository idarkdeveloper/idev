"""Persistent memory of which disclosed trades the agent has already handled."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .quiver import DisclosedTrade


class State:
    def __init__(self, path: Path):
        self.path = Path(path)
        if self.path.exists():
            self.data = json.loads(self.path.read_text())
        else:
            self.data = {"seen": {}, "runs": [], "recommendations": []}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2))

    def new_trades(self, trades: Iterable[DisclosedTrade]) -> list[DisclosedTrade]:
        return [t for t in trades if t.key not in self.data["seen"]]

    def mark_seen(self, trades: Iterable[DisclosedTrade]) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for t in trades:
            self.data["seen"][t.key] = {"at": now, "summary": t.summary()}

    def record_run(self, info: dict[str, Any]) -> None:
        info = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **info}
        self.data["runs"] = (self.data["runs"] + [info])[-200:]

    def record_equity(self, equity: float, cash: float, positions: int, min_gap_s: int = 600) -> bool:
        """Append an equity point for the paper-account curve.

        Points closer than ``min_gap_s`` to the previous one replace it, so frequent
        checks don't bloat the history while the latest value stays current.
        """
        now = datetime.now(timezone.utc)
        hist = self.data.setdefault("equity_history", [])
        point = {"at": now.isoformat(timespec="seconds"), "equity": round(float(equity), 2),
                 "cash": round(float(cash), 2), "positions": int(positions)}
        if hist:
            try:
                last = datetime.fromisoformat(hist[-1]["at"])
                if (now - last).total_seconds() < min_gap_s:
                    hist[-1] = point
                    return False
            except (KeyError, ValueError):
                pass
        hist.append(point)
        self.data["equity_history"] = hist[-5000:]
        return True

    def equity_stats(self) -> dict[str, Any] | None:
        return equity_stats(self.data.get("equity_history", []))

    def record_recommendation(self, rec: dict[str, Any]) -> None:
        rec = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **rec}
        self.data["recommendations"] = (self.data["recommendations"] + [rec])[-500:]

    def dismiss_recommendation(self, index: int) -> bool:
        recs = self.data["recommendations"]
        if 0 <= index < len(recs):
            recs[index]["dismissed"] = True
            return True
        return False

    @property
    def seen_count(self) -> int:
        return len(self.data["seen"])


def equity_stats(hist: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Peak, current fall from peak, and the worst fall from peak in the paper-account history."""
    if not hist:
        return None
    peak = trough_peak = hist[0]["equity"]
    peak_at, mdd, mdd_at = hist[0]["at"], 0.0, None
    for p in hist:
        if p["equity"] > peak:
            peak, peak_at = p["equity"], p["at"]
        dd = p["equity"] / peak - 1 if peak > 0 else 0.0
        if dd < mdd:
            mdd, mdd_at, trough_peak = dd, p["at"], peak
    last = hist[-1]["equity"]
    return {"peak": peak, "peak_at": peak_at, "drawdown_now": last / peak - 1 if peak > 0 else 0.0,
            "max_drawdown": mdd, "max_drawdown_at": mdd_at, "max_drawdown_from": trough_peak,
            "points": len(hist)}
