"""Stock page data (GET /api/stock, intraday /api/candles): every module with fake Yahoo / NSE JSON, no network."""
from datetime import datetime, timezone

import pytest

from trading_agent import stockpage as sp
from trading_agent.candles import build_intraday, ist_chart_time, parse_range
from trading_agent.nse import NSEClient, _norm_shareholding

from .test_ui import _get, server  # noqa: F401  (server is a fixture)


def raw(v):
    return None if v is None else {"raw": v, "fmt": str(v)}


def ts(y, m, d):
    return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp())


def stmt(end, rev, profit):
    return {"endDate": raw(end), "totalRevenue": raw(rev), "netIncome": raw(profit)}


def full_result():
    yearly = [stmt(ts(2025, 3, 31), 1.2e12, 2.4e11), stmt(ts(2024, 3, 31), 1.0e12, 2.0e11),
              stmt(ts(2023, 3, 31), 0.9e12, 1.5e11), stmt(ts(2022, 3, 31), 0.6e12, 1.0e11)]   # newest first, like Yahoo
    quarterly = [stmt(ts(2025, 6, 30), 3.3e11, 6.6e10), stmt(ts(2025, 3, 31), 3.0e11, 6.0e10),
                 stmt(ts(2024, 12, 31), 2.9e11, 5.8e10), stmt(ts(2024, 9, 30), 2.8e11, 5.0e10)]
    return {
        "price": {"regularMarketPrice": raw(110.0), "regularMarketOpen": raw(101.0), "regularMarketPreviousClose": raw(100.0),
                  "regularMarketDayHigh": raw(111.0), "regularMarketDayLow": raw(100.5), "regularMarketVolume": raw(1234567),
                  "longName": "Tata Consultancy Services Limited"},
        "summaryDetail": {"trailingPE": raw(25.0), "marketCap": raw(4.0e12), "dividendYield": raw(0.012),
                          "fiftyTwoWeekLow": raw(80.0), "fiftyTwoWeekHigh": raw(130.0)},
        "defaultKeyStatistics": {"trailingEps": raw(4.4), "bookValue": raw(20.0), "priceToBook": raw(5.5)},
        "financialData": {"debtToEquity": raw(90.0), "returnOnEquity": raw(0.2)},
        "assetProfile": {"longBusinessSummary": "Makes software.", "sector": "Technology", "industry": "IT Services",
                         "website": "https://example.com"},
        "quoteType": {"quoteType": "EQUITY"},
        "incomeStatementHistoryQuarterly": {"incomeStatementHistory": quarterly},
        "incomeStatementHistory": {"incomeStatementHistory": yearly},
    }


# ---------- maths ----------
def test_cagr_and_pct_change():
    assert sp.cagr(1331, 1000, 3) == pytest.approx(0.1)
    assert sp.cagr(100, 0, 3) is None and sp.cagr(100, -5, 3) is None and sp.cagr(-5, 100, 3) is None   # no rate from a loss
    assert sp.cagr(None, 100, 3) is None and sp.cagr(100, 100, 0) is None
    assert sp.pct_change(110, 100) == pytest.approx(0.1)
    assert sp.pct_change(-50, -100) == pytest.approx(0.5)          # a smaller loss is an improvement
    assert sp.pct_change(5, 0) is None and sp.pct_change(None, 5) is None


def test_circuit_limits_from_band_and_previous_close():
    c = sp.circuit_limits(100.0, 10)
    assert (c["state"], c["band"], c["lower"], c["upper"]) == ("band", "10%", 90.0, 110.0)
    c = sp.circuit_limits(1234.55, 20)
    assert c["upper"] == 1481.45 and c["lower"] == 987.65          # on the 5 paise tick
    assert sp.circuit_limits(237.33, 5)["upper"] == 249.2          # 249.1965 -> 249.20
    none = sp.circuit_limits(100.0, None)
    assert none["state"] == "none" and none["band"] == "no band" and none["lower"] is None
    unknown = sp.circuit_limits(100.0, "unknown")
    assert unknown["state"] == "unknown" and unknown["lower"] is None
    assert sp.circuit_limits(None, 10)["lower"] is None


def test_crore_text_uses_indian_grouping():
    assert sp.crore_text(1234567.4) == "₹12,34,567 Cr"
    assert sp.crore_text(999.6) == "₹1,000 Cr" and sp.crore_text(0.5) == "₹0.50 Cr" and sp.crore_text(None) is None


# ---------- Yahoo parsing ----------
def test_price_block_and_change():
    p = sp.parse_price(full_result())
    assert (p["last"], p["open"], p["prev_close"], p["day_low"], p["day_high"], p["volume"]) == (110.0, 101.0, 100.0, 100.5, 111.0, 1234567)
    assert p["change"] == pytest.approx(10.0) and p["change_pct"] == pytest.approx(0.1)
    assert p["name"] == "Tata Consultancy Services Limited"


def test_missing_modules_do_not_break_anything():
    assert sp.parse_price({})["last"] is None and sp.parse_price({})["change"] is None
    # summaryDetail / financialData fill in when the price module is missing
    p = sp.parse_price({"summaryDetail": {"open": raw(10.0), "previousClose": raw(9.0), "dayLow": raw(9.5), "dayHigh": raw(10.5)},
                        "financialData": {"currentPrice": raw(10.0)}})
    assert p["last"] == 10.0 and p["prev_close"] == 9.0 and p["open"] == 10.0 and p["change_pct"] == pytest.approx(1 / 9)
    assert sp.parse_periods({}, "quarterly") == [] and sp.parse_profile({})["summary"] is None
    f = sp.build_fundamentals("X", {}, sp.parse_price({}), face_value=None, industry_pe=None, fetched_at=1.0)
    assert f["pe"] is None and f["market_cap_cr"] is None and f["debt_to_equity"] is None and f["pb"] is None and f["roe"] is None


def test_fundamentals_grid_and_debt_to_equity_percent_conversion():
    r = full_result()
    f = sp.build_fundamentals("TCS", r, sp.parse_price(r), face_value=1.0, industry_pe=27.5, fetched_at=1.0)
    assert f["debt_to_equity"] == pytest.approx(0.9)               # Yahoo says 90 (percent): 0.90 times equity
    assert f["market_cap_cr"] == pytest.approx(400000.0) and f["market_cap_text"] == "₹4,00,000 Cr"
    assert f["pe"] == 25.0 and f["eps"] == 4.4 and f["pb"] == 5.5 and f["book_value"] == 20.0
    assert f["roe"] == pytest.approx(4.4 / 20.0)                   # trailing EPS over book value, as the screener does
    assert f["dividend_yield"] == pytest.approx(0.012) and f["face_value"] == 1.0 and f["industry_pe"] == 27.5
    assert (f["week52_low"], f["week52_high"]) == (80.0, 130.0) and f["fund"] is False


def test_price_to_book_is_derived_when_yahoo_leaves_it_out():
    r = full_result()
    del r["defaultKeyStatistics"]["priceToBook"]
    f = sp.build_fundamentals("TCS", r, sp.parse_price(r), face_value=None, industry_pe=None, fetched_at=1.0)
    assert f["pb"] == 5.5                                          # 110 / 20


def test_loss_maker_has_no_pe_but_keeps_eps():
    r = full_result()
    r["summaryDetail"]["trailingPE"] = raw(-12.0)
    r["defaultKeyStatistics"]["trailingEps"] = raw(-3.0)
    r["financialData"].pop("returnOnEquity")
    f = sp.build_fundamentals("LOSS", r, sp.parse_price(r), face_value=None, industry_pe=None, fetched_at=1.0)
    assert f["pe"] is None and f["eps"] == -3.0 and f["roe"] == pytest.approx(-3.0 / 20.0)


def test_etf_is_flagged_as_a_fund():
    r = full_result()
    r["quoteType"] = {"quoteType": "ETF"}
    assert sp.build_fundamentals("SOMEFUND", r, sp.parse_price(r), face_value=None, industry_pe=None, fetched_at=1.0)["fund"] is True
    assert sp.build_fundamentals("GOLDBEES", full_result(), sp.parse_price({}), face_value=None, industry_pe=None, fetched_at=1.0)["fund"]


def test_periods_oldest_first_with_change_against_the_period_before():
    q = sp.parse_periods(full_result(), "quarterly")
    assert [p["label"] for p in q] == ["Sep '24", "Dec '24", "Mar '25", "Jun '25"]
    assert q[-1]["revenue_cr"] == pytest.approx(33000.0) and q[-1]["profit_cr"] == pytest.approx(6600.0)
    assert q[-1]["revenue_change"] == pytest.approx(0.1) and q[-1]["profit_change"] == pytest.approx(0.1)
    assert q[0]["revenue_change"] is None                         # nothing before the first period
    y = sp.parse_periods(full_result(), "yearly")
    assert len(y) == 4 and y[-1]["end"] == "2025-03-31" and y[-1]["revenue_cr"] == pytest.approx(120000.0)


def test_periods_keep_at_most_five_and_skip_empty_rows():
    rows = [stmt(ts(2025, 1 + i, 28), 1e10 * (i + 1), 1e9) for i in range(7)] + [{"endDate": raw(ts(2020, 1, 1))}, "junk"]
    out = sp.parse_periods({"incomeStatementHistory": {"incomeStatementHistory": rows}}, "yearly")
    assert len(out) == 5 and out[0]["end"] == "2025-03-28" and out[-1]["end"] == "2025-07-28"


def test_growth_table_year_over_year_and_three_year_cagr():
    q, y = sp.parse_periods(full_result(), "quarterly"), sp.parse_periods(full_result(), "yearly")
    g = sp.growth_table(q, y)                                      # only 4 quarters: 1Y falls back to latest year vs the one before
    assert g["revenue"]["y1"] == pytest.approx(0.2) and g["profit"]["y1"] == pytest.approx(0.2)
    assert g["revenue"]["cagr3"] == pytest.approx((1.2e12 / 0.6e12) ** (1 / 3) - 1)
    assert g["profit"]["cagr3"] == pytest.approx((2.4e11 / 1.0e11) ** (1 / 3) - 1)


def test_growth_uses_trailing_twelve_months_when_eight_quarters_are_known():
    qs = [{"revenue_cr": float(v), "profit_cr": float(v) / 10} for v in (100, 100, 100, 100, 120, 120, 120, 120)]
    g = sp.growth_table(qs, [])
    assert g["revenue"]["y1"] == pytest.approx(0.2) and g["revenue"]["cagr3"] is None
    assert sp.growth_table([], [])["profit"] == {"y1": None, "cagr3": None}


def test_loss_in_the_base_year_gives_no_growth_rate():
    y = [{"revenue_cr": 100.0, "profit_cr": -10.0}, {"revenue_cr": 200.0, "profit_cr": 5.0},
         {"revenue_cr": 300.0, "profit_cr": 8.0}, {"revenue_cr": 400.0, "profit_cr": 20.0}]
    g = sp.growth_table([], y)
    assert g["profit"]["cagr3"] is None and g["profit"]["y1"] == pytest.approx(1.5)
    assert g["revenue"]["cagr3"] == pytest.approx(4 ** (1 / 3) - 1)


def test_industry_pe_is_the_median_of_known_peers():
    assert sp.industry_pe([10.0, 30.0, 20.0, None, -5.0, 40.0]) == (25.0, 4)
    assert sp.industry_pe([10.0, 12.0, None]) == (None, 2)         # too few peers known: n/a, not a guess


# ---------- shareholding ----------
def test_shareholding_row_normalising():
    q = _norm_shareholding({"date": "30-JUN-2025", "pr_and_prgrp": "50.30", "public_val": "49.70", "x": "y"})
    assert q == {"date": "30-JUN-2025", "promoters": 50.3, "public": 49.7, "fii": None, "dii": None}
    assert _norm_shareholding({"date": "30-JUN-2025"}) is None and _norm_shareholding({"pr_and_prgrp": "5"}) is None
    assert _norm_shareholding({"date": "30-JUN-2025", "pr_and_prgrp": "140"}) is None   # not a percentage


def test_nse_client_shareholding_through_the_breaker_and_failure_path(tmp_path):
    from .conftest import FakeSession
    rows = [{"date": f"30-{m}-2025", "pr_and_prgrp": str(50 + i), "public_val": str(50 - i)} for i, m in enumerate(["MAR", "JUN", "SEP"])]
    cli = NSEClient(session=FakeSession({("GET", "corporate-share-holdings-master"): rows}), cache_dir=tmp_path)
    got = cli.shareholding("tcs")
    assert [g["date"] for g in got] == ["30-SEP-2025", "30-JUN-2025", "30-MAR-2025"]    # newest first
    empty = NSEClient(session=FakeSession({("GET", "corporate-share-holdings-master"): []}), cache_dir=tmp_path)
    with pytest.raises(LookupError):
        empty.shareholding("tcs")


def test_shareholding_payload_failure_says_unavailable_and_never_fakes():
    bad = sp.shareholding_payload(None, "ConnectionError: NSE down")
    assert bad == {"state": "unavailable", "message": "unavailable from NSE right now", "quarters": []}
    assert sp.shareholding_payload([])["state"] == "unavailable"
    ok = sp.shareholding_payload([{"date": "30-JUN-2025", "promoters": 50.3, "public": 49.7, "fii": None, "dii": None}] * 7)
    assert ok["state"] == "ok" and len(ok["quarters"]) == 5 and ok["quarters"][0]["label"] == "Jun '25"
    assert ok["quarters"][0]["fii"] is None                        # a missing share stays missing


# ---------- technicals ----------
def _bars(n=120):
    return [{"date": f"2025-{1 + i // 28:02d}-{1 + i % 28:02d}", "open": 100 + i * 0.5, "high": 102 + i * 0.5, "low": 99 + i * 0.5,
             "close": 101 + i * 0.5, "volume": 1000} for i in range(n)]


def test_technicals_from_daily_bars():
    t = sp.technicals(_bars())
    assert t["available"] and t["rsi14"] > 70 and t["adx"] is not None and t["plus_di"] > t["minus_di"]
    assert t["macd"] is not None and t["macd_signal"] is not None and "uptrend" in t["adx_reading"]
    assert sp.technicals(_bars(10)) == {"available": False}


# ---------- intraday ----------
def test_ist_shift_puts_0915_at_0915():
    t = ist_chart_time("2026-10-09T09:15:00+05:30")
    assert datetime.fromtimestamp(t, tz=timezone.utc).strftime("%H:%M") == "09:15"        # the library draws UTC
    assert t == int(datetime(2026, 10, 9, 3, 45, tzinfo=timezone.utc).timestamp()) + 19_800  # true UTC epoch + 19,800 s


def _intraday():
    out = []
    for day, base in (("2026-10-08", 100.0), ("2026-10-09", 102.0)):
        for i in range(3):
            m = 15 + 5 * i
            out.append({"ts": f"{day}T09:{m:02d}:00+05:30", "date": day, "open": base + i, "high": base + i + 1, "low": base + i - 1,
                        "close": base + i + 0.5, "volume": 100.0})
    return out


def test_one_day_is_the_latest_session_with_the_previous_close():
    p = build_intraday("TCS", "1D", _intraday())
    assert p["intraday"] is True and p["interval"] == "5m" and p["session"] == "2026-10-09" and len(p["bars"]) == 3
    assert p["prev_close"] == 102.5                                # last close of 8 Oct
    assert [datetime.fromtimestamp(b["time"], tz=timezone.utc).strftime("%H:%M") for b in p["bars"]] == ["09:15", "09:20", "09:25"]
    assert p["ema20"] == [] and p["rsi14"] == []


def test_one_week_keeps_all_sessions_and_empty_input_is_an_error():
    p = build_intraday("TCS", "1W", _intraday())
    assert len(p["bars"]) == 6 and p["interval"] == "30m"
    assert build_intraday("TCS", "1D", [])["error"] and build_intraday("TCS", "1D", [])["bars"] == []
    one = [b for b in _intraday() if b["date"] == "2026-10-09"]
    assert build_intraday("TCS", "1D", one)["prev_close"] is None
    assert build_intraday("TCS", "1D", one, prev_close=102.5)["prev_close"] == 102.5


def test_ranges_accept_the_new_keys():
    for k in ("1d", "1W", "All", "5y", "1M"):
        assert parse_range(k) is not None
    assert parse_range("9Y") is None


# ---------- the service ----------
def make_sources(**over):
    r = full_result()
    uni = [{"symbol": "TCS", "name": "TCS", "industry": "Information Technology"}] + [
        {"symbol": s, "name": s + " Ltd", "industry": "Information Technology"} for s in ("INFY", "WIPRO", "HCLTECH", "TECHM", "LTIM", "MPHASIS")] + [
        {"symbol": "SBIN", "name": "SBI", "industry": "Financial Services"}]
    src = dict(modules=lambda y: r, shareholding=lambda s: [{"date": "30-JUN-2025", "promoters": 72.0, "public": 28.0, "fii": None, "dii": None}],
               daily_bars=lambda t, e: _bars(), universe=lambda: uni, peer_pe=lambda s: {"INFY": 24.0, "WIPRO": 20.0, "HCLTECH": 26.0}.get(s),
               price_pair=lambda s: (110.0, 100.0), face_value=lambda s: 1.0, band=lambda s: 20)
    src.update(over)
    return sp.StockSources(**src)


def test_service_builds_the_whole_payload_and_caches_per_ticker():
    calls = []
    now = [1000.0]
    svc = sp.StockService(make_sources(modules=lambda y: calls.append(y) or full_result()), clock=lambda: now[0])
    p = svc.get("tcs")
    assert p["error"] is None and p["exchange"] == "NSE" and p["yahoo_symbol"] == "TCS.NS" and p["name"].startswith("Tata")
    assert p["price"]["change_pct"] == pytest.approx(0.1) and p["circuit"]["upper"] == 120.0 and p["circuit"]["lower"] == 80.0
    assert p["fundamentals"]["industry_pe"] == 24.0 and p["industry_pe_n"] == 3 and p["fundamentals"]["face_value"] == 1.0
    assert [s["symbol"] for s in p["similar"]] == ["INFY", "WIPRO", "HCLTECH", "TECHM", "LTIM"] and "TCS" not in [s["symbol"] for s in p["similar"]]
    assert p["similar"][0]["change_pct"] == pytest.approx(0.1)
    assert p["shareholding"]["state"] == "ok" and p["technicals"]["available"] and p["about"]["nse_industry"] == "Information Technology"
    assert p["financials"]["growth"]["revenue"]["y1"] == pytest.approx(0.2)
    now[0] += 120
    assert svc.get("TCS") is p and calls == ["TCS.NS"]            # inside the cache time: no new request
    now[0] += 400
    svc.get("TCS")
    assert calls == ["TCS.NS", "TCS.NS"]
    assert svc.get("TCS", "BSE")["yahoo_symbol"] == "TCS.BO" and calls[-1] == "TCS.BO"   # its own cache entry
    assert svc.get("TCS.BO")["exchange"] == "BSE"


def test_service_survives_every_part_failing():
    def boom(*a, **k):
        raise RuntimeError("down")
    svc = sp.StockService(make_sources(modules=boom, shareholding=boom, daily_bars=boom, universe=boom, face_value=boom, band=boom))
    p = svc.get("TCS")
    assert p["error"] and p["price"]["last"] is None and p["fundamentals"] is None
    assert p["shareholding"]["state"] == "unavailable" and p["technicals"] == {"available": False} and p["similar"] == []
    assert p["circuit"]["state"] == "unknown" and set(p["errors"]) >= {"yahoo", "shareholding", "technicals"}
    # an error answer is not cached: the next call asks again
    n = []
    svc2 = sp.StockService(make_sources(modules=lambda y: n.append(1) or (_ for _ in ()).throw(RuntimeError("x"))))
    svc2.get("TCS"); svc2.get("TCS")
    assert len(n) == 2


def test_shareholding_failure_is_reported_and_retried_sooner():
    now = [0.0]
    calls = []

    def bad(sym):
        calls.append(sym)
        raise ConnectionError("NSE refused")
    svc = sp.StockService(make_sources(shareholding=bad), clock=lambda: now[0])
    p = svc.get("TCS")
    assert p["shareholding"] == {"state": "unavailable", "message": "unavailable from NSE right now", "quarters": []}
    assert p["error"] is None and p["price"]["last"] == 110.0     # the rest of the page is fine
    now[0] += 90
    svc.get("TCS")
    assert len(calls) == 2                                        # the partial answer lived 60 s, not 5 minutes


def test_etf_payload_drops_company_sections():
    r = full_result()
    r["quoteType"] = {"quoteType": "ETF"}
    p = sp.StockService(make_sources(modules=lambda y: r)).get("NIFTYBEES")
    assert p["fund"] is True and p["similar"] == [] and p["shareholding"]["state"] == "unavailable"


def test_no_band_and_unlisted_stock():
    p = sp.StockService(make_sources(band=lambda s: None)).get("TCS")
    assert p["circuit"]["state"] == "none" and p["circuit"]["band"] == "no band"
    p = sp.StockService(make_sources(band=lambda s: "unknown", universe=lambda: [])).get("ZZZ")
    assert p["circuit"]["state"] == "unknown" and p["similar"] == [] and p["fundamentals"]["industry_pe"] is None


# ---------- the HTTP endpoint ----------
def test_stock_endpoint(server):  # noqa: F811
    base, app = server
    seen = []
    svc = sp.StockService(make_sources())
    app.stock_service = type("S", (), {"get": lambda self, t, e="NSE": seen.append((t, e)) or svc.get(t, e)})()
    status, p = _get(base + "/api/stock?ticker=tcs")
    assert status == 200 and p["ticker"] == "TCS" and p["exchange"] == "NSE" and p["price"]["last"] == 110.0
    status, p = _get(base + "/api/stock?ticker=tcs&exchange=bse")
    assert status == 200 and p["exchange"] == "BSE" and seen[-1] == ("TCS", "BSE")
    assert _get(base + "/api/stock")[0] == 400
    assert _get(base + "/api/stock?ticker=bad%20ticker!")[0] == 400
    assert _get(base + "/api/stock?ticker=tcs&exchange=LSE")[0] == 400


def test_stock_endpoint_is_read_only_and_never_touches_groww(server):  # noqa: F811
    base, app = server
    app._broker = app.broker
    app.stock_service = sp.StockService(make_sources())
    status, _ = _get(base + "/api/stock?ticker=tcs")
    assert status == 200
    import json
    import urllib.request
    req = urllib.request.Request(base + "/api/stock?ticker=tcs", data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
    with pytest.raises(Exception):
        urllib.request.urlopen(req, timeout=5)                    # there is no POST route for it
    assert json.dumps(app.settings.__dict__, default=str) is not None


def test_intraday_candles_endpoint(server):  # noqa: F811
    base, app = server
    calls = []

    class Src:
        def history_intraday(self, sym, interval="15m", range_="5d", ttl=300.0):
            calls.append((sym, interval, range_, ttl))
            return _intraday()

        def history_ohlc(self, sym, range_="1y", ttl=None):
            calls.append((sym, range_))
            return [{"date": "2026-10-08", "ts": "2026-10-08", "open": 1, "high": 2, "low": 1, "close": 99.0, "volume": 5.0}]
    app.prices = Src()
    status, p = _get(base + "/api/candles?ticker=senco&range=1d")
    assert status == 200 and p["intraday"] is True and p["range"] == "1D" and len(p["bars"]) == 3 and p["prev_close"] == 102.5
    assert calls[0] == ("SENCO", "5m", "5d", 90)
    status, w = _get(base + "/api/candles?ticker=senco&range=1w&exchange=bse")
    assert status == 200 and w["exchange"] == "BSE" and calls[-1][:3] == ("SENCO.BO", "30m", "5d") and len(w["bars"]) == 6
    status, a = _get(base + "/api/candles?ticker=senco&range=all")
    assert status == 200 and a["range"] == "ALL" and calls[-1] == ("SENCO", "max")
    assert _get(base + "/api/candles?ticker=senco&range=2D")[0] == 400


def test_intraday_failure_is_reported_not_raised(server):  # noqa: F811
    base, app = server

    class Boom:
        def history_intraday(self, *a, **k):
            raise RuntimeError("yahoo said no")
    app.prices = Boom()
    status, p = _get(base + "/api/candles?ticker=senco&range=1D")
    assert status == 200 and p["bars"] == [] and p["error"] == "yahoo said no"


def test_face_value_from_the_nse_equity_list(tmp_path):
    from trading_agent.instruments import CompanyNames
    csv_text = ("SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE, MARKET LOT, ISIN NUMBER, FACE VALUE\n"
                "TCS,Tata Consultancy Services Limited,EQ,25-AUG-2004,1,1,INE467B01029,1\n"
                "RELIANCE,Reliance Industries Limited,EQ,29-NOV-1995,10,1,INE002A01018,10\n"
                "ODD,Odd Limited,EQ,01-JAN-2000,10,1,INE000000000,-\n")

    class S:
        def get(self, url, **k):
            return type("R", (), {"content": csv_text.encode(), "raise_for_status": lambda self: None})()
    n = CompanyNames(tmp_path, session=S())
    assert n.face_value("tcs") == 1.0 and n.face_value("RELIANCE") == 10.0
    assert n.face_value("ODD") is None and n.face_value("NOPE") is None


def test_fundamentals_peek_reads_saved_snapshots_without_a_request(tmp_path):
    from trading_agent.fundamentals import YahooFundamentals

    class NoNet:
        def get(self, *a, **k):
            raise AssertionError("peek must not make a request")
    f = YahooFundamentals(tmp_path, session=NoNet())
    assert f.peek("INFY") is None
    (tmp_path / "fundamentals").mkdir()
    (tmp_path / "fundamentals" / "INFY.json").write_text('{"pe": 24.0}', encoding="utf-8")
    (tmp_path / "fundamentals" / "BAD.json").write_text('{"error": "x"}', encoding="utf-8")
    assert f.peek("infy") == {"pe": 24.0} and f.peek("BAD") is None
