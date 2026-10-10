"""Brokerage layer.

Two implementations share one interface:

* ``AlpacaPaperBroker`` – Alpaca's paper-trading API (stocks + crypto, no real money).
* ``LocalPaperBroker``  – a zero-dependency simulator persisted to a JSON file,
  so the whole pipeline runs with nothing but an Anthropic key and QuiverQuant.

Neither can touch a live-money account: the Alpaca broker refuses any base URL
that is not the paper endpoint.
"""

from __future__ import annotations

import contextlib
import json
import threading
import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import requests


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class AlreadyCopied(Exception):
    """copy_in(only_if_not_copied=True) found copied-in positions still held (another request copied first)."""

    def __init__(self, last_copy_at: str | None):
        super().__init__("already copied")
        self.last_copy_at = last_copy_at


@dataclass
class Position:
    symbol: str
    qty: float
    avg_entry_price: float
    current_price: float | None = None
    high_water: float | None = None  # highest price seen since entry (for trailing stops)
    # Shares that can be sold right now. Groww: demat_free_quantity + t1_quantity, never
    # pledged or locked shares. None = every share is sellable (paper accounts).
    sellable_qty: float | None = None
    # Practice stop-loss (LocalPaperBroker only): "trailing" (None = the default), "fixed", "percent" or "none".
    stop_type: str | None = None
    stop_value: float | None = None
    # Practice account only: "groww" for shares copied in from the real portfolio, when the position was first bought
    # (or copied), and whether the person says the shares were held more than a year (Groww gives no buy date).
    source: str | None = None
    opened_at: str | None = None
    copied_at: str | None = None
    held_over_year: bool = False

    @property
    def free_qty(self) -> float:
        return self.qty if self.sellable_qty is None else min(self.qty, self.sellable_qty)

    @property
    def market_value(self) -> float | None:
        return None if self.current_price is None else self.qty * self.current_price

    @property
    def unrealized_pl(self) -> float | None:
        if self.current_price is None:
            return None
        return (self.current_price - self.avg_entry_price) * self.qty

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "qty": self.qty,
            "avg_entry_price": self.avg_entry_price,
            "current_price": self.current_price,
            "market_value": self.market_value,
            "unrealized_pl": self.unrealized_pl,
            "high_water": self.high_water,
            "sellable_qty": self.free_qty,
            "stop_type": self.stop_type or "trailing",
            "stop_value": self.stop_value,
            "source": self.source,
            "opened_at": self.opened_at,
            "copied_at": self.copied_at,
            "held_over_year": self.held_over_year,
        }


@dataclass
class Account:
    cash: float
    equity: float
    currency: str = "USD"
    paper: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"cash": self.cash, "equity": self.equity, "currency": self.currency, "paper": self.paper}


BSE_ONLY_MESSAGE = "BSE-only stock, no NSE listing: tracked for information, not tradable here"


BSE_ONLY_GROWW_MESSAGE = (BSE_ONLY_MESSAGE + "; a .BO symbol is not a valid Groww trading symbol, "
                          "so no order or GTT can be sent for it")


def refuse_bse_only(symbol: Any, side: str = "buy", groww: bool = False) -> None:
    """A BSE deal with no NSE listing is tracked as ``<code>.BO``; it is information only.

    Simulated brokers refuse only a buy (an existing position can still be sold or stopped out); Groww refuses
    both sides, because it would only fail there."""
    if str(symbol or "").strip().upper().endswith(".BO") and (groww or side == "buy"):
        raise ValueError(BSE_ONLY_GROWW_MESSAGE if groww else BSE_ONLY_MESSAGE)


class Broker(Protocol):
    name: str

    def account(self) -> Account: ...
    def positions(self) -> list[Position]: ...
    def latest_price(self, symbol: str) -> float: ...
    def submit_order(self, symbol: str, side: str, notional: float | None = None,
                     qty: float | None = None) -> dict[str, Any]: ...


# --------------------------------------------------------------------------- #
# Alpaca paper trading
# --------------------------------------------------------------------------- #
class AlpacaPaperBroker:
    name = "alpaca-paper"
    DATA_URL = "https://data.alpaca.markets"

    def __init__(self, key_id: str, secret: str,
                 base_url: str = "https://paper-api.alpaca.markets",
                 session: requests.Session | None = None, timeout: float = 30.0):
        if "paper-api" not in base_url:
            raise ValueError(
                "Refusing non-paper Alpaca endpoint. This agent only trades paper money."
            )
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.timeout = timeout
        self.headers = {
            "APCA-API-KEY-ID": key_id,
            "APCA-API-SECRET-KEY": secret,
            "Accept": "application/json",
        }

    def _req(self, method: str, url: str, **kw: Any) -> Any:
        resp = self.session.request(method, url, headers=self.headers, timeout=self.timeout, **kw)
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    def account(self) -> Account:
        a = self._req("GET", f"{self.base_url}/v2/account")
        return Account(cash=float(a["cash"]), equity=float(a["equity"]),
                       currency=a.get("currency", "USD"), paper=True)

    def positions(self) -> list[Position]:
        rows = self._req("GET", f"{self.base_url}/v2/positions")
        return [
            Position(symbol=r["symbol"], qty=float(r["qty"]),
                     avg_entry_price=float(r["avg_entry_price"]),
                     current_price=float(r["current_price"]) if r.get("current_price") else None)
            for r in rows
        ]

    @staticmethod
    def _is_crypto(symbol: str) -> bool:
        return "/" in symbol or symbol.upper().endswith("USD") and len(symbol) > 4

    def latest_price(self, symbol: str) -> float:
        if self._is_crypto(symbol):
            pair = symbol if "/" in symbol else f"{symbol[:-3]}/USD"
            data = self._req("GET", f"{self.DATA_URL}/v1beta3/crypto/us/latest/trades",
                             params={"symbols": pair})
            return float(data["trades"][pair]["p"])
        data = self._req("GET", f"{self.DATA_URL}/v2/stocks/{symbol.upper()}/trades/latest")
        return float(data["trade"]["p"])

    def submit_order(self, symbol: str, side: str, notional: float | None = None,
                     qty: float | None = None) -> dict[str, Any]:
        if side not in {"buy", "sell"}:
            raise ValueError("side must be 'buy' or 'sell'")
        refuse_bse_only(symbol, side)
        if (notional is None) == (qty is None):
            raise ValueError("pass exactly one of notional or qty")
        body: dict[str, Any] = {
            "symbol": symbol.upper(), "side": side, "type": "market",
            "time_in_force": "gtc" if self._is_crypto(symbol) else "day",
        }
        if notional is not None:
            body["notional"] = f"{round(notional, 2):g}"
        else:
            body["qty"] = str(qty)
        return self._req("POST", f"{self.base_url}/v2/orders", json=body)


# --------------------------------------------------------------------------- #
# Local simulator
# --------------------------------------------------------------------------- #
class LocalPaperBroker:
    """File-backed paper account. Prices come from a pluggable ``price_fn``."""

    name = "local-paper"

    def __init__(self, path: Path, starting_cash: float = 80_000.0,
                 price_fn: Any | None = None, currency: str = "USD",
                 whole_shares: bool = False, cost_model: Any | None = None,
                 now_fn: Any | None = None, shared: bool = False):
        self.path = Path(path)
        # shared=True: another process (a separate ``watch``) may use this same file, so every change is made under a
        # lock file after re-reading the file. Only the practice account is shared; others skip the cost.
        self.shared = shared
        self._starting_cash = float(starting_cash)
        self._persisted = False  # True once this object has read the file or written it
        self._lock = threading.RLock()  # the watch thread and the dashboard share this object
        self.price_fn = price_fn
        self.currency = currency
        self.whole_shares = whole_shares  # Indian equities trade in whole shares
        self.cost_model = cost_model  # object with .charges(side, notional); None = free
        self.now_fn = now_fn or _utc_now  # Replay stamps fills with the replay date
        self._depth = 0  # >0 while this object holds the cross-process file lock
        self._state = self._load(starting_cash)
        self._normalise()
        if self._state.get("mirrors"):
            self.name = f"local-paper (mirrors {self._state['mirrors']})"

    def _normalise(self) -> None:
        self._state.setdefault("fees_paid", 0.0)
        if not self._state.get("created_at"):
            # Older files: the account is at least as old as its first order.
            times = [o.get("filled_at") for o in self._state.get("orders", []) if o.get("filled_at")]
            self._state["created_at"] = min(times) if times else _utc_now()

    @contextlib.contextmanager
    def _txn(self):
        """Every read-modify-write of the account runs in here: the in-process lock, then a lock file shared with any
        other process using the same account file (a separate ``watch`` process), and the file is re-read from disk
        on the way in, so what is decided and written is based on what the other process last saved."""
        with self._lock:
            if self._depth:  # nested call from inside a transaction
                yield
                return
            if not self.shared:
                self._depth = 1
                try:
                    yield
                finally:
                    self._depth = 0
                return
            from .news import _file_lock
            with _file_lock(self.path.with_name(self.path.name + ".lock"), what="paper account"):
                self._depth = 1
                try:
                    if self.path.exists():
                        self._state = json.loads(self.path.read_text())
                        self._normalise()
                        self._persisted = True
                    elif self._persisted:  # another process reset the account: start over from the starting cash
                        self._state = self._fresh_state(self._state.get("starting_cash", self._starting_cash))
                        self._normalise()
                        self._persisted = False
                    yield
                finally:
                    self._depth = 0

    @staticmethod
    def _fresh_state(starting_cash: float) -> dict[str, Any]:
        return {"cash": float(starting_cash), "starting_cash": float(starting_cash), "positions": {}, "orders": [],
                "prices": {}, "created_at": _utc_now(), "fees_paid": 0.0}

    def _load(self, starting_cash: float) -> dict[str, Any]:
        if self.path.exists():
            self._persisted = True
            return json.loads(self.path.read_text())
        return {"cash": float(starting_cash), "starting_cash": float(starting_cash),
                "positions": {}, "orders": [], "prices": {}, "created_at": _utc_now()}

    def _save(self) -> None:
        from .state import atomic_write
        with self._lock:
            atomic_write(self.path, json.dumps(self._state, indent=2))
            self._persisted = True

    @property
    def created_at(self) -> str:
        """When this paper account started (UTC ISO time); earlier equity points are not its."""
        return self._state["created_at"]

    @property
    def is_untouched_mirror(self) -> bool:
        """A copy of a real account (see ``seed``) with no paper orders placed in it."""
        return bool(self._state.get("mirrors")) and not self._state.get("orders")

    def reset(self, starting_cash: float, then: Any | None = None) -> bool:
        """Start the account over in place (same object, so every holder of it sees the fresh account).
        Removes the file; it is written again by the next order. True if a file was removed.
        ``then()`` runs in the same transaction on the fresh account (reset to a copy of real holdings), so no other
        process sees the empty account in between. It must not fetch prices."""
        with self._txn():
            existed = self.path.exists()
            if then is None:
                if existed:
                    self.path.unlink()
                self._state = self._fresh_state(starting_cash)
                self._persisted = False
                self.__dict__.pop("name", None)
                return existed
            # With a copy to make: build it on the fresh account first, so a copy that fails leaves the old account
            # (memory and file) untouched. Its save replaces the file; if it saved nothing the old file is removed.
            old, old_persisted, old_name = self._state, self._persisted, self.__dict__.get("name")
            self._state = self._fresh_state(starting_cash)
            self._persisted = False
            try:
                then()
            except BaseException:
                self._state, self._persisted = old, old_persisted
                if old_name is not None:
                    self.name = old_name
                raise
            self.__dict__.pop("name", None)
            if not self._persisted and existed:
                self.path.unlink()
            return existed

    @property
    def is_fresh(self) -> bool:
        """True until the first order or seed is persisted."""
        return not self.path.exists()

    def seed(self, positions: list[Position], cash: float | None = None,
             label: str | None = None) -> None:
        """Mirror a real account into the simulator (positions + optional cash)."""
        with self._txn():
            self._seed(positions, cash, label)

    def _seed(self, positions: list[Position], cash: float | None, label: str | None) -> None:
        for p in positions:
            self._state["positions"][p.symbol.upper()] = {"qty": p.qty,
                                                          "avg_entry_price": p.avg_entry_price}
            if p.current_price is not None:
                self._state["prices"][p.symbol.upper()] = p.current_price
        if cash is not None:
            self._state["cash"] = float(cash)
        start = self._state["cash"] + sum(
            p.qty * (p.current_price or p.avg_entry_price) for p in positions)
        self._state["starting_cash"] = round(start, 2)
        if label:
            self._state["mirrors"] = label
            self.name = f"local-paper (mirrors {label})"
        self._save()

    def set_price(self, symbol: str, price: float) -> None:
        """Manual price override (used by tests and demo mode)."""
        with self._txn():
            self._state["prices"][symbol.upper()] = float(price)
            self._save()

    def latest_price(self, symbol: str) -> float:
        symbol = symbol.upper()
        if self.price_fn is not None:
            try:
                price = float(self.price_fn(symbol))
                self._state["prices"][symbol] = price
                return price
            except Exception:  # fall back to the last known price
                pass
        if symbol in self._state["prices"]:
            return float(self._state["prices"][symbol])
        raise LookupError(f"No price known for {symbol}; set one with set_price() or a price_fn")

    def _held_symbols(self) -> list[str]:
        """The held symbols, without any lock: from the file when shared (it is swapped in whole, never half
        written), else from memory."""
        if self.shared:
            try:
                return list(json.loads(self.path.read_text()).get("positions", {}))
            except (OSError, ValueError):
                pass
        with self._lock:
            return list(self._state["positions"])

    def positions(self) -> list[Position]:
        # Prices come first, with no lock held (they can be network calls); the lock is only for the short
        # re-read, high-water update and save.
        prices: dict[str, float | None] = {}
        for sym in self._held_symbols():
            try:
                prices[sym] = self.latest_price(sym)
            except LookupError:
                prices[sym] = None
        with self._txn():  # the watch thread's auto-exit orders change the same dict
            out = []
            dirty = False
            for sym, p in self._state["positions"].items():
                px = prices.get(sym)
                if px is not None and px > (p.get("high_water") or 0):
                    p["high_water"] = px
                    dirty = True
                stop = p.get("stop") or {}
                out.append(Position(symbol=sym, qty=p["qty"], avg_entry_price=p["avg_entry_price"],
                                    current_price=px, high_water=p.get("high_water"),
                                    stop_type=stop.get("type"), stop_value=stop.get("value"),
                                    source=p.get("source"), opened_at=self._opened_at(sym, p),
                                    copied_at=p.get("copied_at"), held_over_year=bool(p.get("held_over_year"))))
            if dirty:
                self._save()
            return out

    def _opened_at(self, sym: str, raw: dict[str, Any]) -> str | None:
        """When a position was first bought: stored, else the earliest buy order of the symbol (older files)."""
        if raw.get("opened_at"):
            return raw["opened_at"]
        first, held = None, 0.0   # the first buy of the current holding: buys after the last sale that emptied it
        for o in self._state.get("orders", []):
            if o.get("symbol") != sym or not o.get("filled_at"):
                continue
            if o.get("side") == "buy":
                first = first or o["filled_at"]
                held += float(o.get("qty") or 0)
            elif o.get("side") == "sell":
                held -= float(o.get("qty") or 0)
                if held <= 1e-9:
                    first, held = None, 0.0
        return first

    def position(self, symbol: str) -> Position | None:
        """One open position with its latest price (no other price is fetched), or None."""
        sym = symbol.upper()
        with self._txn():
            raw = self._state["positions"].get(sym)
            if raw is None:
                return None
            raw = dict(raw)
            raw["opened_at"] = self._opened_at(sym, raw)
        try:
            px: float | None = self.latest_price(sym)
        except LookupError:
            px = None
        stop = raw.get("stop") or {}
        return Position(symbol=sym, qty=raw["qty"], avg_entry_price=raw["avg_entry_price"], current_price=px,
                        high_water=raw.get("high_water"), stop_type=stop.get("type"), stop_value=stop.get("value"),
                        source=raw.get("source"), opened_at=raw.get("opened_at"), copied_at=raw.get("copied_at"),
                        held_over_year=bool(raw.get("held_over_year")))

    def copy_status(self) -> dict[str, Any]:
        """Copied-in holdings still held, and when the last copy was made (no prices fetched)."""
        with self._txn():
            held = [s for s, p in self._state["positions"].items() if p.get("source") == "groww"]
            copies = [c for c in self._state.get("credits", []) if c.get("kind") == "copy"]
            return {"held": held, "last_copy_at": copies[-1]["at"] if copies else None, "copies": len(copies)}

    def account(self) -> Account:
        equity = self._state["cash"] + sum(
            (p.market_value or p.qty * p.avg_entry_price) for p in self.positions()
        )
        return Account(cash=round(self._state["cash"], 2), equity=round(equity, 2),
                       currency=self.currency)

    def submit_order(self, symbol: str, side: str, notional: float | None = None, qty: float | None = None,
                     stop: dict[str, Any] | None = None, extra: dict[str, Any] | None = None,
                     extra_fn: Any | None = None) -> dict[str, Any]:
        if side not in {"buy", "sell"}:
            raise ValueError("side must be 'buy' or 'sell'")
        refuse_bse_only(symbol, side)
        if (notional is None) == (qty is None):
            raise ValueError("pass exactly one of notional or qty")
        price = self.latest_price(symbol)  # may be a network call: no lock held
        with self._txn():
            return self._submit_order(symbol, side, notional, qty, stop, extra, price=price, extra_fn=extra_fn)

    def _submit_order(self, symbol: str, side: str, notional: float | None = None,
                      qty: float | None = None, stop: dict[str, Any] | None = None,
                      extra: dict[str, Any] | None = None, price: float | None = None,
                      extra_fn: Any | None = None) -> dict[str, Any]:
        """``stop`` ({"type", "value"}) is stored with the position on a buy; ``extra`` is merged into the order.
        ``extra_fn(order, orders_so_far)`` runs inside the transaction and returns more fields for the order (the
        practice tax estimate needs the account's earlier sales, read at the moment of the sale)."""
        symbol = symbol.upper()
        if side not in {"buy", "sell"}:
            raise ValueError("side must be 'buy' or 'sell'")
        refuse_bse_only(symbol, side)
        if (notional is None) == (qty is None):
            raise ValueError("pass exactly one of notional or qty")
        if price is None:
            price = self.latest_price(symbol)
        if qty is None:
            qty = round(float(notional) / price, 6)
        if self.whole_shares:
            qty = float(math.floor(qty))
            if qty < 1:
                raise ValueError(f"{symbol}: amount buys fewer than one whole share at {price}")
        cost = qty * price
        fees = float(self.cost_model.charges(side, cost)) if self.cost_model else 0.0
        pos = self._state["positions"].get(symbol, {"qty": 0.0, "avg_entry_price": 0.0})
        now = self.now_fn()
        sale_facts: dict[str, Any] = {}  # what a sale adds to its order: cost basis, realised P&L, holding period

        if side == "buy":
            if cost + fees > self._state["cash"] + 1e-9:
                raise ValueError(f"Insufficient cash: need {cost + fees:.2f}, have {self._state['cash']:.2f}")
            new_qty = pos["qty"] + qty
            pos["avg_entry_price"] = (pos["qty"] * pos["avg_entry_price"] + cost) / new_qty
            pos["qty"] = new_qty
            pos["high_water"] = max(pos.get("high_water") or 0.0, price)
            pos.setdefault("opened_at", self._opened_at(symbol, pos) or now)  # the first buy; used for the holding period
            if stop is not None:
                pos["stop"] = {"type": stop["type"], "value": stop.get("value")}
            self._state["cash"] -= cost + fees
            self._state["positions"][symbol] = pos
        else:
            if qty > pos["qty"] + 1e-9:
                raise ValueError(f"Cannot sell {qty} {symbol}: only hold {pos['qty']}")
            from .taxes import is_long_term
            sale_facts = {"avg_entry_price": pos["avg_entry_price"],
                          "realised_pl": round(cost - fees - qty * pos["avg_entry_price"], 2),
                          "long_term": is_long_term({**pos, "opened_at": self._opened_at(symbol, pos)}, now)}
            pos["qty"] -= qty
            self._state["cash"] += cost - fees
            if pos["qty"] <= 1e-9:
                self._state["positions"].pop(symbol, None)
            else:
                self._state["positions"][symbol] = pos
        self._state["fees_paid"] = self._state.get("fees_paid", 0.0) + fees

        order = {
            "id": uuid.uuid4().hex[:12], "symbol": symbol, "side": side, "qty": qty,
            "filled_avg_price": price, "notional": round(cost, 2), "fees": round(fees, 2),
            "status": "filled",
            "filled_at": now,
            **sale_facts,
            **(extra or {}),
        }
        if extra_fn is not None:
            order.update(extra_fn(order, self._state["orders"]))
        self._state["orders"].append(order)
        self._save()
        return order

    def set_stop(self, symbol: str, stop: dict[str, Any]) -> None:
        """Change the practice stop-loss of an open position ({"type", "value"} from ``risk.normalize_stop``)."""
        with self._txn():
            pos = self._state["positions"].get(symbol.upper())
            if pos is None:
                raise LookupError(f"no open position in {symbol.upper()}")
            pos["stop"] = {"type": stop["type"], "value": stop.get("value")}
            self._save()

    def set_held_over_year(self, symbol: str, flag: bool) -> None:
        """Remember that the shares of an open position were held more than a year (for the tax estimate)."""
        with self._txn():
            pos = self._state["positions"].get(symbol.upper())
            if pos is None:
                raise LookupError(f"no open position in {symbol.upper()}")
            if bool(pos.get("held_over_year")) != bool(flag):
                pos["held_over_year"] = bool(flag)
                self._save()

    def copy_in(self, holdings: list[dict[str, Any]], note: str = "copied from Groww",
                only_if_not_copied: bool = False) -> dict[str, Any]:
        """Copy real holdings into the account as practice positions without touching cash and without charges.
        Each holding is {symbol, qty, avg_price, price, held_over_year?}. A symbol already held is merged (quantity
        added, average price weighted). The copied cost is recorded as a credit entry (``kind: "copy"``) that adds no
        cash; ``performance()`` counts the value at copy time as part of the starting point, so copying does not
        show up as profit. Returns what was added and merged."""
        at = self.now_fn()
        with self._txn():
            if only_if_not_copied:  # checked on the account as just re-read under the lock
                held = [s for s, p in self._state["positions"].items() if p.get("source") == "groww"]
                if held:
                    copies = [c for c in self._state.get("credits", []) if c.get("kind") == "copy"]
                    raise AlreadyCopied(copies[-1]["at"] if copies else None)
            added, merged, cost, value = [], [], 0.0, 0.0
            for h in holdings:
                sym, qty = str(h["symbol"]).upper(), float(h["qty"])
                avg, price = float(h["avg_price"]), float(h["price"])
                if qty <= 0 or avg <= 0 or price <= 0:
                    raise ValueError(f"{sym}: quantity and prices must be positive")
                cur = self._state["positions"].get(sym)
                if cur:
                    total = cur["qty"] + qty
                    cur["avg_entry_price"] = (cur["qty"] * cur["avg_entry_price"] + qty * avg) / total
                    cur["qty"] = total
                    cur["high_water"] = max(cur.get("high_water") or 0.0, price)
                    cur["copied_qty"] = cur.get("copied_qty", 0.0) + qty
                    cur["held_over_year"] = bool(cur.get("held_over_year") or h.get("held_over_year"))
                    pos = cur
                    merged.append(sym)
                else:
                    pos = {"qty": qty, "avg_entry_price": avg, "high_water": max(avg, price),
                           "stop": {"type": "none", "value": None}, "opened_at": at, "copied_qty": qty,
                           "held_over_year": bool(h.get("held_over_year"))}
                    added.append(sym)
                pos["source"], pos["copied_at"] = "groww", at
                self._state["positions"][sym] = pos
                self._state["prices"][sym] = price
                cost += qty * avg
                value += qty * price
            if added or merged:
                self._state.setdefault("credits", []).append(
                    {"at": at, "amount": round(cost, 2), "note": note, "kind": "copy",
                     "value_at_copy": round(value, 2), "symbols": added + merged})
                self._save()
            return {"added": added, "merged": merged, "cost": round(cost, 2), "value": round(value, 2), "at": at}

    def sell_if_stopped(self, symbol: str, *, qty: float, level: float, stop: dict[str, Any],
                        extra: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Sell the whole position at the latest price, but only if, now and inside the lock, it is still the same
        position (same quantity, same stop setting) and the price is still at or below ``level``. The dashboard's
        checker and the watch thread both come through here, so a position is sold once. None = nothing sold.
        The check runs on the account file re-read under the cross-process lock, so a sale made by another process
        (a separate ``watch``) on the same file is seen too."""
        sym = symbol.upper()
        price: float | None = None
        price_error: Exception | None = None
        try:
            price = self.latest_price(sym)  # may be a network call: no lock held
        except Exception as e:  # noqa: BLE001 - only matters if the position is still there and unchanged
            price_error = e
        with self._txn():
            pos = self._state["positions"].get(sym)
            if pos is None or abs(pos["qty"] - qty) > 1e-9:
                return None
            now = pos.get("stop") or {}
            if (now.get("type") or "trailing", now.get("value")) != (stop.get("type") or "trailing", stop.get("value")):
                return None
            if price is None:
                raise price_error  # type: ignore[misc]
            if price > level:
                return None
            return self._submit_order(sym, "sell", qty=pos["qty"], extra={"stop_hit": True, **(extra or {})}, price=price)

    def orders(self) -> list[dict[str, Any]]:
        return list(self._state["orders"])

    def credit(self, amount: float, note: str, at: str) -> None:
        """Add cash that isn't a trade (a dividend paid out in Replay). Copies of real holdings (``copy_in``) are
        also stored in this list, with ``kind: "copy"``; they add no cash."""
        with self._txn():
            self._state["cash"] += float(amount)
            self._state.setdefault("credits", []).append({"at": at, "amount": round(float(amount), 2), "note": note})
            self._save()

    def credits(self) -> list[dict[str, Any]]:
        return list(self._state.get("credits", []))

    def performance(self) -> dict[str, Any]:
        acct = self.account()
        start = self._state["starting_cash"]
        # Holdings copied in from a real account count at their value when copied: that is where the account
        # starts from, so copying them in is not a profit.
        copied = sum(c.get("value_at_copy", 0.0) for c in self._state.get("credits", []) if c.get("kind") == "copy")
        base = start + copied
        return {
            "starting_cash": start,
            "copied_in": round(copied, 2),
            "baseline": round(base, 2),
            "equity": acct.equity,
            "cash": acct.cash,
            "pnl": round(acct.equity - base, 2),
            "pnl_pct": round((acct.equity - base) / base * 100, 2) if base else 0.0,
            "orders": len(self._state["orders"]),
            "fees_paid": round(self._state.get("fees_paid", 0.0), 2),
        }
