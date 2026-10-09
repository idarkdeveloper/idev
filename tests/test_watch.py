from datetime import datetime
from zoneinfo import ZoneInfo

from trading_agent.broker import LocalPaperBroker
from trading_agent.notify import Notifier
from trading_agent.state import State
from trading_agent.watch import Watcher

IST = ZoneInfo("Asia/Kolkata")


class FakeData:
    def __init__(self):
        self.calls = []

    def announcements(self, symbol, limit=5):
        self.calls.append(symbol)
        return [{"id": f"{symbol}-1", "symbol": symbol, "company": symbol, "at": "2026-10-09 10:00:00",
                 "category": "Results", "text": f"{symbol} results", "file": ""}]


def test_market_window(settings):
    w = Watcher(settings, every=60)
    assert w.market_window_open(datetime(2026, 10, 9, 10, 0, tzinfo=IST))      # Friday 10:00
    assert not w.market_window_open(datetime(2026, 10, 9, 7, 0, tzinfo=IST))   # too early
    assert not w.market_window_open(datetime(2026, 10, 10, 10, 0, tzinfo=IST))  # Saturday
    assert w.every == 60 and Watcher(settings, every=1).every == 15  # floor


def test_tick_runs_check_and_notifies_new_announcements(settings):
    broker = LocalPaperBroker(settings.state_dir / "pb.json", starting_cash=1000)
    broker.set_price("SENCO", 100); broker.submit_order("SENCO", "buy", qty=2)
    st = State(settings.state_dir / "state.json")
    st.record_recommendation({"action": "watch", "ticker": "ZAGGLE"}); st.save()
    data, notifier, ran = FakeData(), Notifier(), []

    class R:
        def to_dict(self):
            return {"ok": True}
    w = Watcher(settings, every=60, data=data, broker=broker, notifier=notifier,
                check_fn=lambda: ran.append(1) or R())
    info = w.tick(force=True)
    assert ran == [1] and info["check"] == {"ok": True}
    assert [a["symbol"] for a in info["new_announcements"]] == ["SENCO", "ZAGGLE"]
    assert notifier.sent and notifier.sent[0]["subject"].startswith("[NEWS] 2")
    # second tick: nothing new, nothing sent
    info2 = w.tick(force=True)
    assert info2["new_announcements"] == [] and len(notifier.sent) == 1
    assert w.status()["ticks"] == 2 and w.status()["on"] is False
    assert State(settings.state_dir / "state.json").data["seen_announcements"]


def test_tick_outside_window_skips(settings, monkeypatch):
    w = Watcher(settings, every=60, check_fn=lambda: (_ for _ in ()).throw(AssertionError("should not run")))
    monkeypatch.setattr(w, "market_window_open", lambda now=None: False)
    assert w.tick()["skipped"] is True
