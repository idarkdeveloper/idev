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
