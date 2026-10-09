"""Dashboard server: JSON API over the demo inputs, no network."""
import json
import threading
import time
from pathlib import Path
import urllib.error
import urllib.request

import pytest

from trading_agent.broker import LocalPaperBroker
from trading_agent.quiver import _norm_congress, filter_by_investor
from trading_agent.ui import App, make_server


@pytest.fixture
def server(settings, sample_rows):
    settings.market = "in"  # exercise INR formatting paths; data itself is the US fixture
    trades = filter_by_investor([_norm_congress(r) for r in sample_rows], "Nancy Pelosi")
    prices = {"NVDA": 180.0, "AVGO": 350.0, "AAPL": 250.0}
    broker = LocalPaperBroker(settings.state_dir / "pb.json", starting_cash=80_000,
                              price_fn=lambda s: prices[s], currency="USD")
    app = App(settings, broker=broker, demo_trades=trades, dotenv=settings.state_dir / ".env")
    srv = make_server(app, "127.0.0.1", 0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", app
    srv.shutdown()
    srv.server_close()


def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            body = r.read().decode()
            return r.status, json.loads(body) if "json" in r.headers.get("Content-Type", "") else body
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def _post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def test_index_and_state(server):
    base, app = server
    status, html = _get(base + "/")
    assert status == 200 and "<title>Trading Agent</title>" in html
    status, st = _get(base + "/api/state")
    assert status == 200
    assert st["settings"]["watch_investor"] == "Nancy Pelosi"
    assert len(st["deals"]) == 3 and all(d["status"] == "new" for d in st["deals"])
    assert st["account"]["cash"] == 80_000 and st["positions"] == []
    assert st["connections"]["claude"] is True and st["settings"]["mode"] == "recommend"
    assert st["settings"]["demo"] is True
    status, _ = _get(base + "/api/nope")
    assert status == 404


def test_paper_order_and_dismiss(server):
    base, app = server
    status, j = _post(base + "/api/order", {"symbol": "NVDA", "side": "buy", "notional": 1800})
    assert status == 200 and j["order"]["qty"] == 10
    _, st = _get(base + "/api/state")
    assert st["positions"][0]["symbol"] == "NVDA" and st["account"]["cash"] == 78_200
    status, j = _post(base + "/api/order", {"symbol": "NVDA", "side": "sell", "notional": 1e9})
    assert status == 400 and "only hold" in j["error"]
    status, j = _post(base + "/api/dismiss", {"index": 0})
    assert status == 200 and j["ok"] is False  # no recommendations yet


def test_dry_run_check_job(server):
    base, app = server
    status, job = _post(base + "/api/check", {"dry_run": True})
    assert status == 202 and job["kind"] == "dry_run"
    for _ in range(50):
        _, st = _get(base + "/api/state")
        if not st["busy"]:
            break
        time.sleep(0.05)
    assert not st["busy"]
    assert st["jobs"][-1]["ok"] is True and "3 new deal" in st["jobs"][-1]["message"]
    assert st["runs"][0]["dry_run"] is True


def test_settings_persist_to_env(server):
    base, app = server
    status, j = _post(base + "/api/settings", {"watch_investor": "Dan Crenshaw", "auto_trade": True,
                                               "notify_email_to": "me@example.com"})
    assert status == 200 and j["applied"]["WATCH_INVESTOR"] == "Dan Crenshaw"
    env = (app.settings.state_dir / ".env").read_text()
    assert "WATCH_INVESTOR=Dan Crenshaw" in env and "AUTO_TRADE=true" in env
    assert "NOTIFY_EMAIL_TO=me@example.com" in env
    _, st = _get(base + "/api/state")
    assert st["settings"]["mode"] == "paper" and st["settings"]["watch_investor"] == "Dan Crenshaw"


def test_reset_clears_paper_account(server):
    base, app = server
    _post(base + "/api/order", {"symbol": "AAPL", "side": "buy", "notional": 2500})
    status, j = _post(base + "/api/reset", {})
    assert status == 200 and "pb.json" in j["removed"]
    _, st = _get(base + "/api/state")
    assert st["positions"] == [] and st["account"]["cash"] == 80_000


def test_lookup_and_regime_endpoints(server, monkeypatch):
    base, app = server
    from trading_agent import regime as rg

    class Src:
        def history(self, sym, range_):
            n = 260
            return [{"date": f"d{i}", "close": 100 + i * 0.1, "adj_close": 100 + i * 0.1, "volume": 10}
                    for i in range(n)]
    app.context = rg.GlobalContext(Src(), ttl=1000)
    app.momentum = __import__("trading_agent.momentum", fromlist=["MomentumScreen"]).MomentumScreen(Src())

    class Data:
        def announcements(self, symbol, limit=8):
            return [{"id": "1", "symbol": symbol, "company": "X", "at": "2026-10-09 09:00:00",
                     "category": "Results", "text": "Q2 results", "file": ""}]
    app._data = Data()
    status, st = _get(base + "/api/state")
    assert status == 200 and st["regime"]["regime"] in ("risk_on", "neutral", "risk_off")
    status, r = _get(base + "/api/regime")
    assert status == 200 and "summary" in r
    status, lk = _get(base + "/api/lookup?ticker=senco")
    assert status == 200 and lk["ticker"] == "SENCO" and lk["momentum"]["verdict"] == "strong"
    assert lk["announcements"][0]["text"] == "Q2 results"
    status, _ = _get(base + "/api/lookup")
    assert status == 400


def test_backtest_job_and_watch_toggle(server):
    base, app = server
    status, job = _post(base + "/api/backtest", {"investor": "Nancy Pelosi", "days": 365, "horizons": "5,20", "cost_bps": 50})
    assert status == 202 and job["kind"] == "backtest"
    for _ in range(100):
        _, st = _get(base + "/api/state")
        if not st["busy"]:
            break
        time.sleep(0.05)
    assert st["jobs"][-1]["ok"] is True, st["jobs"][-1]
    assert st["backtest"]["summary"]["deals"] == 3 and st["backtest"]["summary"]["priced"] == 3
    status, w = _post(base + "/api/watch", {"on": True, "every": 30})
    assert status == 200 and w["on"] is True and w["every"] == 30
    _, st = _get(base + "/api/state")
    assert st["watch"]["on"] is True
    status, w = _post(base + "/api/watch", {"on": False})
    assert w["on"] is False


def test_calculators_orders_close_and_groww_test(server):
    base, app = server
    status, c = _get(base + "/api/costs?amount=10000")
    assert status == 200 and c["model"] == "india_delivery" and 69 < c["charges_bps"] < 70
    assert c["buy"]["stamp_duty"] > 0 and c["sell"]["dp_charge"] == 20
    status, _ = _get(base + "/api/costs?amount=0")
    assert status == 400
    status, sz = _get(base + "/api/size?ticker=nvda&risk_pct=1&max_pct=10")
    assert status == 200 and sz["ticker"] == "NVDA" and sz["qty"] > 0 and sz["notional"] <= 8000.01
    status, _ = _get(base + "/api/size")
    assert status == 400
    # trade ticket by quantity, then close the position
    status, j = _post(base + "/api/order", {"symbol": "nvda", "side": "buy", "qty": 5})
    assert status == 200 and j["order"]["qty"] == 5
    status, j = _post(base + "/api/order", {"symbol": "NVDA", "side": "buy"})
    assert status == 400 and "amount or a quantity" in j["error"]
    status, j = _post(base + "/api/order", {"symbol": "NVDA", "side": "short", "qty": 1})
    assert status == 400
    _, st = _get(base + "/api/state")
    assert st["orders"][0]["symbol"] == "NVDA" and st["positions"][0]["qty"] == 5
    status, j = _post(base + "/api/close", {"symbol": "nvda"})
    assert status == 200 and j["order"]["side"] == "sell" and j["order"]["qty"] == 5
    status, j = _post(base + "/api/close", {"symbol": "NVDA"})
    assert status == 400 and "no open position" in j["error"]
    status, orders = _get(base + "/api/orders")
    assert status == 200 and len(orders) == 2 and orders[0]["side"] == "sell"
    status, g = _post(base + "/api/groww-test", {})
    assert status == 200 and g["ok"] is False and "credentials" in g["message"]
    assert "token" not in json.dumps(g).lower().replace("access_token", "")


def test_settings_market_switch_and_cash(server):
    base, app = server
    status, j = _post(base + "/api/settings", {"market": "us", "paper_starting_cash": "250000"})
    assert status == 200 and j["applied"] == {"MARKET": "us", "PAPER_STARTING_CASH": "250000"}
    _, st = _get(base + "/api/state")
    assert st["settings"]["market"] == "us" and st["settings"]["paper_starting_cash"] == 250000
    assert st["settings"]["watch_source"] == "congress" and st["settings"]["data_source"] == "quiver"
    env = (app.settings.state_dir / ".env").read_text()
    assert "MARKET=us" in env and "PAPER_STARTING_CASH=250000" in env
    status, j = _post(base + "/api/settings", {"market": "mars"})
    assert status == 400
    status, j = _post(base + "/api/settings", {"paper_starting_cash": "-5"})
    assert status == 400


def test_watch_auto_exit_flag_and_regime_refresh(server):
    base, app = server
    status, w = _post(base + "/api/watch", {"on": True, "every": 45, "auto_exit": True})
    assert status == 200 and w["on"] and w["every"] == 45 and w["auto_exit"] is True
    _, st = _get(base + "/api/state")
    assert st["watch"]["auto_exit"] is True
    _post(base + "/api/watch", {"on": False})
    status, r = _get(base + "/api/regime?refresh=1")
    assert status == 200  # "not configured" in the fixture, but the route works


def test_reset_keeps_cost_model(server, tmp_path):
    base, app = server
    from trading_agent.costs import IndianDeliveryCosts
    app._broker.cost_model = IndianDeliveryCosts()
    _post(base + "/api/reset", {})
    assert app._broker.cost_model is not None


def test_equity_history_scorecard_and_factor_endpoints(server):
    base, app = server
    _post(base + "/api/order", {"symbol": "NVDA", "side": "buy", "qty": 2})
    _, st = _get(base + "/api/state")
    assert len(st["equity_history"]) == 1 and st["equity_history"][0]["positions"] == 1
    assert st["equity_stats"]["points"] == 1 and st["equity_stats"]["max_drawdown"] == 0
    status, sc = _get(base + "/api/scorecard")
    assert status == 200 and sc["summary"]["recommendations"] == 0
    status, fb = _get(base + "/api/factor-backtest")
    assert status == 200 and fb == {}
    app.settings.market = "us"
    status, job = _post(base + "/api/factor-backtest", {"universe": "NIFTY50", "top": 10, "years": 2})
    assert status == 202 and job["ok"] is False and "India" in job["message"]


def test_check_records_equity(settings, sample_rows):
    from trading_agent.broker import LocalPaperBroker
    from trading_agent.notify import Notifier
    from trading_agent.quiver import _norm_congress, filter_by_investor
    from trading_agent.runner import check
    from trading_agent.state import State
    trades = filter_by_investor([_norm_congress(r) for r in sample_rows], "Nancy Pelosi")
    broker = LocalPaperBroker(settings.state_dir / "pb.json", starting_cash=1000)
    check(settings, trades=trades, broker=broker, notifier=Notifier(), dry_run=True)
    check(settings, trades=[], broker=broker, notifier=Notifier())  # nothing new: still records
    hist = State(settings.state_dir / "state.json").data.get("practice_equity", [])
    assert len(hist) == 1 and hist[0]["equity"] == 1000


def test_signal_lab_endpoints(server):
    base, app = server
    status, r = _get(base + "/api/signal-lab")
    assert status == 200 and r == {}
    status, j = _post(base + "/api/signal-lab", {"universe": "NIFTY50", "horizons": "0,20"})
    assert status == 400 and "horizons" in j["error"]
    app.settings.market = "us"
    status, job = _post(base + "/api/signal-lab", {"universe": "NIFTY50", "years": 3})
    assert status == 202 and job["kind"] == "signal_lab" and job["ok"] is False and "India" in job["message"]
    _, st = _get(base + "/api/state")
    assert st["signal_lab"] is None


def test_live_orders_gtt_status_and_gtt_toggle(server):
    from trading_agent.state import State
    base, app = server
    app.broker.set_price("NVDA", 180.0)
    app.broker.submit_order("NVDA", "buy", qty=2)
    st = State(app.settings.state_dir / "state.json")
    st.data["live_orders"] = [
        {"groww_order_id": "GMK1", "symbol": "RELIANCE", "side": "buy", "qty": 3, "limit_price": 2876.3,
         "status": "open", "order_status": "OPEN", "filled_quantity": 0, "placed_at": "2099-01-01T10:00:00+05:30"},
        {"groww_order_id": "GMK2", "symbol": "TCS", "side": "sell", "qty": 1, "status": "failed",
         "order_status": "REJECTED", "remark": "price band", "placed_at": "2000-01-01T10:00:00+05:30"}]
    st.data["gtt_stops"] = {"NVDA": {"smart_order_id": "gtt_1", "trigger": 153.0, "limit": 152.25, "qty": 2,
                                     "status": "ACTIVE"}}
    st.save()
    _, s = _get(base + "/api/state")
    assert [o.get("groww_order_id") for o in s["orders"]] == ["GMK1", None, "GMK2"]  # newest first
    assert s["orders"][0]["live"] and s["orders"][0]["status"] == "open"
    assert s["orders"][2]["order_status"] == "REJECTED"
    assert s["positions"][0]["gtt"]["status"] == "ACTIVE" and s["positions"][0]["gtt"]["trigger"] == 153.0
    assert s["positions"][0]["sellable_qty"] == 2
    assert s["settings"]["groww_gtt_stops"] is False and s["connections"]["groww_gtt_active"] is False
    status, j = _post(base + "/api/settings", {"groww_gtt_stops": True})
    assert status == 200 and j["applied"]["GROWW_GTT_STOPS"] == "true"
    assert "GROWW_GTT_STOPS=true" in (app.settings.state_dir / ".env").read_text()
    _, s = _get(base + "/api/state")
    # saved, but still inactive: live orders are off and can't be turned on from here
    assert s["settings"]["groww_gtt_stops"] is True and s["connections"]["groww_gtt_active"] is False
    status, j = _post(base + "/api/settings", {"groww_live_orders": True})
    assert status == 200 and j["applied"] == {} and app.settings.groww_live_orders is False


def test_serves_inter_fonts_only_from_the_whitelist(server):
    base, app = server
    with urllib.request.urlopen(base + "/fonts/inter-latin.woff2", timeout=5) as r:
        assert r.status == 200 and r.headers["Content-Type"] == "font/woff2" and r.read(4) == b"wOF2"
    for bad in ("/fonts/../ui.py", "/fonts/inter-latin.woff2x", "/fonts/LICENSE.txt"):
        status, _ = _get(base + bad)
        assert status == 404
    _, html = _get(base + "/")
    assert "fonts.googleapis.com" not in html and "/fonts/inter-latin.woff2" in html


def test_page_is_served_as_utf8(server):
    base, app = server
    with urllib.request.urlopen(base + "/", timeout=5) as r:
        html = r.read().decode("utf-8")
    assert "Â" not in html and "â‚¹" not in html and "·" in html


def test_forward_test_appears_in_the_dashboard(server):
    from datetime import datetime
    from trading_agent.forward import IST, ForwardTest
    base, app = server
    _, st = _get(base + "/api/state")
    assert st["forward"] is None
    prices = {"MID150BEES": 200.0, "A": 100.0, "B": 50.0}
    ft = ForwardTest(app.settings.state_dir, top=2, capital=100_000, price_fn=lambda s: prices[s],
                     now=lambda: datetime(2026, 10, 9, 16, 0, tzinfo=IST))
    ft.run(lambda: {"top": [{"symbol": "A"}, {"symbol": "B"}], "eligible": 9})
    prices.clear()  # the dashboard must not need live prices
    _, st = _get(base + "/api/state")
    f = st["forward"]
    assert f["universe"] == "NIFTYMIDCAP150" and f["benchmark"] == "MID150BEES" and f["days"] == 1
    assert {h["symbol"] for h in f["holdings"]} == {"A", "B"} and f["last_rebalance"] == "2026-10"


def test_my_groww_portfolio_shows_buy_current_and_pl(server, monkeypatch):
    from trading_agent import groww
    from .conftest import FakeSession
    base, app = server
    _, m = _get(base + "/api/my-portfolio")
    assert m == {"linked": False}
    routes = {
        ("GET", "/holdings/user"): {"status": "SUCCESS", "payload": {"holdings": [
            {"trading_symbol": "TCS", "quantity": 20, "average_price": 3000.0, "demat_free_quantity": 20, "t1_quantity": 0},
            {"trading_symbol": "INFY", "quantity": 10, "average_price": 1600.0, "demat_free_quantity": 6, "t1_quantity": 0,
             "pledge_quantity": 4},
            {"trading_symbol": "NSE", "quantity": 8, "average_price": 1000.0, "demat_free_quantity": 8, "t1_quantity": 0}]}},
        ("GET", "/live-data/ltp"): {"status": "SUCCESS", "payload": {"NSE_TCS": 3300.0, "NSE_INFY": 1400.0}},
        ("GET", "EQUITY_L.csv"): "SYMBOL,NAME OF COMPANY\nTCS,Tata Consultancy Services Limited\nINFY,Infosys Limited\n",
        ("GET", "instrument.csv"): "exchange,trading_symbol,name,segment\nBSE,NSE,NSE,CASH\n",
    }
    sess = FakeSession(routes)
    monkeypatch.setattr(groww.requests, "Session", lambda: sess)

    class NoPrice:
        def __call__(self, sym):
            raise LookupError("unlisted")

    app.prices = NoPrice()
    app.settings.groww_access_token = "tok"
    _, m = _get(base + "/api/my-portfolio?refresh=1")
    assert m["linked"] and [h["symbol"] for h in m["holdings"]] == ["TCS", "INFY", "NSE"]
    tcs, infy, nse = m["holdings"]
    assert tcs["avg_price"] == 3000 and tcs["price"] == 3300 and tcs["pl"] == 6000 and abs(tcs["pl_pct"] - 0.10) < 1e-9
    assert infy["pl"] == -2000 and abs(infy["pl_pct"] + 0.125) < 1e-9 and infy["sellable_qty"] == 6
    assert nse["price"] is None and nse["value"] is None and m["unpriced"] == ["NSE"]
    assert tcs["name"] == "Tata Consultancy Services Limited" and tcs["exchange"] == "NSE"
    assert nse["name"] == "National Stock Exchange of India Limited" and nse["exchange"] == "BSE"
    assert m["invested"] == 60000 + 16000 + 8000 and m["value"] == 66000 + 14000 and m["pl"] == 4000
    assert abs(m["pl_pct"] - 4000 / 76000) < 1e-9
    assert sess.writes() == []  # read-only
    n = len(sess.calls)
    _get(base + "/api/my-portfolio")
    assert len(sess.calls) == n  # cached for a minute
    _, html = _get(base + "/")
    assert 'id="mp-rows"' in html and "My Groww portfolio" in html


def test_refused_job_is_not_recorded_and_names_the_running_one(server, monkeypatch):
    import threading as th
    from trading_agent import screen as screen_mod
    base, app = server
    release = th.Event()

    def slow_screen(members, prices, **kw):
        release.wait(5)
        return {"universe_size": 1, "scored": 1, "eligible": 1, "errors": 0, "top": [], "all": [], "fundamentals": None}

    monkeypatch.setattr(screen_mod, "load_universe", lambda u: [{"symbol": "A", "name": "A", "industry": ""}])
    monkeypatch.setattr(screen_mod, "run_screen", slow_screen)
    status, j = _post(base + "/api/screen", {"universe": "NIFTY50", "top": 5})
    assert status == 202 and j["ok"] is None
    _, st = _get(base + "/api/state")
    assert st["busy"] and st["running"]["label"] == "The factor screen"
    status, refused = _post(base + "/api/signal-lab", {"universe": "NIFTY50", "years": 3, "horizons": "5"})
    assert refused["ok"] is False and refused["message"].startswith("The factor screen is still running")
    release.set()
    for _ in range(50):
        _, st = _get(base + "/api/state")
        if not st["busy"]:
            break
        time.sleep(0.05)
    assert not st["busy"] and st["running"] is None
    assert st["jobs"][-1]["kind"] == "screen" and st["jobs"][-1]["ok"] is True  # the refusal left no trace
    assert all("still running" not in (x["message"] or "") for x in st["jobs"])


def test_search_endpoint_is_offline_in_demo(server):
    base, app = server
    status, hits = _get(base + "/api/search?q=tata")
    assert status == 200 and hits == []
    _, lk = _get(base + "/api/lookup?ticker=senco")
    assert lk["ticker"] == "SENCO" and lk["matched_from"] is None


def test_server_ignores_browser_closing_connection_early(capsys):
    import sys as _sys
    from trading_agent.ui import _Server

    srv = _Server.__new__(_Server)  # no socket needed to test the error hook
    try:
        raise ConnectionAbortedError(10053, "aborted by the host")
    except ConnectionAbortedError:
        srv.handle_error(None, ("127.0.0.1", 1))
    assert "Traceback" not in capsys.readouterr().err
    try:
        raise ValueError("real bug")
    except ValueError:
        srv.handle_error(None, ("127.0.0.1", 1))
    assert "real bug" in capsys.readouterr().err


def test_equity_curve_ignores_points_from_before_the_paper_account(tmp_path):
    from trading_agent.broker import LocalPaperBroker
    from trading_agent.state import State

    st = State(tmp_path / "state.json")
    st.data["equity_history"] = [{"at": "2026-10-09T16:21:03+00:00", "equity": 179426.0, "cash": 0.0, "positions": 16}]
    b = LocalPaperBroker(tmp_path / "pb.json", starting_cash=100_000, price_fn=lambda s: 100.0)
    assert b.created_at > "2026-10-09T16:21:03+00:00"
    assert st.equity_history(b.created_at) == [] and st.equity_stats(b.created_at) is None
    st.record_equity(100_000, 100_000, 0, since=b.created_at)
    assert [p["equity"] for p in st.data["equity_history"]] == [100_000]


def test_old_paper_file_dates_itself_from_its_first_order(tmp_path):
    import json
    from trading_agent.broker import LocalPaperBroker

    path = tmp_path / "pb.json"
    path.write_text(json.dumps({"cash": 1.0, "starting_cash": 1.0, "positions": {}, "prices": {},
                                "orders": [{"filled_at": "2026-10-09T17:54:09+00:00"}]}))
    assert LocalPaperBroker(path).created_at == "2026-10-09T17:54:09+00:00"


def test_static_files_and_tabs_are_served(settings):
    import threading
    import urllib.request
    from trading_agent.ui import App, make_server

    srv = make_server(App(settings, dotenv=None), port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        css = urllib.request.urlopen(base + "/static/nocturne.css").read().decode()
        js = urllib.request.urlopen(base + "/static/common.js").read().decode()
        page = urllib.request.urlopen(base + "/").read().decode()
        assert ".tabs" in css and "window.TA" in js and "attachSuggest" in js
        assert '/static/nocturne.css' in page and '/static/common.js' in page and 'href="/replay"' in page
        assert "<style>" not in page  # the CSS lives in one shared file now
    finally:
        srv.shutdown()


class _FakeGroww:
    """Stands in for the Groww client: reads work, any write is recorded so a test can assert there were none."""
    name = "groww"

    def __init__(self):
        self.writes = []

    def latest_price(self, symbol):
        return 100.0

    def account(self):
        raise AssertionError("the live broker's account is not read by the Demo page")

    def positions(self):
        return []

    def submit_order(self, *a, **k):
        self.writes.append((a, k))
        raise AssertionError("Groww write on the Demo page")

    def orders(self):
        return []


class _FakeData:
    def __init__(self, trades):
        self.trades = trades

    def trades_for_investor(self, investor, source, **kw):
        return self.trades


@pytest.fixture
def two_pages(settings, sample_rows, monkeypatch):
    """A Live app (Groww live orders ON, fake Groww) plus an existing practice account on disk, served over HTTP."""
    import trading_agent.runner as runner
    fake = _FakeGroww()
    monkeypatch.setattr(runner, "make_groww", lambda s, price_fn=None: fake)
    monkeypatch.setattr(App, "holidays", property(lambda self: None))

    def no_notifier(*a, **k):
        raise AssertionError("a notifier was built")
    monkeypatch.setattr("trading_agent.ui.make_notifier", no_notifier)
    settings.market, settings.broker, settings.groww_access_token = "in", "groww", "tok"
    settings.groww_live_orders = True
    settings.notify_webhook_url, settings.resend_api_key = "https://example.invalid/hook", "re_test"
    settings.watch_investor, settings.watch_source = "Nancy Pelosi", "congress"
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    pb = LocalPaperBroker(settings.state_dir / "paper_broker.json", starting_cash=100_000, price_fn=lambda s: 100.0,
                          currency="INR", whole_shares=True)
    pb.submit_order("LAURUSLABS", "buy", qty=10)
    (settings.state_dir / "state.json").write_text(json.dumps(
        {"seen": [], "recommendations": [{"ticker": "SENCO", "action": "buy", "headline": "h", "rationale": "r",
                                          "confidence": "high", "suggested_notional_usd": 5000,
                                          "at": "2026-01-01T00:00:00+00:00"}],
         "runs": [], "equity_history": []}))
    trades = filter_by_investor([_norm_congress(r) for r in sample_rows], "Nancy Pelosi")
    app = App(settings, broker=fake, data=_FakeData(trades), dotenv=None)
    srv = make_server(app, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", app, fake, settings
    srv.shutdown()
    srv.server_close()


def test_live_snapshot_is_the_live_page_and_the_page_switches_on_mode(two_pages):
    base, app, fake, settings = two_pages
    _, live = _get(base + "/api/state")
    assert live["page"] == "live"
    html = (Path(__file__).resolve().parents[1] / "trading_agent" / "ui" / "index.html").read_text(encoding="utf-8")
    assert 'id="pp-card" hidden' in html and 'MODE === "demo"' in html
    assert "practice money on real market data" in html and "Reset practice account" in html
    _, page = _get(base + "/")
    assert 'data-mode="demo"' not in page
    _, dpage = _get(base + "/demo")
    assert 'data-api="/demo"' in dpage and 'data-mode="demo"' in dpage and 'data-sample="1"' not in dpage


def test_demo_uses_the_practice_account_and_shares_recommendations(two_pages):
    base, app, fake, settings = two_pages
    _, live = _get(base + "/api/state")
    _, demo = _get(base + "/demo/api/state")
    assert demo["page"] == "demo"
    assert [p["symbol"] for p in demo["positions"]] == ["LAURUSLABS"]
    assert demo["account"]["cash"] == 100_000 - 10 * 100 - demo["performance"]["fees_paid"]
    assert [r["ticker"] for r in demo["recommendations"]] == [r["ticker"] for r in live["recommendations"]] == ["SENCO"]
    assert len(demo["deals"]) == len(live["deals"]) > 0
    assert demo["connections"]["groww_live_orders"] is False and demo["settings"]["demo"] is False
    assert demo["settings"]["notify_webhook_url"] == "" and demo["settings"]["notify_email_to"] == ""
    assert app.demo.dotenv is None and app.demo.settings.state_dir == settings.state_dir


def test_demo_order_fills_on_practice_even_with_live_orders_on(two_pages):
    base, app, fake, settings = two_pages
    status, j = _post(base + "/demo/api/order", {"symbol": "SENCO", "side": "buy", "qty": 3})
    assert status == 200 and j["order"]["qty"] == 3
    assert fake.writes == []
    saved = json.loads((settings.state_dir / "paper_broker.json").read_text())
    assert saved["positions"]["SENCO"]["qty"] == 3
    status, j = _post(base + "/demo/api/close", {"symbol": "SENCO"})
    assert status == 200 and fake.writes == []
    # the Live page no longer takes practice orders
    status, j = _post(base + "/api/order", {"symbol": "SENCO", "side": "buy", "qty": 1})
    assert status == 403


def test_demo_reset_touches_only_the_practice_account(two_pages):
    base, app, fake, settings = two_pages
    state_before = (settings.state_dir / "state.json").read_bytes()
    status, j = _post(base + "/demo/api/reset", {})
    assert status == 200 and j["removed"] == ["paper_broker.json"]
    assert (settings.state_dir / "state.json").read_bytes() == state_before
    assert not (settings.state_dir / "paper_broker.json").exists()
    _, demo = _get(base + "/demo/api/state")
    assert demo["positions"] == [] and demo["account"]["cash"] == settings.paper_starting_cash
    status, _ = _post(base + "/demo/api/order", {"symbol": "SENCO", "side": "buy", "qty": 1})
    assert status == 200 and (settings.state_dir / "paper_broker.json").exists()


def test_live_reset_forgets_deals_but_never_the_practice_account(two_pages):
    base, app, fake, settings = two_pages
    status, j = _post(base + "/api/reset", {})
    assert status == 200 and j["removed"] == ["state.json"]
    assert (settings.state_dir / "paper_broker.json").exists()


def test_demo_refuses_what_controls_the_real_agent(two_pages):
    base, app, fake, settings = two_pages
    before = dict(vars(settings))
    for path, body in (("/demo/api/check", {"dry_run": True}), ("/demo/api/settings", {"auto_trade": True}),
                       ("/demo/api/watch", {"on": True}), ("/demo/api/groww-test", {})):
        status, j = _post(base + path, body)
        assert status == 403 and "Live page" in j["error"], path
    assert vars(settings) == before and app.watcher is None and fake.writes == []


def test_live_and_demo_share_one_practice_writer_in_paper_mode(settings, monkeypatch):
    monkeypatch.setattr(App, "holidays", property(lambda self: None))
    settings.market = "in"
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    pb = LocalPaperBroker(settings.state_dir / "paper_broker.json", starting_cash=1000, price_fn=lambda s: 10.0,
                          currency="INR", whole_shares=True)
    app = App(settings, broker=pb, data=_FakeData([]), dotenv=None)
    assert app.practice_broker is pb and app.demo.broker is pb
    app.demo.reset()
    assert app.practice_broker is pb and pb.account().cash == settings.paper_starting_cash


def test_sample_mode_is_isolated_and_is_the_whole_dashboard(settings):
    from trading_agent.ui import sample_app
    settings.market = "in"
    settings.broker, settings.groww_access_token, settings.groww_live_orders = "groww", "tok", True
    settings.notify_webhook_url, settings.resend_api_key = "https://example.invalid/hook", "re_test"
    settings.watch_investor = "Ashish Kacholia"
    app = sample_app(settings, None)
    assert app.settings.state_dir == settings.state_dir / "demo-sample" and app.dotenv is None
    assert app.demo_trades and app.demo is app and app.practice_broker is app.broker
    assert app.settings.groww_access_token is None and not app.settings.use_groww
    assert not app.settings.groww_live_orders
    assert app.settings.notify_webhook_url is None and app.settings.resend_api_key is None
    assert app.snapshot()["page"] == "demo" and app.snapshot()["settings"]["demo"] is True
    srv = make_server(app, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        _, page = _get(f"http://127.0.0.1:{srv.server_address[1]}/")
        assert 'data-mode="demo"' in page and 'data-sample="1"' in page and "bundled sample data (offline)" in page
    finally:
        srv.shutdown()
        srv.server_close()
    app.update_settings({"notify_webhook_url": "https://example.invalid/h", "notify_email_to": "a@b.c"})
    assert app.settings.notify_webhook_url is None and app.settings.notify_email_to is None
    assert not (settings.state_dir / "paper_broker.json").exists() and not (settings.state_dir / "state.json").exists()


def test_index_script_parses():
    import re
    import shutil
    import subprocess
    import tempfile
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    html = (Path(__file__).resolve().parents[1] / "trading_agent" / "ui" / "index.html").read_text(encoding="utf-8")
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    assert scripts
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "page.js"
        f.write_text("\n".join(scripts), encoding="utf-8")
        r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr


def test_tiles_and_recommendation_buttons_depend_on_mode():
    """The page, not the server, decides what each mode shows: run it under node with a stub DOM."""
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    harness = Path(__file__).resolve().parent / "ui_mode_harness.js"
    for mode, want_buy, want_tile, no_tile in (("live", False, "Holdings value", "Practice equity"),
                                               ("demo", True, "Practice equity", "Holdings value")):
        r = subprocess.run([node, str(harness), mode], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr + r.stdout
        out = json.loads(r.stdout)
        assert out["paper_buy_button"] is want_buy, mode
        assert want_tile in out["tiles"] and no_tile not in out["tiles"], mode
        assert out["pp_card_hidden"] is (mode == "live"), mode
        assert ("Reset practice account" in out["banner"]) is (mode == "demo"), mode
        assert out["check_hidden"] is out["settings_hidden"] is out["watch_hidden"] is (mode == "demo"), mode
    # the offline sample dashboard is a Demo page that owns everything: banner says so, controls stay
    r = subprocess.run([node, str(harness), "demo", "sample"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert "bundled sample data (offline)" in out["banner"] and out["check_hidden"] is False and out["pp_card_hidden"] is False


def test_run_background_busy_check_is_atomic(settings):
    app = App(settings, dotenv=None)
    gate = threading.Event()
    jobs, start = [], threading.Barrier(8)

    def go():
        start.wait()
        jobs.append(app.run_background("x", lambda j: gate.wait(5) and "ok"))
    ts = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    gate.set()
    assert sum(1 for j in jobs if j in app.jobs) == 1


def test_job_starters_claim_the_slot_under_the_lock(settings):
    import threading
    from trading_agent.ui import App

    app = App(settings, dotenv=None)
    held = threading.Event()
    real = app._slot_lock

    class Spy:
        def __enter__(self):
            held.set()
            return real.__enter__()

        def __exit__(self, *a):
            return real.__exit__(*a)

    app._slot_lock = Spy()
    app.busy = True  # refused straight away, but only after taking the lock
    for start in (lambda: app.start_backtest("x", 30, (5,), 50.0), lambda: app.start_screen("NIFTY50", 5),
                  lambda: app.start_factor_backtest("NIFTY50", 5, 1), lambda: app.start_signal_lab("NIFTY50", 1, [5])):
        held.clear()
        job = start()
        assert job.ok is False and held.is_set()


def test_factor_backtest_job_keeps_result_when_validation_fails(server, monkeypatch):
    base, app = server
    result = {"dates": ["2024-01-02", "2024-02-01"], "strategy": [100.0, 101.0], "stats": {
        "strategy": {"total_return": 0.01}, "benchmark": {"total_return": 0.0}}, "months": 1,
        "index_fund_symbol": None}
    monkeypatch.setattr("trading_agent.screen.load_universe", lambda u: [{"symbol": "AAA"}])
    monkeypatch.setattr("trading_agent.index_history.point_in_time", lambda *a, **k: None)
    monkeypatch.setattr("trading_agent.factor_backtest.run_factor_backtest", lambda *a, **k: dict(result))

    def boom(*a, **k):
        raise RuntimeError("validation exploded")
    monkeypatch.setattr("trading_agent.factor_backtest.validate_factor_backtest", boom)
    job = app.start_factor_backtest("NIFTY50", 10, 1)
    for _ in range(200):
        if job.finished_at:
            break
        time.sleep(0.02)
    assert job.ok is True
    assert app.last_factor_bt["validation"] is None and app.last_factor_bt["strategy"] == [100.0, 101.0]


def test_practice_and_real_equity_curves_are_separate(two_pages):
    base, app, fake, settings = two_pages
    sp = settings.state_dir / "state.json"
    st = json.loads(sp.read_text())
    st["equity_history"] = [{"at": "2026-01-01T00:00:00+00:00", "equity": 500000.0, "cash": 1.0, "positions": 3}]
    sp.write_text(json.dumps(st))
    _post(base + "/demo/api/reset", {})
    status, _ = _post(base + "/demo/api/order", {"symbol": "SENCO", "side": "buy", "qty": 2})
    assert status == 200
    saved = json.loads(sp.read_text())
    assert saved["equity_history"] == st["equity_history"]  # the real account's curve is intact
    assert len(saved["practice_equity"]) == 1
    _, demo = _get(base + "/demo/api/state")
    _, live = _get(base + "/api/state")
    assert [p["equity"] for p in demo["equity_history"]] == [saved["practice_equity"][0]["equity"]]
    assert [p["equity"] for p in live["equity_history"]] == [500000.0]  # Live's own points never show on Demo, nor Demo's on Live


def test_state_write_during_a_slow_account_read_survives(two_pages):
    base, app, fake, settings = two_pages
    from trading_agent.state import State
    entered, release = threading.Event(), threading.Event()
    pb = app.practice_broker
    real = pb.account

    def slow_account():
        entered.set()
        release.wait(5)
        return real()
    t = threading.Thread(target=lambda: app.demo.paper_order("SENCO", "buy", qty=1))
    pb.account = slow_account
    t.start()
    assert entered.wait(5)
    st = State(settings.state_dir / "state.json")
    st.data["seen"] = {"new-deal": {"at": "now", "summary": "s"}}
    st.save()
    release.set()
    t.join(5)
    pb.account = real
    after = json.loads((settings.state_dir / "state.json").read_text())
    assert "new-deal" in after["seen"] and len(after["practice_equity"]) == 1


def test_one_broker_build_and_one_writer_under_threads(settings, monkeypatch):
    import trading_agent.ui as ui
    settings.market = "in"
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    builds = []

    def slow_make_broker(s):
        builds.append(1)
        time.sleep(0.1)
        return LocalPaperBroker(s.state_dir / "paper_broker.json", starting_cash=1_000_000, price_fn=lambda x: 10.0,
                                currency="INR", whole_shares=True)
    monkeypatch.setattr(ui, "make_broker", slow_make_broker)
    app = App(settings, data=_FakeData([]), dotenv=None)
    got = []
    ts = [threading.Thread(target=lambda f=f: got.append(f())) for f in
          (lambda: app.broker, lambda: app.practice_broker, lambda: app.demo.broker, lambda: app.broker)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(builds) == 1 and all(b is got[0] for b in got)
    # concurrent orders (watch auto-exit vs Demo) never interleave: every fill is saved and counted once
    b = got[0]
    ots = [threading.Thread(target=lambda: b.submit_order("AAA", "buy", qty=1)) for _ in range(12)]
    [t.start() for t in ots]
    [t.join() for t in ots]
    saved = json.loads(b.path.read_text())
    assert saved["positions"]["AAA"]["qty"] == 12 and len(saved["orders"]) == 12


def test_orders_table_is_real_orders_on_live_and_practice_fills_on_demo(two_pages):
    base, app, fake, settings = two_pages
    sp = settings.state_dir / "state.json"
    st = json.loads(sp.read_text())
    st["live_orders"] = [{"groww_order_id": "GMK1", "symbol": "TCS", "side": "buy", "qty": 1, "status": "FILLED",
                          "placed_at": "2026-02-01T00:00:00+00:00", "average_fill_price": 10.0}]
    sp.write_text(json.dumps(st))
    _post(base + "/demo/api/order", {"symbol": "SENCO", "side": "buy", "qty": 1})
    _, live = _get(base + "/api/state")
    _, demo = _get(base + "/demo/api/state")
    assert [o.get("groww_order_id") for o in live["orders"]] == ["GMK1"]
    assert demo["orders"] and all(not o.get("live") and not o.get("groww_order_id") for o in demo["orders"])


def test_demo_child_is_rebuilt_only_when_settings_change(two_pages, monkeypatch):
    base, app, fake, settings = two_pages
    import dataclasses
    d1 = app.demo
    monkeypatch.setattr(dataclasses, "asdict", lambda *a, **k: (_ for _ in ()).throw(AssertionError("asdict")))
    assert app.demo is d1 and app.demo is d1
    app.update_settings({"watch_investor": "Someone Else"})
    d2 = app.demo
    assert d2 is not d1 and d2.settings.watch_investor == "Someone Else"


def test_live_tiles_show_a_reason_when_holdings_fail_and_a_placeholder_first():
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    harness = Path(__file__).resolve().parent / "ui_mode_harness.js"
    r = subprocess.run([node, str(harness), "live", "mpfail"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert "Couldn't load" in out["tiles"] and "n/a" in out["tiles"] and "Loading" not in out["tiles"]
    html = (Path(__file__).resolve().parents[1] / "trading_agent" / "ui" / "index.html").read_text(encoding="utf-8")
    assert "Loading your holdings" in html


def test_news_poll_decision_logic():
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    js = """
const fs=require('fs'),vm=require('vm');
const ctx={document:{getElementById:()=>null,body:{dataset:{}}},console,Intl,Date,Math,JSON};ctx.window=ctx;
vm.createContext(ctx);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),ctx);
const T=ctx.TA, pend={items:[{sentiment:'positive'},{}],tagger:'pending'}, done={items:[{sentiment:'positive'}],tagger:'ollama'};
console.log(JSON.stringify([
 T.newsPollNext(pend,0,1000,true), T.newsPollNext({items:[{}],tagger:'ollama:qwen'},0,1000,true),
 T.newsPollNext(done,0,1000,true), T.newsPollNext({items:[{}],tagger:'none'},0,1000,true),
 T.newsPollNext(pend,0,1000,false), T.newsPollNext(pend,0,119999,true), T.newsPollNext(pend,0,120000,true),
 T.newsPollNext({items:[],tagger:'pending'},0,1,true), T.newsPollNext({demo:true,items:[{}],tagger:'none'},0,1,true), T.NEWS_POLL_MS]));
"""
    common = Path(__file__).resolve().parents[1] / "trading_agent" / "ui" / "common.js"
    r = subprocess.run([node, "-e", js, str(common)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == ["poll", "poll", "stop", "stop", "stop", "poll", "stop", "stop", "stop", 5000]


def test_live_reset_keeps_the_practice_curve(two_pages):
    base, app, fake, settings = two_pages
    _post(base + "/demo/api/order", {"symbol": "SENCO", "side": "buy", "qty": 1})
    before = json.loads((settings.state_dir / "state.json").read_text())["practice_equity"]
    _post(base + "/api/reset", {})
    after = json.loads((settings.state_dir / "state.json").read_text())
    assert after["practice_equity"] == before and after.get("recommendations", []) == []


def test_lookup_note_includes_the_market_filter():
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    js = """
const fs=require('fs'),vm=require('vm');
const ctx={document:{getElementById:()=>null,body:{dataset:{}}},console,Intl,Date,Math,JSON};ctx.window=ctx;
vm.createContext(ctx);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),ctx);
const L=ctx.TA.lookupTakeaway;
const strong={ticker:'MCX',momentum:{verdict:'strong',ret_6m:0.2,ret_12_1:0.3,above_200dma:true}};
const weak={ticker:'XYZ',momentum:{verdict:'weak',ret_6m:-0.2,ret_12_1:-0.3,above_200dma:false}};
const off={regime:'risk_off',markets:{nifty50:{above_200dma:false}}}, neutral={regime:'neutral',markets:{nifty50:{above_200dma:true}}};
const scr={top:[{symbol:'A'},{symbol:'B'},{symbol:'C<'},{symbol:'D'},{symbol:'E'},{symbol:'F'}]};
console.log(JSON.stringify({
 off:L(strong,undefined,{regime:off,screen:scr}), offNoScreen:L(strong,undefined,{regime:off,screen:null}),
 neutral:L(strong,undefined,{regime:neutral,screen:scr}), replay:L(strong), weakNeutral:L(weak,undefined,{regime:neutral,screen:scr}),
 replayWeak:L(weak)}));
"""
    common = Path(__file__).resolve().parents[1] / "trading_agent" / "ui" / "common.js"
    r = subprocess.run([node, "-e", js, str(common)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    o = json.loads(r.stdout)
    hold = "The market is risk-off (Nifty below its 200-day average), so the agent's rules hold off new buys until it recovers; if you buy anyway, keep the position small."
    assert hold in o["off"] and "trend-wise it passes, but the market filter says wait." in o["off"]
    assert o["off"].index(hold) < o["off"].index("Bottom line") and "kind of stock the screen buys" not in o["off"]
    assert o["off"].endswith("Stocks passing the screen today: A, B, C&lt;, D, E.")
    assert o["offNoScreen"].endswith("Run Screen to see which stocks pass today.")
    assert "kind of stock the screen buys" in o["neutral"] and hold not in o["neutral"] and "Stocks passing" not in o["neutral"]
    assert o["replay"] == o["neutral"] and "Stocks passing" not in o["replayWeak"] and "Run Screen" not in o["replayWeak"]
    assert o["weakNeutral"].endswith("Stocks passing the screen today: A, B, C&lt;, D, E.")
