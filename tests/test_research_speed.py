"""The research tools' speed-ups give exactly the old answers: archive range reads, momentum columns, the fast volatility,
the backtest, the shared history cache and the warm-up (synthetic data, no network)."""
import math
import random
import threading
import time
from datetime import date, datetime, timedelta

import pytest

from trading_agent import factor_backtest, history_cache, screen
from trading_agent.momentum import SeriesArrays, momentum_stats, momentum_stats_at
from trading_agent.price_archive import _RANGE_DAYS, PriceArchive
from trading_agent.timezones import IST


def make_bars(n, seed=1, start=date(2016, 1, 4), p0=100.0, vol=0.02):
    r = random.Random(seed)
    d, p, out = start, p0, []
    while len(out) < n:
        if d.weekday() < 5:
            p *= math.exp(r.gauss(0.0003, vol))
            out.append({"date": d.isoformat(), "close": round(p, 4), "adj_close": round(p * 0.98, 4), "volume": float(r.randint(10_000, 900_000))})
        d += timedelta(days=1)
    return out


# ---- the archive ----
NOW = datetime(2026, 10, 19, 20, 0, tzinfo=IST)


def old_merge(arch, symbol, fresh, range_="2y"):
    """The previous merge: every archived bar read, then the window cut."""
    by_date = {b["date"]: b for b in arch.bars(symbol)}
    for b in [b for b in (fresh or []) if b.get("close") is not None]:
        by_date.setdefault(b["date"], b)
    out = [by_date[d] for d in sorted(by_date)]
    days = _RANGE_DAYS.get(range_)
    if days and out:
        cutoff = (arch.now_fn().date() - timedelta(days=days)).isoformat()
        out = [b for b in out if b["date"] >= cutoff]
    return out


def old_plan(arch, c, symbol, fresh_by):
    from trading_agent.price_archive import find_split
    fresh_close = {d: float(b["close"]) for d, b in fresh_by.items()}
    have = {r["date"]: r for r in c.execute("SELECT * FROM bars WHERE symbol=?", (symbol,))}
    new_bars = [b for d, b in fresh_by.items() if d not in have and arch.is_closed(d, symbol)]
    hit = find_split({d: r["close"] for d, r in have.items()}, fresh_close) if have else None
    overlap = sorted(d for d in have if d in fresh_by)
    return new_bars, hit, overlap, arch._adj_changes(have, fresh_by)


@pytest.fixture
def arch(tmp_path):
    return PriceArchive(tmp_path / "a.sqlite", now_fn=lambda: NOW)


def test_bars_since_is_the_same_as_slicing_the_full_read(arch):
    bars = make_bars(900)
    arch.merge("AAA.NS", bars)
    full = arch.bars("AAA.NS")
    for cut in ("2015-01-01", bars[300]["date"], bars[-1]["date"], "2030-01-01"):
        assert arch.bars("AAA.NS", since=cut) == [b for b in full if b["date"] >= cut]
    assert arch.bars("NONE.NS", since="2020-01-01") == []


@pytest.mark.parametrize("range_", ["1y", "2y", "5y", "10y", "max"])
def test_merge_with_a_range_bound_equals_the_full_read_then_slice(arch, range_):
    bars = make_bars(2700)
    old_fresh = bars[-520:]                                     # Yahoo's fresh 2y on top of 10y of archive
    arch.merge("AAA.NS", bars[:-5])                              # an archive that holds the older years
    new = arch.merge("AAA.NS", old_fresh, range_)
    assert new == old_merge(arch, "AAA.NS", old_fresh, range_)
    assert new == arch.merge("AAA.NS", old_fresh, range_)        # a repeat changes nothing
    assert arch.merge("EMPTY.NS", [], range_) == old_merge(arch, "EMPTY.NS", [], range_) == []
    fresh_only = bars[-100:]
    assert arch.merge("NEW.NS", fresh_only, range_) == old_merge(arch, "NEW.NS", fresh_only, range_)


def test_plan_reads_only_the_overlap_and_gives_the_old_decisions_incl_a_split(arch):
    bars = make_bars(1500)
    arch.merge("S.NS", bars[:1200])
    scenarios = {
        "plain overlap": bars[900:1500],
        "only newer": bars[1300:1500],
        "split in fresh": [{**b, "close": b["close"] / 2, "adj_close": b["adj_close"] / 2} for b in bars[950:1500]],
        "adj rewrite": [{**b, "adj_close": b["adj_close"] * 0.99} for b in bars[900:1500]],
        "older than the archive": bars[:300],
    }
    for name, fresh in scenarios.items():
        fresh_by = {b["date"]: b for b in fresh}
        c = arch._conn()
        try:
            have, new_bars, hit, overlap, adj, _ = arch._plan(c, "S.NS", fresh_by)
            o_new, o_hit, o_overlap, o_adj = old_plan(arch, c, "S.NS", fresh_by)
        finally:
            c.close()
        assert (new_bars, hit, overlap, adj) == (o_new, o_hit, o_overlap, o_adj), name
        assert all(d >= min(fresh_by) for d in have), name                       # nothing older than the fresh series was read


def test_a_split_is_still_applied_exactly_once_and_the_series_is_rescaled(arch):
    bars = make_bars(800)
    arch.merge("SPL.NS", bars)
    half = [{**b, "close": b["close"] / 2, "adj_close": b["adj_close"] / 2, "volume": b["volume"] * 2} for b in bars[-500:]]
    out = arch.merge("SPL.NS", half, "max")
    assert len(arch.splits("SPL.NS")) == 1 and arch.splits("SPL.NS")[0]["factor"] == 2.0
    by = {b["date"]: b for b in out}
    assert out[-1]["close"] == half[-1]["close"] and by[bars[0]["date"]]["close"] == pytest.approx(bars[0]["close"] / 2)   # the older years follow the new share units
    arch.merge("SPL.NS", half, "max")
    assert len(arch.splits("SPL.NS")) == 1


# ---- momentum columns and volatility ----
def vol_exact(bars, n=60):
    """The original definition (statistics.pstdev sums in exact fractions), kept here to compare the fast one against it."""
    import statistics
    closes = [b["adj_close"] for b in bars[-(n + 1):]]
    if len(closes) < n + 1:
        return None
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1]]
    return statistics.pstdev(rets) * math.sqrt(252) if len(rets) > 1 else None



@pytest.mark.parametrize("n_bars", [1, 2, 50, 199, 200, 260, 280, 700])
def test_momentum_stats_at_equals_momentum_stats_on_the_slice(n_bars):
    bars = make_bars(800, seed=n_bars)
    arr = SeriesArrays(bars)
    for n in sorted({1, 2, 30, 59, 60, 61, 199, 200, 201, 252, 253, 273, 274, 400, n_bars, 800} & set(range(1, 801))):
        assert momentum_stats_at(arr, n) == momentum_stats(bars[:n]), n
    assert momentum_stats_at(arr, 0) == {"error": "no price history"}
    flat = [{"date": f"2020-01-{i % 28 + 1:02d}", "close": 10.0, "adj_close": 10.0, "volume": 0.0} for i in range(300)]
    assert momentum_stats_at(SeriesArrays(flat), 300) == momentum_stats(flat)


def test_fast_volatility_matches_the_exact_one():
    for seed in range(20):
        bars = make_bars(120, seed=seed, vol=0.005 + seed * 0.003)
        fast, exact = screen._vol(bars), vol_exact(bars)
        assert fast == pytest.approx(exact, rel=1e-12, abs=1e-15)
    assert screen._vol(make_bars(30)) is None and vol_exact(make_bars(30)) is None
    assert screen.vol_from_closes([1.0] * 61) == vol_exact([{"adj_close": 1.0}] * 61) == 0.0


# ---- the backtest ----
class Prices:
    def __init__(self, n_symbols=40, n_bars=1700):
        self.series = {f"S{i}": make_bars(n_bars, seed=i, p0=50 + i, vol=0.012 + (i % 7) * 0.004) for i in range(n_symbols)}
        self.series["NIFTYBEES"] = make_bars(n_bars, seed=999, vol=0.008)
        self.series["^NSEI"] = make_bars(n_bars, seed=998, vol=0.008)

    def history(self, sym, rng="2y"):
        if sym not in self.series:
            raise LookupError(sym)
        return self.series[sym]


def test_the_backtest_is_the_same_with_the_column_path_as_with_the_old_one(monkeypatch):
    prices = Prices()
    universe = [{"symbol": s, "name": s, "industry": "X"} for s in prices.series if s.startswith("S")]
    new = factor_backtest.run_factor_backtest(universe, prices, top=8, years=4, workers=4, min_turnover=0.0)

    class BarsArrays(SeriesArrays):
        __slots__ = ("bars",)

        def __init__(self, bars):
            super().__init__(bars)
            self.bars = bars
    monkeypatch.setattr(factor_backtest, "SeriesArrays", BarsArrays)
    monkeypatch.setattr(factor_backtest, "momentum_stats_at", lambda a, n: momentum_stats(a.bars[:n]))
    monkeypatch.setattr(factor_backtest, "vol_from_closes", lambda closes, n=60: vol_exact([{"adj_close": c} for c in closes], n))
    old = factor_backtest.run_factor_backtest(universe, prices, top=8, years=4, workers=1, min_turnover=0.0)
    for key in ("strategy", "benchmark", "equal_weight"):
        assert len(new[key]) == len(old[key]) and all(a == pytest.approx(b, abs=1e-9) for a, b in zip(new[key], old[key])), key
    assert new["picks"] == old["picks"] and new["stats"] == old["stats"]
    assert len(new["strategy"]) > 20


def test_progress_messages_say_how_far_the_history_load_is():
    prices = Prices(12, 700)
    universe = [{"symbol": s, "name": s, "industry": "X"} for s in prices.series if s.startswith("S")]
    seen = []
    screen.run_screen(universe, prices, workers=3, min_turnover=0.0, progress=seen.append)
    assert seen[0].startswith("Reading history ") and "of 12 stocks" in seen[-2] and seen[-2].startswith("Reading history 12 of 12") and seen[-1] == "Scoring the universe…"
    seen2 = []
    factor_backtest.run_factor_backtest(universe, Prices(12, 1700), top=3, years=2, workers=3, progress=seen2.append)
    assert any(m.startswith("Reading history 12 of 12") for m in seen2) and seen2[-1] == "Simulating the monthly rebalances…"


# ---- the shared cache ----
class Source:
    def __init__(self):
        self.calls, self.lock = [], threading.Lock()

    def history(self, sym, rng="2y"):
        with self.lock:
            self.calls.append((sym, rng))
        time.sleep(0.01)
        if sym == "BAD":
            raise LookupError("no data")
        return [{"date": "2026-01-01", "close": 1.0, "adj_close": 1.0, "volume": 1.0}]

    def latest_price(self, sym):
        return 42.0


def test_cache_ttl_hits_errors_and_attribute_passthrough():
    t = [0.0]
    src, c = Source(), history_cache.SharedHistory(Source(), ttl=600, clock=lambda: t[0])
    src = c._source
    a = c.history("tcs", "2y")
    assert c.history("TCS", "2y") is a and len(src.calls) == 1 and c.hits == 1
    c.history("TCS", "10y")
    assert len(src.calls) == 2                                                  # keyed by (symbol, range)
    t[0] = 601
    assert c.history("TCS", "2y") is not a and len(src.calls) == 3             # expired: read again
    with pytest.raises(LookupError):
        c.history("BAD")
    assert ("BAD", "2y") not in c._cache                                       # a failure is not cached
    assert c.latest_price("X") == 42.0                                          # everything else reaches the real source


def test_cache_is_bounded_and_drops_the_oldest():
    t = [0.0]
    c = history_cache.SharedHistory(Source(), ttl=10_000, max_entries=5, clock=lambda: t[0])
    for i in range(12):
        t[0] += 1
        c.history(f"S{i}")
    assert len(c) == 5 and set(k[0] for k in c._cache) == {f"S{i}" for i in range(7, 12)}
    assert len(c._locks) <= 5 + 1


def test_cache_loads_one_key_once_across_many_threads():
    src = Source()
    c = history_cache.SharedHistory(src, ttl=600)
    out = []
    ts = [threading.Thread(target=lambda: out.append(c.history("AAA"))) for _ in range(30)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(src.calls) == 1 and all(o is out[0] for o in out)
    keys = [threading.Thread(target=c.history, args=(f"K{i % 7}",)) for i in range(40)]
    [t.start() for t in keys]
    [t.join() for t in keys]
    assert len(src.calls) == 1 + 7


def test_the_replay_keeps_its_old_import():
    from trading_agent.replay.shared import SharedHistory
    assert SharedHistory is history_cache.SharedHistory


# ---- the warm-up and the App wiring ----
def test_warm_up_loads_the_universes_paced_and_stops_on_request():
    src = Source()
    c = history_cache.SharedHistory(src)
    w = history_cache.ResearchWarm(c, lambda name: [f"{name}{i}" for i in range(3)], universes=("U1", "U2"), five_year=("U1",), delay=0.0, pause=0.0)
    w.start()
    w._thread.join(5)
    assert sorted(src.calls) == sorted([(f"U1{i}", r) for i in range(3) for r in ("2y", "10y")] + [(f"U2{i}", "2y") for i in range(3)])
    slow = history_cache.ResearchWarm(c, lambda n: ["X"], delay=30.0)
    slow.start()
    slow.stop()
    slow._thread.join(2)
    assert not slow._thread.is_alive()
    bad = history_cache.ResearchWarm(c, lambda n: (_ for _ in ()).throw(RuntimeError("down")), delay=0.0, pause=0.0)
    bad.start()
    bad._thread.join(2)
    assert not bad._thread.is_alive() and bad.loaded == 0                       # a failing list ends the run quietly, loading nothing


def test_app_wraps_only_the_research_tools_and_the_warm_up_can_be_switched_off(settings, tmp_path, monkeypatch):
    from trading_agent.broker import LocalPaperBroker
    from trading_agent.ui import App
    prices = Source()
    app = App(settings, broker=LocalPaperBroker(tmp_path / "pb.json", starting_cash=1.0, price_fn=lambda s: 1.0), dotenv=None, prices=prices)
    assert app.research_prices is app.research_prices and isinstance(app.research_prices, history_cache.SharedHistory)
    assert app.prices is prices and app.research_prices._source is prices        # the dashboard's own reads stay unwrapped
    monkeypatch.setenv("RESEARCH_WARM", "false")
    assert app.start_research_warm(delay=0.0) is None
    monkeypatch.setenv("RESEARCH_WARM", "true")
    monkeypatch.setattr("trading_agent.screen.load_universe", lambda name: [{"symbol": "AAA"}])
    w = app.start_research_warm(delay=3600.0)
    assert w is not None and app.start_research_warm() is w                    # started once
    w.stop()
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "trading_agent"
    ui_src = (root / "ui.py").read_text(encoding="utf-8")
    assert ui_src.count("SharedHistory(") == 1                                   # created in one place, the research_prices property
    uses = [ln.strip() for ln in ui_src.splitlines() if "self.research_prices" in ln and "def " not in ln and "return" not in ln]
    wanted = ("run_screen(", "run_factor_backtest(", "run_signal_lab(", "prices = self.research_prices", "ScreenerService(", "ResearchWarm(")
    assert uses and all(any(x in ln for x in wanted) for ln in uses), uses       # only the research call sites
    for agent_file in ("runner.py", "watch.py", "agent.py", "cli.py", "app_feed.py", "live_prices.py", "broker.py", "stops.py"):
        text = (root / agent_file).read_text(encoding="utf-8")
        assert "SharedHistory" not in text and "history_cache" not in text and "research_prices" not in text, agent_file
