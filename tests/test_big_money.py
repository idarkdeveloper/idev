"""Institutions in every stock's bulk and block deals (big_money.py): which clients count, the big deals summed per
fund, stock and side, one stock's deals, alerts only for your stocks (never a month of old deals at once), the
15-minute back-off, GET /api/big-money and the page wiring. Fakes only: no network."""
from datetime import datetime, timedelta
from pathlib import Path

from trading_agent import big_money as bm, health
from trading_agent.quiver import DisclosedTrade
from trading_agent.timezones import IST

from .test_ui import _get, server  # noqa: F401  (fixture)

NOW = datetime(2026, 10, 19, 11, 0, tzinfo=IST)
UI = Path(__file__).resolve().parents[1] / "trading_agent" / "ui"


def deal(sym, client, side="Purchase", qty=1_000_000, price=200.0, kind="block", day="2026-10-08", exchange="NSE"):
    return DisclosedTrade(source=kind, investor=client, ticker=sym, transaction=side, transaction_date=day, report_date=day,
                          size=f"{qty} sh @ ₹{price}", raw={"name": sym.title() + " Ltd"}, exchange=exchange)


NSE_ROWS = {"bulk": [deal("ITC", "SBI MUTUAL FUND", kind="bulk"), deal("ITC", "SBI MUTUAL FUND", qty=500_000, kind="bulk", day="2026-10-09"),
                     deal("ITC", "ASHISH KACHOLIA", kind="bulk"),                                  # a person, not a fund
                     deal("ITC", "MORGAN STANLEY ASIA (SINGAPORE) PTE", kind="bulk")],             # a broker's desk
            "block": [deal("ITC", "GQG PARTNERS EMERGING MARKETS EQUITY FUND", side="Sale", qty=4_000_000),
                      deal("SMALL", "HDFC MUTUAL FUND", qty=1000),                                 # ₹2 lakh: under ₹10 crore
                      deal("ITC", "SBI MUTUAL FUND", kind="block")]}                              # same trade listed twice


class Client:
    def __init__(self):
        self.fail = None

    def historical_deals(self, days, kind):
        if self.fail:
            raise self.fail
        return list(NSE_ROWS[kind])

    def _bse_rows(self, source, days, investors):
        assert source == "deals" and investors is None
        return [deal("BSESTK", "LIFE INSURANCE CORPORATION OF INDIA", exchange="BSE", kind="bulk")]


def test_only_institutions_and_the_big_ones(tmp_path):
    book = bm.BigMoneyBook(tmp_path)
    d = book.refresh(Client(), NOW)
    assert sorted({r["client"] for r in d["rows"]}) == ["GQG PARTNERS EMERGING MARKETS EQUITY FUND", "HDFC MUTUAL FUND",
                                                         "LIFE INSURANCE CORPORATION OF INDIA", "SBI MUTUAL FUND"]
    assert len(d["rows"]) == 5 and health.read_runs(tmp_path)["big_money"]["rows"] == 5   # the bulk+block twin dropped
    v = book.view()
    assert [(g["symbol"], g["side"], g["value"]) for g in v["notable"]] == [("ITC", "sell", 8e8), ("ITC", "buy", 3e8),
                                                                           ("BSESTK", "buy", 2e8)]
    assert v["notable"][1]["deals"] == 2 and v["notable"][1]["first"] == "2026-10-08" and v["notable"][1]["last"] == "2026-10-09"
    assert v["summary"]["ITC"] == {"buys": 2, "sells": 1, "buy_value": 3e8, "sell_value": 8e8}
    assert [t["date"] for t in book.view("itc")["trades"]] == ["2026-10-09", "2026-10-08", "2026-10-08"]


def test_alerts_only_new_deals_in_your_stocks_and_backs_off_after_a_failure(tmp_path):
    book = bm.BigMoneyBook(tmp_path)
    c = Client()
    book.refresh(c, NOW)
    assert book.new_for(["ITC"]) == []                                         # first time: marked, not sent
    NSE_ROWS["bulk"].append(deal("ITC", "NIPPON INDIA MUTUAL FUND", day="2026-10-19", kind="bulk"))
    try:
        book.refresh(c, NOW + timedelta(minutes=15))
        fresh = book.new_for(["ITC"])
    finally:
        NSE_ROWS["bulk"].pop()
    assert [r["client"] for r in fresh] == ["NIPPON INDIA MUTUAL FUND"]
    subject, body = bm.alert_text(fresh)
    assert subject == "[INSTITUTION] 1 fund deal(s) in ITC" and "NIPPON INDIA MUTUAL FUND bought 1,000,000 shares at ₹200.00 (₹20.00 cr)" in body
    c.fail = ConnectionError("NSE refused")
    try:
        book.refresh(c, NOW + timedelta(minutes=40))
    except ConnectionError:
        pass
    assert book.load()["rows"] and not book.due(NOW + timedelta(minutes=50)) and book.due(NOW + timedelta(minutes=56))


def test_api_big_money(server):  # noqa: F811
    base, app = server
    bm.book_for(app.settings).refresh(Client(), NOW)
    _, v = _get(base + "/api/big-money")
    assert v["notable"][0]["client"].startswith("GQG") and v["min_value"] == 1e8
    _, one = _get(base + "/api/big-money?symbol=bsestk")
    assert one["trades"][0]["exchange"] == "BSE"
    assert _get(base + "/api/big-money?symbol=%3Cx%3E")[0] == 400


def test_the_pages_show_it():
    index = (UI / "index.html").read_text(encoding="utf-8")
    js = (UI / "stockpage.js").read_text(encoding="utf-8")
    assert "<h2>Funds buying and selling</h2>" in index and 'api("/api/big-money")' in index and "${fundsChip(h.symbol)}" in index
    assert 'sec("funds", "Fund deals", fundsHtml(), dflt, fundsHint())' in js
    assert 'api("/api/big-money?symbol=" + encodeURIComponent(st.ticker))' in js
