"""Groww Trade API brokerage (Indian equities, NSE/BSE cash segment).

Groww has no paper-trading sandbox, so by default this client is used **read-only**:
real holdings and live prices feed the local paper simulator. Real orders are sent
only when ``GROWW_LIVE_ORDERS=true`` *and* ``AUTO_TRADE=true`` (double opt-in).

API reference: https://groww.in/trade-api/docs/curl
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import math
import struct
import time
import uuid
from typing import Any

import requests

from .broker import Account, Position

BASE_URL = "https://api.groww.in/v1"


# --------------------------------------------------------------------------- #
# Auth helpers
# --------------------------------------------------------------------------- #
def totp_now(secret_b32: str, digits: int = 6, period: int = 30) -> str:
    """RFC 6238 TOTP (SHA-1), so no extra dependency is needed."""
    key = base64.b32decode(secret_b32.strip().replace(" ", "").upper() + "=" * (-len(secret_b32) % 8))
    counter = struct.pack(">Q", int(time.time()) // period)
    digest = hmac.new(key, counter, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-API-VERSION": "1.0",
        "x-request-id": str(uuid.uuid4()),
    }


def get_access_token(api_key: str, *, secret: str | None = None, totp: str | None = None,
                     session: requests.Session | None = None, base_url: str = BASE_URL) -> str:
    """Exchange an API key for a daily access token (approval or TOTP flow)."""
    if (secret is None) == (totp is None):
        raise ValueError("pass exactly one of secret or totp")
    if secret is not None:
        ts = str(int(time.time()))
        body: dict[str, Any] = {"key_type": "approval", "timestamp": ts,
                                "checksum": hashlib.sha256((secret + ts).encode()).hexdigest()}
    else:
        body = {"key_type": "totp", "totp": totp}
    sess = session or requests.Session()
    resp = sess.post(f"{base_url}/token/api/access", headers=_headers(api_key), json=body, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    token = data.get("token") or (data.get("payload") or {}).get("token")
    if not token:
        raise RuntimeError(f"Groww token response had no token: {data}")
    return token


# --------------------------------------------------------------------------- #
# Broker
# --------------------------------------------------------------------------- #
class GrowwBroker:
    name = "groww"

    def __init__(self, access_token: str, *, live_orders: bool = False,
                 exchange: str = "NSE", product: str = "CNC",
                 session: requests.Session | None = None, timeout: float = 30.0,
                 base_url: str = BASE_URL):
        self.token = access_token
        self.live_orders = live_orders
        self.exchange = exchange
        self.product = product
        self.session = session or requests.Session()
        self.timeout = timeout
        self.base_url = base_url.rstrip("/")

    def _req(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        resp = self.session.request(method, f"{self.base_url}/{path.lstrip('/')}",
                                    headers=_headers(self.token), timeout=self.timeout, **kw)
        data = resp.json() if resp.content else {}
        if isinstance(data, dict) and data.get("status") == "FAILURE":
            err = data.get("error") or {}
            raise RuntimeError(f"Groww {err.get('code')}: {err.get('message')}")
        resp.raise_for_status()
        return data.get("payload", data) if isinstance(data, dict) else data

    # -- read ----------------------------------------------------------------
    def holdings(self) -> list[dict[str, Any]]:
        return list(self._req("GET", "holdings/user").get("holdings", []))

    def positions(self) -> list[Position]:
        out = []
        rows = self.holdings()
        prices = self.ltp_many([h["trading_symbol"] for h in rows]) if rows else {}
        for h in rows:
            qty = float(h.get("quantity") or 0)
            if qty == 0:
                continue
            sym = h["trading_symbol"]
            out.append(Position(symbol=sym, qty=qty, avg_entry_price=float(h.get("average_price") or 0),
                                current_price=prices.get(sym)))
        return out

    def account(self) -> Account:
        margin = self._req("GET", "margins/detail/user")
        cash = float(margin.get("clear_cash") or margin.get("available_cash")
                     or margin.get("cash_balance") or 0)
        holdings_value = sum((p.market_value or p.qty * p.avg_entry_price) for p in self.positions())
        return Account(cash=round(cash, 2), equity=round(cash + holdings_value, 2),
                       currency="INR", paper=not self.live_orders)

    def ltp_many(self, symbols: list[str]) -> dict[str, float]:
        out: dict[str, float] = {}
        for i in range(0, len(symbols), 50):
            chunk = symbols[i:i + 50]
            keys = [f"{self.exchange}_{s.upper()}" for s in chunk]
            payload = self._req("GET", "live-data/ltp",
                                params={"segment": "CASH", "exchange_symbols": ",".join(keys)})
            for s, k in zip(chunk, keys):
                v = payload.get(k)
                if v is not None:
                    out[s.upper()] = float(v)
        return out

    def latest_price(self, symbol: str) -> float:
        symbol = symbol.upper().replace(f"{self.exchange}_", "")
        prices = self.ltp_many([symbol])
        if symbol not in prices:
            raise LookupError(f"Groww returned no LTP for {self.exchange}_{symbol}")
        return prices[symbol]

    # -- write ---------------------------------------------------------------
    def submit_order(self, symbol: str, side: str, notional: float | None = None,
                     qty: float | None = None) -> dict[str, Any]:
        side = side.lower()
        if side not in {"buy", "sell"}:
            raise ValueError("side must be 'buy' or 'sell'")
        if (notional is None) == (qty is None):
            raise ValueError("pass exactly one of notional or qty")
        symbol = symbol.upper()
        if qty is None:
            price = self.latest_price(symbol)
            qty = math.floor(float(notional) / price)
        qty = int(qty)
        if qty < 1:
            raise ValueError(f"{symbol}: amount buys fewer than one whole share")
        if not self.live_orders:
            raise PermissionError(
                "Groww live orders are disabled (set GROWW_LIVE_ORDERS=true to place real orders)."
            )
        body = {
            "trading_symbol": symbol, "quantity": qty, "validity": "DAY",
            "exchange": self.exchange, "segment": "CASH", "product": self.product,
            "order_type": "MARKET", "transaction_type": side.upper(), "price": 0,
            "order_reference_id": uuid.uuid4().hex[:16],
        }
        payload = self._req("POST", "order/create", json=body)
        return {"id": payload.get("groww_order_id"), "symbol": symbol, "side": side, "qty": qty,
                "status": payload.get("order_status", "PLACED"), "broker": "groww",
                "live": True, "raw": payload}
