"""Insider trades across every NSE company (insider_watch.py): the book's rows, the big open-market insider buys,
one stock's trades, the per-stock summary, alerts only for your stocks (never a month of old filings at once), the
watch's 15-minute refresh, GET /api/insider, the newest-first filing order and the page wiring. Fakes only: no network."""
from datetime import datetime, timedelta
from pathlib import Path

from trading_agent import health, insider_watch as iw, nse
from trading_agent.quiver import DisclosedTrade
from trading_agent.timezones import IST

from .test_ui import _get, server  # noqa: F401  (fixture)

NOW = datetime(2026, 10, 19, 11, 0, tzinfo=IST)
UI = Path(__file__).resolve().parents[1] / "trading_agent" / "ui"


def pit(sym, person, side="Purchase", qty=10000, value=2e7, cat="Promoters", mode="Market Purchase", day="2026-10-15",
        inst="Equity Shares"):
    return DisclosedTrade(source="insider", investor=person, ticker=sym, transaction=side, transaction_date=day,
                          report_date=day, size=f"{qty} sh (₹{value})" if value else f"{qty} sh",
                          raw={"category": cat, "mode": mode, "instrument": inst, "company": sym.title() + " Ltd",
                               "filed_at": f"{day} 18:00:00"})


ROWS = [pit("ALPHA", "A Promoter"), pit("ALPHA", "A Promoter", qty=5000, value=1e7, day="2026-10-16"),
        pit("BETA", "B Director", cat="Director", value=1.5e7),
        pit("GAMMA", "G Employee", cat="Employees/Designated Persons", value=9e7),          # not an insider role
        pit("DELTA", "D Promoter", mode="ESOP", value=5e7),                                  # not a market buy
        pit("EPS", "E Promoter", mode="Inter-se Transfer", value=5e7),                       # not a market buy
        pit("ZETA", "Z Promoter", mode="Pledge Creation", value=5e7),                        # a pledge
        pit("SMALL", "S Promoter", value=5e6),                                               # under ₹1 crore
        pit("ALPHA", "A Seller", side="Sale", cat="Promoter Group", mode="Market Sale", value=3e6),
        DisclosedTrade(source="insider", investor="", ticker="X", transaction="Purchase", transaction_date="", report_date="",
                       size="", raw={})]


class Client:
    def __init__(self, rows=ROWS):
        self.rows, self.calls = rows, 0

    def insider_trades(self, days=30, end=None, symbol=None):
        self.calls += 1
        if isinstance(self.rows, BaseException):
            raise self.rows
        return list(self.rows)


def test_rows_buys_trades_and_summary(tmp_path):
    rows = [r for r in (iw.compact(t) for t in ROWS) if r]
    assert len(rows) == 9 and rows[0]["qty"] == 10000 and rows[0]["value"] == 2e7 and rows[0]["price"] == 2000
    big = iw.notable_buys(rows)
    assert [(g["symbol"], g["value"], g["trades"], g["who"]) for g in big] == [("ALPHA", 3e7, 2, "Promoter"),
                                                                              ("BETA", 1.5e7, 1, "Director")]
    assert big[0]["first"] == "2026-10-15" and big[0]["last"] == "2026-10-16" and big[0]["avg_price"] == 2000
    assert [g["symbol"] for g in iw.notable_buys(rows, 4e6)][-1] == "SMALL"
    assert [r["side"] for r in iw.trades_in(rows, "alpha")] == ["buy", "buy", "sell"]
    assert iw.trades_in(rows, "ZETA") == []                                       # a pledge is not a trade here
    s = iw.summary(rows)
    assert s["ALPHA"] == {"buys": 2, "sells": 1, "buy_value": 3e7, "sell_value": 3e6, "market_buys": 2}
    assert "ZETA" not in s and s["DELTA"]["market_buys"] == 0


def test_refresh_alerts_only_new_trades_in_your_stocks(tmp_path):
    book = iw.InsiderBook(tmp_path)
    c = Client()
    book.refresh(c, NOW)
    assert health.read_runs(tmp_path)["insider_market"]["rows"] == 9
    assert book.new_for(["ALPHA", "BETA"]) == []                               # first time: everything marked, nothing sent
    c.rows = ROWS + [pit("ALPHA", "A Promoter", qty=7000, value=1.4e7, day="2026-10-19"), pit("OTHER", "O P")]
    book.refresh(c, NOW + timedelta(minutes=15))
    fresh = book.new_for(["ALPHA", "BETA"])
    assert [(r["symbol"], r["qty"]) for r in fresh] == [("ALPHA", 7000)]
    assert book.new_for(["ALPHA", "OTHER"])[0]["symbol"] == "OTHER" and book.new_for(["ALPHA", "OTHER"]) == []
    subject, body = iw.alert_text(fresh)
    assert subject == "[INSIDER] 1 insider trade(s) in ALPHA" and "A Promoter (Promoter) bought 7,000 shares, ₹1.40 cr" in body
    v = book.view()
    assert v["notable"][0]["symbol"] == "ALPHA" and v["summary"]["ALPHA"]["buys"] == 3 and v["min_value"] == 1e7
    assert [t["date"] for t in book.view("alpha")["trades"]][:1] == ["2026-10-19"]


def test_a_failed_refresh_keeps_the_last_rows(tmp_path):
    book = iw.InsiderBook(tmp_path)
    book.refresh(Client(), NOW)
    try:
        book.refresh(Client(ConnectionError("NSE refused")), NOW + timedelta(minutes=20))
    except ConnectionError:
        pass
    d = book.load()
    assert len(d["rows"]) == 9 and "NSE refused" in d["error"] and "NSE refused" in health.read_runs(tmp_path)["insider_market"]["error"]
    assert not book.due(NOW + timedelta(minutes=30)) and book.due(NOW + timedelta(minutes=36))   # 15 min after the failure too


def test_the_watch_refreshes_on_a_worker_every_15_minutes(settings, monkeypatch):
    from trading_agent.watch import Watcher
    settings.market, settings.insider_trades, settings.big_money = "in", True, False
    sent = []

    class Notifier:
        def send(self, subject, body):
            sent.append(subject)
    w = Watcher.__new__(Watcher)
    w.settings, w._data, w._notifier, w._insider_thread = settings, Client(), Notifier(), None
    w.interesting_tickers = lambda: ["ALPHA"]
    made = []
    monkeypatch.setattr(nse, "NSEClient", lambda **kw: made.append(kw) or w._data)
    assert w.poll_insider(NOW.replace(hour=6)) is None                          # before 07:00 IST
    assert w.poll_insider(NOW) == "started"
    w._insider_thread.join(5)
    assert w._data.calls == 1 and made and iw.book_for(settings).load()["rows"]
    assert w.poll_insider(NOW + timedelta(minutes=5)) is None                   # not due yet
    settings.insider_trades = False
    assert w.poll_insider(NOW + timedelta(hours=1)) is None


def test_api_insider(server):  # noqa: F811
    base, app = server
    iw.book_for(app.settings).refresh(Client(), NOW)
    _, v = _get(base + "/api/insider")
    assert v["notable"][0]["symbol"] == "ALPHA" and "ALPHA" in v["summary"]
    _, one = _get(base + "/api/insider?symbol=beta")
    assert one["symbol"] == "BETA" and one["trades"][0]["person"] == "B Director"
    status, _ = _get(base + "/api/insider?symbol=" + "%3Cscript%3E")
    assert status == 400


def test_filings_are_read_newest_first(tmp_path):
    from datetime import date
    c = nse.NSEClient(cache_dir=tmp_path, max_new_downloads=1)
    c.insider_filings = lambda start, end, symbol=None: [
        {"xmlFileName": "https://nsearchives.nseindia.com/old.xml", "broadcastDateTime": "21-Sep-2026 10:00:00"},
        {"xmlFileName": "https://nsearchives.nseindia.com/new.xml", "broadcastDateTime": "19-Oct-2026 18:43:32"}]
    got = []
    c._pit_fetch = lambda url, f: got.append(url) or []
    c.insider_xbrl_trades(date(2026, 9, 20), date(2026, 10, 19))
    assert got == ["https://nsearchives.nseindia.com/new.xml"]                 # one download allowed: the newest


def test_the_pages_show_it():
    index = (UI / "index.html").read_text(encoding="utf-8")
    js = (UI / "stockpage.js").read_text(encoding="utf-8")
    assert "<h2>Promoter and director buying</h2>" in index and "all.slice(0, 10)" in index and 'api("/api/insider")' in index
    assert "${insiderChip(h.symbol)}" in index and "function renderInsider" in index
    assert 'sec("insider", "Insider trades", insiderHtml(), dflt, insiderHint())' in js
    assert 'api("/api/insider?symbol=" + encodeURIComponent(st.ticker))' in js
