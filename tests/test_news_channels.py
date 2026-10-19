"""News alerts: Telegram only (a push, no email) by default when Telegram is set up; email as a fallback and coalesced."""
import logging

import pytest

from trading_agent.config import load_settings
from trading_agent.notify import Notifier
from trading_agent.news_story import basic_card
from trading_agent.watch import NEWS_EMAIL_WINDOW_S, Watcher


class Session:
    def __init__(self, tg_ok=True):
        self.posts, self.tg_ok = [], tg_ok

    def post(self, url, **kw):
        self.posts.append(url)
        outer = self

        class R:
            status_code = 200 if ("resend" in url or outer.tg_ok) else 500

            def raise_for_status(self):
                if self.status_code >= 400:
                    import requests
                    raise requests.HTTPError("boom", response=self)
        return R()


def notifier(session, telegram=True):
    return Notifier(resend_api_key="k", email_to="me@x.y", session=session, **({"telegram_token": "T", "telegram_chat_id": "1"} if telegram else {}))


def mails(s):
    return [u for u in s.posts if "resend" in u]


def telegrams(s):
    return [u for u in s.posts if "telegram" in u]


def card(symbol="HDFCBANK", title="Bank news"):
    return basic_card({"title": title, "source": "Src", "link": "https://x.test/a", "sentiment": "negative", "confidence": "high", "published": "2026-10-19T10:00:00+05:30"}, symbol)


def watcher(settings, n, mode, clock=None):
    settings.news_alert_channels = mode
    if mode in ("", "telegram", "both"):
        settings.telegram_alerts, settings.telegram_bot_token, settings.telegram_chat_id = True, "T", "1"
    w = Watcher(settings, every=60, notifier=n)
    if clock is not None:
        w._news_clock = clock
    return w


def test_mode_defaults_to_telegram_only_when_telegram_is_set_up_else_email(settings):
    settings.telegram_alerts, settings.telegram_bot_token, settings.telegram_chat_id = True, None, None
    settings.news_alert_channels = ""
    assert settings.news_alert_mode == "email"                                 # no Telegram: nothing silently disappears
    settings.news_alert_channels = "telegram"
    assert settings.news_alert_mode == "email"
    settings.telegram_bot_token, settings.telegram_chat_id = "T", "1"
    assert settings.news_alert_mode == "telegram"
    settings.news_alert_channels = ""
    assert settings.news_alert_mode == "telegram"                              # the default
    for want in ("both", "email"):
        settings.news_alert_channels = want
        assert settings.news_alert_mode == want


def test_config_validates_the_env_value(monkeypatch, tmp_path):
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.setenv("NEWS_ALERT_CHANNELS", "sms")
    with pytest.raises(SystemExit, match="NEWS_ALERT_CHANNELS must be both, telegram or email"):
        load_settings()
    monkeypatch.setenv("NEWS_ALERT_CHANNELS", " Telegram ")
    assert load_settings().news_alert_channels == "telegram"
    monkeypatch.delenv("NEWS_ALERT_CHANNELS")
    assert load_settings().news_alert_channels == ""


def test_telegram_mode_sends_no_email(settings):
    s = Session()
    w = watcher(settings, notifier(s), "telegram")
    w._send_news_card(card())
    assert len(telegrams(s)) >= 1 and mails(s) == []


def test_email_mode_sends_no_telegram_and_both_sends_each_once(settings):
    s = Session()
    watcher(settings, notifier(s), "email")._send_news_card(card())
    assert len(mails(s)) == 1 and telegrams(s) == []
    s2 = Session()
    watcher(settings, notifier(s2), "both")._send_news_card(card())
    assert len(mails(s2)) == 1 and len(telegrams(s2)) >= 1


def test_a_telegram_failure_falls_back_to_one_email_and_one_warning(settings, caplog):
    s = Session(tg_ok=False)
    w = watcher(settings, notifier(s), "telegram")
    caplog.set_level(logging.WARNING)
    w._send_news_card(card("A"))
    w._send_news_card(card("B"))
    assert len(mails(s)) == 2                                                  # each undelivered alert goes by email, once
    assert caplog.text.count("not delivered by Telegram") == 1


def test_email_is_one_per_stock_per_window_and_telegram_gets_every_item(settings):
    s = Session()
    now = [1000.0]
    w = watcher(settings, notifier(s), "both", clock=lambda: now[0])
    for i in range(3):
        w._send_news_card(card("HDFCBANK", f"item {i}"))
        now[0] += 60
    w._send_news_card(card("TCS"))
    assert len(mails(s)) == 2                                                  # HDFCBANK once, TCS once
    tg_before = len(telegrams(s))
    now[0] += NEWS_EMAIL_WINDOW_S
    w._send_news_card(card("HDFCBANK", "later"))
    assert len(mails(s)) == 3 and len(telegrams(s)) > tg_before
    assert len(telegrams(s)) >= 5                                              # Telegram was per item throughout


def test_only_limits_the_channels_and_other_alerts_are_unchanged(settings):
    s = Session()
    n = notifier(s)
    assert n.send("[STOP] X", "body") == ["console", "email", "telegram"]       # no `only`: every channel, as before
    assert n.send("n", "b", only=("telegram",)) == ["console", "telegram"]
    assert n.send("n", "b", only=("email",)) == ["console", "email"]
    import inspect
    from trading_agent import watch
    src = inspect.getsource(watch)
    assert src.count("only=(") == 3 and "_send_news_card" in src                # only the news path restricts channels


def test_the_digests_already_carry_the_news_headlines():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "trading_agent"
    assert 'data["news"] = _section(_news_today' in (root / "digest.py").read_text(encoding="utf-8")
    assert 'd["news"]' in (root / "digest_render.py").read_text(encoding="utf-8")
