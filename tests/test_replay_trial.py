import json

import pytest

from trading_agent.replay.trial import Trial, list_trials
from .replay_fakes import FakeUniverse, market, top_by_6m


def make(tmp_path, **kw):
    args = dict(name="My first", start="2021-03-01", cash=100_000, universe="NIFTYMIDCAP150", top=3,
                dividends="reinvest", source=market(), universe_obj=FakeUniverse(), screen_fn=top_by_6m,
                today="2026-10-09")
    args.update(kw)
    return Trial.create(tmp_path / "replay", **args)


def test_create_buys_the_fund_and_the_agents_picks(tmp_path):
    t = make(tmp_path)
    assert t.data["slug"] == "my-first" and t.data["benchmark"] == "MID150BEES" and t.clock.today == "2021-03-01"
    assert [p.symbol for p in t.nifty.positions()] == ["MID150BEES"]
    assert 0 <= t.nifty.account().cash < 300  # all in, less than one unit left
    assert {p.symbol for p in t.agent.positions()} == {"A", "B", "C"}  # fastest growers
    assert t.you.positions() == [] and t.you.account().cash == 100_000
    assert t.data["equity"][0]["date"] == "2021-03-01" and t.data["last_rebalance_month"] == "2021-03"
    assert [r["symbol"] for r in t.data["picks"]["rows"]] == ["A", "B", "C"]
    assert (tmp_path / "replay" / "my-first" / "trial.json").exists()


@pytest.mark.parametrize("kw, msg", [
    ({"start": "2020-12-31"}, "on or after 2021-01-04"),
    ({"start": "2026-10-09"}, "in the past"),
    ({"universe": "NIFTYIT"}, "universe"),
    ({"dividends": "sometimes"}, "dividends"),
    ({"top": 0}, "between 1 and 30"),
    ({"cash": 5_000}, "at least"),
])
def test_create_validates(tmp_path, kw, msg):
    with pytest.raises(ValueError, match=msg):
        make(tmp_path, **kw)
    assert not (tmp_path / "replay" / "my-first").exists()


def test_duplicate_name_refused(tmp_path):
    make(tmp_path)
    with pytest.raises(ValueError, match="already"):
        make(tmp_path)


def test_create_on_a_sunday_uses_fridays_close(tmp_path):
    t = make(tmp_path, start="2021-03-07")
    assert t.clock.today == "2021-03-07"
    assert t.nifty.orders()[0]["filled_avg_price"] == t.prices.price_on("MID150BEES", "2021-03-05")


def test_you_order_fills_at_the_replay_close_and_is_dated(tmp_path):
    t = make(tmp_path)
    o = t.order("d", "buy", notional=10_000)
    assert o["symbol"] == "D" and o["qty"] >= 1
    assert o["filled_avg_price"] == t.prices.latest_price("D") and o["filled_at"].startswith("2021-03-01")


def test_order_before_listing_is_refused_with_the_listing_date(tmp_path):
    t = make(tmp_path)
    before = (t.root / "you.json").read_text() if (t.root / "you.json").exists() else None
    with pytest.raises(LookupError, match="not listed until 2022-06-01"):
        t.order("NEWCO", "buy", notional=5_000)
    after = (t.root / "you.json").read_text() if (t.root / "you.json").exists() else None
    assert before == after


def test_transaction_rolls_back_files_and_clock(tmp_path):
    t = make(tmp_path)
    saved = (t.root / "trial.json").read_text()
    with pytest.raises(RuntimeError):
        with t.transaction():
            t.clock.advance_to("2021-04-01")
            t.order("D", "buy", notional=10_000)
            t.data["clock"] = "2021-04-01"
            t.save()
            raise RuntimeError("Yahoo failed for X")
    assert (t.root / "trial.json").read_text() == saved and t.clock.today == "2021-03-01"
    assert t.you.positions() == [] and t.data["clock"] == "2021-03-01"


def test_load_and_list(tmp_path):
    t = make(tmp_path)
    again = Trial.load(t.root, market(), FakeUniverse(), screen_fn=top_by_6m)
    assert again.data == json.loads((t.root / "trial.json").read_text())
    rows = list_trials(tmp_path / "replay")
    assert rows[0]["slug"] == "my-first" and rows[0]["you"] == 0.0 and rows[0]["agent"] is not None
