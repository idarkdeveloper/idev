"""Day brake (live account down 3.5% from the previous close: no buys, no rebalancing for the session) and the
clearing-holiday cash rule (T-1 sale proceeds are not cash on an NSE clearing holiday). Fake Groww, fake prices and the
real NSE clearing-holiday answer (tests/fixtures/nse_clearing_holidays_sample.json). No network, no real order."""
import json
from datetime import date, datetime
from pathlib import Path

import pytest

from trading_agent import clearing, day_brake, health, runner
from trading_agent.agent import AgentContext, RunResult, build_tools
from trading_agent.broker import LocalPaperBroker, Position
from trading_agent.groww import GrowwBroker
from trading_agent.state import State
from trading_agent.timezones import IST

from .conftest import FakeSession

CLEAR = json.loads((Path(__file__).parent / "fixtures" / "nse_clearing_holidays_sample.json").read_text())
TUE = date(2026, 10, 20)      # an NSE clearing holiday in the real 2026 list
MON = date(2026, 10, 19)
WED = date(2026, 10, 21)


def t(d, h, m=0):
    return datetime(d.year, d.month, d.day, h, m, tzinfo=IST)


def ok(payload):
    return {"status": "SUCCESS", "payload": payload}


class FakeAccount:
    name, live_orders = "groww", True

    def __init__(self, positions, cash):
        self.pos, self.cash = positions, cash

    def positions(self):
        return self.pos

    def available_cash(self):
        return self.cash


class Prices:
    def __init__(self, closes):
        self.closes = closes

    def history(self, symbol, range_):
        return [{"date": "2026-10-16", "close": self.closes[symbol]}, {"date": "2026-10-19", "close": 1.0}]


# --------------------------------------------------------------------------- #
# day brake
# --------------------------------------------------------------------------- #
def test_measure_values_the_account_against_the_previous_close():
    pos = [Position("A", 10, 90.0, current_price=95.0), Position("B", 5, 50.0, current_price=40.0),
           Position("C", 3, 10.0, current_price=11.0)]                  # C: no previous close -> counts flat
    m = day_brake.measure(pos, 1000.0, {"A": 100.0, "B": 50.0}.get)
    assert m == {"value": 2183.0, "prev_value": 2283.0, "fall_pct": 4.38}
    assert day_brake.prev_close_fn(Prices({"A": 100.0}), MON)("A") == 100.0      # the last bar BEFORE today


def test_the_brake_trips_once_alerts_once_and_lifts_next_session(settings):
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    alerts = []

    def alert(key, subject, body):
        alerts.append((key, subject))
    acct = FakeAccount([Position("A", 10, 90.0, current_price=98.0)], 1000.0)
    prev = {"A": 100.0}.get
    assert day_brake.evaluate(settings, acct, prev, t(MON, 10), alert=alert) is None            # -1.0%
    acct.pos = [Position("A", 10, 90.0, current_price=96.0)]                                    # -2.0%: 40 of 2000
    assert day_brake.evaluate(settings, acct, prev, t(MON, 10, 5), alert=alert) is None
    acct.pos = [Position("A", 10, 90.0, current_price=93.0)]                                    # -3.5%
    rec = day_brake.evaluate(settings, acct, prev, t(MON, 10, 10), alert=alert)
    assert rec["fall_pct"] == 3.5 and rec["date"] == "2026-10-19" and alerts[0][0] == "day-brake"
    acct.pos = [Position("A", 10, 90.0, current_price=99.0)]                                    # recovered: still on
    assert day_brake.evaluate(settings, acct, prev, t(MON, 14), alert=alert)["fall_pct"] == 3.5
    assert len(alerts) == 1
    assert "no new buys or rebalancing" in day_brake.reason(settings.state_dir, t(MON, 15))
    assert day_brake.reason(settings.state_dir, t(TUE, 9, 20)) is None                          # next session


def test_the_brake_is_only_read_in_market_hours_and_never_raises(settings):
    class Broken(FakeAccount):
        def positions(self):
            raise RuntimeError("Groww down")
    acct = FakeAccount([Position("A", 10, 90.0, current_price=50.0)], 0.0)
    assert day_brake.evaluate(settings, acct, {"A": 100.0}.get, t(MON, 9, 10)) is None          # before the open
    assert day_brake.evaluate(settings, Broken([], 0), {"A": 100.0}.get, t(MON, 11)) is None


def test_under_the_brake_live_buys_are_refused_and_the_agent_does_not_rebalance(settings, monkeypatch):
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    settings.auto_trade = True
    monkeypatch.setattr(day_brake, "_now", lambda: t(MON, 11))
    acct = FakeAccount([Position("A", 10, 90.0, current_price=90.0)], 0.0)
    assert day_brake.evaluate(settings, acct, {"A": 100.0}.get)["fall_pct"] == 10.0
    assert "day brake" in runner._buy_block(settings)
    sess = FakeSession({})
    ctx = AgentContext(settings=settings, broker=GrowwBroker("tok", session=sess, live_orders=True), data=None,
                       notifier=None, state=State(settings.state_dir / "state.json"),
                       result=RunResult(investor="x", new_trades=[]))
    tool = next(t for t in build_tools(ctx) if t.name == "place_paper_order")
    for side in ("buy", "sell"):
        out = json.loads(tool.call({"symbol": "RELIANCE", "side": side, "notional_usd": 1000}))
        assert "day brake" in out["error"]
    assert sess.calls == []


def test_the_brake_record_is_read_by_date(settings, monkeypatch):
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    (settings.state_dir / "day_brake.json").write_text(json.dumps({"brake": {"date": "2026-10-19", "fall_pct": 4.0}}))
    monkeypatch.setattr(day_brake, "_now", lambda: t(MON, 11))
    assert "down 4.0%" in runner._buy_block(settings)
    monkeypatch.setattr(day_brake, "_now", lambda: t(TUE, 11))
    assert runner._buy_block(settings) is None


def test_the_watch_checks_the_live_account(settings):
    from trading_agent.watch import Watcher
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    acct = FakeAccount([Position("A", 10, 90.0, current_price=90.0)], 0.0)
    w = Watcher(settings, broker=acct, prices=Prices({"A": 100.0}), awake=None)
    assert w.check_day_brake(t(MON, 11))["fall_pct"] == 10.0
    paper = Watcher(settings, broker=LocalPaperBroker(settings.state_dir / "pb.json"), awake=None)
    assert paper.check_day_brake(t(TUE, 11)) is None                                          # live account only


def test_the_strip_shows_the_brake():
    ui = Path(__file__).resolve().parents[1] / "trading_agent" / "ui"
    js = (ui / "common.js").read_text(encoding="utf-8")
    assert 'kind: "brake"' in js and "GTT stops still act" in js
    assert "brake: S.day_brake" in (ui / "index.html").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# clearing holidays
# --------------------------------------------------------------------------- #
class FakeNSE:
    def __init__(self, payload):
        self.payload, self.calls = payload, []

    def _get(self, path, params=None, referer=None):
        self.calls.append((path, params))
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


def test_the_real_list_parses_and_differs_from_trading_holidays():
    days = clearing.parse(CLEAR)
    assert days["2026-04-01"] == "Annual Bank Closing" and len(days) == 20


def test_the_list_is_fetched_once_a_year_and_retried_hourly_after_a_failure(tmp_path):
    ch = clearing.ClearingHolidays(tmp_path)
    nse = FakeNSE(CLEAR)
    assert ch.tick(nse, t(MON, 8)) == 20 and nse.calls == [("api/holiday-master", {"type": "clearing"})]
    assert ch.tick(nse, t(TUE, 8)) is None and len(nse.calls) == 1
    assert ch.status(TUE) is True and ch.status(WED) is False and ch.status(date(2027, 1, 4)) is None
    assert health.read_runs(tmp_path)["clearing"]["rows"] == 20
    other = clearing.ClearingHolidays(tmp_path / "x")
    assert other.tick(FakeNSE({"CM": []}), t(MON, 8)) is None
    assert not other.due(t(MON, 8, 30))                                                   # within the hour


def test_t1_proceeds_are_held_back_only_on_a_clearing_holiday(tmp_path):
    clearing.ClearingHolidays(tmp_path).tick(FakeNSE(CLEAR), t(MON, 8))
    sells = [(MON, 5000.0), (date(2026, 10, 16), 7000.0)]
    amount, why = clearing.hold(tmp_path, sells, TUE)
    assert amount == 5000.0 and "today is an NSE clearing holiday" in why and "19 Oct" in why
    assert clearing.hold(tmp_path, [(TUE, 5000.0)], WED) == (0.0, "")                       # an ordinary day
    amount, why = clearing.hold(tmp_path / "unknown", sells, WED)
    assert amount == 0.0                                                                  # no T-1 sell on 20 Oct
    amount, why = clearing.hold(tmp_path / "unknown", [(TUE, 300.0)], WED)
    assert amount == 300.0 and "list is unknown" in why                                   # fail safe


def test_a_practice_buy_is_never_sized_from_unsettled_money(settings, monkeypatch):
    settings.market = "in"
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    clearing.ClearingHolidays(settings.state_dir).tick(FakeNSE(CLEAR), t(MON, 8))
    stamp = ["2026-10-19T05:00:00+00:00"]                                                  # Monday 10:30 IST
    b = LocalPaperBroker(settings.state_dir / "paper_broker.json", starting_cash=10_000, price_fn=lambda s: 100.0,
                         whole_shares=True, now_fn=lambda: stamp[0], cash_hold=clearing.practice_cash_hold(settings))
    b.submit_order("AAA", "buy", qty=90)
    b.submit_order("AAA", "sell", qty=90)                                                  # 9,000 back, sold Monday
    monkeypatch.setattr(clearing, "_now", lambda: t(TUE, 10))                               # Tuesday: clearing holiday
    with pytest.raises(ValueError, match="clearing holiday"):
        b.submit_order("BBB", "buy", qty=20)                                               # 2,000 > 1,000 settled
    assert b.submit_order("BBB", "buy", qty=9)["status"] == "filled"
    monkeypatch.setattr(clearing, "_now", lambda: t(WED, 10))
    assert b.submit_order("BBB", "buy", qty=50)["status"] == "filled"                       # settled now


def test_the_practice_account_from_the_runner_carries_the_rule(settings):
    settings.market = "in"
    assert runner.make_practice_broker(settings, price_fn=lambda s: 1.0).cash_hold is not None
    settings.market = "us"
    assert runner.make_practice_broker(settings, price_fn=lambda s: 1.0).cash_hold is None


def test_a_live_buy_is_never_sized_from_unsettled_money(settings, monkeypatch):
    settings.auto_trade = True
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    clearing.ClearingHolidays(settings.state_dir).tick(FakeNSE(CLEAR), t(MON, 8))
    monkeypatch.setattr(clearing, "_now", lambda: t(TUE, 10))
    st = State(settings.state_dir / "state.json")
    st.data["live_orders"] = [{"side": "sell", "filled_quantity": 110, "average_fill_price": 900.0,
                               "placed_at": "2026-10-19T11:00:00+05:30", "status": "filled", "groww_order_id": "G0"}]
    sess = FakeSession({("GET", "/margins/detail/user"): ok({"clear_cash": 100000.0}),
                        ("GET", "/holdings/user"): ok({"holdings": []})})
    ctx = AgentContext(settings=settings, broker=GrowwBroker("tok", session=sess, live_orders=True), data=None,
                       notifier=None, state=st, result=RunResult(investor="x", new_trades=[]))
    tool = next(t for t in build_tools(ctx) if t.name == "place_paper_order")
    out = json.loads(tool.call({"symbol": "RELIANCE", "side": "buy", "notional_usd": 5000}))   # under the 10% cap
    assert out["error"].startswith("only 1000.00 settled cash for this buy (today is an NSE clearing holiday")
    assert sess.writes() == []


def test_the_health_row(settings):
    settings.market = "in"
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    row = {r["id"]: r for r in health.report(settings, t(MON, 11))["sources"]}["clearing"]
    assert row["status"] == "late" and "every day counts as a clearing holiday" in row["detail"]
    clearing.ClearingHolidays(settings.state_dir).tick(FakeNSE(CLEAR), t(MON, 8))
    row = {r["id"]: r for r in health.report(settings, t(MON, 11))["sources"]}["clearing"]
    assert row["status"] == "ok" and row["rows"] == 20
