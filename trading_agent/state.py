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
