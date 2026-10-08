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
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import requests


@dataclass
class Position:
    symbol: str
    qty: float
    avg_entry_price: float
    current_price: float | None = None

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
                 price_fn: Any | None = None):
        self.path = Path(path)
        self.price_fn = price_fn
        self._state = self._load(starting_cash)
        if self._state.get("mirrors"):
            self.name = f"local-paper (mirrors {self._state['mirrors']})"

    def _load(self, starting_cash: float) -> dict[str, Any]:
        if self.path.exists():
            return json.loads(self.path.read_text())
        return {"cash": float(starting_cash), "starting_cash": float(starting_cash),
                "positions": {}, "orders": [], "prices": {}}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._state, indent=2))

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
        for sym, p in self._state["positions"].items():
            try:
                px: float | None = self.latest_price(sym)
            except LookupError:
                px = None
            out.append(Position(symbol=sym, qty=p["qty"], avg_entry_price=p["avg_entry_price"],
                                current_price=px))
        return out

    def account(self) -> Account:
        equity = self._state["cash"] + sum(
            (p.market_value or p.qty * p.avg_entry_price) for p in self.positions()
        )
        return Account(cash=round(self._state["cash"], 2), equity=round(equity, 2))

    def submit_order(self, symbol: str, side: str, notional: float | None = None,
                     qty: float | None = None) -> dict[str, Any]:
        symbol = symbol.upper()
        if side not in {"buy", "sell"}:
            raise ValueError("side must be 'buy' or 'sell'")
        if (notional is None) == (qty is None):
            raise ValueError("pass exactly one of notional or qty")
        price = self.latest_price(symbol)
        if qty is None:
            qty = round(float(notional) / price, 6)
        cost = qty * price
        pos = self._state["positions"].get(symbol, {"qty": 0.0, "avg_entry_price": 0.0})

        if side == "buy":
            if cost > self._state["cash"] + 1e-9:
                raise ValueError(f"Insufficient cash: need {cost:.2f}, have {self._state['cash']:.2f}")
            new_qty = pos["qty"] + qty
            pos["avg_entry_price"] = (pos["qty"] * pos["avg_entry_price"] + cost) / new_qty
            pos["qty"] = new_qty
            self._state["cash"] -= cost
            self._state["positions"][symbol] = pos
        else:
            if qty > pos["qty"] + 1e-9:
                raise ValueError(f"Cannot sell {qty} {symbol}: only hold {pos['qty']}")
            pos["qty"] -= qty
            self._state["cash"] += cost
            if pos["qty"] <= 1e-9:
                self._state["positions"].pop(symbol, None)
            else:
                self._state["positions"][symbol] = pos

        order = {
            "id": uuid.uuid4().hex[:12], "symbol": symbol, "side": side, "qty": qty,
            "filled_avg_price": price, "notional": round(cost, 2), "status": "filled",
            "filled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        self._state["orders"].append(order)
        self._save()
        return order

    def orders(self) -> list[dict[str, Any]]:
        return list(self._state["orders"])

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
        }
