"""Fix round 2 for following several investors: .env encodings, setting types, webhook mentions, name matching."""
import pytest

from trading_agent.agent import AgentContext, RunResult, _rec_investor
from trading_agent.config import _load_dotenv
from trading_agent.notify import Notifier

from .test_followed import KEDIA, NAMES, _deal  # noqa: F401
from .test_followed_fix1 import _env_app
from .test_ui import _post, server  # noqa: F401  (fixture)


@pytest.mark.parametrize("enc", ["utf-8", "cp1252"])
def test_dotenv_loads_utf8_and_cp1252(tmp_path, monkeypatch, enc):
    monkeypatch.setenv("ZZ_TEST_NAME", "x")
    monkeypatch.delenv("ZZ_TEST_NAME")
    p = tmp_path / ".env"
    p.write_bytes("ZZ_TEST_NAME=André Café\n".encode(enc))
    _load_dotenv(p)
    import os
    assert os.environ["ZZ_TEST_NAME"] == "André Café"


def test_wrong_types_are_400_and_nothing_is_written(settings):
    app, env = _env_app(settings)
    before = env.read_bytes()
    bad = [{"watch_source": ["deals"]}, {"watch_source": 5}, {"watch_source": None}, {"watch_source": "nope"},
           {"notify_email_to": ["a@b.c"]}, {"notify_webhook_url": {"a": 1}}, {"notify_email_to": 7},
           {"watch_investors": {"a": "b"}}, {"watch_investors": [1, 2]}, {"watch_investor": {"a": 1}},
           {"watch_investor": 3}, {"paper_starting_cash": "nan"}, {"paper_starting_cash": "inf"},
           {"paper_starting_cash": [5]}, {"paper_starting_cash": True}, {"paper_starting_cash": -1},
           {"paper_starting_cash": {"a": 1}}, {"auto_trade": [True]}, {"market": ["in"]}]
    for changes in bad:
        with pytest.raises(ValueError):
            app.update_settings({"auto_trade": True, **changes} if "auto_trade" not in changes else changes)
        assert env.read_bytes() == before and settings.auto_trade is False and settings.investors == ["Nancy Pelosi"]


def test_wrong_types_over_http_are_400(server):
    base, app = server
    for body in ({"watch_source": ["x"]}, {"paper_starting_cash": "inf"}, {"watch_investors": {"a": 1}}):
        status, j = _post(base + "/api/settings", body)
        assert status == 400 and j["error"]


def test_good_values_and_clearing_still_work(settings):
    app, env = _env_app(settings)
    app.update_settings({"watch_source": "Congress", "notify_email_to": None, "paper_starting_cash": "1500.5",
                         "watch_investors": ["A", "B"]})
    assert settings.watch_source == "congress" and settings.notify_email_to is None
    assert settings.paper_starting_cash == 1500.5 and settings.investors == ["A", "B"]


def test_watch_source_follows_the_market_in_the_same_request(settings):
    app, env = _env_app(settings)
    with pytest.raises(ValueError, match="watch_source"):
        app.update_settings({"watch_source": "deals"})                  # US market: not a US source
    assert settings.watch_source == "congress"


def test_webhook_text_neutralises_mentions_and_keeps_lines():
    class S:
        def __init__(self):
            self.posts = []

        def post(self, url, json=None, **kw):
            self.posts.append(json)

            class R:
                def raise_for_status(self):
                    pass
            return R()

    s = S()
    n = Notifier(webhook_url="https://example.invalid/h", session=s)
    n.send("[DEAL] X: BUY Y", "line one\n@everyone <!channel> <@U1> @here\nconfidence <#C1>")
    text = s.posts[0]["text"]
    for bad in ("@everyone", "<!", "<@", "@here", "<#"):
        assert bad not in text
    assert text.count("\n") == 3


def test_a_short_name_does_not_match_every_followed_investor(settings):
    settings.investors = list(NAMES)
    ctx = AgentContext(settings=settings, broker=None, data=None, notifier=Notifier(), state=None,
                       result=RunResult("x", [KEDIA]))
    assert _rec_investor(ctx, "a", "TCS") == "VIJAY KEDIA"        # "a" matches nobody, so the trades decide
    assert _rec_investor(ctx, "a", "ZZZ") == ""
    assert _rec_investor(ctx, "kedia", "ZZZ") == "VIJAY KEDIA"
    assert _rec_investor(ctx, "Ashish", "ZZZ") == "ASHISH KACHOLIA"
