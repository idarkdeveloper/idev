"""US, UK and Japan stocks for viewing only (markets.py): the ticker forms, the Yahoo symbols, the dashboard's look-up,
chart, announcements and stock page for them, orders refused, and the page's currency formatting. Fake prices and
fake Yahoo modules: no network."""
import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from trading_agent import markets
from trading_agent.fundamentals import YahooFundamentals
from trading_agent.momentum import MomentumScreen
from trading_agent.prices import YahooPrices
from trading_agent.stockpage import StockService, StockSources

from .test_ui import _get, _post, server  # noqa: F401  (fixture)

UI = Path(__file__).resolve().parents[1] / "trading_agent" / "ui"


def test_ticker_forms_and_yahoo_symbols():
    assert [markets.foreign_market(t) for t in ("AAPL.US", "vod.l", "7203.T", "TCS", "TCS.BO", ".US", "")] == \
        ["US", "UK", "JP", None, None, None, None]
    assert markets.qualify("aapl", "US") == "AAPL.US" and markets.qualify("vod", "UK") == "VOD.L"
    assert markets.qualify("7203", "JP") == "7203.T" and markets.qualify("tcs", "IN") == "TCS"
    assert markets.qualify("AAPL.US", "UK") == "AAPL.US" and markets.qualify("TCS.BO", "US") == "TCS.BO"
    assert markets.yahoo_symbol("AAPL.US") == "AAPL" and markets.yahoo_symbol("VOD.L") == "VOD.L"
    assert markets.yahoo_symbol("TCS") == "TCS"
    p = YahooPrices()
    assert p.yahoo_symbol("AAPL.US") == "AAPL" and p.yahoo_symbol("7203.T") == "7203.T" and p.yahoo_symbol("TCS") == "TCS.NS"
    i = markets.info("vod.l")
    assert i["market"] == "UK" and i["currency"] == "GBp" and i["view_only"] and markets.info("TCS") is None


def test_company_data_asks_yahoo_for_the_right_symbol(tmp_path):
    asked = []

    class Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"quoteSummary": {"result": [{"price": {"longName": "Apple Inc."}}]}}

    class Sess:
        headers: dict = {}

        def get(self, url, **kw):
            asked.append(url)
            return Resp()
    f = YahooFundamentals(tmp_path, session=Sess())
    f._crumb = "c"
    for sym in ("AAPL.US", "VOD.L", "TCS"):
        try:
            f.fetch_modules(sym, "price")
        except Exception:  # noqa: BLE001 - only the URL matters here
            pass
    got = [u.split("/quoteSummary/")[1].split("?")[0] for u in asked if "/quoteSummary/" in u]
    assert got[:3] == ["AAPL", "VOD.L", "TCS.NS"]


class Bars:
    """Fake daily bars for any symbol, recording which were asked for."""

    def __init__(self):
        self.asked = []

    def history(self, sym, range_="2y"):
        self.asked.append(sym)
        return [{"date": f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}", "close": 100 + i * 0.5, "adj_close": 100 + i * 0.5,
                 "volume": 10} for i in range(260)]

    def latest_price(self, sym):
        return 229.5

    def history_ohlc(self, sym, span="5y"):
        self.asked.append(("ohlc", sym))
        return [{"date": f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}", "open": 100.0, "high": 101.0, "low": 99.0,
                 "close": 100.0 + i, "volume": 5} for i in range(60)]


def test_the_dashboard_shows_a_foreign_stock_for_viewing_only(server):  # noqa: F811
    base, app = server
    src = Bars()
    app.prices, app.momentum = src, MomentumScreen(src)

    class NoIndianData:
        def announcements(self, *a, **k):
            raise AssertionError("NSE announcements asked for a US stock")
    app._data = NoIndianData()
    status, r = _get(base + "/api/lookup?ticker=aapl.us")
    assert status == 200 and r["ticker"] == "AAPL.US" and r["view_only"] and r["market"] == "US"
    assert r["currency"] == "USD" and r["price"] == 229.5 and len(r["history"]) == 252
    assert r["momentum"]["verdict"] == "strong" and "band" not in r and "filter_flags" not in r and "position" not in r
    assert "AAPL.US" in src.asked                              # the prices module turns it into Yahoo's "AAPL"
    _, a = _get(base + "/api/announcements?ticker=AAPL.US")
    assert a["announcements"] == [] and "Indian stocks only" in a["error"]
    _, c = _get(base + "/api/candles?ticker=VOD.L&range=1Y")
    assert c["exchange"] == "UK" and c["bars"] and ("ohlc", "VOD.L") in src.asked
    status, j = _post(base + "/demo/api/order", {"symbol": "7203.T", "side": "buy", "qty": 1})
    assert status == 400 and j["error"] == markets.VIEW_ONLY_NOTE
    status, j = _post(base + "/demo/api/order", {"symbol": "AAPL.US", "side": "buy", "qty": 1, "limit_price": 200})
    assert status == 400 and "viewing only" in j["error"]


def test_the_stock_page_payload_for_a_foreign_stock_skips_the_indian_parts():
    asked = []

    def fail(name):
        def f(*a, **k):
            raise AssertionError(name + " asked for a foreign stock")
        return f

    def modules(sym):
        asked.append(sym)
        return {"price": {"regularMarketPrice": {"raw": 120.5}, "regularMarketPreviousClose": {"raw": 122.0},
                          "longName": "Vodafone Group", "currency": "GBp"},
                "summaryDetail": {"marketCap": {"raw": 2.79e10}, "fiftyTwoWeekLow": {"raw": 85.2}, "fiftyTwoWeekHigh": {"raw": 132.1}},
                "assetProfile": {"sector": "Communication Services", "longBusinessSummary": "Phones."}}
    svc = StockService(StockSources(modules=modules, shareholding=fail("shareholding"), daily_bars=lambda b, e: [],
                                    universe=fail("universe"), peer_pe=fail("peer_pe"), price_pair=fail("price_pair"),
                                    face_value=fail("face_value"), band=fail("band")))
    d = svc.get("VOD.L", "NSE")
    assert asked == ["VOD.L"] and d["foreign"] and d["exchange"] == "LSE" and d["market"] == "UK" and d["currency"] == "GBp"
    assert d["name"] == "Vodafone Group" and d["price"]["last"] == 120.5 and d["about"]["sector"] == "Communication Services"
    assert d["similar"] == [] and d["shareholding"]["state"] == "unavailable" and d["view_only"]
    assert d["fundamentals"]["market_cap_cr"] == pytest.approx(2.79e10 / 1e7)
    assert svc.get("VOD.L") is d                                # cached under the whole ticker


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_the_page_formats_each_market_in_its_own_currency(tmp_path):
    script = textwrap.dedent("""
        const document = {getElementById: () => null, body: null, addEventListener(){}};
        const window = {addEventListener(){}, matchMedia: () => ({matches: false, addEventListener(){}})};
        eval(require('fs').readFileSync(process.argv[2], 'utf8') + '; globalThis.TA = window.TA;');
        console.log(JSON.stringify({
          px: [TA.px(343.456, "USD"), TA.px(120.55, "GBp"), TA.px(3102, "JPY"), TA.px(2489.5, "INR"), TA.px(12.5, "EUR"), TA.px(null, "USD"), TA.px(-1.5, "USD")],
          big: [TA.bigMoney(5.01e12, "USD"), TA.bigMoney(2.79e12, "GBp"), TA.bigMoney(3.65e13, "JPY"), TA.bigMoney(null, "USD")],
          q: [TA.qualifyTicker(" aapl ", "US"), TA.qualifyTicker("vod", "UK"), TA.qualifyTicker("7203", "JP"),
              TA.qualifyTicker("tcs", "IN"), TA.qualifyTicker("AAPL.US", "JP"), TA.qualifyTicker("", "US")],
          fm: [TA.foreignMarket("AAPL.US"), TA.foreignMarket("7203.t"), TA.foreignMarket("TCS")]}));
    """)
    f = tmp_path / "t.js"
    f.write_text(script, encoding="utf-8")
    r = subprocess.run(["node", str(f), str(UI / "common.js")], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["px"] == ["$343.46", "120.55p", "¥3,102", "₹2,489.50", "EUR 12.50", "n/a", "−$1.50"]
    assert out["big"] == ["$5.01T", "£27.9B", "¥36.5T", "n/a"]
    assert out["q"] == ["AAPL.US", "VOD.L", "7203.T", "TCS", "AAPL.US", ""]
    assert out["fm"] == ["US", "JP", None]


def test_the_look_up_card_has_a_market_picker_and_the_stock_page_hides_the_indian_parts():
    index = (UI / "index.html").read_text(encoding="utf-8")
    js = (UI / "stockpage.js").read_text(encoding="utf-8")
    assert '<select id="lk-market"' in index and '<option value="JP">Japan</option>' in index
    assert "TA.qualifyTicker($(\"lk-ticker\").value, $(\"lk-market\").value)" in index
    assert "function renderForeignCard" in index and "if(r.view_only) return renderForeignCard(r);" in index
    assert "const foreign = () =>" in js and 'fx ? "" : sec("checklist"' in js and "fund || fx ?" in js
    assert "View only: no orders for US, UK or Japan stocks." in js and "currency: r.view_only ? r.currency : null" in js


TESLA_SEARCH = [
    {"symbol": "TSLA", "longname": "Tesla, Inc.", "exchDisp": "NASDAQ", "quoteType": "EQUITY"},
    {"symbol": "STSLA=F", "shortname": "Tesla futures", "quoteType": "FUTURE"},
    {"symbol": "TSLT", "longname": "T-REX 2X Long Tesla Daily Target ETF", "exchDisp": "BATS", "quoteType": "ETF"},
    {"symbol": "TSLA.NE", "longname": "Tesla, Inc.", "exchDisp": "NEO", "quoteType": "EQUITY"},
    {"symbol": "TL0.DE", "longname": "Tesla, Inc.", "quoteType": "EQUITY"}, {"quoteType": "EQUITY"}, "junk"]


def test_search_results_are_narrowed_to_the_market():
    us = markets.search_matches(TESLA_SEARCH, "US")
    assert [m["ticker"] for m in us] == ["TSLA.US", "TSLT.US"] and us[0]["name"] == "Tesla, Inc."
    uk = markets.search_matches([{"symbol": "VOD.L", "shortname": "Vodafone", "quoteType": "EQUITY"},
                                 {"symbol": "VOD", "quoteType": "EQUITY"}, {"symbol": ".L", "quoteType": "EQUITY"}], "UK")
    assert [m["ticker"] for m in uk] == ["VOD.L"]
    jp = markets.search_matches([{"symbol": "7203.T", "longname": "Toyota Motor", "quoteType": "EQUITY"}], "jp")
    assert jp[0]["ticker"] == "7203.T"
    assert markets.search_matches(TESLA_SEARCH, "IN") == [] and markets.search_matches(None, "US") == []


def test_yahoo_search_asks_for_the_typed_text(tmp_path):
    asked = []

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"quotes": TESLA_SEARCH}

    class Sess:
        def get(self, url, params=None, **kw):
            asked.append((url, params))
            return Resp()
    assert YahooFundamentals(tmp_path, session=Sess()).search("TESLA") == TESLA_SEARCH
    assert asked[0][0].endswith("/v1/finance/search") and asked[0][1]["q"] == "TESLA" and asked[0][1]["newsCount"] == 0


class FakeYahoo:
    """fetch_modules knows TSLA.US only; search answers Tesla's results or raises."""

    def __init__(self, quotes=TESLA_SEARCH):
        self.quotes, self.searched, self.modules = quotes, [], []

    def fetch_modules(self, sym, modules):
        self.modules.append(sym)
        if sym != "TSLA.US":
            raise LookupError(f"Yahoo has no fundamentals for {sym}")
        return {"price": {"longName": "Tesla, Inc.", "currency": "USD"}}

    def search(self, text):
        self.searched.append(text)
        if isinstance(self.quotes, BaseException):
            raise self.quotes
        return self.quotes


def _live(app, provider):
    app.demo_trades = None      # as on the server: the offline sample skips market-data calls
    app.quote_service.provider = provider
    return app


def test_a_company_name_finds_its_ticker_and_a_real_ticker_is_kept(server):  # noqa: F811
    _, app = server
    y = FakeYahoo()
    _live(app, y)
    assert app.resolve("tesla.us") == ("TSLA.US", "Tesla, Inc.") and y.searched == ["TESLA"]
    assert app.resolve("TESLA.US") == ("TSLA.US", "Tesla, Inc.") and y.searched == ["TESLA"]   # kept for a day
    assert app.resolve("TSLA.US") == ("TSLA.US", None) and y.searched == ["TESLA"]             # known: no search
    y.quotes = []
    assert app.resolve("NOSUCH.US") == ("NOSUCH.US", None)
    y.quotes = ConnectionError("down")
    assert app.resolve("OTHER.US") == ("OTHER.US", None)


def test_the_look_up_card_says_what_it_matched_and_how_to_find_a_missing_stock(server):  # noqa: F811
    base, app = server
    src = Bars()
    app.prices, app.momentum = src, MomentumScreen(src)
    y = FakeYahoo()
    _live(app, y)
    _, r = _get(base + "/api/lookup?ticker=TESLA.US")
    assert r["ticker"] == "TSLA.US" and r["matched_from"] == "TESLA" and r["name"] == "Tesla, Inc."

    class NoBars(Bars):
        def history(self, sym, range_="2y"):
            raise LookupError(f"Yahoo has no data for {sym} (HTTP 404)")
    app.prices = NoBars()
    y.quotes = []
    _, r = _get(base + "/api/lookup?ticker=ZZZQ.US")
    assert r["ticker"] == "ZZZQ.US" and "No United States stock found for ZZZQ.US" in r["momentum_summary"]
    index = (UI / "index.html").read_text(encoding="utf-8")
    assert index.count('Matched "${esc(r.matched_from)}" to ${esc(sym)}') == 2
