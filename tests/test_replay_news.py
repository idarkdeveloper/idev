import pytest

from trading_agent.nse import NSEClient
from trading_agent.replay.clock import FutureDataError, ReplayClock
from trading_agent.replay.news import ClockedNews
from .conftest import FakeSession

ROWS = [{"symbol": "TATASTEEL", "sort_date": d, "desc": "Updates", "attchmntText": t, "seq_id": str(i)}
        for i, (d, t) in enumerate([("2021-01-10 10:00:00", "old"), ("2021-02-20 09:00:00", "in window"),
                                    ("2021-03-01 18:00:00", "on the day"), ("2021-03-02 08:00:00", "future")])]


def client(tmp_path, payload=ROWS):
    return NSEClient(session=FakeSession({("GET", "corporate-announcements"): payload}), cache_dir=tmp_path)


def test_history_is_cached_per_symbol(tmp_path):
    c = client(tmp_path)
    first = c.announcement_history("tatasteel")
    assert [a["text"] for a in first] == ["future", "on the day", "in window", "old"]
    calls = len(c.session.calls)
    assert c.announcement_history("TATASTEEL") == first and len(c.session.calls) == calls
    assert (tmp_path / "nse_ann" / "TATASTEEL.json").exists()


def test_news_is_sliced_at_the_clock(tmp_path):
    news = ClockedNews(client(tmp_path), ReplayClock("2021-03-01"))
    r = news.for_symbol("TATASTEEL", days=30)
    # the 18:00 announcement of the clock day is after the 15:00 IST cut-off: usable only from the next trading day
    assert [a["text"] for a in r["items"]] == ["in window"] and r["error"] is None
    assert [a["text"] for a in ClockedNews(news.client, ReplayClock("2021-03-02")).for_symbol("TATASTEEL", days=30)["items"]] \
        == ["future", "on the day", "in window"]
    with pytest.raises(FutureDataError):
        news.for_symbol("TATASTEEL", until="2021-03-02")


def test_blocked_news_is_reported_not_raised(tmp_path):
    news = ClockedNews(client(tmp_path, RuntimeError("HTTP 403")), ReplayClock("2021-03-01"))
    r = news.for_symbol("TATASTEEL")
    assert r["items"] == [] and r["error"] == "news unavailable for this period (HTTP 403)"
