"""AppFeed writes the phone app's feed file. Fakes only: a fake clock, a fake holiday calendar, fake read/cash
functions. No network, no Groww, no .env."""
import json
import os
import sys
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from trading_agent.app_feed import FEED_FILE, AppFeed, load_feed
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
