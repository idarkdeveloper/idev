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
