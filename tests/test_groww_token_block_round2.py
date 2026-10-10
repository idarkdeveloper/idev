"""Fix round 2 for the Groww token cool-down: deal alerts while blocked, Retry-After clamping, robust clear."""

import logging
from datetime import datetime, timedelta

import pytest

from trading_agent.groww import IST, GrowwTokenUnavailable, TokenCache, _parse_retry_after
from trading_agent.notify import Notifier
from trading_agent.quiver import _norm_congress, filter_by_investor
from trading_agent.state import State
from trading_agent.watch import Watcher

from .test_groww_token_block import NOW, Resp, TokenSession, get_token, no_network, too_many  # noqa: F401


class Sink(Notifier):
    def send(self, subject, body):
        self.sent.append({"subject": subject, "body": body})
        return ["test"]


class Data:
    def __init__(self, trades):
        self.trades = trades

    def trades_for_investor(self, investor, source, **kw):
        return self.trades


def _watcher(settings, trades, factory, notifier, **kw):
    return Watcher(settings, broker=None, broker_factory=factory, data=Data(trades), notifier=notifier,
                   window=("00:00", "23:59"), weekdays_only=False, awake=None, **kw)


def _blocked_factory(until):
    def factory():
        raise GrowwTokenUnavailable("Groww refused a new login token (429 Too Many Requests).", until, 429)
    return factory


def test_new_deal_while_blocked_alerts_once_and_is_not_marked_seen(settings, sample_rows):
    settings.market = "us"
    trades = filter_by_investor([_norm_congress(r) for r in sample_rows], "Nancy Pelosi")[:2]
    n = Sink()
    until = datetime(2026, 10, 10, 10, 52, tzinfo=IST)
    w = _watcher(settings, trades, _blocked_factory(until), n)
    for _ in range(3):
        info = w.tick(force=True)
        assert "alerts_only" in info
    assert len(n.sent) == 1
    subj = n.sent[0]["subject"]
    assert subj.startswith("[DEAL] ") and "2 new deal(s)" in subj and "analysis paused: Groww unavailable until 10:52 IST" in subj
    st = State(settings.state_dir / "state.json")
    assert st.data["seen"] == {} and set(st.data["alerted_while_blocked"]) == {t.key for t in trades}
    # the block lifts: the full check still sees them as new; marking them seen clears the alerted record
    assert len(st.new_trades(trades)) == 2
    st.mark_seen(trades)
    st.save()
    st2 = State(settings.state_dir / "state.json")
    assert st2.data["alerted_while_blocked"] == {} and st2.new_trades(trades) == []


def test_a_later_new_deal_alerts_again(settings, sample_rows):
    trades = filter_by_investor([_norm_congress(r) for r in sample_rows], "Nancy Pelosi")
    data = Data(trades[:1])
    n = Sink()
    w = Watcher(settings, broker=None, broker_factory=_blocked_factory(None), data=data, notifier=n,
                window=("00:00", "23:59"), weekdays_only=False, awake=None)
    w.tick(force=True)
    w.tick(force=True)
    data.trades = trades[:2]
    w.tick(force=True)
    assert len(n.sent) == 2 and "1 new deal(s)" in n.sent[1]["subject"]
    assert "until it is back" in n.sent[0]["subject"]


def test_factory_failures_of_any_kind_keep_alerts_running(settings, sample_rows, caplog):
    trades = filter_by_investor([_norm_congress(r) for r in sample_rows], "Nancy Pelosi")[:1]
    n = Sink()
    errors = [SystemExit("Groww selected but no key"), RuntimeError("boom")]

    def factory():
        raise errors[0 if factory.calls % 2 == 0 else 1]

    factory.calls = 0
    real = factory

    def counting():
        try:
            return real()
        finally:
            real.calls += 1

    w = _watcher(settings, trades, counting, n)
    with caplog.at_level(logging.WARNING, logger="trading_agent"):
        for _ in range(6):
            info = w.tick(force=True)
            assert "alerts_only" in info
    warns = [r.getMessage() for r in caplog.records if "alerts only" in r.getMessage()]
    assert len(warns) == 2 and any("SystemExit" in m for m in warns) and any("RuntimeError" in m for m in warns)
    assert len(n.sent) == 1  # deals were still polled and alerted once


def test_retry_after_is_clamped_to_a_day():
    day = 86400.0
    for v in ("1e309", "99999999999999", "inf", "nan", "Fri, 01 Jan 9999 00:00:00 GMT"):
        assert _parse_retry_after(v, NOW) == day, v
    assert _parse_retry_after("-5", NOW) == 0.0 and _parse_retry_after("120", NOW) == 120.0
    assert _parse_retry_after("soon", NOW) is None


def test_429_with_absurd_retry_after_waits_at_most_a_day(tmp_path):
    cache = TokenCache(tmp_path / "t.json")
    with pytest.raises(GrowwTokenUnavailable):
        get_token(cache, TokenSession(too_many(headers={"Retry-After": "99999999999999"})))
    assert datetime.fromisoformat(cache.read_block()["until"]) == NOW + timedelta(days=1)


def test_clear_block_survives_os_errors(tmp_path, monkeypatch):
    cache = TokenCache(tmp_path / "t.json")
    cache.block_path.write_text("{}")
    from pathlib import Path
    real = Path.unlink

    def locked(self, *a, **k):
        if self.name.endswith(".block"):
            raise PermissionError("in use")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", locked)
    cache.clear_block()  # must not raise
    # and a successful token request is not turned into a failure by it
    assert get_token(TokenCache(tmp_path / "u.json"), TokenSession(Resp(200, {"token": "T"}))) == "T"


def test_block_comparison_uses_instants_not_strings(tmp_path):
    cache = TokenCache(tmp_path / "t.json")
    from trading_agent.groww import _mem_blocks
    # memory: 05:30Z = 11:00 IST (later); file: 05:00Z = 10:30 IST. As strings the first sorts lower.
    _mem_blocks[str(cache.block_path)] = {"until": "2026-10-10T05:30:00+00:00", "status": 429, "reason": "x",
                                          "at": "2026-10-10T10:00:00+05:30", "key": "k"}
    cache.block_path.write_text('{"until": "2026-10-10T10:30:00+05:30", "status": 429, "reason": "x", "at": "x"}')
    b = cache.active_block("APIKEY", NOW)
    assert datetime.fromisoformat(b["until"]) == datetime(2026, 10, 10, 11, 0, tzinfo=IST)
    _mem_blocks.pop(str(cache.block_path))
