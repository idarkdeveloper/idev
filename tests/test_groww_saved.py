"""Saved Groww holdings: remembered after every good read, used (priced from the free source) when Groww refuses."""
import json
import os
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import trading_agent.groww as groww_mod
import trading_agent.runner as runner
from trading_agent.broker import LocalPaperBroker, Position
from trading_agent.groww import IST, GrowwTokenUnavailable
from trading_agent.ui import App


class FakeGroww:
    def __init__(self, token, **kw):
        self.kw = kw

    def positions(self):
        return [Position("TCS", 10, 3000.0, 3300.0, sellable_qty=8),
                Position("INFY", 5, 1000.0, 900.0),
                Position("GOIBOND", 2, 1000.0, None)]


class NoYahoo:
    """Stands in for the BSE Yahoo source: no network in tests."""
    def __init__(self, *a, **k):
        pass

    def latest_price(self, symbol):
        raise LookupError("no BSE quote")


class FreePrices:
    def __init__(self, table):
        self.table = table

    def latest_price(self, symbol):
        return self.table[symbol]


@pytest.fixture
def linked(settings, monkeypatch):
    settings.groww_api_key, settings.groww_api_secret = "KEY-SECRET-VALUE", "s3cret-value"
    monkeypatch.setattr(groww_mod, "GrowwBroker", FakeGroww)
    monkeypatch.setattr(runner, "resolve_groww_token", lambda s, **k: "TOKEN-VALUE-XYZ")
    monkeypatch.setattr(runner, "YahooPrices", NoYahoo)
    return settings


def _app(settings, tmp_path, prices):
    return App(settings, broker=LocalPaperBroker(tmp_path / "pb.json", starting_cash=1000, price_fn=lambda s: 1.0),
               dotenv=None, prices=prices)


def _block(monkeypatch):
    until = datetime.now(IST) + timedelta(hours=2)

    def boom(s, **k):
        raise GrowwTokenUnavailable("Groww refused a new login token (429 Too Many Requests).", until, 429)
    monkeypatch.setattr(runner, "resolve_groww_token", boom)


def test_snapshot_written_on_success_without_secrets(linked, tmp_path):
    app = _app(linked, tmp_path, FreePrices({}))
    out = app.my_portfolio(refresh=True)
    assert out["linked"] and "source" not in out and len(out["holdings"]) == 3
    path = linked.state_dir / "groww_holdings.json"
    text = path.read_text(encoding="utf-8")
    snap = json.loads(text)
    assert datetime.fromisoformat(snap["saved_at"]).utcoffset() is not None
    assert [h["symbol"] for h in snap["holdings"]] == ["TCS", "INFY", "GOIBOND"] or {h["symbol"] for h in snap["holdings"]} == {"TCS", "INFY", "GOIBOND"}
    assert set(snap["holdings"][0]) == {"symbol", "name", "exchange", "kind", "maturity", "qty", "sellable_qty", "avg_price"}
    for secret in ("TOKEN-VALUE-XYZ", "KEY-SECRET-VALUE", "s3cret-value"):
        assert secret not in text
    if os.name != "nt":
        assert (path.stat().st_mode & 0o777) == 0o600
    assert not list(linked.state_dir.glob("*.tmp"))


def test_failure_with_snapshot_is_priced_from_free_prices(linked, tmp_path, monkeypatch):
    app = _app(linked, tmp_path, FreePrices({}))
    app.my_portfolio(refresh=True)
    _block(monkeypatch)
    app2 = _app(linked, tmp_path, FreePrices({"TCS": 3200.0, "INFY": 1100.0}))
    out = app2.my_portfolio(refresh=True)
    assert out["source"] == "saved" and out["prices"] == "yahoo (delayed)" and out["linked"] is True
    assert "Groww refused a new login token" in out["reason"] and out["blocked_until"]
    assert out["saved_at"] == json.loads((linked.state_dir / "groww_holdings.json").read_text())["saved_at"]
    by = {h["symbol"]: h for h in out["holdings"]}
    assert by["TCS"]["value"] == 32000.0 and by["TCS"]["pl"] == 2000.0 and by["TCS"]["sellable_qty"] == 8
    assert by["INFY"]["pl"] == 500.0
    assert by["GOIBOND"]["price"] is None and by["GOIBOND"]["value"] is None
    assert out["unpriced"] == ["GOIBOND"]
    assert out["invested"] == 30000 + 5000 + 2000
    assert out["value"] == 32000 + 5500 and out["pl"] == 2500.0
    assert out["pl_pct"] == pytest.approx(37500 / 35000 - 1)
    # the snapshot is not rewritten by a failed read
    assert json.loads((linked.state_dir / "groww_holdings.json").read_text())["holdings"][0]["qty"] == 10


def test_failure_without_snapshot_keeps_the_old_error(linked, tmp_path, monkeypatch):
    _block(monkeypatch)
    out = _app(linked, tmp_path, FreePrices({})).my_portfolio(refresh=True)
    assert out["linked"] is True and "Groww refused a new login token" in out["error"] and "holdings" not in out
    assert not (linked.state_dir / "groww_holdings.json").exists()


def test_any_error_and_not_linked_use_the_snapshot(linked, tmp_path, monkeypatch):
    _app(linked, tmp_path, FreePrices({})).my_portfolio(refresh=True)
    monkeypatch.setattr(runner, "resolve_groww_token", lambda s, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    out = _app(linked, tmp_path, FreePrices({"TCS": 1.0, "INFY": 1.0})).my_portfolio(refresh=True)
    assert out["source"] == "saved" and "boom" in out["reason"]
    linked.groww_api_key = linked.groww_api_secret = None
    out = _app(linked, tmp_path, FreePrices({"TCS": 1.0, "INFY": 1.0})).my_portfolio(refresh=True)
    assert out == {"linked": False}                                  # no credentials: a leftover snapshot is ignored


def test_empty_successful_read_is_saved_but_never_a_failed_one(linked, tmp_path, monkeypatch):
    monkeypatch.setattr(FakeGroww, "positions", lambda self: [])
    out = _app(linked, tmp_path, FreePrices({})).my_portfolio(refresh=True)
    assert out["holdings"] == [] and json.loads((linked.state_dir / "groww_holdings.json").read_text())["holdings"] == []
    monkeypatch.setattr(FakeGroww, "positions", lambda self: (_ for _ in ()).throw(RuntimeError("down")))
    before = (linked.state_dir / "groww_holdings.json").read_text()
    _app(linked, tmp_path, FreePrices({})).my_portfolio(refresh=True)
    assert (linked.state_dir / "groww_holdings.json").read_text() == before


def test_saved_holdings_are_never_copied_into_practice(linked, tmp_path, monkeypatch):
    _app(linked, tmp_path, FreePrices({})).my_portfolio(refresh=True)
    _block(monkeypatch)
    app = _app(linked, tmp_path, FreePrices({"TCS": 1.0, "INFY": 1.0}))
    with pytest.raises(ValueError, match="not copied"):
        app._copyable()


def test_page_shows_saved_subtitle_and_the_message_once():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    r = subprocess.run([node, str(Path(__file__).parent / "ui_mode_harness.js"), "live", "saved"],
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    sub = out["mp_sub"]
    assert sub.startswith("Saved holdings from 09 Oct") and "15:40 IST" in sub
    assert "Groww unavailable until 14:30 IST" in sub and "prices from Yahoo (delayed)" in sub
    assert "3 trading days old; buys or sells since then are missing" in sub and "from your CAS statement" in sub
    tiles = out["tiles"]
    assert "Holdings value (saved)" in tiles and "Profit / loss (saved)" in tiles and "Holdings (saved)" in tiles
    assert "429" not in tiles and "refused" not in tiles
    live = subprocess.run([node, str(Path(__file__).parent / "ui_mode_harness.js"), "live"],
                          capture_output=True, text=True, encoding="utf-8")
    assert "(saved)" not in json.loads(live.stdout)["tiles"] and json.loads(live.stdout)["mp_sub"].startswith("2 holdings")
