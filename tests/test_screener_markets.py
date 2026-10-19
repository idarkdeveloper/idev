"""Screener for US, UK and Japan stocks: lists, pence, row shape, background fill, caching and politeness (fakes only)."""
import json
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from trading_agent import screener_markets as sm
from trading_agent.screener import ScreenerService, universe_key


def table(rows):
    cells = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f'<html><table id="constituents">{cells}</table></html>'


SP_HTML = table([["Symbol", "Security", "GICS Sector"], ["AAPL", "Apple Inc.", "Information Technology"], ["BRK.B", "Berkshire Hathaway", "Financials"],
                 ["AAPL", "dup", "x"], ["MSFT", "Microsoft", "Information Technology"]])
FTSE_HTML = table([["Company", "Ticker", "FTSE industry classification benchmark sector"], ["Vodafone", "VOD", "Telecoms"], ["BT Group", "BT.A", "Telecoms"]])
TOPIX_CSV = ("Name (English),Code,Sector(33),New Index Series Code\nTOYOTA MOTOR,7203,Transportation Equipment,TOPIX Core30\n"
             "SONY GROUP,6758,Electric Appliances,TOPIX Large70\nSMALL CO,1301,Fishery,TOPIX Mid400\n")


def bars(n=300, start=100.0, step=0.5, vol=1000):
    d0 = datetime(2025, 1, 1)
    return [{"date": (d0 + timedelta(days=i)).strftime("%Y-%m-%d"), "close": start + i * step, "adj_close": start + i * step, "volume": float(vol)} for i in range(n)]


def test_universes_and_suffix_mapping():
    assert set(sm.UNIVERSES) == {"SP500_US", "FTSE100_UK", "TOPIX100_JP"}
    assert universe_key("sp500_us") == "SP500_US" and universe_key("nifty 50") == "NIFTY50" and universe_key("DAX") is None
    assert [m["symbol"] for m in sm.parse_sp500(SP_HTML)] == ["AAPL.US", "BRK-B.US", "MSFT.US"]        # the dot form is Yahoo's dash; no duplicates
    assert [(m["symbol"], m["name"]) for m in sm.parse_ftse100(FTSE_HTML)] == [("BT-A.L", "BT Group"), ("VOD.L", "Vodafone")]
    assert [m["symbol"] for m in sm.parse_topix100(TOPIX_CSV)] == ["6758.T", "7203.T"]                  # Core30 + Large70 only
    from trading_agent import markets
    assert [markets.yahoo_symbol(s) for s in ("AAPL.US", "VOD.L", "7203.T")] == ["AAPL", "VOD.L", "7203.T"]


def test_members_are_read_once_a_day_and_stale_copy_survives_a_failure(tmp_path):
    class R:
        def __init__(self, text):
            self.content, self.status_code = text.encode(), 200

        def raise_for_status(self):
            pass

    class S:
        def __init__(self, text):
            self.n, self.text = 0, text

        def get(self, *a, **k):
            self.n += 1
            if self.text is None:
                raise ConnectionError("down")
            return R(self.text)
    big = table([["Symbol", "Security", "GICS Sector"]] + [[f"T{i}", f"Co {i}", "S"] for i in range(30)])
    s = S(big)
    m1 = sm.load_members("SP500_US", tmp_path, s)
    assert len(m1) == 30 and all(m["code"] == m["symbol"] for m in m1)
    sm.load_members("SP500_US", tmp_path, s)
    assert s.n == 1                                                                  # the day-old copy on disk answered
    later = time.time() + 2 * 24 * 3600
    down = S(None)
    assert len(sm.load_members("SP500_US", tmp_path, down, clock=lambda: later)) == 30     # source down: the stored copy
    with pytest.raises(Exception):
        sm.load_members("FTSE100_UK", tmp_path / "other", down)                      # nothing stored, source down: an error, shown on the page
    with pytest.raises(ValueError):
        sm.load_members("SP500_US", tmp_path / "x", S(SP_HTML))                      # a source with too few members is refused


def test_uk_pence_become_pounds_and_the_others_are_untouched():
    class Inner:
        def history(self, sym, rng="2y"):
            self.sym = sym
            return [{"date": "2026-01-01", "close": 15000.0, "adj_close": 14900.0, "volume": 5.0}]
    i = Inner()
    b = sm.ForeignPrices(i, sm.UK_PENCE).history("VOD.L")[0]
    assert b["close"] == 150.0 and b["adj_close"] == 149.0 and b["volume"] == 5.0 and i.sym == "VOD.L"
    assert sm.ForeignPrices(i).history("AAPL.US")[0]["close"] == 15000.0 and i.sym == "AAPL"


def test_market_hours_and_info():
    mon_open = datetime(2026, 10, 19, 15, 0, tzinfo=timezone.utc)      # 11:00 New York, 16:00 London, 00:00 Tokyo (Tue)
    assert sm.market_open("US", mon_open) and sm.market_open("UK", mon_open) and not sm.market_open("JP", mon_open)
    sat = datetime(2026, 10, 17, 15, 0, tzinfo=timezone.utc)
    assert not any(sm.market_open(m, sat) for m in sm.HOURS)
    info = sm.market_info("SP500_US", mon_open)
    assert info["open"] is True and info["symbol"] == "$" and info["local"] == "11:00" and info["ist"] == "20:30" and "view-only" in info["note"]
    assert sm.market_info("FTSE100_UK", mon_open)["pence_note"] and sm.market_info("TOPIX100_JP", mon_open)["dec"] == 0


class FakePrices:
    def __init__(self, fail=(), delay=0.0):
        self.fail, self.delay, self.calls, self.now, self.peak, self.lock = set(fail), delay, [], 0, 0, threading.Lock()

    def history(self, sym, rng="2y"):
        with self.lock:
            self.calls.append(sym)
            self.now += 1
            self.peak = max(self.peak, self.now)
        time.sleep(self.delay)
        with self.lock:
            self.now -= 1
        if sym in self.fail:
            raise LookupError("Yahoo has no data")
        return bars()


class FakeFunds:
    def get(self, sym):
        return {"pe": 20.0, "dividend_yield": 0.021, "market_cap": 3e12, "name": sym}


def service(prices, members, **kw):
    sleeps = []
    s = ScreenerService(prices=prices, fundamentals=FakeFunds(), load_universe=lambda k: members, workers=3, fund_workers=2, pace=0.1, jitter=True,
                        fund_fn=sm.fund_fields, turnover_floor=0.0, meta=sm.market_info, sleep=sleeps.append, **kw)
    return s, sleeps


MEMBERS = [{"symbol": f"S{i}.US", "name": f"Co {i}", "industry": "Tech", "code": f"S{i}.US"} for i in range(12)]


def test_rows_have_the_screener_shape_without_india_only_data_and_a_bad_symbol_does_not_fail_the_table():
    s, sleeps = service(FakePrices(fail={"S3.US"}), MEMBERS)
    s.snapshot("SP500_US")
    assert s.wait(10)
    d = s.snapshot("SP500_US")
    assert d["count"] == 12 and d["total"] == 12 and d["pending"] == 0 and d["market"]["code"] == "US"
    r = next(x for x in d["rows"] if x["symbol"] == "S0.US")
    for k in ("price", "chg_1d", "ret_1w", "ret_1m", "ret_6m", "ret_1y", "ret_12_1", "rel_volume", "pct_from_high", "above_200", "spark", "div_yield", "code"):
        assert k in r and r[k] is not None, k
    assert r["div_yield"] == 2.1 and r["market_cap_cr"] is None and len(r["spark"]) == 60
    assert r["band"] is None and r["held"] is False and r["deal"] is False and "filter_flags" not in r and "filter_block" not in r
    assert r["mom_eligible"] is True                                              # the factor screen rule, with no rupee liquidity floor
    bad = next(x for x in d["rows"] if x["symbol"] == "S3.US")
    assert bad["price"] is None and "no data" in bad["error"]
    assert all(0.05 <= x <= 0.15 for x in sleeps) and len(set(sleeps)) > 1        # jittered pauses around 0.1 s


def test_progress_while_loading_and_a_concurrency_cap():
    p = FakePrices(delay=0.03)
    members = [{"symbol": f"S{i}.US", "name": "x", "industry": "", "code": f"S{i}.US"} for i in range(30)]
    s, _ = service(p, members)
    first = s.snapshot("SP500_US")                    # the list loads in the background
    assert first["loading"] is True or first["pending"] > 0
    assert s.wait(20)
    d = s.snapshot("SP500_US")
    assert d["pending"] == 0 and d["total"] == 30 and d["count"] == 30
    assert p.peak <= 3 <= 6 and p.peak >= 2           # never more than 3 Yahoo requests at once (the cap is 6)


def test_ttl_is_30_minutes_in_market_hours_and_6_hours_otherwise_and_a_fresh_table_is_not_refetched():
    clock = [1000.0]
    open_now = [True]
    p = FakePrices()
    s = ScreenerService(prices=p, fundamentals=FakeFunds(), load_universe=lambda k: MEMBERS[:4], workers=2, fund_workers=1, pace=0, fund_fn=sm.fund_fields,
                        turnover_floor=0.0, metrics_ttl=lambda: sm.FRESH_TTL if open_now[0] else sm.CLOSED_TTL, clock=lambda: clock[0])
    s.snapshot("SP500_US")
    assert s.wait(10)
    n = len(p.calls)
    clock[0] += 29 * 60
    s.snapshot("SP500_US")
    s.wait(10)
    assert len(p.calls) == n                          # inside 30 minutes: served from the table
    clock[0] += 2 * 60
    s.snapshot("SP500_US")
    s.wait(10)
    assert len(p.calls) == 2 * n                      # open market, older than 30 minutes: read again
    open_now[0] = False
    clock[0] += 3 * 3600
    s.snapshot("SP500_US")
    s.wait(10)
    assert len(p.calls) == 2 * n                      # closed: the 6 hour copy still stands


def test_make_service_keeps_its_own_caches_and_normalises_uk_prices(tmp_path):
    svc = sm.make_service("FTSE100_UK", tmp_path)
    assert svc.prices.divisor == 100.0 and svc.turnover_floor == 0.0 and svc.workers == 3 and svc.fund_workers == 2 and svc.jitter
    assert (tmp_path / "cache" / "screener_uk").name in str(svc.prices.inner.cache_dir)
    assert svc.prices.inner.ttl_fn is not None and sm.make_service("SP500_US", tmp_path).prices.divisor == 1.0


def test_the_api_routes_foreign_universes_and_never_calls_india_code(settings, tmp_path, monkeypatch):
    from trading_agent.broker import LocalPaperBroker
    from trading_agent.ui import App
    app = App(settings, broker=LocalPaperBroker(tmp_path / "pb.json", starting_cash=1.0, price_fn=lambda s: 1.0), dotenv=None)
    fake, _ = service(FakePrices(), MEMBERS)
    app._screener_foreign["SP500_US"] = fake
    monkeypatch.setattr(app, "_held_symbols", lambda: (_ for _ in ()).throw(AssertionError("India holdings read for a US list")))
    d = app.screener_data("sp500_us")
    assert d["universe"] == "SP500_US" and "market" in d
    with pytest.raises(ValueError):
        app.screener_data("DAX")
    assert json.dumps(d)
