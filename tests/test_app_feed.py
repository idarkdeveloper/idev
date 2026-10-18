"""AppFeed writes the phone app's feed file. Fakes only: a fake clock, a fake holiday calendar, fake read/cash
functions. No network, no Groww, no .env."""
import json
import os
import sys
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from trading_agent.app_feed import FEED_FILE, HISTORY_FILE, AppFeed, load_feed, load_history, record_daily_value
from trading_agent.timezones import IST

MON = datetime(2026, 6, 8, 10, 30, tzinfo=IST)   # a Monday, inside the window


class Holidays:
    def __init__(self, closed=()):
        self.closed = set(closed)

    def is_trading_day(self, d):
        return d.weekday() < 5 and d not in self.closed


def good(**over):
    out = {"linked": True, "at": "x", "source": "groww", "invested": 1500.0, "value": 1600.0, "pl": 100.0,
           "pl_pct": 100 / 1500, "unpriced": [],
           "holdings": [{"symbol": "TATASTEEL", "name": "Tata Steel", "qty": 10, "avg_price": 150.0,
                         "price": 160.0, "value": 1600.0, "pl": 100.0, "pl_pct": 160 / 150 - 1}]}
    out.update(over)
    return out


class Rig:
    def __init__(self, tmp_path, read=None, every=60, closed=()):
        self.reads = 0
        self.result = read if read is not None else good()
        self.settings = SimpleNamespace(state_dir=tmp_path, app_feed_every_s=every,
                                        groww_window_start="08:30", groww_window_end="16:00")
        self.feed = AppFeed(self.settings, self._read, lambda: 12345.67, lambda s: 155.0, lambda s: [150.0, 151.2],
                            holidays=Holidays(closed))

    def _read(self):
        self.reads += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_writes_the_feed_in_the_window(tmp_path):
    r = Rig(tmp_path)
    out = r.feed.tick(MON)
    assert out is not None and load_feed(tmp_path) == out
    assert (tmp_path / FEED_FILE).exists()


@pytest.mark.parametrize("when,closed", [(MON.replace(hour=7), ()), (MON.replace(hour=17), ()),
                                         (MON + timedelta(days=5), ()), (MON, (date(2026, 6, 8),))])
def test_no_read_outside_the_window_or_on_a_holiday(tmp_path, when, closed):
    r = Rig(tmp_path, closed=closed)
    assert r.feed.tick(when) is None
    assert r.reads == 0 and not (tmp_path / FEED_FILE).exists()


def test_does_not_read_again_before_the_interval(tmp_path):
    r = Rig(tmp_path, every=60)
    r.feed.tick(MON)
    assert r.feed.tick(MON + timedelta(seconds=30)) is None
    assert r.reads == 1
    assert r.feed.tick(MON + timedelta(seconds=61)) is not None
    assert r.reads == 2


def test_feed_has_the_contract_fields_and_day_change_from_prev_close(tmp_path):
    out = Rig(tmp_path).feed.tick(MON)
    for k in ("as_of", "age_s", "stale", "market_closed", "source", "cash", "value", "invested", "pl", "pl_pct",
              "day_change", "day_change_pct", "holdings", "unpriced", "error"):
        assert k in out
    assert out["as_of"] == MON.isoformat(timespec="seconds")
    assert out["cash"] == 12345.67 and out["source"] == "groww" and out["error"] is None
    assert out["stale"] is False and out["market_closed"] is False
    h = out["holdings"][0]
    assert h["day_change"] == pytest.approx(50.0)           # (160 - 155) * 10
    assert h["day_change_pct"] == pytest.approx(160 / 155 - 1)
    assert h["trend"] == [150.0, 151.2]
    assert out["day_change"] == pytest.approx(50.0)
    assert out["day_change_pct"] == pytest.approx(50.0 / 1550.0)


def test_unlinked_or_error_keeps_the_last_good_feed_and_sets_error(tmp_path):
    r = Rig(tmp_path)
    first = r.feed.tick(MON)
    for i, bad in enumerate(({"linked": False}, {"linked": True, "error": "cool-down until 11:00"},
                             RuntimeError("boom token=SECRET123")), 1):
        r.result = bad
        out = r.feed.tick(MON + timedelta(minutes=5 * i))
        assert out is not None and out["error"]
        assert {k: v for k, v in out.items() if k != "error"} == {k: v for k, v in first.items() if k != "error"}
        assert "SECRET123" not in (tmp_path / FEED_FILE).read_text(encoding="utf-8")
    assert load_feed(tmp_path)["holdings"] == first["holdings"]


def test_empty_holdings_and_none_prices_do_not_raise(tmp_path):
    r = Rig(tmp_path, read=good(holdings=[], value=0.0, invested=0.0, pl=0.0, pl_pct=None))
    out = r.feed.tick(MON)
    assert out["holdings"] == [] and out["day_change"] is None and out["day_change_pct"] is None
    r2 = Rig(tmp_path / "b", read=good(holdings=[{"symbol": "X", "name": None, "qty": 1, "avg_price": 5.0,
                                                  "price": None, "value": None, "pl": None, "pl_pct": None}],
                                       unpriced=["X"]))
    out = r2.feed.tick(MON)
    assert out["holdings"][0]["day_change"] is None and out["day_change"] is None and out["unpriced"] == ["X"]


def test_cash_error_gives_none(tmp_path):
    r = Rig(tmp_path)
    r.feed.cash_fn = lambda: 1 / 0
    assert r.feed.tick(MON)["cash"] is None


def test_feed_file_is_owner_only_and_atomic(tmp_path):
    Rig(tmp_path).feed.tick(MON)
    assert not list(tmp_path.glob("*.tmp"))
    if sys.platform != "win32":
        assert os.stat(tmp_path / FEED_FILE).st_mode & 0o777 == 0o600
    json.loads((tmp_path / FEED_FILE).read_text(encoding="utf-8"))


def _feed(value=1600.0, cash=10.0, source="groww"):
    return {"value": value, "cash": cash, "source": source}


def test_one_row_per_session_and_a_second_call_replaces_not_duplicates(tmp_path):
    assert record_daily_value(tmp_path, _feed(1600.0), date(2026, 6, 8)) is True
    assert record_daily_value(tmp_path, _feed(1700.0), date(2026, 6, 8)) is True
    assert load_history(tmp_path, "all") == [{"date": "2026-06-08", "value": 1700.0, "cash": 10.0}]
    if sys.platform != "win32":
        assert os.stat(tmp_path / HISTORY_FILE).st_mode & 0o777 == 0o600


def test_not_recorded_before_15_35_or_from_a_saved_source(tmp_path):
    r = Rig(tmp_path)
    r.feed.tick(MON.replace(hour=15, minute=34))
    assert load_history(tmp_path, "all") == []
    r.feed.tick(MON.replace(hour=15, minute=35))
    assert [x["date"] for x in load_history(tmp_path, "all")] == ["2026-06-08"]
    r2 = Rig(tmp_path / "b", read=good(source="saved"))
    r2.feed.tick(MON.replace(hour=15, minute=40))
    assert load_history(tmp_path / "b", "all") == []


def test_history_ranges_cut_by_date_and_come_oldest_first(tmp_path):
    last = date(2026, 6, 8)
    for n in (400, 200, 60, 10, 0):   # written out of order on purpose
        record_daily_value(tmp_path, _feed(float(n)), last - timedelta(days=n))
    days = lambda r: [x["value"] for x in load_history(tmp_path, r)]  # noqa: E731
    assert days("all") == [400.0, 200.0, 60.0, 10.0, 0.0]
    assert days("1y") == [200.0, 60.0, 10.0, 0.0]
    assert days("3m") == [60.0, 10.0, 0.0]
    assert days("1m") == [10.0, 0.0]


def test_history_file_survives_garbage_content(tmp_path):
    (tmp_path / HISTORY_FILE).write_text("{not json", encoding="utf-8")
    assert load_history(tmp_path, "all") == []
    (tmp_path / HISTORY_FILE).write_text('{"a": 1}', encoding="utf-8")
    assert load_history(tmp_path, "1m") == []
    assert record_daily_value(tmp_path, _feed(), date(2026, 6, 8)) is True


@pytest.mark.parametrize("bad", [None, True, float("nan"), float("inf"), "1600"])
def test_a_missing_or_non_numeric_value_does_not_overwrite_a_good_row(tmp_path, bad):
    d = date(2026, 6, 8)
    record_daily_value(tmp_path, _feed(1600.0), d)
    assert record_daily_value(tmp_path, _feed(bad), d) is False
    assert load_history(tmp_path, "all") == [{"date": "2026-06-08", "value": 1600.0, "cash": 10.0}]


def test_bad_rows_are_dropped_and_good_rows_still_load(tmp_path):
    rows = [{"date": "abc", "value": 1.0}, {"date": "2026-06-08", "value": 5.0, "cash": None},
            {"date": "2026-06-09", "value": None}, {"date": "2026-06-10", "value": True}, 7]
    (tmp_path / HISTORY_FILE).write_text(json.dumps(rows), encoding="utf-8")
    for r in ("1m", "all"):
        assert load_history(tmp_path, r) == [{"date": "2026-06-08", "value": 5.0, "cash": None}]


# ------------------------------------------------------------------- settings, Watcher and the cli wiring

def test_app_feed_defaults_off(monkeypatch):
    from trading_agent.config import load_settings
    monkeypatch.delenv("APP_FEED", raising=False)
    monkeypatch.delenv("APP_FEED_EVERY_S", raising=False)
    s = load_settings(None)
    assert s.app_feed is False and s.app_feed_every_s == 300


def test_app_feed_env_parsing_and_minimum(monkeypatch):
    from trading_agent.config import load_settings
    monkeypatch.setenv("APP_FEED", "true")
    monkeypatch.setenv("APP_FEED_EVERY_S", "120")
    s = load_settings(None)
    assert s.app_feed is True and s.app_feed_every_s == 120
    for bad in ("59", "soon"):
        monkeypatch.setenv("APP_FEED_EVERY_S", bad)
        with pytest.raises(SystemExit):
            load_settings(None)


def test_watcher_tick_calls_the_feed_once_and_survives_its_error(settings):
    from trading_agent.watch import Watcher

    class Feed:
        def __init__(self, boom=False):
            self.boom, self.calls = boom, []

        def tick(self, now):
            self.calls.append(now)
            if self.boom:
                raise RuntimeError("feed broke")
            return {"as_of": "x"}
    ok = Feed()
    info = Watcher(settings, every=60, app_feed=ok).tick(force=True)
    assert len(ok.calls) == 1 and info["app_feed"] == {"as_of": "x"}
    bad = Feed(boom=True)
    info = Watcher(settings, every=60, app_feed=bad).tick(force=True)
    assert len(bad.calls) == 1 and "app_feed" not in info and "in_window" in info


def test_watcher_without_a_feed_behaves_as_before(settings):
    from trading_agent.watch import Watcher
    assert "app_feed" not in Watcher(settings, every=60).tick(force=True)


def _cli_settings(settings, **over):
    import dataclasses
    return dataclasses.replace(settings, **over)


def test_cli_read_fn_keeps_only_the_error_type_for_unknown_exceptions(settings, monkeypatch):
    from trading_agent import cli, runner
    secret = "sk-SECRET-token-123"
    monkeypatch.setattr(runner, "read_groww_portfolio", lambda *a, **k: {
        "linked": True, "error": f"HTTPError: 401 for url https://x/?key={secret}"})
    out = cli._app_feed_read(settings, object())()
    assert secret not in json.dumps(out) and out["error"] == "HTTPError"
    # a deliberate, human-readable refusal text is kept
    monkeypatch.setattr(runner, "read_groww_portfolio", lambda *a, **k: {
        "linked": True, "error": "Groww is cooling down until 10:00"})
    assert cli._app_feed_read(settings, object())()["error"] == "Groww is cooling down until 10:00"
    # a read that returns good data is untouched
    monkeypatch.setattr(runner, "read_groww_portfolio", lambda *a, **k: good())
    assert cli._app_feed_read(settings, object())()["value"] == 1600.0


@pytest.mark.parametrize("fail", ["token", "exit", "other"])
def test_cli_cash_fn_returns_none_on_any_error(settings, monkeypatch, fail):
    from trading_agent import cli, groww, runner
    from trading_agent.groww import GrowwTokenUnavailable

    class FakeBroker:
        def __init__(self, token, **kw):
            pass

        def available_cash(self):
            raise RuntimeError("boom")

    def resolve(s):
        if fail == "token":
            raise GrowwTokenUnavailable("cooling down")
        if fail == "exit":
            raise SystemExit("no creds")
        return "t"
    monkeypatch.setattr(groww, "GrowwBroker", FakeBroker)
    monkeypatch.setattr(runner, "resolve_groww_token", resolve)
    assert cli._app_feed_cash(settings)() is None


def test_cli_cash_fn_returns_the_balance(settings, monkeypatch):
    from trading_agent import cli, groww, runner
    seen = {}

    class FakeBroker:
        def __init__(self, token, **kw):
            seen.update(kw, token=token)

        def available_cash(self):
            return 99.5
    monkeypatch.setattr(groww, "GrowwBroker", FakeBroker)
    monkeypatch.setattr(runner, "resolve_groww_token", lambda s: "t")
    assert cli._app_feed_cash(settings)() == 99.5 and seen["live_orders"] is False


def test_cli_trend_and_prev_close_come_from_the_free_prices(settings):
    from trading_agent import cli

    class Prices:
        def history(self, symbol, range_="2y"):
            return [{"date": f"2026-05-{d:02d}", "close": float(d)} for d in range(1, 41 - 10)] + [
                {"date": "2999-01-01", "close": 7.0}]
    trend = cli._app_feed_trend(Prices())("X")
    assert len(trend) == 30 and trend[-1] == 7.0 and trend[0] < trend[1]
    assert cli._app_feed_prev_close(Prices())("X") == 30.0


@pytest.mark.parametrize("err", ["ReadTimeout: HTTPSConnectionPool(host='api.groww.in'): Read timed out.",
                                 "InvalidHeader: Invalid return character in Authorization: Bearer SECRETTOKEN\nx"])
def test_cli_read_fn_scrubs_any_type_prefixed_error(settings, monkeypatch, err):
    from trading_agent import cli, runner
    monkeypatch.setattr(runner, "read_groww_portfolio", lambda *a, **k: {"linked": True, "error": err})
    out = cli._app_feed_read(settings, object())()
    assert out["error"] == err.split(":")[0] and "SECRETTOKEN" not in out["error"]


def test_a_saved_source_never_asks_groww_for_cash(tmp_path):
    rig = Rig(tmp_path, read=good(source="saved"))
    calls = []
    rig.feed.cash_fn = lambda: calls.append(1) or 5.0
    feed = rig.feed.tick(MON)
    assert calls == [] and feed["cash"] is None
