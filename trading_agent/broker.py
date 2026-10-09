"""Brokerage layer.

Two implementations share one interface:

* ``AlpacaPaperBroker`` – Alpaca's paper-trading API (stocks + crypto, no real money).
* ``LocalPaperBroker``  – a zero-dependency simulator persisted to a JSON file,
  so the whole pipeline runs with nothing but an Anthropic key and QuiverQuant.

Neither can touch a live-money account: the Alpaca broker refuses any base URL
that is not the paper endpoint.
"""

from __future__ import annotations

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
        }


@dataclass
class Account:
    cash: float
    equity: float
    currency: str = "USD"
    paper: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"cash": self.cash, "equity": self.equity, "currency": self.currency, "paper": self.paper}


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
                 now_fn: Any | None = None):
        self.path = Path(path)
        self._lock = threading.RLock()  # the watch thread and the dashboard share this object
        self.price_fn = price_fn
        self.currency = currency
        self.whole_shares = whole_shares  # Indian equities trade in whole shares
        self.cost_model = cost_model  # object with .charges(side, notional); None = free
        self.now_fn = now_fn or _utc_now  # Replay stamps fills with the replay date
        self._state = self._load(starting_cash)
        self._state.setdefault("fees_paid", 0.0)
        if not self._state.get("created_at"):
            # Older files: the account is at least as old as its first order.
            times = [o.get("filled_at") for o in self._state.get("orders", []) if o.get("filled_at")]
            self._state["created_at"] = min(times) if times else _utc_now()
        if self._state.get("mirrors"):
            self.name = f"local-paper (mirrors {self._state['mirrors']})"

    def _load(self, starting_cash: float) -> dict[str, Any]:
        if self.path.exists():
            return json.loads(self.path.read_text())
        return {"cash": float(starting_cash), "starting_cash": float(starting_cash),
                "positions": {}, "orders": [], "prices": {}, "created_at": _utc_now()}

    def _save(self) -> None:
        from .state import atomic_write
        with self._lock:
            atomic_write(self.path, json.dumps(self._state, indent=2))

    @property
    def created_at(self) -> str:
        """When this paper account started (UTC ISO time); earlier equity points are not its."""
        return self._state["created_at"]

    @property
    def is_untouched_mirror(self) -> bool:
        """A copy of a real account (see ``seed``) with no paper orders placed in it."""
        return bool(self._state.get("mirrors")) and not self._state.get("orders")

    def reset(self, starting_cash: float) -> bool:
        """Start the account over in place (same object, so every holder of it sees the fresh account).
        Removes the file; it is written again by the next order. True if a file was removed."""
        with self._lock:
            existed = self.path.exists()
            if existed:
                self.path.unlink()
            self._state = {"cash": float(starting_cash), "starting_cash": float(starting_cash), "positions": {},
                           "orders": [], "prices": {}, "created_at": _utc_now(), "fees_paid": 0.0}
            self.__dict__.pop("name", None)
            return existed

    @property
    def is_fresh(self) -> bool:
        """True until the first order or seed is persisted."""
        return not self.path.exists()

    def seed(self, positions: list[Position], cash: float | None = None,
             label: str | None = None) -> None:
        """Mirror a real account into the simulator (positions + optional cash)."""
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

    def positions(self) -> list[Position]:
        out = []
        dirty = False
        for sym, p in self._state["positions"].items():
            try:
                px: float | None = self.latest_price(sym)
            except LookupError:
                px = None
            if px is not None and px > (p.get("high_water") or 0):
                p["high_water"] = px
                dirty = True
            out.append(Position(symbol=sym, qty=p["qty"], avg_entry_price=p["avg_entry_price"],
                                current_price=px, high_water=p.get("high_water")))
        if dirty:
            self._save()
        return out

    def account(self) -> Account:
        equity = self._state["cash"] + sum(
            (p.market_value or p.qty * p.avg_entry_price) for p in self.positions()
        )
        return Account(cash=round(self._state["cash"], 2), equity=round(equity, 2),
                       currency=self.currency)

    def submit_order(self, *args: Any, **kw: Any) -> dict[str, Any]:
        with self._lock:
            return self._submit_order(*args, **kw)

    def _submit_order(self, symbol: str, side: str, notional: float | None = None,
                     qty: float | None = None) -> dict[str, Any]:
        symbol = symbol.upper()
        if side not in {"buy", "sell"}:
            raise ValueError("side must be 'buy' or 'sell'")
        if (notional is None) == (qty is None):
            raise ValueError("pass exactly one of notional or qty")
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

        if side == "buy":
            if cost + fees > self._state["cash"] + 1e-9:
                raise ValueError(f"Insufficient cash: need {cost + fees:.2f}, have {self._state['cash']:.2f}")
            new_qty = pos["qty"] + qty
            pos["avg_entry_price"] = (pos["qty"] * pos["avg_entry_price"] + cost) / new_qty
            pos["qty"] = new_qty
            pos["high_water"] = max(pos.get("high_water") or 0.0, price)
            self._state["cash"] -= cost + fees
            self._state["positions"][symbol] = pos
        else:
            if qty > pos["qty"] + 1e-9:
                raise ValueError(f"Cannot sell {qty} {symbol}: only hold {pos['qty']}")
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
            "filled_at": self.now_fn(),
        }
        self._state["orders"].append(order)
        self._save()
        return order

    def orders(self) -> list[dict[str, Any]]:
        return list(self._state["orders"])

    def credit(self, amount: float, note: str, at: str) -> None:
        """Add cash that isn't a trade (a dividend paid out in Replay)."""
        self._state["cash"] += float(amount)
        self._state.setdefault("credits", []).append({"at": at, "amount": round(float(amount), 2), "note": note})
        self._save()

    def credits(self) -> list[dict[str, Any]]:
        return list(self._state.get("credits", []))

    def performance(self) -> dict[str, Any]:
        acct = self.account()
        start = self._state["starting_cash"]
        return {
            "starting_cash": start,
            "equity": acct.equity,
            "cash": acct.cash,
            "pnl": round(acct.equity - start, 2),
            "pnl_pct": round((acct.equity - start) / start * 100, 2) if start else 0.0,
            "orders": len(self._state["orders"]),
            "fees_paid": round(self._state.get("fees_paid", 0.0), 2),
        }
