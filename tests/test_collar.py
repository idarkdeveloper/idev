"""Opening collar: no buys before 09:17 IST, buy limit capped at the opening price + 30 bps, an unfilled live buy
cancelled after 120 s. Fake Groww session and a fake clock only: no network, no real order."""
from datetime import datetime

import pytest

from trading_agent import collar, runner
from trading_agent.groww import GrowwBroker, limit_price
from trading_agent.timezones import IST

from .conftest import FakeSession


def ok(payload):
    return {"status": "SUCCESS", "payload": payload}


def at(h, m, day=13):   # 13 Oct 2026 is a Tuesday
    return datetime(2026, 10, day, h, m, tzinfo=IST)


class Cal:
    def __init__(self, closed=()):
        self.closed = set(closed)

    def is_trading_day(self, d):
        return d.weekday() < 5 and d not in self.closed


class FakeGroww(FakeSession):
    """Groww with an order that stays OPEN (or fills on a given poll); a cancel turns it CANCELLED."""

    def __init__(self, routes, filled_after=None, partial=0):
        super().__init__(routes)
        self.polls, self.filled_after, self.partial, self.cancelled = 0, filled_after, partial, False

    def request(self, method, url, **kw):
        if method == "GET" and "/order/status/GMK1" in url:
            self.calls.append((method, url, kw))
            self.polls += 1
            from .conftest import FakeResponse
            if self.cancelled:
                return FakeResponse(ok({"order_status": "CANCELLED", "filled_quantity": self.partial}))
            if self.filled_after is not None and self.polls >= self.filled_after:
                return FakeResponse(ok({"order_status": "EXECUTED", "filled_quantity": 3}))
            return FakeResponse(ok({"order_status": "OPEN", "filled_quantity": self.partial}))
        resp = super().request(method, url, **kw)
        if method == "POST" and "/order/cancel" in url:
            self.cancelled = True     # only reached when the cancel route did not raise
        return resp


def routes(quote=None, extra=None):
    r = {("GET", "/live-data/ltp"): ok({"NSE_RELIANCE": 1000.0}),
         ("GET", "/holdings/user"): ok({"holdings": [{"trading_symbol": "RELIANCE", "quantity": 10,
                                                      "demat_free_quantity": 10, "average_price": 900.0}]}),
         ("POST", "/order/create"): ok({"groww_order_id": "GMK1", "order_status": "NEW"}),
         ("POST", "/order/cancel"): ok({"order_status": "CANCELLATION_REQUESTED"}),
         ("GET", "/order/detail/GMK1"): ok({"average_fill_price": 992.9, "filled_quantity": 3})}
    if quote is not None:
        r[("GET", "/live-data/quote")] = quote
    r.update(extra or {})
    return r


def broker(sess, **kw):
    clock = [1000.0]

    def sleep(s):
        clock[0] += s
    b = GrowwBroker("tok", session=sess, live_orders=True, sleep=sleep, clock=lambda: clock[0], **kw)
    b.now = clock
    return b


def created(sess):
    return [kw["json"] for m, u, kw in sess.calls if m == "POST" and "/order/create" in u]


# --------------------------------------------------------------------------- #
# time rules
# --------------------------------------------------------------------------- #
def test_no_live_buy_before_0917(settings):
    assert "no buys before 09:17 IST (it is 09:16)" in collar.live_buy_block(settings, at(9, 16))
    assert collar.live_buy_block(settings, at(9, 17)) is None
    assert collar.live_buy_block(settings, at(14, 0)) is None
    settings.open_collar_start = "09:30"
    assert collar.live_buy_block(settings, at(9, 20))


def test_practice_buys_only_wait_out_the_pre_open_and_the_first_minutes(settings):
    cal = Cal(closed={at(9, 0, day=14).date()})
    assert "no practice buys from 09:00 to 09:17" in collar.practice_buy_block(settings, at(9, 5), cal)
    assert collar.practice_buy_block(settings, at(9, 17), cal) is None
    assert collar.practice_buy_block(settings, at(8, 59), cal) is None          # before the pre-open
    assert collar.practice_buy_block(settings, at(20, 0), cal) is None          # evenings work as before
    assert collar.practice_buy_block(settings, at(9, 5, day=17), cal) is None   # Saturday
    assert collar.practice_buy_block(settings, at(9, 5, day=14), cal) is None   # an NSE holiday


def test_the_practice_account_refuses_a_buy_at_0910_but_sells(settings, monkeypatch):
    settings.market = "in"
    pb = runner.make_practice_broker(settings, price_fn=lambda s: 100.0)
    pb.submit_order("AAA", "buy", qty=5)
    monkeypatch.setattr(collar, "_now", lambda: at(9, 10))
    with pytest.raises(PermissionError, match="opening collar"):
        pb.submit_order("AAA", "buy", qty=1)
    assert pb.submit_order("AAA", "sell", qty=1)["status"] == "filled"


def test_the_live_buy_gate_refuses_before_0917_with_no_order_sent(settings, monkeypatch):
    settings.market, settings.broker, settings.groww_live_orders = "in", "groww", True
    settings.groww_access_token, settings.integration_check = "tok", False
    monkeypatch.setattr(collar, "_now", lambda: at(9, 12))
    b = runner.make_groww(settings)
    sess = FakeSession(routes())
    b.session = sess
    with pytest.raises(PermissionError, match="no buys before 09:17"):
        b.submit_order("RELIANCE", "buy", qty=1)
    assert sess.writes() == []


# --------------------------------------------------------------------------- #
# the price collar
# --------------------------------------------------------------------------- #
def test_the_buy_limit_is_the_lower_of_the_cap_and_the_opening_price_plus_30_bps():
    # LTP 1000: cap = 1005.00. Opening 990: 990 x 1.003 = 992.97 -> 992.95 on a 0.05 tick.
    sess = FakeGroww(routes(quote=ok({"ohlc": {"open": 990.0, "high": 1001, "low": 989, "close": 985}})), filled_after=1)
    o = broker(sess, collar_bps=30).submit_order("RELIANCE", "buy", qty=3)
    assert created(sess)[0]["price"] == 992.95 and o["limit_price"] == 992.95 and o["opening_price"] == 990.0
    # opening 1100 (a gap up, then a fall): the 0.5% cap is lower and wins
    sess = FakeGroww(routes(quote=ok({"ohlc": "{open: 1100.0,high: 1110.0,low: 990.0,close: 1050.0}"})), filled_after=1)
    broker(sess, collar_bps=30).submit_order("RELIANCE", "buy", qty=3)
    assert created(sess)[0]["price"] == limit_price(1000.0, "buy", 0.5, 0.05) == 1004.95   # the existing cap


def test_the_price_cap_applies_only_from_0917_to_0945(settings):
    assert not collar.price_cap_active(settings, at(9, 16))
    assert collar.price_cap_active(settings, at(9, 17)) and collar.price_cap_active(settings, at(9, 44))
    assert not collar.price_cap_active(settings, at(9, 45)) and not collar.price_cap_active(settings, at(13, 0))
    settings.open_collar_end = "10:00"
    assert collar.price_cap_active(settings, at(9, 50))


@pytest.mark.parametrize("hh, mm, capped", [(9, 20, True), (9, 44, True), (9, 45, False), (11, 0, False)])
def test_inside_the_window_the_opening_price_caps_the_buy_after_it_only_the_slippage_cap(settings, hh, mm, capped):
    sess = FakeGroww(routes(quote=ok({"ohlc": {"open": 990.0}})), filled_after=1)
    b = broker(sess, collar_bps=30, collar_active=lambda: collar.price_cap_active(settings, at(hh, mm)))
    o = b.submit_order("RELIANCE", "buy", qty=3)
    if capped:
        assert created(sess)[0]["price"] == 992.95 and o["opening_price"] == 990.0
    else:
        assert created(sess)[0]["price"] == limit_price(1000.0, "buy", 0.5, 0.05) and "opening_price" not in o
        assert not any("/live-data/quote" in u for _, u, _ in sess.calls)          # no opening price needed


def test_make_groww_wires_the_window(settings, monkeypatch):
    settings.market, settings.broker, settings.groww_access_token = "in", "groww", "tok"
    settings.groww_live_orders = True
    b = runner.make_groww(settings)
    monkeypatch.setattr(collar, "_now", lambda: at(9, 30))
    assert b.collar_active()
    monkeypatch.setattr(collar, "_now", lambda: at(10, 0))
    assert not b.collar_active()


def test_the_opening_price_falls_back_to_yahoo_and_with_none_the_buy_is_refused():
    sess = FakeGroww(routes(), filled_after=1)      # no quote route: Groww's quote fails
    broker(sess, collar_bps=30, open_price_fn=lambda s: 995.0).submit_order("RELIANCE", "buy", qty=3)
    assert created(sess)[0]["price"] == 997.95          # 995 x 1.003 = 997.985 -> 997.95
    sess = FakeGroww(routes(quote=ok({"ltp": 1000.0})))
    with pytest.raises(PermissionError, match="no opening price for RELIANCE"):
        broker(sess, collar_bps=30, open_price_fn=lambda s: None).submit_order("RELIANCE", "buy", qty=3)
    assert created(sess) == []


def test_yahoo_opening_uses_only_todays_bar():
    class Prices:
        def history_ohlc(self, symbol, range_, ttl=None):
            return [{"date": "2026-10-12", "open": 980.0}, {"date": "2026-10-13", "open": 990.5}]
    assert collar.yahoo_opening(Prices(), "X", at(10, 0).date()) == 990.5
    assert collar.yahoo_opening(Prices(), "X", at(10, 0, day=14).date()) is None
    assert collar.yahoo_opening(lambda s: 1.0, "X") is None


def test_sells_are_never_collared():
    sess = FakeGroww(routes(quote=ok({"ohlc": {"open": 900.0}})), filled_after=1)
    broker(sess, collar_bps=30, buy_timeout_s=120).submit_order("RELIANCE", "sell", qty=3)
    assert created(sess)[0]["price"] == limit_price(1000.0, "sell", 0.5, 0.05)   # LTP - 0.5%, not the opening price
    assert not any("/live-data/quote" in u for _, u, _ in sess.calls)


# --------------------------------------------------------------------------- #
# the 120 s time-out
# --------------------------------------------------------------------------- #
def test_an_unfilled_buy_is_cancelled_after_120_seconds():
    sess = FakeGroww(routes(quote=ok({"ohlc": {"open": 990.0}})))
    b = broker(sess, collar_bps=30, buy_timeout_s=120)
    o = b.submit_order("RELIANCE", "buy", qty=3)
    cancels = [kw["json"] for m, u, kw in sess.calls if "/order/cancel" in u]
    assert cancels == [{"segment": "CASH", "groww_order_id": "GMK1"}]
    assert b.now[0] - 1000.0 >= 120 and o["cancelled_unfilled"] and o["status"] == "failed"
    assert o["remark"] == "unfilled after 120 s: cancelled (filled 0 of 3)"
    assert len(created(sess)) == 1                       # never re-placed


def test_a_partial_fill_keeps_what_filled():
    sess = FakeGroww(routes(quote=ok({"ohlc": {"open": 990.0}}), extra={
        ("GET", "/order/detail/GMK1"): ok({"average_fill_price": 992.9, "filled_quantity": 1})}), partial=1)
    o = broker(sess, collar_bps=30, buy_timeout_s=120).submit_order("RELIANCE", "buy", qty=3)
    assert o["filled_quantity"] == 1 and o["average_fill_price"] == 992.9
    assert o["remark"] == "unfilled after 120 s: cancelled (filled 1 of 3)"


def test_a_buy_that_fills_in_time_is_not_cancelled():
    sess = FakeGroww(routes(quote=ok({"ohlc": {"open": 990.0}})), filled_after=8)   # fills about 25 s in
    b = broker(sess, collar_bps=30, buy_timeout_s=120)
    o = b.submit_order("RELIANCE", "buy", qty=3)
    assert o["status"] == "filled" and not o.get("cancelled_unfilled")
    assert not any("/order/cancel" in u for _, u, _ in sess.calls) and b.now[0] - 1000.0 < 120


def test_a_failed_cancel_alerts():
    alerts = []
    sess = FakeGroww(routes(quote=ok({"ohlc": {"open": 990.0}}), extra={("POST", "/order/cancel"): RuntimeError("boom")}))
    o = broker(sess, collar_bps=30, buy_timeout_s=120,
               alert_fn=lambda key, subject, body: alerts.append(subject)).submit_order("RELIANCE", "buy", qty=3)
    assert "cancel failed" in o["remark"] and alerts == ["[ORDER STILL OPEN] buy could not be cancelled"]


def test_make_groww_turns_the_collar_on_only_with_live_orders(settings):
    settings.market, settings.broker, settings.groww_access_token = "in", "groww", "tok"
    b = runner.make_groww(settings)
    assert b.collar_bps is None and b.buy_timeout_s is None
    settings.groww_live_orders = True
    b = runner.make_groww(settings)
    assert b.collar_bps == 30.0 and b.buy_timeout_s == 120


def test_settings(monkeypatch, tmp_path):
    from trading_agent.config import load_settings
    monkeypatch.chdir(tmp_path)
    s = load_settings(None)
    assert (s.open_collar_start, s.open_collar_bps, s.buy_timeout_s, s.day_brake_pct) == ("09:17", 30.0, 120, 3.5)
    assert s.open_collar_end == "09:45"
    for k, v in (("OPEN_COLLAR_START", "9:00"), ("OPEN_COLLAR_START", "noon"), ("OPEN_COLLAR_BPS", "0"),
                 ("OPEN_COLLAR_END", "09:10"), ("OPEN_COLLAR_END", "16:00"),
                 ("BUY_TIMEOUT_S", "5"), ("DAY_BRAKE_PCT", "x")):
        monkeypatch.setenv(k, v)
        with pytest.raises(SystemExit):
            load_settings(None)
        monkeypatch.delenv(k)
    monkeypatch.setenv("OPEN_COLLAR_START", "9:20")
    assert load_settings(None).open_collar_start == "09:20"


