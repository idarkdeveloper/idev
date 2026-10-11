"""The Groww login counter (groww_logins.py): every token request sent is logged with its result, counted from 06:00 IST,
never with a secret, and the token code refuses to send the 146th of the day. Fakes only: a scripted session stands
in for Groww, a passed-in clock for the time."""
import json
from datetime import datetime, timedelta

import pytest
import requests

from trading_agent import groww_logins as gl
from trading_agent import runner
from trading_agent.groww import GrowwTokenUnavailable, TokenCache, cached_access_token
from trading_agent.timezones import IST

from .test_groww_token_block import Resp, TokenSession, no_network, too_many  # noqa: F401  (no_network is a fixture)

NOW = datetime(2026, 10, 12, 10, 0, tzinfo=IST)


def ok():
    return Resp(200, {"token": "SECRET-TOKEN-abc", "expiry": None})


def get(cache, sess, now=NOW, **kw):
    return cached_access_token("APIKEY", cache, secret="s3cret", now=now, session=sess, fresh=True, **kw)


def test_the_day_starts_at_0600_ist():
    assert gl.day_start(datetime(2026, 10, 12, 5, 59, tzinfo=IST)) == datetime(2026, 10, 11, 6, 0, tzinfo=IST)
    assert gl.day_start(datetime(2026, 10, 12, 6, 0, tzinfo=IST)) == datetime(2026, 10, 12, 6, 0, tzinfo=IST)
    utc = datetime(2026, 10, 12, 1, 0).replace(tzinfo=IST).astimezone(IST)
    assert gl.day_start(utc).hour == 6


def test_each_request_is_logged_with_its_result_and_host_and_no_secret(tmp_path):
    cache = TokenCache(tmp_path / "groww_token.json")
    get(cache, TokenSession(ok()), host="server")
    with pytest.raises(GrowwTokenUnavailable):
        get(cache, TokenSession(too_many()), now=NOW + timedelta(minutes=1))
    with pytest.raises(GrowwTokenUnavailable):
        get(cache, TokenSession(Resp(401, text="bad key")), now=NOW + timedelta(hours=7), force=True)
    with pytest.raises(GrowwTokenUnavailable):
        get(cache, TokenSession(requests.ConnectionError("down")), now=NOW + timedelta(hours=8), force=True)
    log = tmp_path / gl.LOG_FILE
    raw = log.read_text()
    rows = [json.loads(x) for x in raw.splitlines()]
    assert [r["result"] for r in rows] == ["ok", "refused-429", "error-401", "network error"]
    assert rows[0]["host"] == "server" and rows[1]["host"] == "laptop" and rows[0]["at"].endswith("+05:30")
    for secret in ("SECRET-TOKEN", "APIKEY", "s3cret", "bad key", "Too Many", "checksum"):
        assert secret not in raw
    assert set(rows[0]) == {"at", "host", "result"}


def test_a_cached_token_or_an_active_cool_down_sends_nothing_and_logs_nothing(tmp_path):
    cache = TokenCache(tmp_path / "groww_token.json")
    cached_access_token("APIKEY", cache, secret="s", now=NOW, session=TokenSession(ok()))
    cached_access_token("APIKEY", cache, secret="s", now=NOW + timedelta(minutes=5), session=TokenSession(ok()))   # cached
    assert gl.sent_today(tmp_path / gl.LOG_FILE, NOW) == 1
    with pytest.raises(GrowwTokenUnavailable):
        get(cache, TokenSession(too_many()), now=NOW + timedelta(minutes=10))
    sess = TokenSession(ok())
    with pytest.raises(GrowwTokenUnavailable):
        get(cache, sess, now=NOW + timedelta(minutes=11))                  # cooling down: not sent, not counted
    assert sess.calls == 0 and gl.sent_today(tmp_path / gl.LOG_FILE, NOW + timedelta(minutes=11)) == 2


def write(path, times, result="ok"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        for t in times:
            f.write(json.dumps({"at": t.isoformat(), "host": "laptop", "result": result}) + "\n")


def test_counting_resets_at_0600_and_the_summary_levels(tmp_path):
    log = tmp_path / gl.LOG_FILE
    write(log, [datetime(2026, 10, 12, 5, 59, tzinfo=IST)] * 3)               # yesterday's Groww day
    write(log, [datetime(2026, 10, 12, 6, 0, tzinfo=IST)] * 2)
    write(log, [datetime(2026, 10, 12, 9, 0, tzinfo=IST)], "refused-429")
    write(log, [datetime(2026, 10, 12, 9, 5, tzinfo=IST)], "network error")
    s = gl.summary(log, NOW)
    assert (s["sent"], s["ok"], s["refused"], s["errors"]) == (4, 2, 1, 1) and s["limit"] == 150 and s["level"] == "ok"
    assert s["since"].startswith("2026-10-12T06:00") and s["resets_at"].startswith("2026-10-13T06:00")
    assert len(s["last"]) == 7 and s["last"][0]["result"] == "network error"           # newest first
    assert gl.summary(log, datetime(2026, 10, 13, 6, 1, tzinfo=IST))["sent"] == 0       # the next Groww day
    assert [gl.level(n) for n in (99, 100, 139, 140, 144, 145)] == ["ok", "amber", "amber", "red", "red", "stopped"]
    log.write_text(log.read_text() + "garbage\n{\"at\": 5}\n")
    assert gl.summary(log, NOW)["sent"] == 4                                             # unreadable lines are skipped


def test_the_145th_request_of_the_day_is_the_last_one_sent_even_when_forced(tmp_path):
    cache = TokenCache(tmp_path / "groww_token.json")
    log = tmp_path / gl.LOG_FILE
    write(log, [NOW - timedelta(hours=1)] * 144)
    get(cache, TokenSession(ok()))                                                     # the 145th goes out
    sess = TokenSession(ok())
    with pytest.raises(GrowwTokenUnavailable) as e:
        get(cache, sess, now=NOW + timedelta(minutes=1), force=True)
    assert sess.calls == 0 and "145 Groww login token requests" in str(e.value) and e.value.until.hour == 6
    assert gl.sent_today(log, NOW + timedelta(minutes=1)) == 145
    tomorrow = datetime(2026, 10, 13, 6, 1, tzinfo=IST)
    assert get(cache, TokenSession(ok()), now=tomorrow) == "SECRET-TOKEN-abc"          # a new day, a new budget


def test_runner_names_the_host_from_the_role(settings):
    settings.ta_role = "server"
    assert runner.login_host(settings) == "server"
    settings.ta_role = ""
    assert runner.login_host(settings) == "laptop"


def test_the_log_is_trimmed_and_a_write_failure_never_raises(tmp_path):
    log = tmp_path / gl.LOG_FILE
    write(log, [NOW - timedelta(days=30)] * (2 * gl.KEEP_LINES + 1))
    gl.record(log, NOW, "laptop", "ok")
    assert len(log.read_text().splitlines()) == gl.KEEP_LINES
    gl.record(tmp_path / "missing-dir" / "sub" / "\0bad", NOW, "laptop", "ok")          # bad path: logged, not raised
