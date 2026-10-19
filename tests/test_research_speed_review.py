"""Review follow-ups: the constant-group guard, the warm-up and its TTLs, and the archive's write path old vs new."""
import math
import random
import shutil

import pytest

from trading_agent import history_cache, screen
from trading_agent.price_archive import _LOCK, PriceArchive, find_split

from .test_research_speed import NOW, Prices, Source, make_bars


# ---- signal lab ----
def test_signal_lab_zscore_and_ranks_equal_the_old_definitions():
    import statistics
    from trading_agent import signal_lab
    r = random.Random(7)
    for _ in range(30):
        vals = [None if r.random() < 0.1 else r.gauss(0, r.choice([1e-3, 1, 50])) for _ in range(r.randint(2, 150))]
        xs = [v for v in vals if v is not None]
        mu, sd = statistics.fmean(xs), statistics.pstdev(xs) if len(xs) > 1 else 0.0
        old = [max(-3.0, min(3.0, (v - mu) / sd)) if v is not None and sd > 0 else 0.0 for v in vals] if len(xs) >= 2 else [0.0] * len(vals)
        assert signal_lab._zscore(vals) == pytest.approx(old, rel=1e-12, abs=1e-12)
        data = [round(r.gauss(0, 1), 1) for _ in range(60)]           # ties
        assert sorted(range(60), key=lambda i: data[i]) == sorted(range(60), key=data.__getitem__)


def test_a_constant_group_has_no_spread_like_the_old_code():
    from trading_agent import signal_lab
    assert signal_lab._zscore([0.9115343500130386] * 106) == [0.0] * 106          # the mean of identical floats is not exactly them
    assert signal_lab._zscore([3.0, None, 3.0, 3.0]) == [0.0] * 4
    assert signal_lab._zscore([1.0 + i * 1e-15 for i in range(50)]) == [0.0] * 50    # rounding noise only: no spread either
    assert screen._pstdev([2.5] * 30) == 0.0 and screen._pstdev([1.0, 2.0, 3.0]) == pytest.approx(math.sqrt(2 / 3))
    assert max(abs(z) for z in signal_lab._zscore([1.0, 2.0, 3.0, 4.0])) <= 3.0


def test_signal_lab_result_is_unchanged_by_the_fast_zscore(monkeypatch):
    import statistics
    from trading_agent import signal_lab
    prices = Prices(30, 1500)
    universe = [{"symbol": s, "name": s, "industry": "X"} for s in prices.series if s.startswith("S")]
    kw = {"horizons": [5, 20], "years": 2}
    new = signal_lab.run_signal_lab(universe, prices, **kw)
    monkeypatch.setattr(signal_lab, "_pstdev", lambda xs: statistics.pstdev(xs))
    old = signal_lab.run_signal_lab(universe, prices, **kw)

    def same(a, b, path=""):
        if isinstance(a, dict):
            assert a.keys() == b.keys(), path
            for k in a:
                same(a[k], b[k], f"{path}/{k}")
        elif isinstance(a, list):
            assert len(a) == len(b), path
            for i, (x, y) in enumerate(zip(a, b)):
                same(x, y, f"{path}[{i}]")
        elif isinstance(a, float) and isinstance(b, float):
            assert a == pytest.approx(b, rel=1e-9, abs=1e-9), path
        else:
            assert a == b, path
    same(new, old)


# ---- warm-up: never from Yahoo while the NSE is open; entries outlive the run ----
def test_warm_up_does_not_fetch_while_the_nse_is_open_and_warms_when_closed(caplog):
    import logging
    caplog.set_level(logging.INFO)
    src = Source()
    open_now = [True]
    w = history_cache.ResearchWarm(history_cache.SharedHistory(src), lambda n: ["A", "B"], universes=("U",), delay=0.0, pause=0.0, is_open=lambda: open_now[0])
    w.start()
    w._thread.join(5)
    assert src.calls == [] and w.loaded == 0 and caplog.text.count("research warm-up skipped: the NSE is open") == 1
    open_now[0] = False
    w2 = history_cache.ResearchWarm(history_cache.SharedHistory(src), lambda n: ["A", "B"], universes=("U",), delay=0.0, pause=0.0, is_open=lambda: open_now[0])
    w2.start()
    w2._thread.join(5)
    assert sorted(src.calls) == [("A", "2y"), ("B", "2y")] and w2.loaded == 2
    src3, flips = Source(), []

    def is_open():
        flips.append(1)
        return len(flips) > 2                                                      # closed for the start check and the first symbol
    w3 = history_cache.ResearchWarm(history_cache.SharedHistory(src3), lambda n: ["A", "B", "C"], universes=("U",), delay=0.0, pause=0.0, is_open=is_open)
    w3.start()
    w3._thread.join(5)
    assert len(src3.calls) == 1 and "stopped: the NSE opened" in caplog.text        # the market opens mid-run: no more fetches
    broken = history_cache.ResearchWarm(history_cache.SharedHistory(Source()), lambda n: ["A"], universes=("U",), delay=0.0, pause=0.0, is_open=lambda: 1 / 0)
    broken.start()
    broken._thread.join(5)
    assert broken.loaded == 0                                                      # an unreadable calendar counts as open
    assert history_cache.ResearchWarm(history_cache.SharedHistory(Source()), lambda n: []).pause == 0.5


def test_a_warmed_entry_outlives_the_run_outside_market_hours_and_not_during_them():
    t, is_open = [0.0], [False]
    c = history_cache.SharedHistory(Source(), ttl=600, clock=lambda: t[0], market_open=lambda: is_open[0])
    c.warm("AAA", "2y", history_cache.WARM_TTL)
    first = c.history("AAA", "2y")
    t[0] = 15 * 60
    assert c.history("AAA", "2y") is first and len(c._source.calls) == 1             # closed: still served at 15 min
    t[0] = 29 * 60
    assert c.history("AAA", "2y") is first
    t[0] = 31 * 60
    assert c.history("AAA", "2y") is not first and len(c._source.calls) == 2         # expired after 30 min
    c.warm("BBB", "2y", history_cache.WARM_TTL)
    t[0] += 11 * 60
    is_open[0] = True                                                               # the market opens: the normal 10 minutes apply
    c.history("BBB", "2y")
    assert len(c._source.calls) == 4                                               # warmed for 30 min, but 11 min old while open: read again
    c2 = history_cache.SharedHistory(Source(), ttl=600, max_entries=2, clock=lambda: t[0], market_open=lambda: False)
    for sym in ("X", "Y", "Z"):
        t[0] += 1
        c2.warm(sym, "2y", 1800)
    assert len(c2) == 2 and ("X", "2y") not in c2._cache                            # the size bound still evicts the oldest
    t[0] += 700
    assert c2.history("Y") is c2.history("Y") and len(c2._source.calls) == 3          # Y was warmed for 30 min: still fresh at 11 min


# ---- the archive's write path: the old _update and the new one leave identical tables ----
def old_update(arch, symbol, fresh):
    """The previous PriceArchive._update, verbatim apart from reading all rows in its plan."""
    def plan(c):
        fresh_by_ = {b["date"]: b for b in fresh}
        fresh_close = {d: float(b["close"]) for d, b in fresh_by_.items()}
        have = {r["date"]: r for r in c.execute("SELECT * FROM bars WHERE symbol=?", (symbol,))}
        new_bars = [b for d, b in fresh_by_.items() if d not in have and arch.is_closed(d, symbol)]
        hit = find_split({d: r["close"] for d, r in have.items()}, fresh_close) if have else None
        overlap = sorted(d for d in have if d in fresh_by_)
        return have, new_bars, hit, overlap, arch._adj_changes(have, fresh_by_), fresh_close
    now = arch.now_fn().isoformat(timespec="seconds")
    fresh_by = {b["date"]: b for b in fresh}
    c = arch._conn()
    try:
        have, new_bars, hit, overlap, adj_changes, fresh_close = plan(c)
        if not hit:
            for d in overlap:
                if abs(have[d]["close"] / fresh_close[d] - 1) > 0.005:
                    arch.conflicts += 1
                    break
        if not (new_bars or hit or adj_changes):
            return
        with _LOCK:
            c.execute("BEGIN IMMEDIATE")
            try:
                have, new_bars, hit, overlap, adj_changes, fresh_close = plan(c)
                if not (new_bars or hit or adj_changes):
                    c.execute("COMMIT")
                    return
                arch.writes += 1
                if hit:
                    f, boundary = hit
                    where, args = ("AND date<?", (boundary,)) if boundary else ("", ())
                    c.execute(f"UPDATE bars SET close=close/?, adj_close=adj_close/?, volume=volume*? WHERE symbol=? {where}", (f, f, f, symbol, *args))
                    c.execute("INSERT INTO splits VALUES (?,?,?,?,?)", (symbol, now[:10], f, len(overlap), boundary))
                    have = {r["date"]: r for r in c.execute("SELECT * FROM bars WHERE symbol=?", (symbol,))}
                    adj_changes = arch._adj_changes(have, fresh_by)
                ov = sorted(d for d in have if d in fresh_by)
                if ov:
                    d0 = ov[0]
                    a0, f0 = have[d0]["adj_close"], float(fresh_by[d0].get("adj_close") or 0)
                    if a0 > 0 and f0 > 0 and 0.2 < f0 / a0 < 5 and abs(f0 / a0 - 1) > 1e-9:
                        c.execute("UPDATE bars SET adj_close=adj_close*? WHERE symbol=? AND date<?", (f0 / a0, symbol, d0))
                c.executemany("UPDATE bars SET adj_close=? WHERE symbol=? AND date=?", [(v, symbol, d) for v, d in adj_changes])
                for b in new_bars:
                    close = float(b["close"])
                    vol = float(b.get("volume") or 0)
                    c.execute("INSERT OR IGNORE INTO bars VALUES (?,?,?,?,?,?,?,?)", (symbol, b["date"], close, float(b.get("adj_close") or close), vol, close, vol, now))
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise
    finally:
        c.close()


def table_dump(arch):
    c = arch._conn()
    try:
        bars = [tuple(r) for r in c.execute("SELECT symbol, date, close, adj_close, volume, close_raw, volume_raw FROM bars ORDER BY symbol, date")]
        splits = [tuple(r) for r in c.execute("SELECT symbol, detected_on, factor, overlap, effective FROM splits ORDER BY symbol, detected_on, effective")]
    finally:
        c.close()
    return bars, splits


def test_old_and_new_update_leave_identical_archive_tables(tmp_path):
    bars = make_bars(1400)

    def scaled(rows, f):
        return [{**b, "close": b["close"] * f, "adj_close": b["adj_close"] * f} for b in rows]
    scenarios = {
        "empty archive": (None, bars[800:1300]),
        "only new dates": (bars[:1200], bars[1100:1400]),
        "split boundary inside the overlap, archive holds bars after it": (bars[:1250], scaled(bars[1000:1050], 0.5) + bars[1050:1400]),
        "split covering the whole overlap": (bars[:1200], scaled(bars[1100:1200], 0.5) + bars[1200:1300]),
        "adj_close rewrite": (bars[:1200], [{**b, "adj_close": b["adj_close"] * 0.99} for b in bars[1000:1300]]),
        "conflicting closes that are not a split": (bars[:1200], [{**b, "close": b["close"] * (1.0 + 0.02 * (i % 2))} for i, b in enumerate(bars[1000:1300])]),
        "nothing changes": (bars[:1200], bars[1000:1200]),
    }
    splits_seen = []
    for n, (name, (base, fresh)) in enumerate(scenarios.items()):
        seed = PriceArchive(tmp_path / f"seed{n}.sqlite", now_fn=lambda: NOW)
        if base:
            seed.merge("Z.NS", base)
        shutil.copy(seed.path, tmp_path / f"old{n}.sqlite")
        shutil.copy(seed.path, tmp_path / f"new{n}.sqlite")
        a_old, a_new = PriceArchive(tmp_path / f"old{n}.sqlite", now_fn=lambda: NOW), PriceArchive(tmp_path / f"new{n}.sqlite", now_fn=lambda: NOW)
        old_update(a_old, "Z.NS", fresh)
        a_new._update("Z.NS", fresh)
        assert table_dump(a_old) == table_dump(a_new), name
        assert (a_old.writes, a_old.conflicts) == (a_new.writes, a_new.conflicts), name
        splits_seen.append(len(table_dump(a_new)[1]))
    assert splits_seen[2] == 1 and splits_seen[3] == 1 and splits_seen[0] == splits_seen[1] == splits_seen[4] == 0   # the split scenarios really split
