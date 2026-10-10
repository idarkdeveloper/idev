"""Persistent memory of which disclosed trades the agent has already handled."""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .quiver import DisclosedTrade


# One lock for every State save in this process (dashboard requests, the watch thread, checks), so two writers
# never interleave; read-modify-write sequences that must not lose an update hold it across load and save.
STATE_LOCK = threading.RLock()


def atomic_write(path: Path, text: str) -> None:
    """Write beside the file, then swap it in, so a reader never sees half a file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    attempts = 20  # Windows: a reader (or antivirus) can hold the file open briefly; ~2 s in total, then give up
    for attempt in range(attempts):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == attempts - 1:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(min(0.02 * (attempt + 1), 0.2))


class State:
    def __init__(self, path: Path):
        self.path = Path(path)
        if self.path.exists():
            self.data = json.loads(self.path.read_text())
        else:
            self.data = {"seen": {}, "runs": [], "recommendations": []}

    def save(self) -> None:
        with STATE_LOCK:
            atomic_write(self.path, json.dumps(self.data, indent=2))

    def new_trades(self, trades: Iterable[DisclosedTrade]) -> list[DisclosedTrade]:
        return [t for t in trades if t.key not in self.data["seen"]]

    def mark_seen(self, trades: Iterable[DisclosedTrade]) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for t in trades:
            self.data["seen"][t.key] = {"at": now, "summary": t.summary()}
        alerted = self.data.get("alerted_while_blocked")
        if alerted:  # analysed now: the "analysis paused" alert for it has done its job
            for t in trades:
                alerted.pop(t.key, None)

    def record_run(self, info: dict[str, Any]) -> None:
        info = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **info}
        self.data["runs"] = (self.data["runs"] + [info])[-200:]

    def record_equity(self, equity: float, cash: float, positions: int, min_gap_s: int = 600,
                      since: str | None = None, key: str = "equity_history") -> bool:
        """Append an equity point for the paper-account curve.

        Points closer than ``min_gap_s`` to the previous one replace it, so frequent
        checks don't bloat the history while the latest value stays current.
        """
        now = datetime.now(timezone.utc)
        hist = self.equity_history(since, key)
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
        self.data[key] = hist[-5000:]
        return True

    def equity_history(self, since: str | None = None, key: str = "equity_history") -> list[dict[str, Any]]:
        """One curve (``key``: "equity_history" for a real account, "practice_equity" for the practice account),
        dropping its points from before ``since`` (a reset or replaced paper account). Other curves are untouched."""
        hist = self.data.setdefault(key, [])
        if since:
            kept = [p for p in hist if _parse(p.get("at")) >= _parse(since)]
            if len(kept) != len(hist):
                hist = self.data[key] = kept
        return hist

    def equity_stats(self, since: str | None = None, key: str = "equity_history") -> dict[str, Any] | None:
        return equity_stats(self.equity_history(since, key))

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



def _parse(at: Any) -> datetime:
    try:
        d = datetime.fromisoformat(str(at))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
