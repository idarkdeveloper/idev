"""Practice limit orders: wait until the price reaches the limit, fill at the latest price, expire or cancel."""
from datetime import datetime

import pytest

from trading_agent.broker import LocalPaperBroker
from trading_agent.practice_limits import LimitBook, marketable
from trading_agent.stops import PracticeStopChecker
from trading_agent.timezones import IST

MON_10 = datetime(2026, 10, 19, 10, 0, tzinfo=IST)     # a Monday, market open
MON_16 = datetime(2026, 10, 19, 16, 0, tzinfo=IST)     # after the close
TUE_10 = datetime(2026, 10, 20, 10, 0, tzinfo=IST)


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def broker(tmp_path, prices):
    return LocalPaperBroker(tmp_path / "paper_broker.json", starting_cash=100_000, price_fn=lambda s: prices[s],
                            currency="INR", whole_shares=True)


def test_marketable_is_at_or_better_than_the_limit():
    assert marketable("buy", 99.0, 100.0) and marketable("buy", 100.0, 100.0) and not marketable("buy", 100.5, 100.0)
    assert marketable("sell", 101.0, 100.0) and marketable("sell", 100.0, 100.0) and not marketable("sell", 99.5, 100.0)


def test_a_buy_waits_until_the_price_reaches_its_limit_then_fills_at_the_latest_price(tmp_path):
    prices = {"TCS": 3100.0}
    b, book = broker(tmp_path, prices), LimitBook(tmp_path, now_fn=Clock(MON_10))
    o = book.add("tcs", "buy", 3000, qty=2)
    assert o["status"] == "open" and o["symbol"] == "TCS"
    assert book.sweep(b, MON_10) == [] and b.positions() == []          # 3100 is above the limit: it waits
    prices["TCS"] = 2995.0
    filled = book.sweep(b, MON_10)
    assert len(filled) == 1 and filled[0]["fill_qty"] == 2 and filled[0]["fill_price"] == 2995.0
    assert b.positions()[0].qty == 2
    assert book.orders()[0]["status"] == "filled"
    assert book.sweep(b, MON_10) == []                                    # filled once, never again


def test_a_sell_limit_waits_for_a_higher_price(tmp_path):
    prices = {"TCS": 3000.0}
    b = broker(tmp_path, prices)
    b.submit_order("TCS", "buy", qty=3)
    book = LimitBook(tmp_path, now_fn=Clock(MON_10))
    book.add("TCS", "sell", 3200, qty=3, validity="gtc")
    assert book.sweep(b, MON_10) == []
    prices["TCS"] = 3210.0
    assert book.sweep(b, MON_10)[0]["fill_qty"] == 3 and b.positions() == []


def test_day_orders_expire_at_the_close_and_gtc_orders_keep_waiting(tmp_path):
    clock = Clock(MON_10)
    book = LimitBook(tmp_path, now_fn=clock)
    day, gtc = book.add("TCS", "buy", 3000, qty=1), book.add("INFY", "buy", 1400, qty=1, validity="gtc")
    clock.t = MON_16
    by_id = {o["id"]: o for o in book.orders()}
    assert by_id[day["id"]]["status"] == "expired" and by_id[gtc["id"]]["status"] == "open"
    clock.t = TUE_10
    assert {o["id"]: o["status"] for o in book.orders()}[gtc["id"]] == "open"


def test_cancel_and_a_failed_fill_keeps_waiting_with_its_reason(tmp_path):
    prices = {"TCS": 2900.0, "INFY": 1300.0}
    b, book = broker(tmp_path, prices), LimitBook(tmp_path, now_fn=Clock(MON_10))
    gone = book.add("TCS", "buy", 3000, qty=1)
    book.cancel(gone["id"])
    with pytest.raises(ValueError, match="already cancelled"):
        book.cancel(gone["id"])
    too_big = book.add("INFY", "buy", 1400, qty=10_000)                   # more than the cash: refused at fill time
    assert book.sweep(b, MON_10) == [] and b.positions() == []
    waiting = next(o for o in book.orders() if o["id"] == too_big["id"])
    assert waiting["status"] == "open" and waiting["note"]
    with pytest.raises(LookupError):
        book.cancel("nope")


def test_orders_are_validated(tmp_path):
    book = LimitBook(tmp_path, now_fn=Clock(MON_10))
    for kw in ({"qty": 1, "limit": 0}, {"qty": None, "limit": 10}, {"qty": -1, "limit": 10}):
        with pytest.raises(ValueError):
            book.add("TCS", "buy", kw["limit"], qty=kw["qty"])
    with pytest.raises(ValueError):
        book.add("TCS", "buy", 10, qty=1, validity="forever")


def test_the_practice_checker_fills_them_only_in_market_hours_and_never_under_halt(tmp_path, settings):
    prices = {"TCS": 2900.0}
    b = broker(settings.state_dir, prices)
    book = LimitBook(settings.state_dir, now_fn=Clock(MON_10))
    book.add("TCS", "buy", 3000, qty=1, validity="gtc")
    chk = PracticeStopChecker(lambda: b, settings.state_dir / "state.json", limits=book)
    chk.check_once(MON_16)
    assert chk.limit_fills == [] and b.positions() == []                  # after the close: nothing
    from trading_agent import killswitch
    (settings.state_dir / "HALT").write_text('{"at": "2026-10-19T09:00:00+05:30", "by": "test", "cancel_gtt": false}')
    assert killswitch.status(settings.state_dir) is not None
    chk.check_once(TUE_10)
    assert b.positions() == []                                            # kill switch: no fills
    (settings.state_dir / "HALT").unlink()
    chk.check_once(TUE_10)
    assert len(chk.limit_fills) == 1 and b.positions()[0].qty == 1
