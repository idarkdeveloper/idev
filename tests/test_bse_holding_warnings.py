"""BSE-listed holdings with a name (not a scrip code) and bonds: no per-minute warnings, no repeated Yahoo 404s."""
import json
import logging
from datetime import datetime

from trading_agent import app_feed
from trading_agent.app_feed import AppFeed
from trading_agent.bse_announcements import cached_bse_code
from trading_agent.timezones import IST
from trading_agent.watch import Watcher


def _snapshot(settings, rows):
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    (settings.state_dir / "groww_holdings.json").write_text(json.dumps({"saved_at": "2026-10-16T15:40:00+05:30", "holdings": rows}), encoding="utf-8")


def _row(sym, exchange="NSE", kind="equity"):
    return {"symbol": sym, "name": sym, "exchange": exchange, "kind": kind, "qty": 1, "avg_price": 1.0}


def _csv(settings, rows):
    d = settings.state_dir / "cache"
    d.mkdir(parents=True, exist_ok=True)
    head = "exchange,exchange_token,trading_symbol,name,isin,segment,series\n"
    (d / "groww_instruments.csv").write_text(head + "".join(f"{e},{t},{s},{s} Ltd,,CASH,EQ\n" for e, t, s in rows), encoding="utf-8")


def _watcher(settings):
    class W(Watcher):
        def __init__(self, s):
            self.settings = s
            self._unpollable, self._ann_failed = {}, {}
    return W(settings)


def test_bse_name_resolves_from_the_cached_instrument_list_else_is_skipped_with_one_info_line(settings, caplog):
    settings.market = "in"
    _snapshot(settings, [_row("TCS"), _row("NSE", "BSE"), _row("543287", "BSE"), _row("13PCL31", "BSE", "bond"), _row("GEEK", "BSE")])
    _csv(settings, [("BSE", "500400", "NSE"), ("NSE", "123", "TCS")])
    w = _watcher(settings)
    caplog.set_level(logging.INFO)
    assert w.real_holdings() == ["TCS", "500400.BO", "543287.BO"]
    assert caplog.text.count("GEEK is a BSE holding without a scrip code") == 1     # unresolvable: skipped, one line
    w.real_holdings()
    w.real_holdings()
    assert caplog.text.count("GEEK is a BSE holding without a scrip code") == 1     # not again the same day
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    (settings.state_dir / "cache" / "groww_instruments.csv").unlink()                # no cached list at all
    assert w.real_holdings() == ["TCS", "543287.BO"]                                 # a numeric code needs no list; names are skipped


def test_cached_bse_code_is_offline_and_exact(settings):
    assert cached_bse_code(settings.state_dir / "cache", "NSE") is None               # no file: no code, no network
    _csv(settings, [("BSE", "500400", "NSE"), ("BSE", "bad", "XYZ"), ("NSE", "7", "TCS")])
    assert cached_bse_code(settings.state_dir / "cache", "nse.bo") == "500400"
    assert cached_bse_code(settings.state_dir / "cache", "XYZ") is None               # a non-numeric token is not a scrip code
    assert cached_bse_code(settings.state_dir / "cache", "TCS") is None               # NSE rows are not BSE codes


def test_a_failing_announcement_warns_once_a_day(settings, caplog, monkeypatch):
    w = _watcher(settings)
    caplog.set_level(logging.DEBUG)
    for _ in range(3):
        today = datetime.now(IST).date()
        (logging.getLogger("trading_agent.watch").debug if w._ann_failed.get("X.BO") == today else logging.getLogger("trading_agent.watch").warning)("probe")
        w._ann_failed["X.BO"] = today
    assert sum(r.levelno == logging.WARNING for r in caplog.records) == 1
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "trading_agent" / "watch.py").read_text(encoding="utf-8")
    assert '(log.debug if self._ann_failed.get(t) == today else log.warning)("announcements for %s failed' in src


# ---- the phone feed ----

class FeedSettings:
    app_feed_every_s = 300

    def __init__(self, d):
        self.state_dir = d
        self.groww_window_start, self.groww_window_end = "08:30", "16:00"


class Hol:
    def is_trading_day(self, d):
        return True


def _feed(tmp_path, rows):
    asked = []

    def prev(sym):
        asked.append(("prev", sym))
        if sym in ("GONE",):
            raise LookupError("Yahoo has no data for GONE.NS (HTTP 404)")
        return 100.0

    def trend(sym):
        asked.append(("trend", sym))
        if sym == "GONE":
            raise LookupError("Yahoo has no data for GONE.NS (HTTP 404)")
        return [1.0, 2.0]
    holdings = [{"symbol": r["symbol"], "name": r["symbol"], "exchange": r.get("exchange", "NSE"), "kind": r.get("kind", "equity"), "qty": 2,
                 "avg_price": 90.0, "price": 110.0, "value": 220.0, "pl": 40.0, "pl_pct": 0.2} for r in rows]
    out = {"linked": True, "holdings": holdings, "value": 1.0, "invested": 1.0, "pl": 0.0, "pl_pct": 0.0, "unpriced": [], "source": "groww"}
    f = AppFeed(FeedSettings(tmp_path / "state"), lambda: out, lambda: 5.0, prev, trend, Hol())
    return f, asked


def test_feed_skips_bonds_and_bse_names_quietly_and_keeps_the_contract(tmp_path, caplog):
    f, asked = _feed(tmp_path, [{"symbol": "TCS"}, {"symbol": "NSE", "exchange": "BSE"}, {"symbol": "13PCL31", "exchange": "BSE", "kind": "bond"}])
    caplog.set_level(logging.DEBUG)
    feed = f.tick(datetime(2026, 10, 19, 11, 0, tzinfo=IST))
    assert {s for _, s in asked} == {"TCS"}                                           # Yahoo asked for the NSE equity only
    rows = {h["symbol"]: h for h in feed["holdings"]}
    assert set(rows["TCS"]) == set(rows["NSE"]) == set(rows["13PCL31"]) == {"symbol", "name", "qty", "avg_price", "price", "value", "pl", "pl_pct", "day_change", "day_change_pct", "trend"}
    assert rows["TCS"]["trend"] == [1.0, 2.0] and rows["NSE"]["trend"] == [] and rows["NSE"]["day_change"] is None and rows["13PCL31"]["price"] == 110.0
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert (app_feed.app_dir(f.settings.state_dir) / app_feed.FEED_FILE).exists()


def test_a_404_symbol_is_asked_once_a_day_not_every_cycle(tmp_path, monkeypatch):
    f, asked = _feed(tmp_path, [{"symbol": "GONE"}, {"symbol": "TCS"}])
    t = datetime(2026, 10, 19, 11, 0, tzinfo=IST)
    f.tick(t)
    first = [a for a in asked if a[1] == "GONE"]
    assert first == [("prev", "GONE"), ("trend", "GONE")]
    for i in range(1, 4):                                                              # three more 5-minute cycles
        f._last = None
        f.tick(t.replace(minute=5 * i))
    assert [a for a in asked if a[1] == "GONE"] == first and sum(a[1] == "TCS" for a in asked) == 8
    clock = [f._miss["GONE"] + 86401]
    monkeypatch.setattr(app_feed.time_mod, "monotonic", lambda: clock[0])             # a day later it is tried again
    f._last = None
    f.tick(t.replace(minute=30))
    assert len([a for a in asked if a[1] == "GONE"]) == 4
