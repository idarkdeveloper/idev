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
