"""Groww refusing new login tokens (HTTP 429 and friends): remember it, stop asking, say so plainly.

Fakes only: a scripted session stands in for Groww, a passed-in clock for the time. Anything that
would reach the network fails the test.
"""

import json
import logging
from datetime import datetime, timedelta

import pytest
import requests

from trading_agent import cli, runner
from trading_agent.broker import LocalPaperBroker
from trading_agent.groww import (IST, GrowwTokenUnavailable, TokenCache, cached_access_token)
from trading_agent.ui import App
from trading_agent.watch import Watcher

NOW = datetime(2026, 10, 10, 10, 0, tzinfo=IST)


class Resp:
    def __init__(self, status=200, payload=None, text="", headers=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = text
        self.headers = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")


class TokenSession:
    """Answers token requests from a script: a Resp, or an exception to raise. The last one repeats."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls = 0

    def post(self, url, **kw):
        self.calls += 1
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, BaseException):
            raise item
        return item

    request = post


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a real network call was attempted")
    monkeypatch.setattr(requests.Session, "request", boom)
    monkeypatch.setattr(requests.Session, "post", boom)
    monkeypatch.setattr(requests.Session, "get", boom)
    import trading_agent.groww as g
    g._warned_blocks.clear()


def too_many(**kw):
    return Resp(429, text=kw.pop("text", "Too Many Requests"), **kw)


def get_token(cache, sess, now=NOW, **kw):
    return cached_access_token("APIKEY", cache, secret="s3cret", now=now, session=sess, **kw)


def block(cache):
    return json.loads(cache.block_path.read_text())


def until(cache):
    return datetime.fromisoformat(block(cache)["until"])


# --------------------------------------------------------------------------- #
# the cool-down itself
# --------------------------------------------------------------------------- #
def test_429_writes_block_and_next_call_makes_no_request(tmp_path):
    cache = TokenCache(tmp_path / "groww_token.json")
    sess = TokenSession(too_many())
    with pytest.raises(GrowwTokenUnavailable) as e:
        get_token(cache, sess)
    assert sess.calls == 1
    assert until(cache) == NOW + timedelta(minutes=15)
    msg = str(e.value)
    assert "Groww refused a new login token (429 Too Many Requests)" in msg
    assert "Next try after 10:15 IST" in msg and "keeps working without Groww data" in msg
    # inside the window: zero requests, same clear error
    with pytest.raises(GrowwTokenUnavailable) as e2:
        get_token(cache, sess, now=NOW + timedelta(minutes=5))
    assert sess.calls == 1 and "10:15 IST" in str(e2.value)
    # after the window it asks again
    sess.script = [Resp(200, {"token": "T1"})]
    assert get_token(cache, sess, now=NOW + timedelta(minutes=16)) == "T1"
    assert sess.calls == 2


def test_success_clears_block(tmp_path):
    cache = TokenCache(tmp_path / "groww_token.json")
    with pytest.raises(GrowwTokenUnavailable):
        get_token(cache, TokenSession(too_many()))
    assert cache.block_path.exists()
    sess = TokenSession(Resp(200, {"token": "T1"}))
    assert get_token(cache, sess, now=NOW + timedelta(minutes=20)) == "T1"
    assert not cache.block_path.exists()


def test_retry_after_seconds_and_http_date(tmp_path):
    cache = TokenCache(tmp_path / "a.json")
    with pytest.raises(GrowwTokenUnavailable):
        get_token(cache, TokenSession(too_many(headers={"Retry-After": "3600"})))
    assert until(cache) == NOW + timedelta(hours=1)
    # a short Retry-After never shortens the 15 minute minimum
    cache2 = TokenCache(tmp_path / "b.json")
    with pytest.raises(GrowwTokenUnavailable):
        get_token(cache2, TokenSession(too_many(headers={"Retry-After": "30"})))
    assert until(cache2) == NOW + timedelta(minutes=15)
    # HTTP-date: 10:00 IST is 04:30 GMT, so 05:30 GMT is an hour away
    cache3 = TokenCache(tmp_path / "c.json")
    with pytest.raises(GrowwTokenUnavailable):
        get_token(cache3, TokenSession(too_many(headers={"Retry-After": "Sat, 10 Oct 2026 05:30:00 GMT"})))
    assert until(cache3) == NOW + timedelta(hours=1)
    # junk is ignored
    cache4 = TokenCache(tmp_path / "d.json")
    with pytest.raises(GrowwTokenUnavailable):
        get_token(cache4, TokenSession(too_many(headers={"Retry-After": "soon"})))
    assert until(cache4) == NOW + timedelta(minutes=15)


def test_consecutive_429_doubles_and_caps_at_six_hours(tmp_path):
    cache = TokenCache(tmp_path / "t.json")
    sess = TokenSession(too_many())
    now = NOW
    waits = []
    for _ in range(7):
        with pytest.raises(GrowwTokenUnavailable):
            get_token(cache, sess, now=now)
        waits.append((until(cache) - now).total_seconds() / 60)
        now = until(cache) + timedelta(seconds=1)
    assert waits == [15, 30, 60, 120, 240, 360, 360]
    assert block(cache)["strikes"] == 7


def test_a_success_in_between_resets_the_doubling(tmp_path):
    cache = TokenCache(tmp_path / "t.json")
    with pytest.raises(GrowwTokenUnavailable):
        get_token(cache, TokenSession(too_many()))
    assert get_token(cache, TokenSession(Resp(200, {"token": "T"})), now=NOW + timedelta(minutes=20), fresh=True) == "T"
    later = NOW + timedelta(hours=1)
    with pytest.raises(GrowwTokenUnavailable):
        get_token(cache, TokenSession(too_many()), now=later, fresh=True)
    assert until(cache) == later + timedelta(minutes=15)


def test_daily_limit_text_waits_until_6am_ist(tmp_path):
    cache = TokenCache(tmp_path / "t.json")
    with pytest.raises(GrowwTokenUnavailable) as e:
        get_token(cache, TokenSession(too_many(text="Daily limit of token generation reached")))
    assert until(cache) == datetime(2026, 10, 11, 6, 0, tzinfo=IST)
    assert "06:00 IST on 11 Oct" in str(e.value)


def test_401_waits_30_minutes_and_says_to_fix_env(tmp_path):
    cache = TokenCache(tmp_path / "t.json")
    sess = TokenSession(Resp(401, text="bad"))
    with pytest.raises(GrowwTokenUnavailable) as e:
        get_token(cache, sess)
    assert until(cache) == NOW + timedelta(minutes=30)
    assert ".env" in str(e.value) and "retrying will not help" in str(e.value)
    with pytest.raises(GrowwTokenUnavailable):
        get_token(cache, sess, now=NOW + timedelta(minutes=10))
    assert sess.calls == 1
    # a different key is not held back by the old key's rejection
    sess.script = [Resp(200, {"token": "NEW"})]
    assert cached_access_token("OTHERKEY", cache, secret="x", now=NOW + timedelta(minutes=10),
                               session=sess) == "NEW"
    cache403 = TokenCache(tmp_path / "u.json")
    with pytest.raises(GrowwTokenUnavailable):
        get_token(cache403, TokenSession(Resp(403)))
    assert until(cache403) == NOW + timedelta(minutes=30)


def test_network_error_and_5xx_wait_two_minutes(tmp_path):
    for i, item in enumerate((requests.ConnectionError("down"), requests.Timeout("slow"), Resp(503))):
        cache = TokenCache(tmp_path / f"t{i}.json")
        sess = TokenSession(item)
        with pytest.raises(GrowwTokenUnavailable):
            get_token(cache, sess)
        assert until(cache) == NOW + timedelta(minutes=2)
        with pytest.raises(GrowwTokenUnavailable):
            get_token(cache, sess, now=NOW + timedelta(minutes=1))
        assert sess.calls == 1


def test_force_bypasses_block_and_fresh_alone_does_not(tmp_path):
    cache = TokenCache(tmp_path / "t.json")
    sess = TokenSession(too_many())
    with pytest.raises(GrowwTokenUnavailable):
        get_token(cache, sess)
    with pytest.raises(GrowwTokenUnavailable):
        get_token(cache, sess, now=NOW + timedelta(minutes=1), fresh=True)
    assert sess.calls == 1
    sess.script = [Resp(200, {"token": "T9"})]
    assert get_token(cache, sess, now=NOW + timedelta(minutes=1), fresh=True, force=True) == "T9"
    assert sess.calls == 2 and not cache.block_path.exists()


def test_a_valid_cached_token_is_used_even_while_blocked(tmp_path):
    cache = TokenCache(tmp_path / "t.json")
    assert get_token(cache, TokenSession(Resp(200, {"token": "T1"}))) == "T1"
    cache.write_block({"until": (NOW + timedelta(hours=1)).isoformat(), "status": 429, "reason": "x",
                       "at": NOW.isoformat()})
    assert get_token(cache, TokenSession(too_many()), now=NOW + timedelta(minutes=5)) == "T1"


def test_block_is_shared_between_users_of_the_same_state_dir(tmp_path):
    a, b = TokenCache(tmp_path / "groww_token.json"), TokenCache(tmp_path / "groww_token.json")
    sess_a, sess_b = TokenSession(too_many()), TokenSession(Resp(200, {"token": "NO"}))
    with pytest.raises(GrowwTokenUnavailable):
        get_token(a, sess_a)
    with pytest.raises(GrowwTokenUnavailable):
        get_token(b, sess_b, now=NOW + timedelta(minutes=1))
    assert sess_a.calls == 1 and sess_b.calls == 0


def test_block_file_holds_no_secrets(tmp_path):
    cache = TokenCache(tmp_path / "groww_token.json")
    with pytest.raises(GrowwTokenUnavailable):
        get_token(cache, TokenSession(too_many(text="reply mentioning APIKEY and s3cret and TOKENVALUE")))
    text = cache.block_path.read_text()
    for secret in ("APIKEY", "s3cret", "TOKENVALUE", "Bearer"):
        assert secret not in text
    assert set(json.loads(text)) == {"until", "status", "reason", "at", "strikes", "key"}
    assert not list(tmp_path.glob("*.tmp"))


def test_corrupt_block_file_is_ignored(tmp_path):
    cache = TokenCache(tmp_path / "t.json")
    cache.block_path.write_text("{not json")
    assert get_token(cache, TokenSession(Resp(200, {"token": "T"}))) == "T"


# --------------------------------------------------------------------------- #
# callers
# --------------------------------------------------------------------------- #
def _env(tmp_path, monkeypatch, **env):
    monkeypatch.chdir(tmp_path)
    for k in ("GROWW_ACCESS_TOKEN", "GROWW_API_KEY", "GROWW_API_SECRET", "GROWW_TOTP_SECRET", "GROWW_LIVE_ORDERS",
              "GROWW_GTT_STOPS", "AUTO_TRADE", "BROKER", "MARKET"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GROWW_API_KEY", "APIKEY")
    monkeypatch.setenv("GROWW_API_SECRET", "s3cret")
    for k, v in env.items():
        monkeypatch.setenv(k, v)


def test_cli_groww_token_prints_message_exit_2_and_no_traceback(tmp_path, monkeypatch, capsys):
    _env(tmp_path, monkeypatch)
    sess = TokenSession(too_many(), Resp(200, {"token": "SECRETTOKEN"}))
    monkeypatch.setattr(runner, "groww_session", lambda settings: sess)
    # groww-token builds its own settings; hand it the fake session through the cache layer
    monkeypatch.setattr(runner, "resolve_groww_token", _with_session(runner.resolve_groww_token, sess))
    assert cli.main(["groww-token"]) == 2
    cap = capsys.readouterr()
    assert "Groww refused a new login token (429 Too Many Requests)" in cap.err
    assert "Traceback" not in cap.err + cap.out and "SECRETTOKEN" not in cap.err + cap.out and cap.out == ""
    assert sess.calls == 1
    # again: still blocked, no request
    assert cli.main(["groww-token"]) == 2
    assert sess.calls == 1
    # --fresh alone does not bypass; --force does, with a warning
    assert cli.main(["groww-token", "--fresh"]) == 2 and sess.calls == 1
    capsys.readouterr()
    assert cli.main(["groww-token", "--fresh", "--force"]) == 0
    cap = capsys.readouterr()
    assert sess.calls == 2 and cap.out.strip() == "SECRETTOKEN"
    assert "may extend Groww's wait" in cap.err and "treat it like a password" in cap.err


def _with_session(real, sess):
    def wrapper(settings, *, fresh=False, session=None, force=False):
        return real(settings, fresh=fresh, session=sess, force=force)
    return wrapper


def test_cli_groww_check_and_other_commands_print_the_message(tmp_path, monkeypatch, capsys):
    _env(tmp_path, monkeypatch)
    TokenCache(tmp_path / "state" / "groww_token.json").write_block(
        {"until": (datetime.now(IST) + timedelta(hours=1)).isoformat(), "status": 429,
         "reason": "429 Too Many Requests", "at": datetime.now(IST).isoformat()})
    assert cli.main(["groww-check"]) == 2
    err = capsys.readouterr().err
    assert "Groww refused a new login token" in err and "Traceback" not in err
    assert cli.main(["holdings"]) == 2  # any command that needs a token ends the same way
    assert "Groww refused a new login token" in capsys.readouterr().err


def test_dashboard_my_portfolio_returns_the_message(settings, tmp_path):
    settings.groww_api_key, settings.groww_api_secret = "APIKEY", "s3cret"
    cache = runner.token_cache(settings)
    with pytest.raises(GrowwTokenUnavailable):
        cached_access_token("APIKEY", cache, secret="s3cret", now=datetime.now(IST), session=TokenSession(too_many()))
    app = App(settings, broker=LocalPaperBroker(tmp_path / "pb.json", starting_cash=1000, price_fn=lambda s: 1.0),
              dotenv=None)
    out = app.my_portfolio(refresh=True)
    assert out["linked"] is True and "Groww refused a new login token" in out["error"]
    assert app.groww_test()["ok"] is False


def test_watch_logs_once_per_block_period(settings, tmp_path, caplog):
    until_ = datetime.now(IST) + timedelta(hours=1)

    class Blocked:
        name = "groww"

        def positions(self):
            raise GrowwTokenUnavailable("Groww refused a new login token (429 Too Many Requests).", until_, 429)

    w = Watcher(settings, broker=Blocked(), awake=None)
    with caplog.at_level(logging.WARNING, logger="trading_agent"):
        for _ in range(4):
            assert w.check_trailing_stops() == []
    warnings = [r for r in caplog.records if "login token" in r.getMessage()]
    assert len(warnings) == 1
    # a new block period warns again
    Blocked.positions = lambda self: (_ for _ in ()).throw(
        GrowwTokenUnavailable("Groww refused a new login token (429 Too Many Requests).", until_ + timedelta(hours=2), 429))
    with caplog.at_level(logging.WARNING, logger="trading_agent"):
        w.check_trailing_stops()
    assert len([r for r in caplog.records if "login token" in r.getMessage()]) == 2


def test_make_broker_paper_mode_falls_back_to_free_prices_while_blocked(settings, tmp_path, caplog):
    settings.broker, settings.market = "groww", "in"
    settings.groww_api_key, settings.groww_api_secret = "APIKEY", "s3cret"
    cache = runner.token_cache(settings)
    with pytest.raises(GrowwTokenUnavailable):
        cached_access_token("APIKEY", cache, secret="s3cret", now=datetime.now(IST), session=TokenSession(too_many()))
    free = lambda sym: 123.0  # noqa: E731
    with caplog.at_level(logging.WARNING):
        broker = runner.make_broker(settings, free)
        assert isinstance(broker, LocalPaperBroker)
        assert broker.price_fn("TCS") == 123.0 and broker.price_fn("INFY") == 123.0
    assert len([r for r in caplog.records if "login token" in r.getMessage()]) == 1


def test_make_broker_live_mode_refuses_with_the_message(settings):
    settings.broker, settings.market, settings.groww_live_orders = "groww", "in", True
    settings.groww_api_key, settings.groww_api_secret = "APIKEY", "s3cret"
    cache = runner.token_cache(settings)
    with pytest.raises(GrowwTokenUnavailable):
        cached_access_token("APIKEY", cache, secret="s3cret", now=datetime.now(IST), session=TokenSession(too_many()))
    with pytest.raises(GrowwTokenUnavailable, match="Groww refused a new login token"):
        runner.make_broker(settings, lambda s: 1.0)


def test_nothing_else_requests_a_token_directly():
    """Every token request goes through cached_access_token (cache + cool-down)."""
    import pathlib
    import re
    root = pathlib.Path(runner.__file__).parent
    offenders = []
    for f in root.glob("*.py"):
        if f.name == "groww.py":
            continue
        if re.search(r"\b(request_access_token|get_access_token)\b", f.read_text(encoding="utf-8")):
            offenders.append(f.name)
    assert offenders == []
