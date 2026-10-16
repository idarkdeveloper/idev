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


def test_stop_hits_notify_once_and_auto_exit(settings):
    broker = LocalPaperBroker(settings.state_dir / "pb.json", starting_cash=10_000)
    broker.set_price("X", 100); broker.submit_order("X", "buy", qty=10)
    broker.set_price("X", 120); broker.positions()   # high-water 120
    broker.set_price("X", 90)                        # below 15% / 3xATR stop
    bars = [{"date": f"d{i}", "close": 100.0, "adj_close": 100.0, "high": 101.0, "low": 99.0, "volume": 1} for i in range(40)]

    class Prices:
        def history(self, s, r):
            return bars
    notifier = Notifier()
    w = Watcher(settings, every=60, broker=broker, notifier=notifier, prices=Prices(), auto_exit=True)
    hits = w.check_trailing_stops()
    assert len(hits) == 1 and hits[0]["symbol"] == "X" and hits[0]["order"]["side"] == "sell"
    assert broker.positions() == [] and notifier.sent[0]["subject"].startswith("[STOP]")
    assert w.check_trailing_stops() == []  # position gone, nothing to alert


def test_india_tz_falls_back_without_tz_database(monkeypatch):
    from datetime import datetime, timedelta

    from zoneinfo import ZoneInfoNotFoundError

    from trading_agent import forward, groww, live, timezones, watch

    def missing(key):
        raise ZoneInfoNotFoundError(key)

    monkeypatch.setattr(timezones, "ZoneInfo", missing)
    tz = timezones.india_tz()
    assert tz.utcoffset(datetime(2026, 10, 9)) == timedelta(hours=5, minutes=30)
    # every module that needs India time uses the shared, fallback-safe IST
    assert watch.IST is live.IST is forward.IST is groww.IST is timezones.IST


class Filings:
    """Announcements per symbol, newest first; tests append to simulate a new filing."""

    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def announcements(self, symbol, limit=5):
        self.calls.append((symbol, limit))
        return [r for r in self.rows if r["symbol"] == symbol][:limit]


def _filing(sym, n, category="Dividend"):
    return {"id": f"{sym}-{n}", "symbol": sym, "company": sym, "at": f"2026-10-1{n} 18:00:00",
            "category": category, "text": f"{sym} filing {n}", "file": ""}


def test_real_groww_holdings_are_polled_with_a_silent_first_look(settings):
    import json
    settings.market = "in"
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    (settings.state_dir / "groww_holdings.json").write_text(json.dumps({"saved_at": "2026-10-15T16:05:00+05:30", "holdings": [
        {"symbol": "INFY", "exchange": "NSE", "kind": "equity", "qty": 10, "avg_price": 1500.0},
        {"symbol": "532123", "exchange": "BSE", "kind": "equity", "qty": 5, "avg_price": 50.0},
        {"symbol": "NIFTY GOLD FUND", "kind": "mutual_fund", "qty": 1, "avg_price": 10.0}]}), encoding="utf-8")
    data = Filings([_filing("INFY", 1, "Board Meeting"), _filing("INFY", 2, "Results")])
    notifier = Notifier()
    w = Watcher(settings, every=60, data=data, notifier=notifier)   # no broker: practice account empty / not read
    assert w.real_holdings() == ["INFY", "532123.BO"]
    assert w.poll_announcements() == [] and notifier.sent == []   # existing filings recorded, no flood of old ones
    assert {s for s, _ in data.calls} == {"INFY", "532123.BO"} and all(lim == 20 for _, lim in data.calls)
    data.rows.insert(0, _filing("INFY", 3, "Dividend"))           # the dividend filed afterwards
    fresh = w.poll_announcements()
    assert [a["id"] for a in fresh] == ["INFY-3"] and len(notifier.sent) == 1
    assert w.poll_announcements() == [] and len(notifier.sent) == 1
    assert State(settings.state_dir / "state.json").data["announcement_tickers"] == ["532123.BO", "INFY"]


def test_without_a_holdings_snapshot_nothing_changes(settings):
    data = Filings([_filing("ZAGGLE", 1)])
    st = State(settings.state_dir / "state.json")
    st.record_recommendation({"action": "watch", "ticker": "ZAGGLE"}); st.save()
    w = Watcher(settings, every=60, data=data, notifier=Notifier())
    assert w.real_holdings() == []
    assert [a["id"] for a in w.poll_announcements()] == ["ZAGGLE-1"]   # a recommended stock still alerts on first look
