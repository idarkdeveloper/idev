"""Dashboard server: JSON API over the demo inputs, no network."""
import json
import threading
import time
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
    hist = State(settings.state_dir / "state.json").data.get("equity_history", [])
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


def test_demo_is_isolated_from_live_and_groww(settings, monkeypatch, tmp_path):
    import trading_agent.groww as g
    from trading_agent.ui import App
    monkeypatch.setattr(g.GrowwBroker, "__init__", lambda *a, **k: (_ for _ in ()).throw(AssertionError("groww")))
    settings.market, settings.broker, settings.groww_access_token = "in", "groww", "tok"
    settings.watch_investor = "Ashish Kacholia"
    settings.notify_webhook_url, settings.resend_api_key = "https://example.invalid/hook", "re_test"
    live = App(settings, dotenv=None)
    demo = live.demo
    assert demo.settings.notify_webhook_url is None and demo.settings.resend_api_key is None
    assert demo.settings.state_dir == settings.state_dir / "demo" and demo.dotenv is None
    assert demo.settings.groww_access_token is None and not demo.settings.use_groww
    snap = demo.snapshot()
    assert snap["settings"]["demo"] is True
    assert not (settings.state_dir / "paper_broker.json").exists()  # live paper account untouched


def test_demo_routes_share_the_page_with_a_prefix(settings):
    import threading
    import urllib.request
    from trading_agent.ui import App, make_server

    settings.market = "in"
    srv = make_server(App(settings, dotenv=None), port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        page = urllib.request.urlopen(base + "/demo").read().decode()
        assert 'data-api="/demo"' in page and 'data-mode="demo"' in page
        import json
        st = json.loads(urllib.request.urlopen(base + "/demo/api/state").read())
        assert st["settings"]["demo"] is True
    finally:
        srv.shutdown()


def test_serve_demo_app_resets_only_the_isolated_demo(settings):
    """`serve(demo=True)` builds the normal App; the Demo tab is the isolated child."""
    settings.market = "in"
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    live_pb, live_state = settings.state_dir / "paper_broker.json", settings.state_dir / "state.json"
    live_pb.write_text("{}")
    live_state.write_text("{}")
    app = App(settings)  # what serve(demo=True) now builds
    assert app.demo_trades is None and app.demo is not app
    app.demo.reset()
    assert live_pb.read_text() == "{}" and live_state.read_text() == "{}"
    assert app.demo.dotenv is None


def test_demo_settings_ignore_notification_keys(settings):
    settings.market = "in"
    demo = App(settings).demo
    applied = demo.update_settings({"notify_webhook_url": "https://example.invalid/h", "notify_email_to": "a@b.c"})
    assert demo.settings.notify_webhook_url is None and demo.settings.notify_email_to is None
    assert "NOTIFY_WEBHOOK_URL" not in applied


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
