"""Promoter pledges from NSE's quarterly shareholding-pattern filings: the fallback when NSE's pledge list is empty.

NSE's pledged-data page (``api/corporate-pledgedata``) sometimes publishes nothing at all (its own count line reads 0
companies). Every listed company still files its shareholding pattern each quarter, and that XBRL file states how many
of the promoter group's shares are pledged. This module reads it, one company at a time:

* ``api/corporate-share-holdings-master?index=equities&symbol=X`` lists the company's filings (quarter end, filing
  time, XBRL link), through the NSE client (cookies, breaker, back-off);
* the latest filing's XBRL (about 300 KB, on ``nsearchives.nseindia.com``) gives ``NumberOfSharesEncumberedUnderPledged``
  and ``NumberOfShares`` for the ``ShareholdingOfPromoterAndPromoterGroup`` context. Pledged % of the promoter holding is
  their ratio: the same measure as NSE's pledge list, so the 20% and 5-point rules mean the same.

Filings never change once filed (a revision gets a new record id), so each one is cached on disk for good
(``cache/shp_pledge/<SYMBOL>.json``) and the filing list is asked again only every ``RECHECK_DAYS``. Work is spread over
watch ticks within the tick's time budget. Read-only and public: no login, no orders.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

log = logging.getLogger(__name__)

MASTER_PATH = "api/corporate-share-holdings-master"
REFERER = "https://www.nseindia.com/companies-listing/corporate-filings-shareholding-pattern"
XBRL_HOST = "nsearchives.nseindia.com"   # the only host an XBRL link may point at
RECHECK_DAYS = 7          # ask a company's filing list again after this many days (new quarters arrive in ~3 weeks)
READY_SHARE = 0.9         # the list is usable once this share of the stocks has a filing
PROMOTER_CTX = "ShareholdingOfPromoterAndPromoterGroup_ContextI"
_TAG = r'<[A-Za-z0-9_-]+:{tag}\s+contextRef="{ctx}"[^>]*>\s*([-0-9.]+)\s*<'


def _num(xml: str, tag: str, ctx: str = PROMOTER_CTX) -> float | None:
    m = re.search(_TAG.format(tag=tag, ctx=re.escape(ctx)), xml)
    try:
        return float(m.group(1)) if m else None
    except ValueError:
        return None


def parse_shp_pledge(xml: str | bytes) -> dict[str, Any] | None:
    """{"pct": pledged % of the promoter holding, "pledged", "promoter_shares"} from one filing's XBRL, or None when
    the file does not state the promoter group's shares. A company whose promoters hold nothing pledges 0%."""
    text = xml.decode("utf-8", errors="replace") if isinstance(xml, bytes) else str(xml)
    held = _num(text, "NumberOfShares")
    if held is None:
        held = _num(text, "NumberOfFullyPaidUpEquityShares")
    if held is None:
        return None
    pledged = _num(text, "NumberOfSharesEncumberedUnderPledged") or 0.0
    pct = 0.0 if held <= 0 else min(100.0, max(0.0, pledged / held * 100))
    return {"pct": round(pct, 2), "pledged": int(pledged), "promoter_shares": int(held)}


def _quarter(row: dict[str, Any]) -> date | None:
    try:
        return datetime.strptime(str(row.get("date") or "").strip().title(), "%d-%b-%Y").date()
    except ValueError:
        return None


def latest_filing(rows: Any) -> dict[str, Any] | None:
    """The newest quarter's filing from the filing list (a revised one, filed later, wins within its quarter)."""
    best: tuple[date, str, dict[str, Any]] | None = None
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict) or not r.get("xbrl"):
            continue
        q = _quarter(r)
        if q is None:
            continue
        key = (q, str(r.get("submissionDate") or r.get("broadcastDate") or ""))
        if best is None or key > best[:2]:
            best = (q, key[1], r)
    return best[2] if best else None


def _pct_or_none(v: Any) -> float | None:
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def _filed(row: dict[str, Any]) -> str | None:
    from .filing_time import parse_ts
    ts = parse_ts(row.get("broadcastDate") or row.get("submissionDate"))
    return ts.isoformat() if ts is not None else None


class ShpPledges:
    """Per-company cache and the incremental builder. ``today`` and ``monotonic`` are for tests."""

    def __init__(self, cache_dir: Path, today: Callable[[], date] | None = None,
                 monotonic: Callable[[], float] = time.monotonic):
        self.dir = Path(cache_dir) / "shp_pledge"
        self._today = today or date.today
        self._mono = monotonic

    def _path(self, sym: str) -> Path:
        return self.dir / f"{re.sub(r'[^A-Z0-9&_-]', '_', sym.upper())}.json"

    def cached(self, sym: str) -> dict[str, Any]:
        try:
            d = json.loads(self._path(sym).read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, sym: str, d: dict[str, Any]) -> None:
        from .state import atomic_write
        self.dir.mkdir(parents=True, exist_ok=True)
        atomic_write(self._path(sym), json.dumps(d, sort_keys=True))

    def _stale(self, sym: str) -> bool:
        try:
            checked = date.fromisoformat(str(self.cached(sym).get("checked")))
        except ValueError:
            return True
        return (self._today() - checked).days >= RECHECK_DAYS

    def refresh_one(self, client: Any, sym: str) -> dict[str, Any] | None:
        """Ask the filing list once; fetch the latest XBRL only when it is a filing not yet cached. Raises on failure."""
        rows = client._get(MASTER_PATH, params={"index": "equities", "symbol": sym}, referer=REFERER)
        row = latest_filing(rows)
        d = self.cached(sym)
        d["checked"] = self._today().isoformat()
        if row is None:
            d.setdefault("filing", None)
            self._save(sym, d)
            return d.get("filing")
        rec = str(row.get("recordId") or row.get("xbrl"))
        if (d.get("filing") or {}).get("record") != rec:
            url = str(row["xbrl"])
            u = urlparse(url)
            if u.scheme != "https" or u.hostname != XBRL_HOST:
                raise ValueError(f"unexpected XBRL link host for {sym}: {u.hostname}")
            from .nse import HEADERS   # the archive host stalls requests without a browser's headers
            resp = client.session.get(url, headers={**HEADERS, "Accept": "*/*", "Referer": REFERER},
                                      timeout=getattr(client, "timeout", 20))
            resp.raise_for_status()
            parsed = parse_shp_pledge(resp.content)
            if parsed is None and _pct_or_none(row.get("pr_and_prgrp")) == 0:
                parsed = {"pct": 0.0, "pledged": 0, "promoter_shares": 0}   # no promoters (e.g. after a merger)
            if parsed is None:
                raise ValueError(f"{sym}: shareholding XBRL states no promoter shares")
            q = _quarter(row)
            d["filing"] = {**parsed, "record": rec, "quarter": q.isoformat() if q else None, "filed": _filed(row)}
        self._save(sym, d)
        return d.get("filing")

    def build(self, client: Any, symbols: Iterable[str], deadline: float | None = None) -> dict[str, Any]:
        """Refresh what is stale until ``deadline`` (time.monotonic), then report. Returns {"symbols": {sym: {"pct",
        "filed"}} for every stock with a cached filing, "done": stocks with a filing, "total", "ready": bool,
        "errors": count}. Never raises."""
        syms = list(dict.fromkeys(str(s).strip().upper() for s in symbols if str(s).strip()))
        errors = 0
        for sym in syms:
            if deadline is not None and self._mono() >= deadline:
                break
            if not self._stale(sym):
                continue
            try:
                self.refresh_one(client, sym)
            except Exception as e:  # noqa: BLE001 - one company's filing missing never stops the rest
                errors += 1
                log.debug("shareholding pledge for %s: %s", sym, e)
                if errors >= 5 and errors * 2 > len(syms):   # NSE refusing everything: stop, try again later
                    break
        out: dict[str, Any] = {}
        for sym in syms:
            f = self.cached(sym).get("filing")
            if isinstance(f, dict) and isinstance(f.get("pct"), (int, float)):
                out[sym] = {"pct": float(f["pct"]), "filed": f.get("filed")}
        total = len(syms)
        return {"symbols": out, "done": len(out), "total": total, "errors": errors,
                "ready": total > 0 and len(out) >= READY_SHARE * total}
