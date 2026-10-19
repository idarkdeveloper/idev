"""Groww live prices: the real client wiring in cli.py (patched, no network), the kill switch, and the start decision."""
import json
import logging
from datetime import datetime, timedelta

import trading_agent.cli as cli
import trading_agent.groww as groww_mod
import trading_agent.runner as runner
from trading_agent import app_feed, live_prices
from trading_agent.live_prices import LivePrices, feed_symbols
from trading_agent.timezones import IST

NOW = datetime(2026, 10, 19, 11, 0, tzinfo=IST)


class Hol:
    def is_trading_day(self, d):
        return True


def _feed(state, rows, now=NOW):
    d = app_feed.app_dir(state)
    d.mkdir(parents=True, exist_ok=True)
    (d / app_feed.FEED_FILE).write_text(json.dumps({"as_of": now.isoformat(), "source": "groww", "holdings": rows}), encoding="utf-8")


def test_real_client_is_built_read_only_and_only_ltp_strict_is_called(settings, monkeypatch):
    seen = {"token": [], "init": [], "calls": []}

    def token(s, **kw):
        seen["token"].append(kw)
        return "TOKEN"

    class FakeGroww:
        def __init__(self, tok, **kw):
            seen["init"].append((tok, kw))

        def ltp_strict(self, symbols):
            seen["calls"].append(("ltp_strict", list(symbols)))
            return {s: 10.0 for s in symbols}

        def __getattr__(self, name):   # any other method (orders, cash, ltp_many, ...) must never be touched
            raise AssertionError(f"unexpected Groww call: {name}")

    monkeypatch.setattr(runner, "resolve_groww_token", token)
    monkeypatch.setattr(groww_mod, "GrowwBroker", FakeGroww)
    _feed(settings.state_dir, [{"symbol": "TCS"}])
    lp = cli._live_prices(settings, Hol())
    assert lp.tick(NOW) is not None
    assert seen["token"] == [{}]                                  # defaults: no fresh=, force=, session or halt_ok
    (tok, kw), = seen["init"]
    assert tok == "TOKEN" and kw["live_orders"] is False and "price_fallback" not in kw
    assert seen["calls"] == [("ltp_strict", ["TCS"])]


def test_the_kill_switch_stops_every_call(settings):
    _feed(settings.state_dir, [{"symbol": "TCS"}])
    (settings.state_dir / "HALT").write_text("halted by test", encoding="utf-8")
    calls = []
    lp = LivePrices(settings, lambda syms: calls.append(syms) or {x: 1.0 for x in syms}, Hol())
    assert lp.tick(NOW) is None and calls == []
    (settings.state_dir / "HALT").unlink()
    assert lp.tick(NOW) is not None and len(calls) == 1


def test_start_decision_flag_off_needs_the_feed_and_logs(settings, monkeypatch, caplog):
    started = []
    monkeypatch.setattr(cli, "_live_prices", lambda s, h: type("L", (), {"start": lambda self: started.append(1)})())
    monkeypatch.setattr(type(settings), "use_groww", property(lambda s: True), raising=False)
    monkeypatch.setattr(type(settings), "has_groww_credentials", property(lambda s: True), raising=False)
    settings.market = "in"
    settings.live_prices, settings.app_feed = False, True
    assert cli.start_live_prices(settings, Hol()) is None and started == []          # flag off: nothing starts
    settings.live_prices, settings.app_feed = True, False
    caplog.set_level(logging.WARNING)
    assert cli.start_live_prices(settings, Hol()) is None and started == []          # needs APP_FEED
    assert "LIVE_PRICES needs APP_FEED=true" in caplog.text
    settings.app_feed = True
    assert cli.start_live_prices(settings, Hol()) is not None and started == [1]
    assert not (app_feed.app_dir(settings.state_dir) / live_prices.LIVE_FILE).exists()


def test_feed_age_limit_follows_the_feed_interval(settings):
    _feed(settings.state_dir, [{"symbol": "TCS"}], NOW - timedelta(minutes=8))        # 480 s old
    settings.app_feed_every_s = 300                                                    # limit max(300, 600) = 600 s
    assert LivePrices(settings, lambda syms: {s: 1.0 for s in syms}, Hol()).tick(NOW) is not None
    settings.app_feed_every_s = 60                                                     # limit 300 s: too old
    assert LivePrices(settings, lambda syms: {s: 1.0 for s in syms}, Hol()).tick(NOW) is None


def test_exchange_filter_uses_the_exchange_field_and_the_saved_list(settings):
    _feed(settings.state_dir, [{"symbol": "TCS"}, {"symbol": "ABB", "exchange": "BSE"}, {"symbol": "XYZ"}, {"symbol": "Q.BO"}])
    (settings.state_dir / "groww_holdings.json").write_text(json.dumps(
        {"holdings": [{"symbol": "XYZ", "exchange": "BSE"}, {"symbol": "TCS", "exchange": "NSE"}]}), encoding="utf-8")
    assert feed_symbols(settings.state_dir, NOW) == ["TCS"]


def test_partial_result_is_kept_when_a_later_batch_fails(settings):
    _feed(settings.state_dir, [{"symbol": f"S{i}"} for i in range(60)])
    n = []

    def ltp(syms):
        n.append(len(syms))
        if len(n) == 2:
            raise RuntimeError("second batch")
        return {s: 5.0 for s in syms}
    lp = LivePrices(settings, ltp, Hol())
    out = lp.tick(NOW)
    assert n == [50, 10] and len(out["prices"]) == 50 and lp._fails == 1 and lp._next > NOW + timedelta(seconds=lp.every - 1)


def test_backoff_log_reports_the_real_wait(settings, caplog):
    from trading_agent.groww import GrowwTokenUnavailable
    caplog.set_level(logging.WARNING)
    lp = LivePrices(settings, lambda s: {}, Hol())
    lp._back_off(NOW, GrowwTokenUnavailable("cooling", NOW + timedelta(hours=1), 429))
    assert "retrying in 3600 s" in caplog.text


def test_ltp_strict_skips_one_bad_value_and_missing_symbols(monkeypatch):
    g = groww_mod.GrowwBroker.__new__(groww_mod.GrowwBroker)
    g.exchange = "NSE"
    monkeypatch.setattr(g, "_req", lambda *a, **k: {"NSE_A": 1.5, "NSE_B": "x", "NSE_C": True, "NSE_D": float("nan"), "NSE_E": -2, "NSE_F": 3}, raising=False)
    assert g.ltp_strict(["a", "b", "c", "d", "e", "f", "missing"]) == {"A": 1.5, "F": 3.0}
