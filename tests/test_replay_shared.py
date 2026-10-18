import copy
import threading
import time

import pytest

from trading_agent.replay.clock import ClockedPrices, ReplayClock
from trading_agent.replay.shared import SharedHistory
from trading_agent.screen import run_screen
from .replay_fakes import FakeSource, market, path


class Counting(FakeSource):
    suffix = ".NS"

    def history(self, symbol, range_="2y"):
        time.sleep(0.02)
        return super().history(symbol, range_)


def test_loads_once_and_shares_the_list():
    src = Counting({"A": path(100, 0.001)})
    sh = SharedHistory(src)
    a, b = sh.history("a", "10y"), sh.history("A", "10y")
    assert a is b and len(src.calls) == 1
    sh.history("A", "2y")
    assert len(src.calls) == 2   # range is part of the key


def test_ttl_expiry_reloads():
    src = Counting({"A": path(100, 0.001)})
    sh = SharedHistory(src, ttl=0)
    sh.history("A", "10y"); sh.history("A", "10y")
    assert len(src.calls) == 2


def test_threads_load_a_symbol_once_and_symbols_in_parallel():
    src = Counting({"A": path(100, 0.001), "B": path(100, 0.001)})
    sh = SharedHistory(src)
    ts = [threading.Thread(target=sh.history, args=(s, "10y")) for s in "AAAAAAAABBBBBBBB"]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert sorted(src.calls) == [("history", "A"), ("history", "B")]


def test_errors_are_not_cached_and_propagate():
    class Flaky(FakeSource):
        n = 0

        def history(self, symbol, range_="2y"):
            self.n += 1
            if self.n == 1:
                raise RuntimeError("net down")
            return super().history(symbol, range_)

    sh = SharedHistory(Flaky({"A": path(100, 0.001)}))
    with pytest.raises(RuntimeError, match="net down"):
        sh.history("A", "10y")
    assert sh.history("A", "10y")
    with pytest.raises(LookupError):
        sh.history("ZZ", "10y")
    with pytest.raises(LookupError):
        sh.history("ZZ", "10y")


def test_other_attributes_pass_through():
    src = Counting({"A": path(100, 0.001)}, {"A": [{"date": "2021-02-01", "amount": 1}]})
    sh = SharedHistory(src)
    assert sh.suffix == ".NS" and sh.dividends("A") == src.dividends("A")
    with pytest.raises(AttributeError):
        sh.nope


def test_consumers_do_not_mutate_the_cached_lists():
    sh = SharedHistory(market())
    for s in ("A", "B", "C", "D", "E", "^NSEI"):
        sh.history(s, "10y")
    before = copy.deepcopy(sh._cache)
    for field in ("adj_close", "close"):
        p = ClockedPrices(sh, ReplayClock("2022-03-03"), field=field)
        for r in ("1y", "2y", "10y"):
            p.history("A", r)
        p.latest_price("A"); p.price_on("A", "2022-03-01"); p.calendar("A", "2022-01-01", "2022-03-01")
        run_screen([{"symbol": s, "name": s, "industry": ""} for s in "ABCDE"], p, min_turnover=0,
                   require_above_200dma=False)
    assert sh._cache == before


def test_warmup_loads_universe_and_benchmark_through_the_cache(monkeypatch):
    from trading_agent.replay import web
    from trading_agent.replay.web import ReplayApp

    class U:
        def __init__(self, name, state_dir):
            if name != "NIFTY50":
                raise OSError("no membership file")
            self.membership = type("M", (), {"ever_members": staticmethod(lambda start: ["B", "A", "GONE"])})()

    monkeypatch.setattr(web, "ReplayUniverse", U)
    monkeypatch.setattr(web, "BENCHMARKS", {"NIFTY50": "NIFTYBEES", "NIFTY100": "NIFTYBEES"})
    src = market()
    app = ReplayApp.__new__(ReplayApp)
    app.settings = type("S", (), {"state_dir": "x"})()
    app._stop = threading.Event()
    app.source = SharedHistory(src)
    app._warm(0)
    assert sorted(c[1] for c in src.calls) == ["A", "B", "GONE", "NIFTYBEES"]   # GONE: LookupError ignored
    app._stop.set()
    src.calls.clear()
    app._warm(0)
    assert src.calls == []


def test_injected_source_is_left_as_given_and_no_warm_thread(settings):
    from trading_agent.ui import App
    from trading_agent.replay.web import ReplayApp
    from .test_replay_web import FakeNews, FakeUniverse
    src = market()
    r = ReplayApp(App(settings, dotenv=None), source=src, universe_factory=lambda n: FakeUniverse(), news_client=FakeNews())
    assert r.source is src
    assert "replay-warm" not in [t.name for t in threading.enumerate()]
