import pytest

from trading_agent.groww import GrowwBroker, LiveOrdersDisabled, WrongIP
from trading_agent.netcheck import public_ip
from .conftest import FakeSession, Seq


def test_public_ip_reads_json_or_text_and_falls_back():
    s = FakeSession({("GET", "ipify"): {"ip": "203.0.113.7"}})
    assert public_ip(s) == "203.0.113.7"
    s = FakeSession({("GET", "ipify"): RuntimeError("down"), ("GET", "ifconfig.me"): "198.51.100.9\n"})
    assert public_ip(s) == "198.51.100.9"
    with pytest.raises(RuntimeError, match="could not find"):
        public_ip(FakeSession({("GET", "ipify"): {"ip": "not-an-ip"}, ("GET", "ifconfig.me"): "<html>"}))


def broker(ip_fn, allowed="203.0.113.7", live=True, clock=None):
    sess = FakeSession({("POST", "/order/create"): {"status": "SUCCESS", "payload": {"groww_order_id": "X"}}})
    b = GrowwBroker("tok", live_orders=live, session=sess, allowed_ip=allowed, ip_fn=ip_fn,
                    clock=clock or (lambda: 0.0))
    return b, sess


def test_live_order_refused_from_another_ip_before_any_call():
    b, sess = broker(lambda: "198.51.100.9")
    with pytest.raises(WrongIP, match="198.51.100.9.*203.0.113.7"):
        b._require_live("place an order")
    assert sess.writes() == []


def test_live_order_allowed_from_the_registered_ip_and_cached():
    calls = []
    t = [0.0]
    b, _ = broker(lambda: calls.append(1) or "203.0.113.7", clock=lambda: t[0])
    b._require_live("place an order")
    b._require_live("cancel an order")
    assert len(calls) == 1  # cached for a few minutes
    t[0] += 601
    b._require_live("place an order")
    assert len(calls) == 2


def test_unknown_ip_refuses_and_live_off_wins_first():
    def boom():
        raise RuntimeError("offline")
    b, _ = broker(boom)
    with pytest.raises(WrongIP, match="could not confirm"):
        b._require_live("place an order")
    b, _ = broker(lambda: "198.51.100.9", live=False)
    with pytest.raises(LiveOrdersDisabled) as e:
        b._require_live("place an order")
    assert not isinstance(e.value, WrongIP)


def test_no_allowed_ip_keeps_todays_behaviour():
    b, _ = broker(lambda: (_ for _ in ()).throw(AssertionError("not asked")), allowed=None)
    b._require_live("place an order")


def test_proxy_session_is_used_for_groww(settings):
    from trading_agent.runner import groww_session
    settings.groww_proxy_url = "http://user:pw@203.0.113.7:3128"
    assert groww_session(settings).proxies == {"https": settings.groww_proxy_url, "http": settings.groww_proxy_url}
    settings.groww_proxy_url = None
    assert groww_session(settings).proxies == {}


def test_watch_keeps_the_laptop_awake_only_in_the_window(settings, monkeypatch):
    from datetime import datetime
    import trading_agent.watch as watch
    from trading_agent.watch import IST, Watcher

    seen = []
    w = Watcher(settings, awake=seen.append, check_fn=lambda: None)
    times = iter([datetime(2026, 10, 12, 10, 0, tzinfo=IST), datetime(2026, 10, 12, 10, 1, tzinfo=IST),
                  datetime(2026, 10, 12, 20, 0, tzinfo=IST)])

    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return next(times)
    monkeypatch.setattr(watch, "datetime", FakeDT)
    monkeypatch.setattr(w, "poll_announcements", lambda: [])
    monkeypatch.setattr(w, "check_trailing_stops", lambda: [])
    monkeypatch.setattr(w, "sync_live", lambda: None)
    w.tick(); w.tick(); w.tick()
    assert seen == [True, False]  # on entering the window, off after it; no repeats
