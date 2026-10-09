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
