"""Estimated Indian capital-gains tax on listed shares sold in the practice account.

Rules used (listed equity, STT paid): a sale of shares held up to 12 months is short term, taxed at 20% of the gain.
Held more than 12 months it is long term, taxed at 12.5% of the year's long-term gains above a Rs 1,25,000
exemption. 4% cess is added to the tax. Losses: a short-term loss offsets short- and long-term gains, a long-term
loss only long-term gains. The financial year runs 1 April to 31 March, India time.

An ESTIMATE only: surcharge, other income and grandfathering are ignored.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from .timezones import IST

ST_RATE = 0.20
LT_RATE = 0.125
CESS = 0.04
LT_EXEMPTION = 125_000.0

DISCLAIMER = "estimate — not tax advice; surcharge, other income and grandfathering are ignored"
LOSS_NOTE = ("a short-term loss can offset short- or long-term gains; a long-term loss only long-term gains; "
             "losses carry forward 8 years if you file on time")


def parse_time(at: Any) -> datetime:
    """An ISO time as an aware datetime (no zone = UTC); unreadable = the epoch."""
    try:
        d = datetime.fromisoformat(str(at))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.fromtimestamp(0, timezone.utc)


def ist_date(at: Any) -> date:
    return parse_time(at).astimezone(IST).date()


def fy_start_year(d: date) -> int:
    return d.year if d.month >= 4 else d.year - 1


def fy_label(d: date) -> str:
    y = fy_start_year(d)
    return f"{y}-{str(y + 1)[2:]}"


def held_over_a_year(opened_at: Any, now: Any) -> bool:
    """True when ``now`` is more than 12 months after ``opened_at`` (India dates)."""
    o, n = ist_date(opened_at), ist_date(now)
    try:
        anniversary = o.replace(year=o.year + 1)
    except ValueError:  # 29 February
        anniversary = o.replace(year=o.year + 1, day=28)
    return n > anniversary


def is_long_term(pos: dict[str, Any], now: Any) -> bool:
    """Holding period of a practice position. Groww gives no buy date, so a copied position counts as long term only
    when the person says so (``held_over_year``). Other positions use the time of the position's FIRST buy (not
    FIFO lots), unless the person says so."""
    if pos.get("held_over_year"):
        return True
    if pos.get("source") == "groww" or not pos.get("opened_at"):
        return False
    return held_over_a_year(pos["opened_at"], now)


def tax_on(st: float, lt: float) -> dict[str, float]:
    """Tax for a year whose realised short-term total is ``st`` and long-term total is ``lt`` (rupees, losses
    negative): netting, the exemption, the two rates and the cess."""
    taxable_st = max(0.0, st)
    lt_after = max(0.0, lt + st) if st < 0 else max(0.0, lt)  # only a short-term loss can reach into long-term gains
    exemption_used = min(LT_EXEMPTION, lt_after)
    taxable_lt = max(0.0, lt_after - LT_EXEMPTION)
    base = ST_RATE * taxable_st + LT_RATE * taxable_lt
    return {"taxable_st": taxable_st, "lt_after_netting": lt_after, "exemption_used": exemption_used,
            "taxable_lt": taxable_lt, "tax_before_cess": base, "tax": base * (1 + CESS)}


def _sells_in_year(orders: list[dict[str, Any]], day: date, before: Any = None) -> list[dict[str, Any]]:
    out = []
    for o in orders:
        if o.get("side") != "sell" or o.get("realised_pl") is None or not o.get("filled_at"):
            continue
        if fy_start_year(ist_date(o["filled_at"])) != fy_start_year(day):
            continue
        if before is not None and parse_time(o["filled_at"]) >= before:
            continue
        out.append(o)
    return out


def _totals(sells: list[dict[str, Any]]) -> tuple[float, float]:
    st = sum(float(o["realised_pl"]) for o in sells if not o.get("long_term"))
    lt = sum(float(o["realised_pl"]) for o in sells if o.get("long_term"))
    return st, lt


def estimate_sale(realised_pl: float, long_term: bool, orders: list[dict[str, Any]], now: Any) -> dict[str, Any]:
    """The extra tax this one sale adds to the financial year of ``now``, given the practice sales already made in
    it (``orders`` = the practice order list). Tax on the year with the sale minus tax on the year without it, so
    the exemption already used and any losses already booked are counted."""
    day = ist_date(now)
    st0, lt0 = _totals(_sells_in_year(orders, day))
    before = tax_on(st0, lt0)
    st1, lt1 = (st0, lt0 + realised_pl) if long_term else (st0 + realised_pl, lt0)
    after = tax_on(st1, lt1)
    marginal = after["tax"] - before["tax"]
    lt_used_before = before["exemption_used"]
    out = {
        "fy": fy_label(day), "term": "long" if long_term else "short", "gain": round(realised_pl, 2),
        "estimate": round(max(0.0, marginal), 2),
        "tax_saved": round(max(0.0, -marginal), 2),   # a loss that cuts tax already due on earlier gains this year
        "rate": LT_RATE if long_term else ST_RATE, "cess_pct": CESS * 100,
        "exemption_limit": LT_EXEMPTION, "exemption_used_before": round(lt_used_before, 2),
        "exemption_left_before": round(LT_EXEMPTION - lt_used_before, 2),
        "disclaimer": DISCLAIMER,
    }
    if realised_pl < 0:
        out["loss_note"] = LOSS_NOTE
    return out


def fy_summary(orders: list[dict[str, Any]], now: Any) -> dict[str, Any]:
    """This financial year's practice gains, losses and estimated tax, from the practice sales recorded in it."""
    day = ist_date(now)
    sells = _sells_in_year(orders, day)
    untracked = sum(1 for o in orders if o.get("side") == "sell" and o.get("realised_pl") is None and o.get("filled_at")
                    and fy_start_year(ist_date(o["filled_at"])) == fy_start_year(day))
    st = [float(o["realised_pl"]) for o in sells if not o.get("long_term")]
    lt = [float(o["realised_pl"]) for o in sells if o.get("long_term")]
    t = tax_on(sum(st), sum(lt))
    return {"fy": fy_label(day), "sales": len(sells), "untracked_sales": untracked,
            "st_gain": round(sum(v for v in st if v > 0), 2), "st_loss": round(sum(v for v in st if v < 0), 2),
            "lt_gain": round(sum(v for v in lt if v > 0), 2), "lt_loss": round(sum(v for v in lt if v < 0), 2),
            "exemption_limit": LT_EXEMPTION, "exemption_used": round(t["exemption_used"], 2),
            "taxable_st": round(t["taxable_st"], 2), "taxable_lt": round(t["taxable_lt"], 2),
            "estimate": round(t["tax"], 2), "disclaimer": DISCLAIMER}
