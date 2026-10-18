import http.client
import json
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trading_agent import app_api, app_feed
from trading_agent.app_auth import Authenticator, add_device

ROOT = Path(__file__).resolve().parent.parent


def _feed(age_s=17, closed=False, **extra):
    as_of = (datetime.now(timezone.utc) - timedelta(seconds=age_s)).astimezone().isoformat()
    return {"as_of": as_of, "age_s": 0, "stale": False, "market_closed": closed, "source": "groww", "cash": 10.0,
            "value": 1000.0, "holdings": [], "unpriced": [], "error": None, **extra}


class Api:
    def __init__(self, state, auth=None):
        self.state = state
        self.server = app_api.make_server(state, "127.0.0.1", 0, auth)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def get(self, path, secret=None, method="GET", headers=None):
        h = dict(headers or {})
        if secret is not None:
            h["Authorization"] = f"Bearer {secret}"
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read()), r.headers
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null"), e.headers

    def raw(self, authorization: bytes | None, path="/v1/health"):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.putrequest("GET", path)
        if authorization is not None:
            c.putheader("Authorization", authorization)
        c.endheaders()
        r = c.getresponse()
        status, body = r.status, r.read()
        c.close()
        return status, body


@pytest.fixture
def api(tmp_path):
    a = Api(tmp_path)
    a.secret = add_device(tmp_path, "phone")
    yield a
    a.server.shutdown()
    a.server.server_close()


def _write_feed(state, feed):
    (state / app_feed.FEED_FILE).write_text(json.dumps(feed), encoding="utf-8")


def test_health_and_portfolio_with_a_valid_secret(api):
    _write_feed(api.state, _feed(age_s=17))
    status, body, headers = api.get("/v1/health", api.secret)
    assert status == 200 and body["ok"] is True and 15 <= body["feed_age_s"] <= 30
    assert headers["Cache-Control"] == "no-store" and headers["Content-Type"] == "application/json"
    status, body, _ = api.get("/v1/portfolio", api.secret)
    assert status == 200 and body["value"] == 1000.0 and body["source"] == "groww"
    assert 15 <= body["age_s"] <= 30 and body["stale"] is False


def test_no_or_wrong_secret_is_401_with_no_detail(api):
    _write_feed(api.state, _feed())
    for path in ("/v1/portfolio", "/v1/nope"):   # an unknown path must not leak to an unauthenticated caller
        for secret in (None, "wrong"):
            status, body, _ = api.get(path, secret)
            assert (status, body) == (401, {"error": "unauthorised"})


def test_odd_authorization_headers_are_401_and_never_raise(api):
    # a fresh server per header: five failures from one address would lock it out (429), which is tested elsewhere
    for value in (b"", b"Bearer", b"Bearer ", b"Basic " + api.secret.encode(), b"Bearer " + b"x" * 30000,
                  "Bearer caf\xe9\xff".encode("latin-1"), b"Bearer " + api.secret.encode() + b" extra"):
        fresh = Api(api.state)
        status, body = fresh.raw(value)
        fresh.server.shutdown()
        fresh.server.server_close()
        assert status == 401 and json.loads(body) == {"error": "unauthorised"}, value[:20]
    assert api.raw(None)[0] == 401
    assert api.get("/v1/health", api.secret)[0] == 200   # still serving


def test_every_method_but_get_is_405_and_writes_nothing(api):
    before = sorted(p.name for p in api.state.iterdir())
    for method in ("POST", "PUT", "DELETE", "PATCH"):
        for secret in (api.secret, None):
            status, body, _ = api.get("/v1/portfolio", secret, method=method)
            assert (status, body) == (405, {"error": "method not allowed"})
    for method in ("HEAD", "OPTIONS"):
        c = http.client.HTTPConnection("127.0.0.1", api.port, timeout=5)
        c.request(method, "/v1/portfolio")
        assert c.getresponse().status == 405
        c.close()
    assert sorted(p.name for p in api.state.iterdir()) == before


def test_405_never_reads_the_body(api):
    # a declared body that is never sent would hang a handler that read it; the 405 comes back at once
    c = http.client.HTTPConnection("127.0.0.1", api.port, timeout=5)
    c.putrequest("POST", "/v1/portfolio")
    c.putheader("Content-Length", "1000000")
    c.endheaders()
    assert c.getresponse().status == 405
    c.close()


def test_unknown_path_is_404(api):
    assert api.get("/v1/orders", api.secret)[:2] == (404, {"error": "not found"})
    assert api.get("/", api.secret)[0] == 404


def test_no_feed_yet_is_503(api):
    assert api.get("/v1/portfolio", api.secret)[:2] == (503, {"error": "no data yet"})
    status, body, _ = api.get("/v1/health", api.secret)
    assert status == 200 and body == {"ok": True, "feed_age_s": None}
    for skeleton in ({"source": "none", "holdings": []}, {"as_of": "garbage"}, {"as_of": "2026-10-19T10:00:00"}):
        _write_feed(api.state, skeleton)
        assert api.get("/v1/portfolio", api.secret)[0] == 503


def test_stale_flag_and_age_s(api):
    _write_feed(api.state, _feed(age_s=900))
    body = api.get("/v1/portfolio", api.secret)[1]
    assert body["stale"] is True and body["age_s"] >= 900
    _write_feed(api.state, _feed(age_s=900, closed=True))
    assert api.get("/v1/portfolio", api.secret)[1]["stale"] is False   # closed market: old is normal
    _write_feed(api.state, _feed(age_s=500))
    assert api.get("/v1/portfolio", api.secret)[1]["stale"] is False


def test_history_ranges(api):
    rows = [{"date": (datetime(2026, 10, 19) - timedelta(days=d)).date().isoformat(), "value": float(d), "cash": None}
            for d in (400, 200, 60, 10, 0)]
    (api.state / app_feed.HISTORY_FILE).write_text(json.dumps(rows), encoding="utf-8")
    counts = {r: len(api.get(f"/v1/history?range={r}", api.secret)[1]["points"]) for r in ("1m", "3m", "1y", "all")}
    assert counts == {"1m": 2, "3m": 3, "1y": 4, "all": 5}
    pts = api.get("/v1/history?range=all", api.secret)[1]["points"]
    assert [p["date"] for p in pts] == sorted(p["date"] for p in pts)


def test_bad_range_falls_back_to_1m(api):
    rows = [{"date": d, "value": 1.0, "cash": None} for d in ("2026-05-01", "2026-10-18", "2026-10-19")]
    (api.state / app_feed.HISTORY_FILE).write_text(json.dumps(rows), encoding="utf-8")
    for q in ("", "?range=bogus", "?range=", "?range=1m&range=all"):
        assert len(api.get(f"/v1/history{q}", api.secret)[1]["points"]) == 2


def test_lockout_returns_429(tmp_path):
    a = Api(tmp_path, Authenticator(tmp_path))
    secret = add_device(tmp_path, "phone")
    try:
        codes = [a.get("/v1/health", "wrong")[0] for _ in range(5)]
        assert codes == [401] * 5
        assert a.get("/v1/health", "wrong")[0] == 429
        assert a.get("/v1/health", secret)[0] == 429   # the locked client stays out even with a good secret
    finally:
        a.server.shutdown()
        a.server.server_close()


def test_secret_is_never_logged(api, caplog):
    _write_feed(api.state, _feed())
    with caplog.at_level("DEBUG"):
        api.get("/v1/portfolio", api.secret)
        api.get("/v1/portfolio", "sekrit-wrong")
    text = caplog.text
    assert "GET /v1/portfolio 200 phone" in text and "401" in text
    assert api.secret not in text and "sekrit-wrong" not in text and "Bearer" not in text


def test_module_imports_no_groww_broker_config_runner_or_ui():
    code = ("import sys; sys.path.insert(0, sys.argv[1]); import trading_agent.app_api; "
            "bad = [m for m in ('trading_agent.groww', 'trading_agent.broker', 'trading_agent.config', "
            "'trading_agent.runner', 'trading_agent.ui') if m in sys.modules]; "
            "assert not bad, bad")
    r = subprocess.run([sys.executable, "-I", "-c", code, str(ROOT)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr


def test_runs_without_any_env_file(tmp_path):
    # a clean folder with no .env: the server starts, answers, and a clean import graph is all it needs
    code = ("import sys, json, threading, urllib.request, pathlib; sys.path.insert(0, sys.argv[1]); "
            "from trading_agent import app_api, app_auth; st = pathlib.Path(sys.argv[2]); "
            "s = app_auth.add_device(st, 'p'); srv = app_api.make_server(st, '127.0.0.1', 0); "
            "threading.Thread(target=srv.serve_forever, daemon=True).start(); "
            "rq = urllib.request.Request(f'http://127.0.0.1:{srv.server_address[1]}/v1/health', "
            "headers={'Authorization': 'Bearer ' + s}); "
            "print(json.loads(urllib.request.urlopen(rq).read())['ok'])")
    cwd = tmp_path / "empty"
    cwd.mkdir()
    r = subprocess.run([sys.executable, "-I", "-c", code, str(ROOT), str(tmp_path / "state")], cwd=cwd,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and r.stdout.strip() == "True", r.stderr
    assert not (cwd / ".env").exists()
