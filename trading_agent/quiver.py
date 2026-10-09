"""QuiverQuant market-data client.

Covers the data the reel uses: congress (politician) trades and insider
transactions. Everything is plain REST with an ``Authorization: Token`` header.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Iterable

import requests

BASE_URL = "https://api.quiverquant.com/beta"


@dataclass(frozen=True)
class DisclosedTrade:
    """A single publicly disclosed trade, normalised across sources."""

    source: str  # "congress" | "insider"
    investor: str
    ticker: str
    transaction: str  # "Purchase" | "Sale" | raw code for insiders
    transaction_date: str
    report_date: str
    size: str  # dollar range or share count as reported
    raw: dict[str, Any]

    @property
    def key(self) -> str:
        """Stable id used to remember which trades we've already seen."""
        material = json.dumps(
            [self.source, self.investor, self.ticker, self.transaction,
             self.transaction_date, self.report_date, self.size],
            sort_keys=True,
        )
        return hashlib.sha1(material.encode()).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["key"] = self.key
        return d

    def summary(self) -> str:
        return (f"{self.investor} {self.transaction.lower()} {self.ticker} "
                f"({self.size}) traded {self.transaction_date}, reported {self.report_date}")


def _norm_congress(row: dict[str, Any]) -> DisclosedTrade:
    return DisclosedTrade(
        source="congress",
        investor=str(row.get("Representative") or row.get("Name") or "").strip(),
        ticker=str(row.get("Ticker") or "").upper().strip(),
        transaction=str(row.get("Transaction") or "").strip(),
        transaction_date=str(row.get("TransactionDate") or "")[:10],
        report_date=str(row.get("ReportDate") or "")[:10],
        size=str(row.get("Range") or row.get("Amount") or ""),
        raw=row,
    )


def _norm_insider(row: dict[str, Any]) -> DisclosedTrade:
    code = str(row.get("TransactionCode") or row.get("AcquiredDisposedCode") or "").strip()
    transaction = {"P": "Purchase", "S": "Sale", "A": "Purchase", "D": "Sale"}.get(code, code or "Trade")
    shares = row.get("Shares")
    price = row.get("PricePerShare")
    size = f"{shares} sh @ {price}" if shares is not None else str(row.get("Value") or "")
    return DisclosedTrade(
        source="insider",
        investor=str(row.get("Name") or row.get("Insider") or "").strip(),
        ticker=str(row.get("Ticker") or "").upper().strip(),
        transaction=transaction,
        transaction_date=str(row.get("Date") or row.get("TransactionDate") or "")[:10],
        report_date=str(row.get("fileDate") or row.get("ReportDate") or "")[:10],
        size=size,
        raw=row,
    )


class QuiverClient:
    """Thin REST wrapper. Pass ``session`` to inject a fake in tests."""

    def __init__(self, api_key: str, base_url: str = BASE_URL,
                 session: requests.Session | None = None, timeout: float = 30.0):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.timeout = timeout

    # -- low level -----------------------------------------------------------
    def _get(self, path: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        resp = self.session.get(
            f"{self.base_url}/{path.lstrip('/')}",
            headers={"Authorization": f"Token {self.api_key}", "Accept": "application/json"},
            params=params,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):  # some endpoints wrap results
            data = data.get("data") or data.get("results") or []
        return list(data)

    # -- public --------------------------------------------------------------
    def congress_trades(self, ticker: str | None = None) -> list[DisclosedTrade]:
        path = f"historical/congresstrading/{ticker.upper()}" if ticker else "live/congresstrading"
        return [_norm_congress(r) for r in self._get(path)]

    def insider_trades(self, ticker: str | None = None) -> list[DisclosedTrade]:
        path = f"historical/insiders/{ticker.upper()}" if ticker else "live/insiders"
        return [_norm_insider(r) for r in self._get(path)]

    def trades_for_investor(self, investor: str, source: str = "congress") -> list[DisclosedTrade]:
        """All recent disclosed trades whose investor name contains ``investor``."""
        rows = self.congress_trades() if source == "congress" else self.insider_trades()
        return filter_by_investor(rows, investor)

    def history_for_ticker(self, investor: str, ticker: str,
                           source: str = "congress") -> list[DisclosedTrade]:
        rows = self.congress_trades(ticker) if source == "congress" else self.insider_trades(ticker)
        return filter_by_investor(rows, investor)


def matches_investor(name: str, investor: str) -> bool:
    """True if ``investor`` is a substring of ``name`` or all its words appear in ``name``.

    Exchanges print names in varying order ("KACHOLIA ASHISH" for Ashish Kacholia), so
    word-set matching is what makes WATCH_INVESTOR usable.
    """
    needle = " ".join(investor.lower().split())
    hay = " ".join(name.lower().replace(".", " ").split())
    if not needle:
        return False
    if needle in hay:
        return True
    hay_words = set(hay.split())
    return all(w in hay_words for w in needle.split())


def filter_by_investor(rows: Iterable[DisclosedTrade], investor: str) -> list[DisclosedTrade]:
    out = [t for t in rows if matches_investor(t.investor, investor)]
    out.sort(key=lambda t: (t.report_date, t.transaction_date), reverse=True)
    return out
