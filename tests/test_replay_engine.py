import pytest

from trading_agent.replay.engine import step, step_target
from trading_agent.replay.trial import Trial
from .replay_fakes import FakeSource, FakeUniverse, market, path, top_by_6m


def make(tmp_path, name="t", source=None, **kw):
    args = dict(name=name, start="2021-03-15", cash=100_000, universe="NIFTYMIDCAP150", top=3,
                dividends="reinvest", source=source or market(), universe_obj=FakeUniverse(),
                screen_fn=top_by_6m, today="2026-10-09")
    args.update(kw)
    return Trial.create(tmp_path / "replay", **args)


def test_step_target():
    assert step_target("2021-03-15", "week", "2026-10-09") == "2021-03-22"
    assert step_target("2021-03-15", "month", "2026-10-09") == "2021-04-15"
    assert step_target("2021-03-15", "year", "2026-10-09") == "2022-03-15"
    with pytest.raises(ValueError):
        step_target("2021-03-15", "decade", "2026-10-09")


def test_month_step_records_every_day_and_rebalances_once(tmp_path):
    t = make(tmp_path)
    r = step(t, "2021-04-15", today="2026-10-09")
    assert t.clock.today == "2021-04-15" and t.data["clock"] == "2021-04-15"
    dates = [p["date"] for p in t.data["equity"]]
    assert dates[0] == "2021-03-15" and dates[1] == "2021-03-16" and dates[-1] == "2021-04-15"
    assert r["days"] == len(dates) - 1 and r["rebalances"] == ["2021-04-01"]
    assert [x["date"] for x in t.data["rebalances"]] == ["2021-03-15", "2021-04-01"]
    assert t.data["picks"]["date"] == "2021-04-15"
    assert t.nifty.positions()[0].current_price == t.prices.latest_price("MID150BEES")


def test_one_year_equals_twelve_months(tmp_path):
    a, b = make(tmp_path, "a"), make(tmp_path, "b")
    step(a, "2022-03-15", today="2026-10-09")
    for _ in range(12):
        step(b, step_target(b.clock.today, "month", "2026-10-09"), today="2026-10-09")
    assert a.data["equity"] == b.data["equity"]
    assert [x["date"] for x in a.data["rebalances"]] == [x["date"] for x in b.data["rebalances"]]


def test_agent_stop_sells_on_the_day_of_the_fall(tmp_path):
    bars = path(100, 0.002)
    for b in bars:
        if b["date"] >= "2021-04-06":
            b["close"] = b["adj_close"] = 60.0  # a crash on 6 April
    src = market()
    src.bars["A"] = bars
    t = make(tmp_path, source=src)
    assert "A" in {p.symbol for p in t.agent.positions()}
    step(t, "2021-04-15", today="2026-10-09")
    assert t.data["stops"][0]["date"] == "2021-04-06" and t.data["stops"][0]["symbol"] == "A"
    assert t.data["stops"][0]["who"] == "agent"


def test_your_stops_only_with_auto_sell(tmp_path):
    src = market()
    src.bars["D"] = [dict(b, close=60.0, adj_close=60.0) if b["date"] >= "2021-03-20" else b for b in src.bars["D"]]
    t = make(tmp_path, source=src)
    t.order("D", "buy", qty=10)
    step(t, "2021-03-25", today="2026-10-09")
    assert "D" in {p.symbol for p in t.you.positions()}
    t.data["auto_stop"] = True
    step(t, "2021-03-26", today="2026-10-09")
    assert "D" not in {p.symbol for p in t.you.positions()}


def test_dividends_reinvest_and_cash_agree(tmp_path):
    def src():
        bars = path(100, 0.0)
        for b in bars:
            if b["date"] < "2021-06-15":
                b["adj_close"] = 90.0
            else:
                b["close"] = b["adj_close"] = 90.0
        m = market()
        m.bars["X"] = bars
        m.divs["X"] = [{"date": "2021-06-15", "amount": 10.0}]
        return m
    totals = {}
    for mode in ("reinvest", "cash"):
        t = make(tmp_path, mode, source=src(), dividends=mode)
        t.order("X", "buy", qty=10)
        step(t, "2021-07-01", today="2026-10-09")
        totals[mode] = t.you.account().equity
        if mode == "cash":
            assert t.you.credits() == [{"at": "2021-06-15", "amount": 100.0, "note": "dividend X"}]
    assert abs(totals["reinvest"] - totals["cash"]) < 20  # the reinvestment's own purchase charges


def test_failed_step_leaves_the_trial_unchanged(tmp_path):
    t = make(tmp_path)
    before = (t.root / "trial.json").read_text()

    def boom(members, prices, top):
        raise LookupError("Yahoo returned no history for B")
    t.screen_fn = boom
    with pytest.raises(LookupError):
        step(t, "2021-04-15", today="2026-10-09")
    assert (t.root / "trial.json").read_text() == before and t.clock.today == "2021-03-15"


def test_step_is_capped_at_today(tmp_path):
    t = make(tmp_path, start="2026-03-02")
    r = step(t, step_target("2026-03-02", "year", "2026-10-09"), today="2026-10-09")
    assert r["to"] == "2026-10-09" and t.clock.today == "2026-10-09"


def test_ended_trial_cannot_step(tmp_path):
    t = make(tmp_path)
    t.data["ended"] = "2021-03-15"
    with pytest.raises(ValueError, match="ended"):
        step(t, "2021-04-15", today="2026-10-09")


def test_a_price_source_failure_aborts_the_step_and_changes_nothing(tmp_path):
    class Flaky(FakeSource):
        down = False

        def history(self, symbol, range_="2y"):
            if self.down and symbol.upper() == "B":
                raise ConnectionError("Yahoo is down")
            return super().history(symbol, range_)
    def tolerant_screen(members, prices, top):  # like the real screen: a symbol that fails is skipped
        ok = []
        for m in members:
            try:
                prices.history(m["symbol"], "1y")
                ok.append(m)
            except Exception:  # noqa: BLE001
                pass
        return top_by_6m(ok, prices, top)
    src = Flaky(market().bars)
    t = make(tmp_path, source=src, screen_fn=tolerant_screen)
    assert "B" in {p.symbol for p in t.agent.positions()}
    before = {f: (t.root / f).read_bytes() for f in Trial.FILES if (t.root / f).exists()}
    t.prices._bars.pop("B"), t.prices._dates.pop("B")  # as after a restart: B must be fetched again
    src.down = True
    with pytest.raises(RuntimeError, match="Price data could not be loaded for B.*nothing was changed"):
        step(t, "2021-03-26", today="2026-10-09")
    assert {f: (t.root / f).read_bytes() for f in Trial.FILES if (t.root / f).exists()} == before
    assert t.clock.today == "2021-03-15" and not (t.root / ".pre-step").exists()
    src.down = False
    assert step(t, "2021-03-26", today="2026-10-09")["to"] == "2021-03-26"


def test_a_member_the_source_does_not_know_is_skipped_not_fatal(tmp_path):
    class Delisted(FakeSource):
        def history(self, symbol, range_="2y"):
            if symbol.upper() == "OLDCO":
                raise LookupError("Yahoo has no data for OLDCO.NS (HTTP 404)")
            return super().history(symbol, range_)

    class Uni(FakeUniverse):
        def __init__(self):
            super().__init__(("A", "B", "C", "D", "E", "OLDCO"))

    def screen(members, prices, top):
        ok = []
        for m in members:
            try:
                prices.history(m["symbol"], "1y")
                ok.append(m)
            except LookupError:
                pass
        return top_by_6m(ok, prices, top)
    t = make(tmp_path, source=Delisted(market().bars), universe_obj=Uni(), screen_fn=screen)
    r = step(t, "2021-04-15", today="2026-10-09")
    assert r["to"] == "2021-04-15" and t.clock.today == "2021-04-15"
