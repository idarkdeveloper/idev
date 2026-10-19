"""Dashboard price reads are cached 60 s while the NSE is open; the agent's own prices object keeps its TTLs."""
import json
import os
import time
from datetime import date, datetime

from trading_agent.prices import FRESH_TTL, YahooPrices, price_ttl
from trading_agent.timezones import IST


class Hol:
    def __init__(self, closed=()):
        self.closed = set(closed)

    def is_trading_day(self, d):
        return d not in self.closed


def at(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=IST)


def test_price_ttl_open_closed_holiday_weekend():
    mon = (2026, 10, 19)
    assert price_ttl(at(*mon, 11, 0), 21600, Hol()) == FRESH_TTL            # open
    assert price_ttl(at(*mon, 9, 15), 900, Hol()) == FRESH_TTL
    assert price_ttl(at(*mon, 8, 0), 21600, Hol()) == 21600                 # before the open
    assert price_ttl(at(*mon, 15, 31), 900, Hol()) == 900                   # after the close
    assert price_ttl(at(2026, 10, 17, 11, 0), 21600, Hol()) == 21600        # Saturday
    assert price_ttl(at(*mon, 11, 0), 21600, Hol({date(2026, 10, 19)})) == 21600   # NSE holiday
    assert price_ttl(at(*mon, 11, 0), 30, Hol()) == 30                      # never longer lived than the default allows


class Resp:
    status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return {"chart": {"result": [{"timestamp": [1759900000, 1760000000], "indicators": {"quote": [{"close": [99.0, 100.0], "volume": [1, 1]}]}}]}}


class Sess:
    def __init__(self):
        self.calls = 0

    def get(self, *a, **k):
        self.calls += 1
        return Resp()


def _aged(tmp_path, seconds):
    for f in tmp_path.glob("*.json"):
        old = time.time() - seconds
        os.utime(f, (old, old))


def test_history_cache_ttl_is_60s_only_for_the_dashboard(tmp_path):
    agent, dash = Sess(), Sess()
    a = YahooPrices(session=agent, cache_dir=tmp_path / "a")
    d = YahooPrices(session=dash, cache_dir=tmp_path / "d")
    d.ttl_fn = lambda default: price_ttl(at(2026, 10, 19, 11, 0), default, Hol())   # market open
    for p in (a, d):
        p.history("TCS", "1mo")
    for p in (a, d):
        _aged(p.cache_dir, 120)       # two minutes old
        p.history("TCS", "1mo")
    assert agent.calls == 1           # the agent's 6 h cache still serves it
    assert dash.calls == 2            # the dashboard refetched: older than 60 s while open
    d.ttl_fn = lambda default: price_ttl(at(2026, 10, 19, 20, 0), default, Hol())   # closed: the long cache again
    _aged(d.cache_dir, 120)
    d.history("TCS", "1mo")
    assert dash.calls == 2


def test_only_the_dashboard_app_installs_the_fresh_ttl():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "trading_agent"
    assert "ttl_fn" in (root / "ui.py").read_text(encoding="utf-8")
    for name in ("runner.py", "watch.py", "agent.py", "cli.py"):
        assert "ttl_fn" not in (root / name).read_text(encoding="utf-8"), name
    assert json  # (imported for the fake response shape)
