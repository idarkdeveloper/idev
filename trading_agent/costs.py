"""Indian equity transaction costs (verified from Groww's and Zerodha's charge pages, Oct 2026).

Delivery (CNC) on NSE, per executed order:
  brokerage      Groww: min(₹20, 0.1% of value), minimum ₹5
  STT            0.1% on buy and on sell
  NSE txn charge 0.00297% (Groww's figure; Zerodha lists 0.00307%)
  SEBI fee       0.0001% (₹10 per crore)
  stamp duty     0.015% on the buy side only
  GST            18% on brokerage + exchange charge + SEBI fee (+ on DP charge)
  DP charge      on sell: ₹3.5 depository + ₹16.5 Groww (+18% GST), per scrip per day
Slippage is not a charge but is real; a default is included and configurable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class IndianDeliveryCosts:
    brokerage_pct: float = 0.001       # 0.1%
    brokerage_cap: float = 20.0
    brokerage_min: float = 5.0
    stt_pct: float = 0.001             # 0.1% each side
    exchange_pct: float = 0.0000297    # 0.00297%
    sebi_pct: float = 0.000001         # 0.0001%
    stamp_buy_pct: float = 0.00015     # 0.015%, buy only
    gst_pct: float = 0.18
    dp_charge_sell: float = 20.0       # ₹3.5 depository + ₹16.5 Groww, before GST
    slippage_bps: float = 15.0         # one-way, assumed; raise for illiquid small caps

    def brokerage(self, notional: float) -> float:
        return max(self.brokerage_min, min(self.brokerage_cap, notional * self.brokerage_pct)) if notional > 0 else 0.0

    def breakdown(self, side: str, notional: float) -> dict[str, float]:
        side = side.lower()
        b = self.brokerage(notional)
        exch = notional * self.exchange_pct
        sebi = notional * self.sebi_pct
        stt = notional * self.stt_pct
        stamp = notional * self.stamp_buy_pct if side == "buy" else 0.0
        dp = self.dp_charge_sell if side == "sell" else 0.0
        gst = (b + exch + sebi + dp) * self.gst_pct
        slip = notional * self.slippage_bps / 10_000
        total = b + exch + sebi + stt + stamp + dp + gst
        return {"brokerage": b, "stt": stt, "exchange": exch, "sebi": sebi, "stamp_duty": stamp,
                "dp_charge": dp, "gst": gst, "charges": total, "slippage": slip,
                "total": total + slip}

    def charges(self, side: str, notional: float) -> float:
        """Explicit charges only (no slippage), in rupees."""
        return self.breakdown(side, notional)["charges"]

    def round_trip(self, notional: float) -> dict[str, Any]:
        buy, sell = self.breakdown("buy", notional), self.breakdown("sell", notional)
        charges = buy["charges"] + sell["charges"]
        total = buy["total"] + sell["total"]
        return {"notional": notional, "buy": buy, "sell": sell, "charges": charges,
                "charges_bps": charges / notional * 10_000 if notional else 0.0,
                "total": total, "total_bps": total / notional * 10_000 if notional else 0.0}

    def round_trip_bps(self, notional: float, include_slippage: bool = True) -> float:
        rt = self.round_trip(notional)
        return rt["total_bps"] if include_slippage else rt["charges_bps"]


@dataclass(frozen=True)
class FlatCosts:
    """Simple percentage model for markets we have not verified (US paper trading)."""

    one_way_bps: float = 5.0

    def charges(self, side: str, notional: float) -> float:
        return notional * self.one_way_bps / 10_000

    def breakdown(self, side: str, notional: float) -> dict[str, float]:
        c = self.charges(side, notional)
        return {"charges": c, "slippage": 0.0, "total": c}

    def round_trip_bps(self, notional: float, include_slippage: bool = True) -> float:
        return 2 * self.one_way_bps


def cost_model_for(market: str) -> IndianDeliveryCosts | FlatCosts:
    return IndianDeliveryCosts() if market == "in" else FlatCosts()


def cost_quote_for(market: str, amount: float) -> dict[str, Any]:
    """Round-trip charges for one amount under a market's cost model (shared by Live and Replay)."""
    if not amount > 0 or amount == float("inf"):
        raise ValueError("amount must be positive")
    m = cost_model_for(market)
    if not hasattr(m, "round_trip"):
        bps = m.round_trip_bps(amount)
        return {"amount": amount, "model": "flat", "total_bps": bps, "total": amount * bps / 10_000}
    rt = m.round_trip(amount)
    return {"amount": amount, "model": "india_delivery", "buy": rt["buy"], "sell": rt["sell"],
            "charges": rt["charges"], "charges_bps": rt["charges_bps"], "total": rt["total"],
            "total_bps": rt["total_bps"], "slippage_bps_one_way": m.slippage_bps}
