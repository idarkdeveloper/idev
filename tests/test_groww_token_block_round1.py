"""Fix round 1 for the Groww token cool-down: concurrency, write robustness, never-500 dashboard, alerts-only watch."""

import json
import logging
import threading
import time as _t
from datetime import datetime, timedelta

import pytest
import requests

from trading_agent import runner
from trading_agent.groww import IST, GrowwTokenUnavailable, TokenCache, cached_access_token
from trading_agent.ui import App
from trading_agent.watch import Watcher

from .test_groww_token_block import (NOW, Resp, TokenSession, block, get_token, no_network,  # noqa: F401
                                     too_many, until)


def test_threads_with_an_expired_token_make_exactly_one_request(tmp_path):
    cache = TokenCache(tmp_path / "t.json")

    class Slow(TokenSession):
        def post(self, url, **kw):
            _t.sleep(0.2)
            return super().post(url, **kw)

    sess = Slow(Resp(200, {"token": "ONE"}))
    out = []
    threads = [threading.Thread(target=lambda: out.append(get_token(cache, sess))) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert out == ["ONE"] * 8 and sess.calls == 1


def test_other_process_waits_while_a_request_is_in_progress(tmp_path):
    import trading_agent.groww as g
    cache = TokenCache(tmp_path / "groww_token.json")
    other_sess = TokenSession(Resp(200, {"token": "NO"}))
    seen = {}

    class Probe(TokenSession):
        def post(self, url, **kw):
            real_lock = g._TOKEN_LOCK
            g._TOKEN_LOCK = threading.Lock()  # a second process does not share our lock
            try:
                try:
                    get_token(TokenCache(tmp_path / "groww_token.json"), other_sess)
                except GrowwTokenUnavailable as e:
                    seen["msg"] = str(e)
            finally:
                g._TOKEN_LOCK = real_lock
            return super().post(url, **kw)

    assert get_token(cache, Probe(Resp(200, {"token": "MINE"}))) == "MINE"
    assert other_sess.calls == 0 and "in progress" in seen["msg"]
    assert not cache.block_path.exists()  # success cleared the provisional block


def test_failure_overwrites_the_in_progress_block(tmp_path):
    cache = TokenCache(tmp_path / "t.json")
    with pytest.raises(GrowwTokenUnavailable):
        get_token(cache, TokenSession(too_many()))
    assert block(cache)["status"] == 429 and until(cache) == NOW + timedelta(minutes=15)


def test_unwritable_block_path_still_holds_in_process(tmp_path, monkeypatch):
    def nope(self, record):
        raise PermissionError("read-only")
    monkeypatch.setattr(TokenCache, "write_block", nope)
    cache = TokenCache(tmp_path / "t.json")
    sess = TokenSession(too_many())
    for i in range(3):
        with pytest.raises(GrowwTokenUnavailable):
            get_token(cache, sess, now=NOW + timedelta(minutes=i))
    assert sess.calls == 1
    assert not list(tmp_path.glob("*.tmp"))


def test_temp_files_have_unique_names(tmp_path, monkeypatch):
    import os
    import trading_agent.groww as g
    names = []
    real = os.replace
    monkeypatch.setattr(g.os, "replace", lambda a, b: (names.append(str(a)), real(a, b)))
    cache = TokenCache(tmp_path / "t.json")
    cache.put("K", "tok", NOW + timedelta(hours=1))
    cache.put("K", "tok", NOW + timedelta(hours=1))
    cache.write_block({"until": NOW.isoformat(), "status": 429})
    cache.write_block({"until": NOW.isoformat(), "status": 429})
    assert len(set(names)) == 4 and all(str(os.getpid()) in n for n in names)
    assert not list(tmp_path.glob("*.tmp"))


def test_get_access_token_is_gone():
    import trading_agent.groww as g
    assert not hasattr(g, "get_access_token")


def test_naive_until_is_read_as_ist(tmp_path):
    cache = TokenCache(tmp_path / "t.json")
    cache.block_path.write_text(json.dumps({"until": "2026-10-10T11:00:00", "status": 429, "reason": "x",
                                            "at": "2026-10-10T10:00:00"}))
    sess = TokenSession(Resp(200, {"token": "T"}))
    with pytest.raises(GrowwTokenUnavailable):
        get_token(cache, sess, now=NOW)
    assert get_token(cache, sess, now=NOW + timedelta(minutes=61)) == "T"


def test_network_error_after_429_keeps_the_strikes(tmp_path):
    cache = TokenCache(tmp_path / "t.json")
    now = NOW
    waits = []
    for item in (too_many(), requests.ConnectionError("x"), too_many()):
        with pytest.raises(GrowwTokenUnavailable):
            get_token(cache, TokenSession(item), now=now)
        waits.append((until(cache) - now).total_seconds() / 60)
        now = until(cache) + timedelta(seconds=1)
    assert waits == [15, 2, 30]


def test_cap_applies_to_the_doubled_wait_not_to_retry_after(tmp_path):
    cache = TokenCache(tmp_path / "t.json")
    ten_hours = {"Retry-After": "36000"}
    with pytest.raises(GrowwTokenUnavailable):
        get_token(cache, TokenSession(too_many(headers=ten_hours)))
    assert until(cache) == NOW + timedelta(hours=10)
    later = until(cache) + timedelta(seconds=1)
    with pytest.raises(GrowwTokenUnavailable):
        get_token(cache, TokenSession(too_many(headers=ten_hours)), now=later)
    assert until(cache) == later + timedelta(hours=10)


def _blocked_settings(settings, live=False):
    settings.broker, settings.market, settings.groww_live_orders = "groww", "in", live
    settings.groww_api_key, settings.groww_api_secret = "APIKEY", "s3cret"
    with pytest.raises(GrowwTokenUnavailable):
        cached_access_token("APIKEY", runner.token_cache(settings), secret="s3cret", now=datetime.now(IST),
                            session=TokenSession(too_many()))


def test_price_lookups_while_blocked_build_no_session(settings, monkeypatch):
    _blocked_settings(settings)
    made = []
    monkeypatch.setattr(runner, "groww_session", lambda st: made.append(1) or TokenSession(too_many()))
    broker = runner.make_practice_broker(settings, lambda sym: 7.0)
    after_build = len(made)
    for _ in range(5):
        assert broker.price_fn("TCS") == 7.0
    assert len(made) == after_build


def test_state_endpoint_does_not_fail_while_blocked_in_live_mode(settings):
    _blocked_settings(settings, live=True)
    app = App(settings, dotenv=None, demo_trades=[])
    snap = app.snapshot()
    assert "Groww refused a new login token" in snap["broker_error"]
    assert snap["settings"]["mode"] == "live" and snap["equity_history"] == []


def test_watch_degrades_to_alerts_only_then_recovers(settings, caplog):
    checks, built = [], []
    until_ = datetime.now(IST) + timedelta(hours=1)

    class Fake:
        name = "groww"
        live_orders = False

        def positions(self):
            return []

    def factory():
        if len(built) < 3:
            built.append(1)
            raise GrowwTokenUnavailable("Groww refused a new login token (429 Too Many Requests).", until_, 429)
        return Fake()

    w = Watcher(settings, broker=None, broker_factory=factory, check_fn=lambda: checks.append(1) or {},
                window=("00:00", "23:59"), weekdays_only=False, awake=None)
    with caplog.at_level(logging.WARNING, logger="trading_agent"):
        for _ in range(3):
            info = w.tick(force=True)
            assert "alerts_only" in info and info["stop_hits"] == []
    assert checks == [] and len([r for r in caplog.records if "login token" in r.getMessage()]) == 1
    info = w.tick(force=True)  # the block lifted: broker built, normal checks resume
    assert "alerts_only" not in info and checks == [1]
