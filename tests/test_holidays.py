from datetime import date, datetime

from trading_agent.forward import IST, ForwardTest
from trading_agent.holidays import NSEHolidays
from trading_agent.nse import NSEClient
from trading_agent.watch import Watcher
from .conftest import FakeSession

LIST = {"CM": [{"tradingDate": "26-Jan-2026", "weekDay": "Monday", "description": "Republic Day"},
               {"tradingDate": "09-Nov-2026", "weekDay": "Monday", "description": "Diwali Laxmi Pujan*"},
               {"tradingDate": "bad date", "description": "ignored"}],
        "FO": [{"tradingDate": "27-Jan-2026", "description": "not the equity segment"}]}


def holidays(tmp_path, payload=LIST):
    client = NSEClient(session=FakeSession({("GET", "holiday-master"): payload}))
    return NSEHolidays(client, cache_dir=tmp_path), client


def test_reads_equity_holidays_and_caches_them(tmp_path):
    h, client = holidays(tmp_path)
    assert h.holiday(date(2026, 1, 26)) == "Republic Day"
    assert h.holiday(date(2026, 1, 27)) is None  # an F&O-only date doesn't close equities
    assert not h.is_trading_day(date(2026, 11, 9)) and h.is_trading_day(date(2026, 11, 10))
    assert not h.is_trading_day(date(2026, 11, 7))  # a Saturday
    again, client2 = holidays(tmp_path)
    assert again.holiday(date(2026, 1, 26)) == "Republic Day"
    assert not any("holiday-master" in c[1] for c in client2.session.calls)  # served from the cache


def test_unreachable_nse_falls_back_to_weekdays(tmp_path):
    h, _ = holidays(tmp_path, RuntimeError("HTTP 403"))
    assert h.is_trading_day(date(2026, 1, 26)) and not h.is_trading_day(date(2026, 1, 24))
    assert "HTTP 403" in h.error
    t = h.today(datetime(2026, 1, 26, 10, 0, tzinfo=IST))
    assert t["open"] is True and "unavailable" in t["warning"]


def test_today_names_the_reason(tmp_path):
    h, _ = holidays(tmp_path)
    assert h.today(datetime(2026, 11, 9, 9, 0, tzinfo=IST)) == {
        "date": "2026-11-09", "open": False, "reason": "holiday", "holiday": "Diwali Laxmi Pujan", "warning": None}
    assert h.today(datetime(2026, 11, 8, 9, 0, tzinfo=IST))["reason"] == "weekend"


def test_watch_mode_sleeps_on_a_holiday(tmp_path, settings):
    h, _ = holidays(tmp_path)
    w = Watcher(settings, holidays=h)
    assert not w.market_window_open(datetime(2026, 11, 9, 11, 0, tzinfo=IST))
    assert w.market_window_open(datetime(2026, 11, 10, 11, 0, tzinfo=IST))


def test_forward_test_is_not_due_on_a_holiday(tmp_path):
    h, _ = holidays(tmp_path)
    ft = ForwardTest(tmp_path, price_fn=lambda s: 100.0, now=lambda: datetime(2026, 11, 9, 16, 0, tzinfo=IST),
                     holidays=h)
    assert not ft.due()
    ft.now = lambda: datetime(2026, 11, 10, 16, 0, tzinfo=IST)
    assert ft.due()


def test_check_skips_on_a_holiday(tmp_path, monkeypatch, capsys):
    from trading_agent import cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("MARKET", "in")
    called = []
    monkeypatch.setattr(cli, "check", lambda *a, **k: called.append(1))
    monkeypatch.setattr(cli, "_holiday_today", lambda settings: "Diwali Laxmi Pujan")
    assert cli.main(["check", "--skip-holidays"]) == 0
    assert "NSE is closed today (Diwali Laxmi Pujan)" in capsys.readouterr().out and not called


def test_a_failed_download_is_retried_after_an_hour(tmp_path, monkeypatch):
    import trading_agent.holidays as hol
    from .conftest import Seq

    client = NSEClient(session=FakeSession({("GET", "holiday-master"): Seq(RuntimeError("timeout"), LIST)}))
    h = NSEHolidays(client, cache_dir=tmp_path)
    clock = [1_000_000.0]
    monkeypatch.setattr(hol.time, "time", lambda: clock[0])
    assert h.holiday(date(2026, 1, 26)) is None and h.error
    clock[0] += 600  # ten minutes later: still the fallback, no new request
    assert h.holiday(date(2026, 1, 26)) is None
    clock[0] += 3600  # over an hour later: asks NSE again
    assert h.holiday(date(2026, 1, 26)) == "Republic Day" and h.error is None
