"""A replay trial: three practice portfolios that start on a past date.

Files in ``state/replay/<slug>/``: ``trial.json`` (settings, clock, curves, logs) and one
paper-broker file per portfolio (``you.json``, ``agent.json``, ``nifty.json``). Fills are
stamped with the replay date. Nothing here can reach a real broker.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any, Callable, Iterator

from ..broker import LocalPaperBroker
from ..costs import cost_model_for
from ..forward import rebalance_to
from ..nse import check_ticker
from .clock import EARLIEST_START, ClockedPrices, ReplayClock

BENCHMARKS = {"NIFTYMIDCAP150": "MID150BEES", "NIFTY50": "NIFTYBEES", "NIFTY100": "NIFTYBEES",
              "NIFTY200": "NIFTYBEES", "NIFTY500": "NIFTYBEES", "NIFTYSMALLCAP250": "HDFCSML250"}
PORTFOLIOS = ("you", "agent", "nifty")


class ReplayUniverse:
    """Today's constituent list (names) plus the point-in-time membership history."""

    def __init__(self, name: str, state_dir: Path):
        from ..index_history import point_in_time
        from ..screen import load_universe

        self.name = name.upper()
        self.current = load_universe(self.name)
        self.membership = point_in_time(self.name, [m["symbol"] for m in self.current], Path(state_dir))
        if self.membership is None:
            raise ValueError(f"no membership history for {self.name}; pick another universe")
        self._names = {m["symbol"]: m for m in self.current}
        self._state_dir = Path(state_dir)
        self._names_filled = False

    def _lookup_names(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        from ..instruments import CompanyNames
        return CompanyNames(self._state_dir / "cache").lookup(symbols)

    def members_on(self, day: str) -> list[dict[str, str]]:
        if not self._names_filled:  # past members that have since left the index have no name in today's list
            fill_names(self._names, sorted(self.membership.ever_members(EARLIEST_START)), self._lookup_names)
            self._names_filled = True
        return [{"symbol": s, "name": self._names.get(s, {}).get("name", ""),
                 "industry": self._names.get(s, {}).get("industry", "")}
                for s in sorted(self.membership.members_on(day))]


def fill_names(names: dict[str, dict[str, Any]], symbols: list[str],
               lookup: Callable[[list[str]], dict[str, dict[str, Any]]]) -> None:
    """Give every symbol a name entry, asking ``lookup`` once for the ones missing.
    Names are a nicety: a failed lookup leaves them blank instead of failing."""
    missing = [s for s in symbols if not (names.get(s) or {}).get("name") and not (names.get(s) or {}).get("looked_up")]
    if not missing:
        return
    try:
        found = lookup(missing) or {}
    except Exception:  # noqa: BLE001
        found = {}
    for s in missing:
        names[s] = {**names.get(s, {}), "name": (found.get(s) or {}).get("name") or "",
                    "industry": (names.get(s) or {}).get("industry", ""), "looked_up": True}


def default_screen(members: list[dict[str, str]], prices: Any, top: int) -> list[dict[str, Any]]:
    from ..screen import run_screen
    return run_screen(members, prices, top=top, workers=8)["top"]


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")[:40]
    if not s:
        raise ValueError("give the replay a name")
    return s


class Trial:
    FILES = ("trial.json", "you.json", "agent.json", "nifty.json")

    def __init__(self, root: Path, data: dict[str, Any], source: Any, universe_obj: Any,
                 screen_fn: Callable[..., list[dict[str, Any]]] | None = None):
        self.root, self.data, self.source, self.universe = Path(root), data, source, universe_obj
        self.screen_fn = screen_fn or default_screen
        self.cost_model = cost_model_for("in")
        self.clock = ReplayClock(data["clock"])
        # price_basis "raw" (every replay made since the dividend fix): fills, stops, values and share counts use the real
        # (split-adjusted) close; dividends arrive as events on their ex-dates (engine._dividends), in cash or reinvested.
        # An older save has no marker and keeps its original basis (reinvest = dividend-adjusted prices), so its saved
        # fills and positions stay consistent with the prices it is valued at.
        self.raw_basis = data.get("price_basis") == "raw"
        self.prices = ClockedPrices(source, self.clock,
                                    field="close" if self.raw_basis or data["dividends"] == "cash" else "adj_close")
        self._open_brokers()

    # -- files -------------------------------------------------------------------
    def _stamp(self) -> str:
        return f"{self.clock.today}T15:30:00+05:30"

    def _open_brokers(self) -> None:
        def mk(name: str) -> LocalPaperBroker:
            return LocalPaperBroker(self.root / f"{name}.json", starting_cash=self.data["cash"],
                                    price_fn=self.prices, currency="INR", whole_shares=True,
                                    cost_model=self.cost_model, now_fn=self._stamp)
        self.you, self.agent, self.nifty = mk("you"), mk("agent"), mk("nifty")

    def broker(self, who: str) -> LocalPaperBroker:
        return {"you": self.you, "agent": self.agent, "nifty": self.nifty}[who]

    def save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.root / "trial.json.tmp"
        tmp.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
        os.replace(tmp, self.root / "trial.json")

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """All or nothing: on any error, put every file, the data and the clock back.

        The files are also copied to ``.pre-step/`` (marker file ``ok`` written last) so a
        killed process is undone by the next ``Trial.load``."""
        saved = {f: (self.root / f).read_bytes() for f in self.FILES if (self.root / f).exists()}
        backup = self.root / ".pre-step"
        shutil.rmtree(backup, ignore_errors=True)
        backup.mkdir(parents=True)
        for f, b in saved.items():
            (backup / f).write_bytes(b)
        (backup / "ok").write_text("ok", encoding="utf-8")
        data, clock = json.loads(json.dumps(self.data)), self.clock.today
        try:
            yield
        except BaseException:
            self._restore(saved)
            self.data = data
            self.clock = ReplayClock(clock)
            self.prices.clock = self.clock
            self._open_brokers()
            raise
        finally:
            self._drop_backup(backup)

    @staticmethod
    def _drop_backup(backup: Path) -> None:
        """Marker first, so a kill mid-cleanup can't leave a marker with a partial backup."""
        (backup / "ok").unlink(missing_ok=True)
        shutil.rmtree(backup, ignore_errors=True)

    def _restore(self, saved: dict[str, bytes]) -> None:
        for f in self.FILES:
            p = self.root / f
            if f in saved:
                p.write_bytes(saved[f])
            elif p.exists():
                p.unlink()

    @classmethod
    def _recover(cls, root: Path) -> None:
        """Undo a step that was killed part-way: its broker files are ahead of trial.json."""
        backup = Path(root) / ".pre-step"
        if not backup.exists():
            return
        if (backup / "ok").exists():
            for f in cls.FILES:
                p = Path(root) / f
                if (backup / f).exists():
                    p.write_bytes((backup / f).read_bytes())
                elif p.exists():
                    p.unlink()
        cls._drop_backup(backup)  # no marker: the backup itself was interrupted

    # -- create / load -------------------------------------------------------------
    @classmethod
    def create(cls, replay_dir: Path, *, name: str, start: str, cash: float, universe: str, top: int,
               dividends: str, source: Any, universe_obj: Any,
               screen_fn: Callable[..., list[dict[str, Any]]] | None = None, today: str | None = None) -> "Trial":
        today = today or date.today().isoformat()
        start = date.fromisoformat(str(start)[:10]).isoformat()
        universe, top, cash = universe.upper(), int(top), float(cash)
        if start < EARLIEST_START:
            raise ValueError(f"start on or after {EARLIEST_START}: index membership history begins in 2021")
        if start >= today:
            raise ValueError("pick a start date in the past")
        if universe not in BENCHMARKS:
            raise ValueError(f"unknown universe {universe}; choose from {', '.join(BENCHMARKS)}")
        if dividends not in ("reinvest", "cash"):
            raise ValueError("dividends must be reinvest or cash")
        if not 1 <= top <= 30:
            raise ValueError("the agent's number of stocks must be between 1 and 30")
        if cash < 10_000:
            raise ValueError("practice money must be at least ₹10,000")
        slug = _slug(name)
        root = Path(replay_dir) / slug
        if root.exists():
            raise ValueError(f"a replay called {slug} already exists")
        data = {"name": name.strip(), "slug": slug, "start": start, "clock": start, "universe": universe,
                "benchmark": BENCHMARKS[universe], "cash": cash, "top": top, "dividends": dividends,
                "price_basis": "raw", "auto_stop": False, "ended": None, "last_rebalance_month": None, "equity": [],
                "rebalances": [], "stops": [], "rebalance_count": 0, "agent_stop_count": 0, "counts_exact": True, "picks": None, "claude": [], "claude_presses": 0}
        root.mkdir(parents=True)
        try:
            t = cls(root, data, source, universe_obj, screen_fn)
            t._buy_benchmark()
            t.rebalance_agent()
            t.data["equity"].append(t.point())
            t.save()
        except BaseException:
            shutil.rmtree(root, ignore_errors=True)
            raise
        return t

    @classmethod
    def load(cls, root: Path, source: Any, universe_obj: Any,
             screen_fn: Callable[..., list[dict[str, Any]]] | None = None) -> "Trial":
        cls._recover(Path(root))
        data = json.loads((Path(root) / "trial.json").read_text(encoding="utf-8"))
        return cls(root, data, source, universe_obj, screen_fn)

    # -- trading -------------------------------------------------------------------
    def _buy_benchmark(self) -> None:
        fund, cash = self.data["benchmark"], self.data["cash"]
        try:
            price = self.prices.latest_price(fund)
        except LookupError as e:
            raise ValueError(f"{fund} has no price on {self.clock.today}: pick a later date or "
                             f"another universe ({e})") from e
        qty = int((cash - self.cost_model.charges("buy", cash)) // price)
        while qty > 0 and qty * price + self.cost_model.charges("buy", qty * price) > cash:
            qty -= 1
        if qty < 1:
            raise ValueError(f"₹{cash:,.0f} buys less than one unit of {fund}")
        self.nifty.submit_order(fund, "buy", qty=qty)

    def order(self, symbol: str, side: str, notional: float | None = None, qty: float | None = None) -> dict[str, Any]:
        if self.data["ended"]:
            raise ValueError("this replay has ended; it is read-only")
        if side not in ("buy", "sell"):
            raise ValueError("side must be buy or sell")
        symbol = check_ticker(symbol)
        # Look the price up first so a stock not yet listed raises the clocked error with its
        # listing date (LocalPaperBroker would otherwise swallow it into a generic message).
        self.prices.latest_price(symbol)
        if side == "buy":
            last = self.prices.last_trade_date(symbol)
            if last and (date.fromisoformat(self.clock.today) - date.fromisoformat(last)).days > 7:
                raise LookupError(f"{symbol} last traded {last}: it is suspended or delisted on the replay date")
        return self.you.submit_order(symbol, side, notional=notional, qty=qty)

    def rebalance_agent(self) -> dict[str, Any]:
        day, top = self.clock.today, self.data["top"]
        rows = self.screen_fn(self.universe.members_on(day), self.prices, top)
        picks = [r["symbol"] for r in rows][:top]
        trades = rebalance_to(self.agent, picks, top=top, price_fn=self.prices, cost_model=self.cost_model)
        entry = {"date": day, "picks": picks, "trades": trades}
        self.data["rebalances"] = (self.data["rebalances"] + [entry])[-120:]
        self.data["last_rebalance_month"] = day[:7]
        self.data["picks"] = {"date": day, "rows": [_pick_row(r) for r in rows]}
        return entry

    def refresh_picks(self) -> None:
        """The screen as of the clock (shown on the page; the agent trades only on rebalance days)."""
        rows = self.screen_fn(self.universe.members_on(self.clock.today), self.prices, self.data["top"])
        self.data["picks"] = {"date": self.clock.today, "rows": [_pick_row(r) for r in rows]}

    def point(self) -> dict[str, Any]:
        return {"date": self.clock.today, **{w: round(self.broker(w).account().equity, 2) for w in PORTFOLIOS}}


def _pick_row(r: dict[str, Any]) -> dict[str, Any]:
    keep = ("symbol", "name", "ret_12_1", "ret_6m", "above_200dma", "last_close", "verdict")
    return {k: r.get(k) for k in keep if k in r}


def list_trials(replay_dir: Path) -> list[dict[str, Any]]:
    out = []
    for p in sorted(Path(replay_dir).glob("*/trial.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            last = d["equity"][-1] if d.get("equity") else {}
            ret = {w: (last[w] / d["cash"] - 1) if last.get(w) else None for w in PORTFOLIOS}
            out.append({"slug": d["slug"], "name": d["name"], "start": d["start"], "clock": d["clock"],
                        "universe": d["universe"], "ended": d["ended"], **ret})
        except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError, ZeroDivisionError):
            continue  # unreadable or half-written: leave it out of the list
    out.sort(key=lambda r: r["clock"], reverse=True)
    return out
