"""Replay end-of-replay summary, Position size and Trade cost routes."""
import json
import re
import time

import pytest

from trading_agent.costs import cost_model_for
from trading_agent.replay.summary import _names, build_summary
from trading_agent.replay.web import ReplayApp
from trading_agent.ui import App

from .replay_fakes import FakeUniverse, market, top_by_6m
from .test_replay_web import FakeNews, create


@pytest.fixture
def rapp(settings):
    settings.market = "in"
    app = App(settings, dotenv=None)
    src = market()
    r = ReplayApp(app, source=src, universe_factory=lambda name: FakeUniverse(), news_client=FakeNews(),
                  client_factory=None, today_fn=lambda: "2026-10-09", screen_fn=top_by_6m)
    app._replay = r
    return app, r, src


def step(app, r, slug, by="month"):
    st, _ = r.route("POST", f"/replay/api/trial/{slug}/step", {}, {"by": by})
    assert st == 202
    job = app.jobs[-1]
    for _ in range(3000):
        if job.finished_at:
            break
        time.sleep(0.02)
    assert job.ok, job.message


def order(r, slug, sym, side, qty):
    st, o = r.route("POST", f"/replay/api/trial/{slug}/order", {}, {"symbol": sym, "side": side, "qty": qty})
    assert st == 200, o


def text(s):
    return " ".join(l["text"] for l in s["lines"]) + " " + s["bottom"]


def test_no_summary_before_the_first_step(rapp):
    app, r, _ = rapp
    slug = create(app, r)
    assert r.snapshot(slug)["summary"] is None


def test_you_ahead_of_agent_and_index(rapp):
    app, r, _ = rapp
    slug = create(app, r, top=2)
    order(r, slug, "A", "buy", 400)  # A is the fastest grower in the fake market
    step(app, r, slug, "month")
    tr = r.trial(slug)
    tr.data["equity"].append({"date": tr.clock.today, "you": 121_105, "agent": 101_078, "nifty": 102_200})
    s = r.snapshot(slug)["summary"]
    t = text(s)
    assert s["title"] == "Summary so far" and not s["ended"]
    assert ("You made +21.1% (₹21,105), the agent's rules made +1.1% (₹1,078), MID150BEES made +2.2% (₹2,200). "
            "You beat both.") in t
    assert s["bottom"] == "Your picks did better over this period; one period is not proof of skill."
    assert any(l["label"] == "Holdings compared" for l in s["lines"])
    rows = {x["symbol"]: x for x in s["table"]}
    assert rows["A"]["you"]["qty"] == 400 and rows["A"]["you"]["pl"] > 0
    assert any(x["agent"] for x in s["table"])


def test_agent_ahead_and_ended_title(rapp):
    app, r, _ = rapp
    slug = create(app, r, top=2)
    order(r, slug, "E", "buy", 150)  # E falls in the fake market
    step(app, r, slug, "year")
    r.route("POST", f"/replay/api/trial/{slug}/end", {}, {})
    s = r.snapshot(slug)["summary"]
    assert s["title"] == "Final summary" and s["ended"]
    assert "did better than" in s["bottom"] and "trailed" in text(s)


def test_both_losing_says_lost_not_made(rapp):
    app, r, _ = rapp
    slug = create(app, r, top=1)
    order(r, slug, "E", "buy", 100)
    t = r.trial(slug)
    t.data["equity"].append({"date": t.clock.today, "you": 95_000, "agent": 97_000, "nifty": 101_000})
    s = build_summary(t)
    txt = text(s)
    assert "You lost 5.0% (₹5,000)" in txt and "the agent's rules lost 3.0% (₹3,000)" in txt
    assert "Neither your picks nor the agent's rules are in profit" in s["bottom"]


def test_no_trades_yet_and_all_cash(rapp):
    app, r, _ = rapp
    slug = create(app, r, top=2)
    step(app, r, slug, "week")
    s = r.snapshot(slug)["summary"]
    assert "You have not placed a trade yet" in text(s)
    assert "Place a trade of your own" in s["bottom"]
    assert any("only the agent's" in l["text"] for l in s["lines"] if l["label"] == "Holdings compared")
    assert all(x["you"] is None for x in s["table"])


def test_holdings_overlap_sets_and_name_cap(rapp):
    app, r, _ = rapp
    slug = create(app, r, top=3)
    t = r.trial(slug)
    step(app, r, slug, "week")
    agent_syms = {p.symbol for p in t.agent.positions()}
    mine = sorted(agent_syms)[0]
    order(r, slug, mine, "buy", 1)
    other = next(s for s in "ABCDE" if s not in agent_syms)
    order(r, slug, other, "buy", 1)
    s = build_summary(t)
    held = next(l["text"] for l in s["lines"] if l["label"] == "Holdings compared")
    assert f"in both: {mine}" in held and f"only yours: {other}" in held
    assert "only the agent's:" in held
    assert _names([str(i) for i in range(11)]) == "0, 1, 2, 3, 4, 5, 6, 7 +3 more"


def test_agent_stop_exits_are_counted_only_from_the_log(rapp):
    app, r, _ = rapp
    slug = create(app, r, top=2)
    t = r.trial(slug)
    step(app, r, slug, "week")
    t.data["stops"] = [{"date": "2021-03-20", "who": "agent", "symbol": "A", "qty": 1, "price": 1, "stop": 1},
                       {"date": "2021-03-21", "who": "you", "symbol": "B", "qty": 1, "price": 1, "stop": 1},
                       {"date": "2021-03-22", "who": "agent", "symbol": "C", "error": "x"}]
    del t.data["agent_stop_count"]  # an older save: fall back to the log, worded "at least"
    assert "railing stop sold at least 1 position" in text(build_summary(t))


def test_summary_never_uses_prices_after_the_clock(rapp):
    app, r, src = rapp
    slug = create(app, r, top=2)
    order(r, slug, "A", "buy", 100)
    step(app, r, slug, "month")
    before = json.dumps(r.snapshot(slug)["summary"], sort_keys=True)
    for sym in ("A", "B", "C", "MID150BEES"):  # poison every bar after the replay date
        src.bars[sym] = [dict(b, close=b["close"] * 10, adj_close=b["adj_close"] * 10) if b["date"] > "2021-04-15" else b
                         for b in src.bars[sym]]
    r._trials.clear()
    assert json.dumps(r.snapshot(slug)["summary"], sort_keys=True) == before
    assert max(re.findall(r"\d{4}-\d{2}-\d{2}", before)) <= "2021-04-15"


# -- Position size and Trade cost -------------------------------------------------------------

def test_size_uses_replay_equity_and_clocked_prices(rapp):
    app, r, src = rapp
    slug = create(app, r)
    st, a = r.route("GET", f"/replay/api/trial/{slug}/size", {"ticker": "A"}, None)
    assert st == 200 and a["equity"] == 100_000 and a["today"] == "2021-03-15"
    assert a["qty"] > 0 and a["notional"] <= 10_000 and a["round_trip_cost"]["model"] == "india_delivery"
    for sym in src.bars:  # prices after the clock must not matter
        src.bars[sym] = [dict(b, close=b["close"] * 5, adj_close=b["adj_close"] * 5) if b["date"] > "2021-03-15" else b
                         for b in src.bars[sym]]
    r._trials.clear()
    st, b = r.route("GET", f"/replay/api/trial/{slug}/size", {"ticker": "A"}, None)
    assert st == 200 and b == a
    order(r, slug, "B", "buy", 10)  # the account is the replay's own
    st, c = r.route("GET", f"/replay/api/trial/{slug}/size", {"ticker": "A", "max_pct": "5"}, None)
    assert c["equity"] < 100_000 and c["max_notional"] == round(c["equity"] * 0.05, 2)


@pytest.mark.parametrize("q", [{}, {"ticker": "A", "risk_pct": "abc"}, {"ticker": "A", "risk_pct": "0"},
                               {"ticker": "A", "max_pct": "150"}, {"ticker": "A", "max_pct": "nan"},
                               {"ticker": "NOSUCH"}, {"ticker": "A$"}])
def test_size_bad_input_is_400(rapp, q):
    app, r, _ = rapp
    slug = create(app, r)
    st, body = r.route("GET", f"/replay/api/trial/{slug}/size", q, None)
    assert st == 400 and "error" in body


def test_cost_matches_costs_module(rapp):
    app, r, _ = rapp
    slug = create(app, r)
    st, c = r.route("GET", f"/replay/api/trial/{slug}/cost", {"amount": "25000"}, None)
    rt = cost_model_for("in").round_trip(25000)
    assert st == 200 and c["charges"] == rt["charges"] and c["buy"] == rt["buy"] and c["total_bps"] == rt["total_bps"]
    assert c == app.cost_quote(25000)  # the same numbers Live shows


@pytest.mark.parametrize("amount", ["", "0", "-5", "abc", "nan", "inf", "1e12"])
def test_cost_bad_input_is_400(rapp, amount):
    app, r, _ = rapp
    slug = create(app, r)
    st, body = r.route("GET", f"/replay/api/trial/{slug}/cost", {"amount": amount}, None)
    assert st == 400 and "error" in body


def test_size_cost_unknown_replay_is_404(rapp):
    app, r, _ = rapp
    assert r.route("GET", "/replay/api/trial/nope/cost", {"amount": "100"}, None)[0] == 404
    assert r.route("GET", "/replay/api/trial/nope/size", {"ticker": "A"}, None)[0] == 404


# -- fix round 1 ---------------------------------------------------------------------------------

def _fake_curve(r, slug, you, agent, nifty):
    t = r.trial(slug)
    t.data["equity"].append({"date": t.clock.today, "you": you, "agent": agent, "nifty": nifty})
    return t


def test_running_totals_survive_trimmed_lists_and_old_saves_say_at_least(rapp):
    app, r, _ = rapp
    slug = create(app, r, top=2)
    order(r, slug, "A", "buy", 5)
    step(app, r, slug, "week")
    t = r.trial(slug)
    assert t.data["rebalance_count"] == 0  # the opening buy is not counted
    t.data["rebalance_count"], t.data["agent_stop_count"] = 150, 7
    t.data["rebalances"] = t.data["rebalances"][-1:]  # trimmed lists must not matter
    t.data["stops"] = []
    txt = text(build_summary(t))
    assert "rebalanced 150 times" in txt and "sold 7 positions" in txt and "at least" not in txt
    del t.data["rebalance_count"], t.data["agent_stop_count"]
    t.data["rebalances"] = [{}, {}, {}]
    t.data["stops"] = [{"who": "agent", "symbol": "A"}]
    txt = text(build_summary(t))
    assert "at least 2 times" in txt and "at least 1 position" in txt


def test_ties_name_both_and_match_both(rapp):
    app, r, _ = rapp
    slug = create(app, r)
    order(r, slug, "A", "buy", 1)
    step(app, r, slug, "week")
    t = _fake_curve(r, slug, 105_000, 105_000, 105_000)
    assert "You matched both." in text(build_summary(t))
    t.data["equity"][-1]["agent"] = 105_040  # inside the 0.1% shown
    assert "You matched both." in text(build_summary(t))
    t.data["equity"][-1].update(you=105_000, agent=104_000, nifty=104_000)
    assert "You beat both." in text(build_summary(t))
    t.data["equity"][-1].update(you=105_000, agent=106_000, nifty=106_000)
    assert "You trailed both." in text(build_summary(t))
    t.data["equity"][-1].update(you=105_000, agent=105_000, nifty=104_000)
    assert "You beat MID150BEES and matched the agent's rules." in text(build_summary(t))
    t.data["equity"][-1].update(you=105_000, agent=105_000, nifty=106_000)
    assert "You matched the agent's rules and trailed MID150BEES." in text(build_summary(t))


def test_ended_without_own_trades_does_not_ask_for_a_trade(rapp):
    app, r, _ = rapp
    slug = create(app, r)
    step(app, r, slug, "week")
    r.route("POST", f"/replay/api/trial/{slug}/end", {}, {})
    s = r.snapshot(slug)["summary"]
    assert "Place a trade" not in text(s)
    assert s["bottom"].startswith("You did not trade in this replay; the agent's rules ")
    line = next(l["text"] for l in s["lines"] if l["label"] == "Stocks that made money")
    assert line.startswith("You have not traded yet; the agent: ") and "none for you" not in line


def test_charges_larger_than_gain_has_no_huge_percentage(rapp):
    app, r, _ = rapp
    slug = create(app, r)
    order(r, slug, "A", "buy", 1)
    step(app, r, slug, "week")
    t = _fake_curve(r, slug, 100_001, 100_001, 100_001)
    costs = next(l["text"] for l in build_summary(t)["lines"] if l["label"] == "Costs")
    assert "larger than the gain" in costs and "% of the gain" not in costs.split("the agent made")[0]


def test_size_falls_back_without_history_resolves_names_and_warns(rapp):
    app, r, src = rapp
    slug = create(app, r)
    app.resolve = lambda text: ("A", "A Limited")
    st, a = r.route("GET", f"/replay/api/trial/{slug}/size", {"ticker": "a limited"}, None)
    assert st == 200 and a["ticker"] == "A" and a["name_today"] == "A Limited" and "warning" not in a
    t = r.trial(slug)
    real = t.prices.history

    def flaky(sym, rng="2y"):
        if rng == "1y":
            raise RuntimeError("boom")
        return real(sym, rng)
    t.prices.history = flaky
    st, b = r.route("GET", f"/replay/api/trial/{slug}/size", {"ticker": "A"}, None)
    assert st == 200 and b["atr"] is None and "half of max" in b["basis"]
    t.prices.history = real
    t.prices.last_trade_date = lambda sym: "2021-01-01"
    st, c = r.route("GET", f"/replay/api/trial/{slug}/size", {"ticker": "A"}, None)
    assert st == 200 and "suspended or delisted" in c["warning"]


def test_old_save_stepping_stays_at_least_and_new_replays_are_exact(rapp):
    app, r, _ = rapp
    slug = create(app, r, top=2)
    t = r.trial(slug)
    assert t.data["counts_exact"] is True
    order(r, slug, "A", "buy", 5)
    step(app, r, slug, "week")
    assert "at least" not in text(build_summary(t))
    for k in ("rebalance_count", "agent_stop_count", "counts_exact"):  # make it an old save
        t.data.pop(k)
    t.data["rebalances"] = [{}, {}]
    t.data["stops"] = [{"who": "agent", "symbol": "A"}]
    step(app, r, slug, "month")
    t = r.trial(slug)
    assert t.data["counts_exact"] is False and t.data["rebalance_count"] >= 2 and t.data["agent_stop_count"] >= 1
    assert "at least" in text(build_summary(t))


def test_size_unlisted_on_the_date_is_400_and_name_is_labelled_today(rapp):
    app, r, _ = rapp
    slug = create(app, r)
    app.resolve = lambda text: ("NEWCO", "Newco Ltd")  # listed only from 2022-06
    st, body = r.route("GET", f"/replay/api/trial/{slug}/size", {"ticker": "newco"}, None)
    assert st == 400 and "not listed on the replay date" in body["error"]
    app.resolve = lambda text: ("A", "A Limited")
    st, a = r.route("GET", f"/replay/api/trial/{slug}/size", {"ticker": "A"}, None)
    assert st == 200 and a["name_today"] == "A Limited" and "name" not in a


def test_size_malformed_last_trade_date_warns_not_500(rapp):
    app, r, _ = rapp
    slug = create(app, r)
    r.trial(slug).prices.last_trade_date = lambda sym: "not-a-date"
    st, a = r.route("GET", f"/replay/api/trial/{slug}/size", {"ticker": "A"}, None)
    assert st == 200 and "could not be read" in a["warning"]


def test_cost_route_is_busy_guarded(rapp):
    app, r, _ = rapp
    slug = create(app, r)
    lk = r._trial_lock(slug)
    lk.acquire()
    try:
        assert r.route("GET", f"/replay/api/trial/{slug}/cost", {"amount": "1000"}, None)[0] == 409
    finally:
        lk.release()
