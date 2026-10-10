"""Practice stop-loss: the stop-level maths, validation, the dashboard checker and watch-mode auto-exit."""
import json
import threading
from datetime import date, datetime

import pytest

from trading_agent.broker import LocalPaperBroker
from trading_agent.notify import Notifier
from trading_agent.risk import AT_ONCE, check_stops, normalize_stop, position_stop
from trading_agent.stops import PracticeStopChecker
from trading_agent.timezones import IST
from trading_agent.watch import Watcher

FRI_OPEN = datetime(2026, 10, 9, 10, 0, tzinfo=IST)   # a Friday, market hours


def flat_bars(n=40, high=101.0, low=99.0):
    return [{"date": f"d{i}", "close": 100.0, "adj_close": 100.0, "high": high, "low": low, "volume": 1} for i in range(n)]


class P:
    def __init__(self, **kw):
        self.symbol, self.qty, self.avg_entry_price, self.current_price = "X", 10, 100.0, 100.0
        self.high_water, self.stop_type, self.stop_value = None, None, None
        self.__dict__.update(kw)


def broker_with(tmp_path, price=100.0, qty=10, stop=None, name="pb.json"):
    prices = {"X": price}
    b = LocalPaperBroker(tmp_path / name, starting_cash=100_000, price_fn=lambda s: prices[s], currency="INR",
                         whole_shares=True)
    b.submit_order("X", "buy", qty=qty, stop=stop)
    b.prices = prices
    return b


# -- the stop-level maths ------------------------------------------------------
def test_trailing_uses_high_water_and_atr():
    bars = flat_bars()                       # ATR 2 -> 3x ATR = 6
    s = position_stop(P(high_water=120.0, current_price=118.0), bars)
    assert s["type"] == "trailing" and s["label"] == "trailing"
    assert s["level"] == pytest.approx(max(120 * 0.85, 120 - 6))   # the tighter one: 114
    assert position_stop(P(high_water=120.0, current_price=118.0), [])["level"] == pytest.approx(102.0)  # 15% alone
    assert position_stop(P(stop_type="trailing"), bars)["level"] == pytest.approx(94.0)  # high falls back to price


def test_fixed_percent_and_none_levels():
    assert position_stop(P(stop_type="fixed", stop_value=92.5))["level"] == 92.5
    s = position_stop(P(stop_type="percent", stop_value=8, high_water=150.0))
    assert s["level"] == pytest.approx(92.0) and s["label"] == "−8% from buy"   # from the buy price, not the high
    s = position_stop(P(stop_type="none"))
    assert s["level"] is None and s["label"] == "none"
    assert position_stop({"avg_entry_price": 100, "stop_type": "fixed", "stop_value": 90})["level"] == 90  # a dict works too


def test_validation_messages():
    assert normalize_stop("trailing") == {"type": "trailing", "value": None}
    assert normalize_stop("none") == {"type": "none", "value": None}
    assert normalize_stop("fixed", "95", price=100) == {"type": "fixed", "value": 95.0}
    assert normalize_stop("percent", 10) == {"type": "percent", "value": 10.0}
    with pytest.raises(ValueError, match="at or above today's price, so it would sell at once"):
        normalize_stop("fixed", 100, price=100)
    assert "sell at once" in AT_ONCE
    for bad in (0.4, 50.1, "abc", None):
        with pytest.raises(ValueError):
            normalize_stop("percent", bad)
    with pytest.raises(ValueError, match="between 0.5% and 50%"):
        normalize_stop("percent", 60)
    with pytest.raises(ValueError):
        normalize_stop("fixed", "x")
    with pytest.raises(ValueError):
        normalize_stop("weird")


def test_check_stops_honours_each_type():
    bars = lambda s: flat_bars()   # noqa: E731
    assert check_stops([P(current_price=80.0, stop_type="none")], bars) == []
    assert [h["type"] for h in check_stops([P(current_price=89.0, stop_type="fixed", stop_value=90)], bars)] == ["fixed"]
    assert check_stops([P(current_price=91.0, stop_type="fixed", stop_value=90)], bars) == []
    assert len(check_stops([P(current_price=80.0)], bars)) == 1     # no setting = trailing


# -- the stop is stored with the position -------------------------------------------
def test_order_with_stop_stores_it_and_it_survives_a_restart(tmp_path):
    b = broker_with(tmp_path, stop={"type": "fixed", "value": 90.0})
    pos = b.positions()[0]
    assert (pos.stop_type, pos.stop_value) == ("fixed", 90.0)
    again = LocalPaperBroker(tmp_path / "pb.json", price_fn=lambda s: 100.0, whole_shares=True)
    assert (again.positions()[0].stop_type, again.positions()[0].stop_value) == ("fixed", 90.0)
    b.submit_order("X", "buy", qty=1)                     # no stop given: the position keeps its stop
    assert b.positions()[0].stop_type == "fixed"
    b.submit_order("X", "buy", qty=1, stop={"type": "none", "value": None})
    assert b.positions()[0].stop_type == "none"
    b.set_stop("x", {"type": "percent", "value": 5.0})
    assert (b.positions()[0].stop_type, b.positions()[0].stop_value) == ("percent", 5.0)
    with pytest.raises(LookupError):
        b.set_stop("NOPE", {"type": "none", "value": None})


# -- the dashboard's checker -------------------------------------------------------------
def checker(tmp_path, b, holidays=None, **kw):
    return PracticeStopChecker(lambda: b, tmp_path / "state.json", bars_fn=lambda s: flat_bars(),
                               holidays=holidays, **kw)


def fills(tmp_path):
    return json.loads((tmp_path / "state.json").read_text()).get("practice_stop_fills", [])


def test_checker_sells_at_or_below_the_stop_and_records_it(tmp_path):
    b = broker_with(tmp_path, stop={"type": "fixed", "value": 90.0})
    c = checker(tmp_path, b)
    b.prices["X"] = 91.0
    assert c.check_once(FRI_OPEN) == [] and b.positions()
    b.prices["X"] = 90.0
    got = c.check_once(FRI_OPEN)
    assert len(got) == 1 and b.positions() == []
    f = fills(tmp_path)[0]
    assert (f["symbol"], f["qty"], f["price"], f["stop"], f["type"]) == ("X", 10, 90.0, 90.0, "fixed")
    order = b.orders()[-1]
    assert order["side"] == "sell" and order["stop_hit"] is True and order["fees"] >= 0
    assert c.check_once(FRI_OPEN) == []            # nothing left to sell


def test_checker_trailing_percent_and_none(tmp_path):
    b = broker_with(tmp_path)                       # no setting = trailing: 15% rule (flat bars: ATR 2 -> high-6)
    c = checker(tmp_path, b)
    b.prices["X"] = 95.0
    assert c.check_once(FRI_OPEN) == []
    b.prices["X"] = 93.0
    assert len(c.check_once(FRI_OPEN)) == 1
    b2 = broker_with(tmp_path, stop={"type": "none", "value": None}, name="b2.json")
    b2.prices["X"] = 1.0
    assert checker(tmp_path, b2).check_once(FRI_OPEN) == [] and b2.positions()
    b3 = broker_with(tmp_path, stop={"type": "percent", "value": 8.0}, name="b3.json")
    b3.prices["X"] = 93.0
    assert checker(tmp_path, b3).check_once(FRI_OPEN) == []
    b3.prices["X"] = 92.0
    assert len(checker(tmp_path, b3).check_once(FRI_OPEN)) == 1


def test_checker_only_in_market_hours_on_trading_days(tmp_path):
    class Hols:
        def is_trading_day(self, d):
            return d != date(2026, 10, 9)
    b = broker_with(tmp_path, stop={"type": "fixed", "value": 90.0})
    b.prices["X"] = 50.0
    c = checker(tmp_path, b)
    for when in (datetime(2026, 10, 9, 9, 14, tzinfo=IST), datetime(2026, 10, 9, 15, 31, tzinfo=IST),
                 datetime(2026, 10, 10, 11, 0, tzinfo=IST)):      # before open, after close, Saturday
        assert c.check_once(when) == [], when
    assert checker(tmp_path, b, holidays=Hols()).check_once(FRI_OPEN) == []   # an NSE holiday
    assert b.positions()
    assert len(checker(tmp_path, b).check_once(datetime(2026, 10, 9, 9, 15, tzinfo=IST))) == 1   # open sharp
    # the clock can be given as UTC: 04:45 UTC is 10:15 IST
    b = broker_with(tmp_path, stop={"type": "fixed", "value": 90.0}, name="c.json")
    b.prices["X"] = 50.0
    assert len(checker(tmp_path, b).check_once(datetime(2026, 10, 9, 4, 45, tzinfo=__import__("datetime").timezone.utc))) == 1


def test_checker_and_watch_never_sell_the_same_position_twice(tmp_path, settings):
    b = broker_with(tmp_path, stop={"type": "fixed", "value": 90.0})
    b.prices["X"] = 80.0
    start, results = threading.Barrier(5), []

    def go(fn):
        start.wait()
        results.append(fn())
    w = Watcher(settings, every=60, broker=b, notifier=Notifier(), prices=None, auto_exit=True)
    state_file = settings.state_dir / "state.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)

    def unrelated_write():   # another writer (e.g. a new seen deal) saving state.json during the race
        from trading_agent.state import STATE_LOCK, State
        with STATE_LOCK:
            st = State(state_file)
            st.data["unrelated_key"] = {"kept": True}
            st.save()
    ts = [threading.Thread(target=go, args=(f,)) for f in (
        lambda: checker(settings.state_dir, b).check_once(FRI_OPEN), lambda: checker(settings.state_dir, b).check_once(FRI_OPEN),
        lambda: checker(settings.state_dir, b).check_once(FRI_OPEN), w.check_trailing_stops, unrelated_write)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    sells = [o for o in b.orders() if o["side"] == "sell"]
    assert len(sells) == 1 and b.positions() == []
    saved = json.loads(state_file.read_text())
    assert len(saved["practice_stop_fills"]) == 1 and saved["unrelated_key"] == {"kept": True}
    # a stale caller (it saw 10 shares) is refused inside the lock
    b2 = broker_with(tmp_path, stop={"type": "fixed", "value": 90.0}, name="b2.json")
    b2.prices["X"] = 80.0
    b2.submit_order("X", "sell", qty=4)
    assert b2.sell_if_stopped("X", qty=10, level=90.0, stop={"type": "fixed", "value": 90.0}) is None
    assert b2.sell_if_stopped("X", qty=6, level=90.0, stop={"type": "fixed", "value": 95.0}) is None   # stop was edited
    b2.prices["X"] = 95.0
    assert b2.sell_if_stopped("X", qty=6, level=90.0, stop={"type": "fixed", "value": 90.0}) is None   # price came back
    assert b2.positions()[0].qty == 6


def test_two_brokers_on_one_file_sell_a_stopped_position_once(tmp_path):
    """A separate ``watch`` process opens its own LocalPaperBroker on the same file: only one of them sells."""
    prices = {"X": 100.0}
    mk = lambda: LocalPaperBroker(tmp_path / "shared.json", starting_cash=100_000, price_fn=lambda s: prices[s],
                                  currency="INR", whole_shares=True, shared=True)
    a = mk()
    a.submit_order("X", "buy", qty=10, stop={"type": "fixed", "value": 90.0})
    b = mk()                                   # loaded now: it still sees the 10 shares after `a` sells them
    prices["X"] = 80.0
    stop = {"type": "fixed", "value": 90.0}
    start, got = threading.Barrier(2), []

    def go(br):
        start.wait()
        got.append(br.sell_if_stopped("X", qty=10, level=90.0, stop=stop))
    ts = [threading.Thread(target=go, args=(br,)) for br in (a, b)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sum(o is not None for o in got) == 1
    on_disk = json.loads((tmp_path / "shared.json").read_text())
    assert on_disk["positions"] == {} and [o["side"] for o in on_disk["orders"]] == ["buy", "sell"]
    assert on_disk["cash"] == pytest.approx(100_000 - 10 * 100.0 + 10 * 80.0)
    # sequentially, with a stale object: the second broker re-reads the file and sells nothing
    c, d = mk(), mk()
    assert c.positions() == []
    prices["X"] = 100.0
    c.submit_order("X", "buy", qty=5, stop=stop)
    prices["X"] = 80.0
    assert d.sell_if_stopped("X", qty=5, level=90.0, stop=stop) is not None   # d sees c's buy, sells it
    assert c.sell_if_stopped("X", qty=5, level=90.0, stop=stop) is None       # c sees d's sale
    assert not (tmp_path / "shared.json.lock").exists()


def test_checker_does_nothing_when_the_market_is_not_india(tmp_path):
    b = broker_with(tmp_path, stop={"type": "fixed", "value": 90.0})
    b.prices["X"] = 50.0
    market = {"v": "us"}
    c = checker(tmp_path, b, enabled_fn=lambda: market["v"] == "in")
    assert c.check_once(FRI_OPEN) == [] and b.positions()
    market["v"] = "in"
    assert len(c.check_once(FRI_OPEN)) == 1


def test_checker_has_no_notifier_and_no_groww(tmp_path, monkeypatch):
    import trading_agent.runner as runner

    def boom(*a, **k):
        raise AssertionError("a notifier or a Groww client was built")
    monkeypatch.setattr(runner, "make_notifier", boom)
    monkeypatch.setattr(runner, "make_groww", boom)
    monkeypatch.setattr("trading_agent.watch.make_notifier", boom, raising=False)
    b = broker_with(tmp_path, stop={"type": "fixed", "value": 90.0})
    b.prices["X"] = 50.0
    assert len(checker(tmp_path, b).check_once(FRI_OPEN)) == 1
    import inspect
    import trading_agent.stops as stops
    src = inspect.getsource(stops)
    assert "make_notifier" not in src and "make_groww" not in src


def test_checker_thread_survives_errors_and_stops(tmp_path):
    calls = []

    def broker_fn():
        calls.append(1)
        raise RuntimeError("boom")
    c = PracticeStopChecker(broker_fn, tmp_path / "s.json", every=15, now_fn=lambda: FRI_OPEN)
    c.start()
    for _ in range(100):
        if calls:
            break
        threading.Event().wait(0.02)
    assert calls and c.running and "boom" in (c.last_error or "")
    c.stop()


# -- watch-mode auto-exit uses the same rule ----------------------------------------------
class Prices:
    def history(self, s, r):
        return flat_bars()


def test_watch_auto_exit_honours_none_and_fixed(tmp_path, settings):
    b = broker_with(tmp_path, stop={"type": "none", "value": None})
    b.prices["X"] = 10.0
    w = Watcher(settings, every=60, broker=b, notifier=Notifier(), prices=Prices(), auto_exit=True)
    assert w.check_trailing_stops() == [] and b.positions()          # "none": watch never sells it either
    b.set_stop("X", {"type": "fixed", "value": 20.0})
    hits = w.check_trailing_stops()
    assert len(hits) == 1 and hits[0]["type"] == "fixed" and hits[0]["order"]["stop_hit"] is True
    assert b.positions() == []
    assert json.loads((settings.state_dir / "state.json").read_text())["practice_stop_fills"][0]["type"] == "fixed"
    # fixed stop not reached: no sale
    b = broker_with(tmp_path, stop={"type": "fixed", "value": 90.0}, name="b2.json")
    b.prices["X"] = 95.0
    w = Watcher(settings, every=60, broker=b, notifier=Notifier(), prices=Prices(), auto_exit=True)
    assert w.check_trailing_stops() == [] and b.positions()


def _shared(tmp_path, price_fn, name="sh.json", **kw):
    return LocalPaperBroker(tmp_path / name, starting_cash=100_000, price_fn=price_fn, currency="INR",
                            whole_shares=True, shared=True, **kw)


def test_no_lock_is_held_while_a_price_is_fetched(tmp_path):
    holder = {}
    seen = []

    def price_fn(sym):
        b = holder["b"]
        seen.append((b._lock._is_owned(), b._depth, (tmp_path / "sh.json.lock").exists()))
        return 100.0 if len(seen) < 100 else 80.0
    b = holder["b"] = _shared(tmp_path, price_fn)
    b.submit_order("X", "buy", qty=10, stop={"type": "fixed", "value": 90.0})
    b.positions()
    b.account()
    n = len(seen)
    assert n >= 3
    state = {"price": 80.0}
    b.price_fn = lambda sym: (seen.append((b._lock._is_owned(), b._depth, (tmp_path / "sh.json.lock").exists())), state["price"])[1]
    assert b.sell_if_stopped("X", qty=10, level=90.0, stop={"type": "fixed", "value": 90.0}) is not None
    assert all(owned is False and depth == 0 and not lockfile for owned, depth, lockfile in seen)


def test_file_lock_release_does_not_delete_a_lock_taken_after_a_stale_break(tmp_path):
    from trading_agent.news import _file_lock
    lock = tmp_path / "x.lock"
    a = _file_lock(lock)
    a.__enter__()
    b = _file_lock(lock, stale=-1)       # everything counts as stale: B breaks A's lock and takes its own
    b.__enter__()
    a.__exit__(None, None, None)         # A releases late: B's lock must survive
    assert lock.exists()
    b.__exit__(None, None, None)
    assert not lock.exists()


def test_a_reset_by_one_broker_is_seen_by_the_other(tmp_path):
    a = _shared(tmp_path, lambda s: 100.0)
    a.submit_order("X", "buy", qty=10)
    b = _shared(tmp_path, lambda s: 100.0)
    assert b.positions()
    assert a.reset(100_000) is True
    assert b.positions() == []
    assert b.account().cash == 100_000
    o = b.submit_order("X", "buy", qty=1)
    assert o["status"] == "filled" and [p.qty for p in a.positions()] == [1]


def test_unshared_brokers_make_no_lock_file(tmp_path):
    b = LocalPaperBroker(tmp_path / "plain.json", starting_cash=1000, price_fn=lambda s: 10.0)
    b.submit_order("X", "buy", qty=1)
    b.positions()
    assert not list(tmp_path.glob("*.lock"))


def test_a_replay_step_makes_no_lock_file(tmp_path):
    from tests.test_replay_trial import make
    t = make(tmp_path)
    t.save()
    assert not list(tmp_path.rglob("*.lock"))
