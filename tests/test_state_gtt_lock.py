"""state.json and the GTT records: only a locked GTT sync writes gtt_*; every other save keeps them as on disk; the
GTT lock is re-entrant in a thread, times out instead of hanging, and is kept fresh during a long sync."""
import os
import threading
import time

import pytest

from trading_agent import state as st_mod
from trading_agent.state import GTT_KEYS, State, gtt_lock


def test_a_plain_save_after_a_locked_sync_keeps_the_syncs_gtt_records(tmp_path):
    path = tmp_path / "state.json"
    old = State(path)                                         # loaded before the sync (an announcements loop, a check)
    sync = State(path)
    sync.data.update(gtt_stops={"A": {"smart_order_id": "S1"}}, gtt_suspended={"B": {"ex_date": "2026-10-19"}},
                     gtt_corp_done={"B:2026-10-19": "x"})
    with gtt_lock(tmp_path):
        sync.save_only(GTT_KEYS)
    old.data["seen"]["deal-1"] = {"at": "now"}
    old.save()                                                # plain save, stale copy
    disk = State(path).data
    assert disk["gtt_stops"] == {"A": {"smart_order_id": "S1"}} and disk["gtt_suspended"] == {"B": {"ex_date": "2026-10-19"}}
    assert disk["gtt_corp_done"] == {"B:2026-10-19": "x"} and "deal-1" in disk["seen"]
    full = State(path)
    full.data["gtt_stops"] = {}
    full.save(fresh_keys=())                                  # an explicit full write still can
    assert State(path).data["gtt_stops"] == {}


def test_the_gtt_lock_is_reentrant_and_times_out_instead_of_hanging(tmp_path):
    with gtt_lock(tmp_path):
        with gtt_lock(tmp_path):                              # the same thread: no self-deadlock
            pass
    held = threading.Event()
    done = threading.Event()

    def hold():
        with gtt_lock(tmp_path):
            held.set()
            done.wait(5)
    t = threading.Thread(target=hold)
    t.start()
    held.wait(5)
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        with gtt_lock(tmp_path, wait=0.3):
            pass
    assert time.monotonic() - start < 2
    done.set()
    t.join()


def test_a_save_takes_the_gtt_lock_and_a_busy_lock_never_loses_the_save(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    entered = []
    real = st_mod.gtt_lock

    def spy(state_dir, wait=None):
        entered.append(wait)
        return real(state_dir, wait=wait)
    monkeypatch.setattr(st_mod, "gtt_lock", spy)
    s = State(path)
    s.data["seen"]["x"] = {}
    s.save()
    assert entered and "x" in State(path).data["seen"]
    monkeypatch.setattr(st_mod, "SAVE_LOCK_WAIT_S", 0.2)
    lock = tmp_path / "state.json.gtt.lock"
    lock.write_text("someone else")                           # another process holds it
    s.data["seen"]["y"] = {}
    s.save()                                                  # waits briefly, then saves anyway (with a warning)
    assert "y" in State(path).data["seen"]
    os.utime(lock, None)


def test_a_long_sync_keeps_its_lock_fresh(tmp_path):
    lock = tmp_path / "state.json.gtt.lock"
    with gtt_lock(tmp_path):
        old = time.time() - 1000
        os.utime(lock, (old, old))
        st_mod.touch_gtt_lock(tmp_path)
        assert time.time() - lock.stat().st_mtime < 5
