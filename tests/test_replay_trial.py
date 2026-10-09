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


def _files(root):
    return {f: (root / f).read_bytes() for f in Trial.FILES if (root / f).exists()}


def test_killed_mid_step_is_restored_on_load(tmp_path):
    import shutil
    t = make(tmp_path)
    before = _files(t.root)
    snap = tmp_path / "killed"
    with t.transaction():
        t.clock.advance_to("2021-04-01")
        t.order("D", "buy", notional=10_000)
        t.nifty.credit(5.0, "x", "2021-04-01")
        shutil.copytree(t.root, snap)  # the disk as it looks if the process dies right here
    assert (snap / ".pre-step" / "ok").exists() and _files(snap) != before
    again = Trial.load(snap, market(), FakeUniverse(), screen_fn=top_by_6m)
    assert _files(snap) == before and not (snap / ".pre-step").exists()
    assert again.clock.today == "2021-03-01" and again.you.positions() == []
    assert not (t.root / ".pre-step").exists()  # a normal exit cleans up


def test_killed_mid_backup_leaves_live_files_alone(tmp_path):
    t = make(tmp_path)
    before = _files(t.root)
    (t.root / ".pre-step").mkdir()
    (t.root / ".pre-step" / "you.json").write_text("half a copy")
    Trial.load(t.root, market(), FakeUniverse(), screen_fn=top_by_6m)
    assert _files(t.root) == before and not (t.root / ".pre-step").exists()


def test_kill_restore_deletes_files_the_step_created(tmp_path):
    import shutil
    t = make(tmp_path)
    (t.root / "agent.json").unlink()  # pretend a file did not exist before the step
    before = _files(t.root)
    snap = tmp_path / "killed"
    with t.transaction():
        t.agent.set_price("A", 1.0)  # creates agent.json again
        shutil.copytree(t.root, snap)
    assert (snap / "agent.json").exists()
    Trial.load(snap, market(), FakeUniverse(), screen_fn=top_by_6m)
    assert not (snap / "agent.json").exists() and _files(snap) == before


def test_buying_a_delisted_stock_is_refused_but_selling_is_not(tmp_path):
    t = make(tmp_path, start="2022-03-15")
    t.order("GONE", "buy", notional=10_000)  # still trading then
    t.clock.advance_to("2023-01-02")
    with pytest.raises(LookupError, match="GONE last traded 2022-03-31"):
        t.order("GONE", "buy", notional=10_000)
    o = t.order("GONE", "sell", qty=1)
    assert o["side"] == "sell" and o["filled_avg_price"] == t.prices.latest_price("GONE")


@pytest.mark.parametrize("sym", ["../../state", "a/b", "", "x" * 21, "A B"])
def test_order_rejects_bad_tickers(tmp_path, sym):
    t = make(tmp_path)
    with pytest.raises(ValueError, match="ticker"):
        t.order(sym, "buy", qty=1)


def test_list_trials_skips_a_malformed_trial(tmp_path):
    make(tmp_path)
    bad = tmp_path / "replay" / "bad"
    bad.mkdir()
    (bad / "trial.json").write_text("{}")
    (tmp_path / "replay" / "worse").mkdir()
    (tmp_path / "replay" / "worse" / "trial.json").write_text("[1]")
    assert [r["slug"] for r in list_trials(tmp_path / "replay")] == ["my-first"]


def test_missing_member_names_are_filled_once_from_the_company_list():
    from trading_agent.replay.trial import fill_names

    calls = []

    def lookup(symbols):
        calls.append(sorted(symbols))
        return {"OLDCO": {"name": "Old Company Limited"}, "GONE": {"name": None}}

    names = {"A": {"name": "A Limited", "industry": "x"}}
    fill_names(names, ["A", "OLDCO", "GONE"], lookup)
    assert names["OLDCO"]["name"] == "Old Company Limited" and names["A"]["name"] == "A Limited"
    assert names["GONE"]["name"] == "" and calls == [["GONE", "OLDCO"]]
    fill_names(names, ["A", "OLDCO", "GONE"], lookup)  # nothing missing now: no second lookup
    assert len(calls) == 1


def test_names_lookup_failure_is_not_fatal():
    from trading_agent.replay.trial import fill_names

    def boom(symbols):
        raise RuntimeError("NSE list unavailable")

    names = {}
    fill_names(names, ["X"], boom)
    assert names["X"]["name"] == ""
