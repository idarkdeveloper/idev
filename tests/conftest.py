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


class FakeSession:
    """Records requests; answers from a {(method, url_substring): payload} table."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        for (m, sub), payload in self.routes.items():
            if m == method and sub in url:
                return FakeResponse(payload)
        return FakeResponse({"error": "no route"}, 404)

    def get(self, url, **kw):
        return self.request("GET", url, **kw)

    def post(self, url, **kw):
        return self.request("POST", url, **kw)
