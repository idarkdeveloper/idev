"""``python -m trading_agent telegram-preview [--type deal|news|group|brief|all] [--base-url URL]``: prints the Telegram HTML and the plain
fallback of a sample alert, without sending anything and without reading any setting, key or state."""
from __future__ import annotations

from typing import Any

from . import telegram_brief, telegram_cards

SAMPLE_REC = {
    "action": "watch", "ticker": "XPROINDIA", "confidence": "medium", "investor": "ASHISH KACHOLIA", "currency": "INR", "suggested_notional_usd": 0,
    "headline": "Ashish Kacholia sold 2.22 lakh XPROINDIA shares (~Rs21.5 cr); you hold none. Avoid for now",
    "rationale": ("Ashish Kacholia, a well-known small-cap investor, sold about 2.22 lakh XPROINDIA shares at around Rs970 in a bulk deal on 19 Oct, "
                  "roughly Rs21.5 crore. You hold none, so nothing needs selling. The Nifty is below its 200-day average and the market regime is risk-off, "
                  "so the rules make no new buys; a sale by a name-brand holder is a reason to stay on the sidelines rather than to buy. "
                  "Watch for a second disclosure, a filing on the reason for the sale, and whether the price holds Rs950."),
}
SAMPLE_DEAL = {"shares": 222000.0, "price": 970.0, "value": 215340000.0, "entities": 1, "date": "2026-10-19", "side": "Sale"}
SAMPLE_NEWS = {
    "symbol": "HDFCBANK", "name": "HDFC Bank Limited", "impact": "NEGATIVE", "headline": "HDFC Bank slips as RBI flags concerns over a rise in unsecured loan stress",
    "source": "Moneycontrol", "published": "2026-10-19T09:41:00+05:30", "url": "https://www.moneycontrol.com/news/business/hdfc-bank-1234.html",
    "takeaways": ["RBI flagged rising stress in unsecured loans at large lenders.", "HDFC Bank fell 1.8% in early trade.", "Analysts see a limited effect on this quarter's earnings."],
    "context": "Held in your portfolio: about 8% of its value.", "story": "The central bank said in a note on Monday that stress in unsecured retail loans has risen.\n\nHDFC Bank shares fell 1.8% by 9:40.",
    "sentiment": "negative"}
_FILINGS = [("2026-10-19 17:53:33", "Copy of Newspaper Publication", "Xpro India Limited has informed the Exchange about Copy of Newspaper Publication"),
            ("2026-10-19 16:10:00", "Board Meeting Intimation", "Xpro India Limited has informed the Exchange about Board Meeting on 28 Oct to consider the quarterly results"),
            ("2026-10-18 11:00:00", "Trading Window", "Xpro India Limited has informed the Exchange about closure of trading window"),
            ("2026-10-17 15:20:00", "Outcome of Board Meeting", "Xpro India Limited has informed the Exchange about dividend of Rs2 per share"),
            ("2026-10-16 10:05:00", "Appointment of Director", "Xpro India Limited has informed the Exchange about appointment of an independent director"),
            ("2026-10-15 09:30:00", "Copy of Newspaper Publication", "Xpro India Limited has informed the Exchange about Copy of Newspaper Publication")]


def sample_filings(n: int = 20) -> list[dict[str, Any]]:
    rows = [{"at": at, "symbol": "XPROINDIA", "category": cat, "text": txt} for at, cat, txt in _FILINGS]
    while len(rows) < n:
        i = len(rows)
        rows.append({"at": f"2026-10-{15 + i % 5} {10 + i % 8}:00:00", "symbol": "XPROINDIA", "category": "Copy of Newspaper Publication" if i % 2 else "Updates",
                     "text": "Xpro India Limited has informed the Exchange about Copy of Newspaper Publication" if i % 2 else f"Xpro India Limited has informed the Exchange about item {i}"})
    return rows[:n]


def sample_brief() -> dict[str, Any]:
    data = {"kind": "evening", "date": "2026-10-19",
            "groww": {"value": 186274.0, "day_pl": -1579.0, "day_pct": -0.84, "pl": -14443.0, "pl_pct": -7.2},
            "bulletin": {"nifty": {"close": 24100.5, "change_pct": -0.4}},
            "mood": {"regime": "risk_off", "score": -2, "why": ["Nifty below 200-day avg", "risk-off"], "no_new_buys": True},
            "buy_ideas": {"ideas": [{"symbol": "LAURUSLABS"}, {"symbol": "APARINDS"}]},
            "deals": {"total": 3, "deals": [{"who": ["Ashish Kacholia"], "investor": "ASHISH KACHOLIA", "transaction": "Sale", "ticker": "XPROINDIA", "size": "222000 sh"}]},
            "news": {"items": [{"symbol": "HDFCBANK", "sentiment": "negative"}]}}
    return {"summary": "", "writer": "rules", "data": data}


def previews(kind: str = "all", base_url: str | None = "https://box.tailnet.ts.net") -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = []
    if kind in ("deal", "all"):
        out.append(("deal", telegram_cards.deal_card(SAMPLE_REC, deal=SAMPLE_DEAL, holding_qty=None, market_wait=True, regime="risk_off", base_url=base_url)))
    if kind in ("news", "all"):
        out.append(("news", telegram_cards.news_card(SAMPLE_NEWS, base_url)))
    if kind in ("group", "all"):
        out.append(("group", telegram_cards.filings_card(sample_filings(20), base_url)))
    if kind in ("brief", "all"):
        b = telegram_brief.telegram_brief(sample_brief(), [base_url.split("//")[-1]] if base_url else None)
        out.append(("brief", b))
    return out


def run(kind: str = "all", base_url: str | None = "https://box.tailnet.ts.net") -> int:
    for name, card in previews(kind, base_url):
        print(f"===== {name.upper()}: HTML ({len(card['html'])} chars) =====\n{card['html']}\n")
        print(f"===== {name.upper()}: plain fallback ({len(card['plain'])} chars) =====\n{card['plain']}\n")
        print(f"===== {name.upper()}: buttons =====\n{card['button']}\n")
    return 0
