"""Prices on the page follow the market: server re-pricing between polls, the market strip's 60 s display copy, and the page wiring."""
import os
import time
from pathlib import Path

import trading_agent.prices as prices_mod
import trading_agent.ui as ui_mod
from trading_agent.broker import LocalPaperBroker
from trading_agent.prices import YahooPrices
from trading_agent.regime import SYMBOLS, GlobalContext
from trading_agent.ui import App

UI = Path(__file__).resolve().parent.parent / "trading_agent" / "ui"


def _app(settings, tmp_path, context=None):
    return App(settings, broker=LocalPaperBroker(tmp_path / "pb.json", starting_cash=1000, price_fn=lambda s: 1.0), dotenv=None, context=context)


def test_portfolio_is_repriced_on_every_poll_after_the_60s_cache(settings, tmp_path, monkeypatch):
    app = _app(settings, tmp_path)
    reads = []

    def read(settings_, prices, stamp="", **kw):
        reads.append(1)
        px = 100.0 + len(reads)      # the market moves between reads
        return {"linked": True, "at": f"2026-10-19T11:0{len(reads)}:00+05:30", "invested": 1000.0, "value": 10 * px, "pl": 10 * px - 1000.0,
                "pl_pct": 0.0, "unpriced": [], "holdings": [{"symbol": "TCS", "qty": 10, "avg_price": 100.0, "price": px, "invested": 1000.0,
                                                            "value": 10 * px, "pl": 10 * px - 1000.0, "pl_pct": 0.0}]}
    monkeypatch.setattr(ui_mod, "read_groww_portfolio", read)
    app._bands_for = lambda syms: {}
    clock = [1_000_000.0]
    monkeypatch.setattr(ui_mod.time, "time", lambda: clock[0])
    first = app.my_portfolio()
    clock[0] += 30
    assert app.my_portfolio()["holdings"][0]["price"] == first["holdings"][0]["price"] and len(reads) == 1   # inside 60 s: no new Groww read
    clock[0] += 33                                                            # the page polls every 62 s: past the cache
    second = app.my_portfolio()
    assert len(reads) == 2 and second["holdings"][0]["price"] == 102.0 and second["value"] == 1020.0


class _Resp:
    status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return {"chart": {"result": [{"timestamp": [1759900000, 1760000000], "indicators": {"quote": [{"close": [99.0, 100.0], "volume": [1, 1]}]}}]}}


class _Sess:
    def __init__(self):
        self.calls = 0

    def get(self, *a, **k):
        self.calls += 1
        return _Resp()


def _age(d, secs):
    for f in Path(d).glob("*.json"):
        old = time.time() - secs
        os.utime(f, (old, old))


def test_market_strip_uses_a_60s_copy_while_the_nse_is_open_and_leaves_the_agents_context_alone(settings, tmp_path, monkeypatch):
    sess = _Sess()
    src = YahooPrices(suffix="", session=sess, cache_dir=tmp_path / "cache", cache_ttl=900)
    ctx = GlobalContext(src, ttl=900)
    monkeypatch.setattr(prices_mod, "price_ttl", lambda now, default, holidays=None: min(default, 60.0))   # the market is open
    app = _app(settings, tmp_path, context=ctx)
    view = app._regime_view()
    assert view is not ctx and view.source is not src and src.ttl_fn is None and ctx.ttl_fn is None
    view.fetch()
    n = sess.calls
    assert n >= len(SYMBOLS)                                    # one history request per market symbol
    view.fetch()
    assert sess.calls == n                                      # inside 60 s: served from the copy
    view._at -= 61
    _age(tmp_path / "cache", 120)
    view.fetch()
    assert sess.calls == n + len(SYMBOLS)                       # older than 60 s while open: one request per symbol (at most 7 a minute)
    assert app._regime_view() is view
    ctx.fetch()                                                 # the agent's own context still keeps its 15 minutes
    ctx._at -= 61
    before = sess.calls
    ctx.fetch()
    assert sess.calls == before


def test_a_context_without_a_yahoo_source_is_used_as_it_is(settings, tmp_path):
    class Fake:
        def fetch(self, force=False):
            return {}
    f = Fake()
    assert _app(settings, tmp_path, context=f)._regime_view() is f


def test_the_page_updates_prices_quietly_and_says_when_it_stopped():
    index = (UI / "index.html").read_text(encoding="utf-8")
    sp = (UI / "stockpage.js").read_text(encoding="utf-8")
    common = (UI / "common.js").read_text(encoding="utf-8")
    assert 'id="live-stamp"' in index and "updates paused" in index and "liveStat.fails >= 3" in index
    assert "}, 62000)" in index and "nseOpenNow()" in index and "document.hidden" in index
    assert "tkPx.update($(\"tk-symbol\").value, true, true)" in index and "spDrawer.refresh" in index and ".lk-price" in index
    assert "async function refresh()" in sp and "load, setExchange, refresh" in sp
    assert "update(text, now, quiet)" in common and "if(!quiet) show(" in common


def test_freshness_chip_moves_to_today_after_a_priced_read_while_the_nse_is_open(settings, tmp_path, monkeypatch):
    app = _app(settings, tmp_path)
    app._bands_for = lambda syms: {}
    raw = {"linked": True, "at": "2026-10-19T11:00:00+05:30", "invested": 1.0, "value": 1.0, "pl": 0.0, "pl_pct": 0.0, "unpriced": [],
           "holdings": [{"symbol": "TCS", "qty": 1, "avg_price": 1.0, "price": 1.0, "invested": 1.0, "value": 1.0, "pl": 0.0, "pl_pct": 0.0}]}
    monkeypatch.setattr(ui_mod, "read_groww_portfolio", lambda *a, **k: dict(raw))
    monkeypatch.setattr(ui_mod.safety, "market_open", lambda now, hol=None: False)
    app.my_portfolio(refresh=True)
    assert app.freshness()["prices"]["bar_at"] is None                      # market closed: still "last close"
    monkeypatch.setattr(ui_mod.safety, "market_open", lambda now, hol=None: True)
    app.my_portfolio(refresh=True)
    assert app.freshness()["prices"]["bar_at"] == app._now_dt().astimezone(ui_mod.IST if hasattr(ui_mod, "IST") else __import__("trading_agent.timezones", fromlist=["IST"]).IST).date().isoformat()


def test_data_health_indicators_are_polled_noted_and_flagged_when_paused():
    index = (UI / "index.html").read_text(encoding="utf-8")
    assert 'id="poll-stamp"' in index and 'pollNote("fresh")' in index and 'pollNote("integ")' in index and 'pollNote("health")' in index
    assert "updates paused: ${bad.map" in index and "visibilitychange" in index and "loadHealth(); }" in index
    assert "}, 60000);" in index and "loadHealth(); }, 120000)" in index
