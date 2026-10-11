"""Stock screener: the metric calculations on fake bars, the background fill with pending/poll behaviour, the holdings
and deal flags, GET /api/screener, and that nothing here reaches Groww. Fakes only: no network."""
import json
import threading
import time
import urllib.error
import urllib.request
from datetime import date, timedelta

import pytest

from trading_agent import screener as sc
from trading_agent.quiver import DisclosedTrade
from trading_agent.timezones import IST
from trading_agent.ui import App, make_server

from .test_ui import _get, server  # noqa: F401  (server is a fixture)


# ---------- fake data ----------
def bars_from(closes, volumes=None, start=date(2025, 1, 1)):
    out = []
    for i, c in enumerate(closes):
        d = start + timedelta(days=i)
        out.append({"date": d.isoformat(), "close": float(c), "adj_close": float(c),
                    "volume": float(volumes[i] if volumes else 1000)})
    return out


def rising(n=300, last_volume=1000):
    vols = [1000] * n
    vols[-1] = last_volume
    return bars_from([100 + i for i in range(n)], vols)


class FakePrices:
    def __init__(self, table, gate=None):
        self.table, self.gate, self.calls = table, gate or {}, []
        self.lock = threading.Lock()

    def history(self, symbol, range_="2y"):
        with self.lock:
            self.calls.append(symbol)
        if symbol in self.gate:
            assert self.gate[symbol].wait(5)
        if symbol == "BAD":
            raise LookupError("Yahoo has no data for BAD")
        return self.table[symbol]


class FakeFunds:
    def __init__(self, table=None):
        self.table, self.calls = table or {}, []

    def get(self, symbol):
        self.calls.append(symbol)
        return self.table.get(symbol, {"pe": 20.0, "market_cap": 1.5e12, "dividend_yield": 0.0123, "quote_type": "EQUITY"})


MEMBERS = [{"symbol": "AAA", "name": "Alpha Ltd", "industry": "IT"}, {"symbol": "BBB", "name": "Beta Ltd", "industry": "Banks"},
           {"symbol": "CCC", "name": "Gamma Ltd", "industry": "IT"}]


def make_service(prices=None, funds=None, members=None, clock=None, **kw):
    table = {"AAA": rising(), "BBB": rising(), "CCC": rising()}
    clk = clock or (lambda: 1_000_000.0)
    members = MEMBERS if members is None else members
    svc = sc.ScreenerService(prices=prices or FakePrices(table), fundamentals=funds or FakeFunds(),
                             load_universe=kw.pop("load_universe", lambda name: list(members)),
                             pace=0, sleep=lambda s: None, clock=clk, **kw)
    return svc


# ---------- metric calculations on fake bars ----------
def test_returns_on_a_steady_climb():
    b = rising(300)
    m = sc.compute_metrics(b)
    c = [x["close"] for x in b]
    assert m["price"] == c[-1] == 399.0
    assert m["chg_1d"] == round((c[-1] / c[-2] - 1) * 100, 2)
    assert m["ret_1w"] == round((c[-1] / c[-6] - 1) * 100, 2)
    assert m["ret_1m"] == round((c[-1] / c[-22] - 1) * 100, 2)
    assert m["ret_6m"] == round((c[-1] / c[-127] - 1) * 100, 2)
    assert m["ret_1y"] == round((c[-1] / c[-253] - 1) * 100, 2)
    assert m["ret_12_1"] == round((c[-22] / c[-253] - 1) * 100, 2)      # the past year, leaving out the latest month
    assert m["as_of"] == b[-1]["date"]


def test_relative_volume_is_today_over_the_average_of_the_20_days_before():
    m = sc.compute_metrics(rising(300, last_volume=3000))
    assert m["rel_volume"] == 3.0 and m["volume"] == 3000.0
    vols = [1000] * 279 + [500] * 20 + [1500]       # the 20 days before today average 500
    assert sc.compute_metrics(bars_from([100 + i for i in range(300)], vols))["rel_volume"] == 3.0


def test_relative_volume_is_unknown_when_today_has_no_volume_or_history_is_short():
    assert sc.compute_metrics(rising(300, last_volume=0))["rel_volume"] is None
    assert sc.compute_metrics(rising(15))["rel_volume"] is None


def test_distance_from_52_week_high_and_above_200_day():
    up = sc.compute_metrics(rising(300))
    assert up["pct_from_high"] == 0.0 and up["above_200"] is True
    closes = [100 + i for i in range(150)] + [250 - i for i in range(150)]    # up, then a long slide
    m = sc.compute_metrics(bars_from(closes))
    high = max(closes[-252:])
    assert m["pct_from_high"] == round((closes[-1] / high - 1) * 100, 2) and m["pct_from_high"] < -35
    assert m["above_200"] is False


def test_rsi_is_100_on_a_straight_climb_and_0_on_a_straight_fall():
    assert sc.compute_metrics(rising(300))["rsi"] == 100.0
    assert sc.compute_metrics(bars_from([500 - i for i in range(300)]))["rsi"] == 0.0


def test_rsi_in_between_matches_wilder():
    closes = [100, 101, 100, 102, 101, 103, 102, 104, 103, 105, 104, 106, 105, 107, 106, 108, 107, 109, 108, 110]
    m = sc.compute_metrics(bars_from(closes * 12))
    from trading_agent.bulletin import rsi
    assert m["rsi"] == round(rsi([float(c) for c in closes * 12])[-1], 1) and 40 < m["rsi"] < 75


def test_atr_percent_is_the_average_move_over_the_price():
    m = sc.compute_metrics(rising(300))                  # every close is 1 above the last: ATR 1, price 399
    assert m["atr_pct"] == round(1 / 399 * 100, 2)


def test_a_short_history_leaves_the_long_figures_unknown_and_no_history_is_an_error():
    m = sc.compute_metrics(rising(100))
    assert m["above_200"] is None and m["ret_1y"] is None and m["ret_12_1"] is None
    assert m["ret_1m"] is not None and m["price"] == 199.0
    assert sc.compute_metrics([]) == {"error": "no price history"}
    assert "error" in sc.compute_metrics(rising(1))


def test_fundamentals_fields_use_crore_and_percent_and_drop_nonsense():
    f = sc.fundamentals_fields("X", {"pe": 22.5, "market_cap": 1.5e12, "dividend_yield": 0.0123, "quote_type": "EQUITY"}, 1.0)
    assert f == {"pe": 22.5, "market_cap_cr": 150000.0, "div_yield": 1.23}
    pct = sc.fundamentals_fields("X", {"pe": 10, "market_cap": 5e10, "dividend_yield": 2.3, "quote_type": "EQUITY"}, 1.0)
    assert pct["div_yield"] == 2.3                         # Yahoo sometimes gives a percent already
    etf = sc.fundamentals_fields("GOLDBEES", {"pe": 10, "market_cap": 5e10, "dividend_yield": None, "quote_type": "ETF"}, 1.0)
    assert etf["pe"] is None and etf["div_yield"] is None
    assert sc.fundamentals_fields("X", {"error": "boom"}, 1.0) == {"error": "boom"}


# ---------- the service: immediate answer, pending, polling ----------
def test_first_answer_is_loading_then_complete_after_the_fill():
    svc = make_service()
    first = svc.snapshot("NIFTY50")
    assert first["rows"] == [] and first["loading"] is True and first["pending"] == 1
    assert svc.wait()
    done = svc.snapshot("NIFTY50")
    assert done["loading"] is False and done["pending"] == 0 and done["count"] == 3
    r = {x["symbol"]: x for x in done["rows"]}
    assert r["AAA"]["name"] == "Alpha Ltd" and r["AAA"]["industry"] == "IT"
    assert r["AAA"]["price"] == 399.0 and r["AAA"]["market_cap_cr"] == 150000.0 and r["AAA"]["pe"] == 20.0
    assert r["AAA"]["div_yield"] == 1.23 and r["AAA"]["above_200"] is True and r["AAA"]["ready"] is True
    assert done["as_of"] == r["AAA"]["as_of"] and "Yahoo" in done["source"]


def test_cached_rows_come_back_at_once_with_a_pending_count_until_the_slow_ones_arrive():
    gate = threading.Event()
    prices = FakePrices({"AAA": rising(), "BBB": rising(), "CCC": rising()}, gate={"CCC": gate})
    svc = make_service(prices=prices)
    svc.snapshot("NIFTY50")                                   # starts the fill (loads the list, then the prices)
    deadline = time.time() + 5
    while time.time() < deadline and sorted(set(prices.calls)) != ["AAA", "BBB", "CCC"]:
        time.sleep(0.01)
    time.sleep(0.05)
    mid = svc.snapshot("NIFTY50")                             # CCC is still blocked: rows exist, the rest is pending
    assert mid["count"] == 3 and mid["pending"] == 3 and mid["loading"] is False
    got = {x["symbol"]: x for x in mid["rows"]}
    assert got["AAA"]["price"] == 399.0 and got["CCC"]["price"] is None and got["AAA"]["market_cap_cr"] is None
    assert got["CCC"]["ready"] is False
    gate.set()
    assert svc.wait()
    assert svc.snapshot("NIFTY50")["pending"] == 0


def test_a_failing_stock_is_n_a_not_pending_forever():
    table = {"AAA": rising(), "BAD": rising(), "CCC": rising()}
    members = [{"symbol": s, "name": s, "industry": ""} for s in table]
    funds = FakeFunds({"CCC": {"error": "Yahoo refused"}})
    svc = make_service(prices=FakePrices(table), funds=funds, members=members)
    svc.snapshot("NIFTY100")
    assert svc.wait()
    out = svc.snapshot("NIFTY100")
    assert out["pending"] == 0
    r = {x["symbol"]: x for x in out["rows"]}
    assert r["BAD"]["price"] is None and "no data" in r["BAD"]["error"] and r["BAD"]["mom_score"] is None
    assert r["CCC"]["price"] == 399.0 and r["CCC"]["pe"] is None and r["CCC"]["market_cap_cr"] is None


def test_a_dead_constituent_list_is_reported_and_retried_later():
    now = [1000.0]
    calls = []

    def load(name):
        calls.append(name)
        raise OSError("NSE down")

    svc = make_service(load_universe=load, clock=lambda: now[0])
    svc.snapshot("NIFTY200")
    assert svc.wait()
    out = svc.snapshot("NIFTY200")
    assert out["rows"] == [] and out["pending"] == 0 and out["loading"] is False and "NSE down" in out["error"]
    assert len(calls) == 1                                    # not hammered while the error is fresh
    now[0] += sc.UNIVERSE_RETRY + 1
    svc.snapshot("NIFTY200")
    assert svc.wait() and len(calls) == 2


def test_stale_prices_are_refreshed_in_the_background_without_going_pending():
    now = [1000.0]
    prices = FakePrices({"AAA": rising(), "BBB": rising(), "CCC": rising()})
    svc = make_service(prices=prices, clock=lambda: now[0])
    svc.snapshot("NIFTY50")
    assert svc.wait()
    n = len(prices.calls)
    svc.snapshot("NIFTY50")
    assert svc.wait() and len(prices.calls) == n              # fresh: no new fetch
    now[0] += sc.METRICS_TTL + 1
    out = svc.snapshot("NIFTY50")
    assert out["pending"] == 0 and out["rows"][0]["price"] == 399.0   # old figures stay on screen
    assert svc.wait() and len(prices.calls) == n + 3


def test_fundamentals_are_cached_for_a_day():
    now = [1000.0]
    funds = FakeFunds()
    svc = make_service(funds=funds, clock=lambda: now[0])
    svc.snapshot("NIFTY50")
    assert svc.wait() and len(funds.calls) == 3
    now[0] += sc.METRICS_TTL + 1                              # prices refresh, fundamentals do not
    svc.snapshot("NIFTY50")
    assert svc.wait() and len(funds.calls) == 3
    now[0] += sc.FUNDS_TTL
    svc.snapshot("NIFTY50")
    assert svc.wait() and len(funds.calls) == 6


def test_stocks_shared_by_two_lists_are_fetched_once():
    prices = FakePrices({"AAA": rising(), "BBB": rising(), "CCC": rising()})
    svc = make_service(prices=prices)
    for name in ("NIFTY50", "NIFTY100"):
        svc.snapshot(name)
        assert svc.wait()
    assert sorted(prices.calls) == ["AAA", "BBB", "CCC"]


def test_momentum_leaders_use_the_factor_screens_own_score_and_rules():
    big = [100_000] * 300                                                  # turnover above the factor screen's floor
    falling = bars_from([500 - i for i in range(300)], big)                # below its 200-day average: not eligible
    table = {"AAA": bars_from([100 + i for i in range(300)], big), "BBB": bars_from([100 + i * 2 for i in range(300)], big), "CCC": falling}
    svc = make_service(prices=FakePrices(table))
    svc.snapshot("NIFTY50")
    assert svc.wait()
    r = {x["symbol"]: x for x in svc.snapshot("NIFTY50")["rows"]}
    assert r["CCC"]["mom_eligible"] is False and r["AAA"]["mom_eligible"] is True and r["BBB"]["mom_eligible"] is True
    from trading_agent.screen import score_universe
    from trading_agent.momentum import momentum_stats
    stats = {s: {**momentum_stats(b), "vol_60d": sc._vol(b)} for s, b in table.items()}
    want = {x["symbol"]: round(x["score"], 2) for x in score_universe(stats, min_turnover=sc.TURNOVER_FLOOR)}
    assert {s: r[s]["mom_score"] for s in r} == want


def test_held_and_deal_flags_come_from_what_the_caller_passes():
    svc = make_service()
    svc.snapshot("NIFTY50")
    assert svc.wait()
    out = svc.snapshot("NIFTY50", held={"AAA"}, deals={"BBB": ["ASHISH KACHOLIA"]})
    r = {x["symbol"]: x for x in out["rows"]}
    assert r["AAA"]["held"] is True and r["BBB"]["held"] is False
    assert r["BBB"]["deal"] is True and r["BBB"]["deal_who"] == ["ASHISH KACHOLIA"] and r["AAA"]["deal"] is False


def test_given_lists_for_holdings_fill_in_names_from_the_names_function():
    svc = make_service(names=lambda syms: {"AAA": "Alpha Limited"})
    given = [{"symbol": "AAA", "name": "", "industry": ""}]
    first = svc.snapshot(sc.HOLDINGS, given=given, held={"AAA"})
    assert first["count"] == 1 and first["loading"] is False
    assert svc.wait()
    row = svc.snapshot(sc.HOLDINGS, given=given, held={"AAA"})["rows"][0]
    assert row["name"] == "Alpha Limited" and row["price"] == 399.0 and row["held"] is True
    assert svc.snapshot(sc.HOLDINGS, given=[], held=set())["count"] == 0   # nothing held: an empty table, not an error


def test_the_band_label_is_shown_when_a_band_book_is_given():
    class Book:
        def rule(self, sym):
            return {"label": "5%" if sym == "AAA" else "no band"}
    svc = make_service(bands=lambda: Book())
    svc.snapshot("NIFTY50")
    assert svc.wait()
    r = {x["symbol"]: x for x in svc.snapshot("NIFTY50")["rows"]}
    assert r["AAA"]["band"] == "5%" and r["BBB"]["band"] == "no band"


def test_tradingview_columns_are_asked_once_per_list_and_only_when_on():
    class TV:
        def __init__(self, state):
            self.state, self.fetched = state, []

        def status(self):
            return {"state": self.state, "until": None}

        def fetch(self, universe, symbols):
            self.fetched.append((universe, list(symbols)))
            return {"AAA": {"summary": 0.6, "summary_label": "Strong buy", "sector": "Tech", "industry": "Software", "eps_growth": 12.5}}

    off = TV("off")
    svc = make_service(tv=off)
    svc.snapshot("NIFTY50")
    assert svc.wait()
    out = svc.snapshot("NIFTY50")
    assert off.fetched == [] and "tv_summary" not in out["rows"][0] and out["tv"]["state"] == "off"
    on = TV("on")
    svc = make_service(tv=on)
    svc.snapshot("NIFTY50")
    assert svc.wait()
    for _ in range(3):
        out = svc.snapshot("NIFTY50")
        svc.wait()
    assert on.fetched == [("NIFTY50", ["AAA", "BBB", "CCC"])]            # one request for the list
    r = {x["symbol"]: x for x in out["rows"]}
    assert r["AAA"]["tv_summary_label"] == "Strong buy" and r["AAA"]["tv_eps_growth"] == 12.5
    assert "tv_summary" not in r["BBB"] and out["tv"]["state"] == "on"


def test_deal_buyers_are_purchases_inside_30_days_only():
    def t(who, ticker, kind, when):
        return DisclosedTrade(source="deals", investor=who, ticker=ticker, transaction=kind, transaction_date=when,
                              report_date=when, size="1", raw={})
    today = "2026-10-12"
    deals = [t("A", "TCS", "Purchase", "2026-10-02"), t("B", "TCS", "Purchase", "2026-09-20"), t("A", "INFY", "Sale", "2026-10-05"),
             t("A", "OLD", "Purchase", "2026-08-01"), t("A", "TCS", "Purchase", "2026-10-03"), t("C", "LATER", "Purchase", "2026-10-20")]
    assert sc.deal_buyers(deals, today) == {"TCS": ["A", "B"]}
    assert sc.deal_buyers(deals, "not a date") == {}


def test_universe_names():
    assert sc.universe_key("nifty 50") == "NIFTY50" and sc.universe_key("holdings") == "HOLDINGS" and sc.universe_key("deals") == "DEALS"
    assert sc.universe_key("NIFTYSMALLCAP250") == "NIFTYSMALLCAP250"
    assert sc.universe_key("sensex") is None and sc.universe_key("") is None


# ---------- GET /api/screener ----------
def _live_app(settings, **kw):
    app = App(settings, dotenv=None, **kw)
    app._screener = make_service()
    app._deals, app._deals_at = [], time.time()          # no deal fetch
    return app


def _serve(app):
    srv = make_server(app, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _poll(base, universe, tries=60):
    for _ in range(tries):
        status, j = _get(f"{base}/api/screener?universe={universe}")
        assert status == 200
        if not j["pending"] and not j["loading"]:
            return j
        time.sleep(0.05)
    raise AssertionError("never completed")


def test_endpoint_shape_and_polling_until_complete(settings):
    app = _live_app(settings)
    srv, base = _serve(app)
    try:
        status, first = _get(base + "/api/screener?universe=NIFTY50")
        assert status == 200 and set(first) >= {"universe", "count", "pending", "loading", "error", "as_of", "generated_at", "source", "tv", "rows"}
        done = _poll(base, "NIFTY50")
        assert done["count"] == 3 and done["universe"] == "NIFTY50" and done["source"].startswith("Prices and fundamentals from Yahoo")
        row = done["rows"][0]
        for k in ("symbol", "name", "industry", "price", "chg_1d", "ret_1w", "ret_1m", "ret_6m", "ret_1y", "ret_12_1", "volume", "rel_volume",
                  "market_cap_cr", "pe", "div_yield", "pct_from_high", "above_200", "rsi", "atr_pct", "band", "held", "deal"):
            assert k in row, k
        assert _get(base + "/api/screener")[1]["universe"] == "NIFTY50"            # the default list
        status, bad = _get(base + "/api/screener?universe=sensex")
        assert status == 400 and "universe must be one of" in bad["error"]
    finally:
        srv.shutdown()
        srv.server_close()


def test_endpoint_flags_holdings_and_recent_buys_from_saved_state(settings):
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    (settings.state_dir / "groww_holdings.json").write_text(json.dumps({
        "saved_at": "2026-10-09T16:00:00+05:30",
        "holdings": [{"symbol": "AAA", "name": "Alpha Ltd", "exchange": "NSE", "kind": "equity", "maturity": None, "qty": 5, "sellable_qty": 5, "avg_price": 90.0},
                     {"symbol": "13BOND", "name": "A bond", "exchange": "BSE", "kind": "bond", "maturity": None, "qty": 1, "sellable_qty": 1, "avg_price": 1000.0}]}))
    (settings.state_dir / "paper_broker.json").write_text(json.dumps({"cash": 1.0, "positions": {"CCC": {"qty": 3.0, "avg_entry_price": 100.0}, "ZERO": {"qty": 0}}}))
    app = _live_app(settings)
    today = app._now_dt().astimezone(IST).date()
    recent = (today - timedelta(days=3)).isoformat()
    app._deals = [DisclosedTrade(source="deals", investor="ASHISH KACHOLIA", ticker="BBB", transaction="Purchase", transaction_date=recent,
                                 report_date=recent, size="1", raw={}),
                  DisclosedTrade(source="deals", investor="ASHISH KACHOLIA", ticker="AAA", transaction="Sale", transaction_date=recent,
                                 report_date=recent, size="1", raw={})]
    srv, base = _serve(app)
    try:
        rows = {r["symbol"]: r for r in _poll(base, "NIFTY50")["rows"]}
        assert rows["AAA"]["held"] and rows["CCC"]["held"] and not rows["BBB"]["held"]      # snapshot + practice account; a bond is not a stock held
        assert rows["BBB"]["deal"] and rows["BBB"]["deal_who"] == ["ASHISH KACHOLIA"] and not rows["AAA"]["deal"]   # a sale is not a buy
        # the two dynamic lists: what you hold, and who followed investors bought
        held = _poll(base, "HOLDINGS")
        assert sorted(r["symbol"] for r in held["rows"]) == ["AAA", "CCC"]
        buys = _poll(base, "DEALS")
        assert [r["symbol"] for r in buys["rows"]] == ["BBB"] and buys["rows"][0]["deal"] is True
    finally:
        srv.shutdown()
        srv.server_close()


def test_the_screener_never_calls_groww(settings, monkeypatch):
    from trading_agent import groww, runner

    def boom(*a, **k):
        raise AssertionError("the screener must not touch Groww")
    monkeypatch.setattr(groww.GrowwBroker, "__init__", boom)
    monkeypatch.setattr(runner, "read_groww_portfolio", boom)
    monkeypatch.setattr(runner, "resolve_groww_token", boom)
    settings.groww_api_key, settings.groww_api_secret, settings.groww_totp_secret = "key", "secret", "totp"   # linked on paper
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    (settings.state_dir / "groww_holdings.json").write_text(json.dumps({
        "saved_at": "2026-10-09T16:00:00+05:30",
        "holdings": [{"symbol": "AAA", "name": "A", "exchange": "NSE", "kind": "equity", "maturity": None, "qty": 5, "sellable_qty": 5, "avg_price": 90.0}]}))
    app = _live_app(settings)
    srv, base = _serve(app)
    try:
        for universe in ("NIFTY50", "HOLDINGS", "DEALS"):
            _poll(base, universe)
        assert _poll(base, "HOLDINGS")["rows"][0]["held"] is True
    finally:
        srv.shutdown()
        srv.server_close()


def test_the_screener_names_come_from_the_nse_list_only(settings, tmp_path):
    from trading_agent.instruments import CompanyNames

    class NoGroww(CompanyNames):
        def _groww_names(self):
            raise AssertionError("the Groww instrument file must not be read by the screener")

        def _nse_names(self):
            return {"AAA": "Alpha Limited"}
    app = App(settings, dotenv=None)
    app._names = NoGroww(tmp_path)
    assert app._screener_names(["AAA", "ZZZ"]) == {"AAA": "Alpha Limited"}


def test_the_offline_sample_does_not_use_the_network(server):
    base, app = server
    status, j = _get(base + "/api/screener?universe=NIFTY50")
    assert status == 200 and j["rows"] == [] and "live market data" in j["error"] and j["pending"] == 0
    assert _get(base + "/demo/api/screener?universe=NIFTY50")[1]["rows"] == []


def test_demo_page_shares_the_same_screener_service(settings):
    app = _live_app(settings)
    assert app.demo.screener is app.screener
    srv, base = _serve(app)
    try:
        assert _get(base + "/demo/api/screener?universe=NIFTY50")[0] == 200
    finally:
        srv.shutdown()
        srv.server_close()


def test_page_and_script_are_served_and_the_host_guard_applies(settings):
    app = _live_app(settings)
    srv, base = _serve(app)
    try:
        status, html = _get(base + "/screener")
        assert status == 200 and "Stock screener" in html and "/static/screener.js" in html
        status, js = _get(base + "/static/screener.js")
        assert status == 200 and "applyFilters" in js
        for path in ("/screener", "/api/screener?universe=NIFTY50"):
            req = urllib.request.Request(base + path, headers={"Host": "evil.example"})
            with pytest.raises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(req, timeout=5)
            assert e.value.code == 421
        req = urllib.request.Request(base + "/api/screener?universe=NIFTY50", headers={"Sec-Fetch-Site": "cross-site"})
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=5)
        assert e.value.code == 403
    finally:
        srv.shutdown()
        srv.server_close()


def test_the_page_is_read_only_the_endpoint_has_no_post(settings):
    app = _live_app(settings)
    srv, base = _serve(app)
    try:
        req = urllib.request.Request(base + "/api/screener", data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=5)
        assert e.value.code == 404
    finally:
        srv.shutdown()
        srv.server_close()
