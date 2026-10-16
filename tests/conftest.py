import json
from pathlib import Path

import pytest

from trading_agent.config import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        anthropic_api_key="test", claude_model="claude-opus-5-5",
        market="us", data_source="quiver", broker="local",
        quiver_api_key="qk",
        groww_access_token=None, groww_api_key=None, groww_api_secret=None,
        groww_totp_secret=None, groww_live_orders=False, groww_exchange="NSE",
        alpaca_key_id=None, alpaca_secret=None,
        alpaca_base_url="https://paper-api.alpaca.markets",
        watch_investor="Nancy Pelosi", watch_source="congress",
        paper_starting_cash=80_000, auto_trade=False,
        resend_api_key=None, notify_email_to=None, notify_email_from="x <a@b.c>",
        notify_webhook_url=None, state_dir=tmp_path / "state",
    )


@pytest.fixture(autouse=True)
def groww_window_open_by_default(monkeypatch):
    """The Groww gate (groww_hours) reads the clock; unless a test sets its own, it is Tuesday 11:00 IST, inside the
    window, so tests never depend on the time of day they run at."""
    from datetime import datetime

    import trading_agent.groww_hours as gh
    from trading_agent.timezones import IST
    monkeypatch.setattr(gh, "now_ist", lambda: datetime(2026, 6, 9, 11, 0, tzinfo=IST))


@pytest.fixture(autouse=True)
def collar_clock_mid_session(monkeypatch):
    """The opening collar (no buys before 09:17 IST) reads the clock; unless a test sets its own, it is Tuesday
    11:00 IST, so a test placing a buy never depends on the time of day it runs at."""
    from datetime import datetime

    import trading_agent.collar as collar
    from trading_agent.timezones import IST
    monkeypatch.setattr(collar, "_now", lambda: datetime(2026, 6, 9, 11, 0, tzinfo=IST))


@pytest.fixture(autouse=True)
def no_real_clock_check(monkeypatch):
    """The integration check's "Server clock" step runs timedatectl; a CI runner has it, a container may not, so the
    step count would depend on the machine. Unless a test passes its own runner, the clock is "not checked" (skipped)."""
    import trading_agent.clockcheck as cc
    monkeypatch.setattr(cc, "_run", lambda cmd: None)


@pytest.fixture
def sample_rows() -> list[dict]:
    fx = Path(__file__).resolve().parents[1] / "trading_agent" / "fixtures" / "congress_sample.json"
    return json.loads(fx.read_text())


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.content = payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()
        self.text = self.content.decode()

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class Seq:
    """Route answer that changes per call: answered in turn, the last one repeats."""

    def __init__(self, *answers):
        self.answers = list(answers)

    def next(self):
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


class FakeSession:
    """Records requests; answers from a {(method, url_substring): payload} table.

    A payload may be a ``Seq`` of answers, and any answer may be an exception
    instance, which is raised instead of returned.
    """

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        for (m, sub), payload in self.routes.items():
            if m == method and sub in url:
                if isinstance(payload, Seq):
                    payload = payload.next()
                if isinstance(payload, BaseException):
                    raise payload
                return FakeResponse(payload)
        return FakeResponse({"error": "no route"}, 404)

    def writes(self):
        """Calls that could change something at the broker (anything but GET)."""
        return [c for c in self.calls if c[0] != "GET"]

    def get(self, url, **kw):
        return self.request("GET", url, **kw)

    def post(self, url, **kw):
        return self.request("POST", url, **kw)


# -- no real secrets, no real .env, no real network ------------------------------------------------------------------
# On a machine with a real .env (the server, the laptop) a test that read it put the real Resend, Telegram and Groww
# keys into os.environ, where a later test's notifier picked them up: on 16 Oct a kill-switch test sent a real
# "[HALT] kill switch engaged" / "[HALT released]" pair. These three guards make that impossible.
SECRET_ENV = ("ANTHROPIC_API_KEY", "RESEND_API_KEY", "NOTIFY_EMAIL_TO", "NOTIFY_EMAIL_FROM", "NOTIFY_WEBHOOK_URL",
              "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_COMMANDS", "TELEGRAM_ALERTS", "HEARTBEAT_URL",
              "GROWW_ACCESS_TOKEN", "GROWW_API_KEY", "GROWW_API_SECRET", "GROWW_TOTP_SECRET", "GROWW_LIVE_ORDERS",
              "GROWW_GTT_STOPS", "AUTO_TRADE", "QUIVER_API_KEY", "ALPACA_KEY_ID", "ALPACA_SECRET", "STATE_DIR",
              "TA_ROLE")


@pytest.fixture(autouse=True)
def no_real_secrets(monkeypatch, tmp_path_factory):
    """Each test starts without any credential in the environment and ends with the environment it started with
    (``config._load_dotenv`` writes os.environ directly, which monkeypatch would not undo). A .env file is only read
    when it is one a test wrote under pytest's temporary folder: the repository's own .env never is."""
    import os

    import trading_agent.config as config
    saved = dict(os.environ)
    for k in SECRET_ENV:
        monkeypatch.delenv(k, raising=False)
    base = Path(tmp_path_factory.getbasetemp()).resolve()
    real_loader = config._load_dotenv

    def guarded(path):
        try:
            inside = Path(path).resolve().is_relative_to(base)
        except (OSError, ValueError):
            inside = False
        if inside:
            real_loader(path)
    monkeypatch.setattr(config, "_load_dotenv", guarded)
    yield
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    """Any connection to a non-loopback address fails at once: tests use fakes, and a local test server on 127.0.0.1
    still works. A real email, Telegram message, Groww or NSE call from a test is a bug this turns into an error."""
    import socket

    real_connect = socket.socket.connect

    def connect(self, address):
        host = address[0] if isinstance(address, tuple) else None
        if host is not None and host not in ("127.0.0.1", "::1", "localhost"):
            raise OSError(f"network access in tests is blocked (tried {host})")
        return real_connect(self, address)
    monkeypatch.setattr(socket.socket, "connect", connect)
    real_getaddrinfo = socket.getaddrinfo

    def getaddrinfo(host, *a, **k):
        if host not in (None, "127.0.0.1", "::1", "localhost", ""):
            raise OSError(f"network access in tests is blocked (tried {host})")
        return real_getaddrinfo(host, *a, **k)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
