"""Replay on the raw (split-adjusted) close: levels are real prices, dividends arrive on their ex-dates."""

import json

from trading_agent.replay.engine import step
from trading_agent.replay.trial import Trial
from .replay_fakes import FakeUniverse, market, path, top_by_6m

EX = "2021-06-15"


def make(tmp_path, name="t", source=None, **kw):
    args = dict(name=name, start="2021-03-15", cash=100_000, universe="NIFTYMIDCAP150", top=3,
                dividends="reinvest", source=source or market(), universe_obj=FakeUniverse(),
                screen_fn=top_by_6m, today="2026-10-09")
    args.update(kw)
    return Trial.create(tmp_path / "replay", **args)


def source(drop_to=90.0, amount=10.0, with_div=True):
    """X trades at 100 until the ex-date, then at ``drop_to``. Yahoo's adj_close is flat at ``drop_to`` (it back-adjusts)."""
    bars = path(100, 0.0)
    for b in bars:
        b["adj_close"] = drop_to
        if b["date"] >= EX:
            b["close"] = drop_to
    m = market()
    m.bars["X"] = bars
    if with_div:
        m.divs["X"] = [{"date": EX, "amount": amount}]
    return m


def test_new_replays_are_on_the_raw_basis(tmp_path):
    t = make(tmp_path, source=source())
    assert t.data["price_basis"] == "raw" and t.prices.field == "close"
    assert t.prices.history("X")[-1]["close"] == 100.0           # the real quote, not the dividend-adjusted 90


def test_ex_date_drop_triggers_a_stop_on_the_raw_series(tmp_path):
    t = make(tmp_path, source=source(drop_to=70.0, amount=30.0))
    t.order("X", "buy", qty=10)
    t.data["auto_stop"] = True
    step(t, "2021-06-30", today="2026-10-09")
    stop = [s for s in t.data["stops"] if s["symbol"] == "X" and s["who"] == "you"]
    assert stop and stop[0]["date"] == EX and stop[0]["price"] == 70.0
    assert "X" not in {p.symbol for p in t.you.positions()}
    assert [c["amount"] for c in t.you.credits() if c["note"] == "dividend X"] == [300.0]   # held at the previous close


def test_prices_before_the_clock_ignore_a_later_dividend(tmp_path):
    with_div, without = (make(tmp_path, n, source=source(with_div=d)) for n, d in (("a", True), ("b", False)))
    for t in (with_div, without):
        t.order("X", "buy", qty=10)
        step(t, "2021-06-14", today="2026-10-09")
    assert with_div.data["equity"] == without.data["equity"]
    assert with_div.you.credits() == without.you.credits()
    assert with_div.prices.dividends("X") == []                  # the 15 June ex-date is still in the future


def test_cash_mode_credits_quantity_times_amount(tmp_path):
    t = make(tmp_path, source=source(), dividends="cash")
    t.order("X", "buy", qty=100)
    step(t, "2021-07-01", today="2026-10-09")
    assert t.you.credits() == [{"at": EX, "amount": 1000.0, "note": "dividend X"}]
    assert {p.symbol: p.qty for p in t.you.positions()}["X"] == 100


def test_reinvest_mode_buys_whole_shares_and_keeps_the_rest_as_cash(tmp_path):
    t = make(tmp_path, source=source(), dividends="reinvest")
    t.order("X", "buy", qty=100)
    cash0 = t.you.account().cash
    r = step(t, "2021-07-01", today="2026-10-09")
    row = next(d for d in r["dividends"] if d["who"] == "you")
    assert row["amount"] == 1000.0 and row["reinvested_shares"] == 11 and row["price"] == 90.0   # 1000 // 90
    assert {p.symbol: p.qty for p in t.you.positions()}["X"] == 111
    assert t.you.credits() == [{"at": EX, "amount": 1000.0, "note": "dividend X"}]
    spent = cash0 + 1000.0 - t.you.account().cash
    assert 11 * 90.0 <= spent < 1000.0                            # shares plus charges, never more than the dividend


def test_an_older_save_keeps_its_original_basis(tmp_path):
    t = make(tmp_path, source=source())
    path_ = t.root / "trial.json"
    data = json.loads(path_.read_text())
    del data["price_basis"]                                       # what a replay saved before this change looks like
    path_.write_text(json.dumps(data))
    old = Trial.load(t.root, source(), FakeUniverse(), top_by_6m)
    assert not old.raw_basis and old.prices.field == "adj_close"
    assert old.prices.history("X")[-1]["close"] == 90.0           # dividend-adjusted, as it was saved
    step(old, "2021-04-15", today="2026-10-09")                   # still steps; no dividend events in reinvest mode
    assert old.clock.today == "2021-04-15"
    assert Trial.load(t.root, source(), FakeUniverse(), top_by_6m).raw_basis is False
