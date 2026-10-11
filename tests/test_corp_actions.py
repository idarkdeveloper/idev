"""Corporate-action guard and normaliser. The NSE answers are built from real rows (tests/fixtures/
nse_corporate_actions_sample.json, fetched from api/corporates-corporateActions) with their ex-dates moved; Groww is
a fake that records every call. No network, no real order."""
import json
from datetime import date, datetime
from pathlib import Path

import pytest

from trading_agent import health
from trading_agent.broker import LocalPaperBroker, Position
from trading_agent.corp_actions import ActionBook, normalise_all, parse_actions, parse_subject, session_before
from trading_agent.digest_render import _flags
from trading_agent.live import GttStopManager
from trading_agent.state import State
from trading_agent.timezones import IST

FIX = json.loads((Path(__file__).parent / "fixtures" / "nse_corporate_actions_sample.json").read_text())
EX = date(2026, 10, 19)                                   # a Monday
FRI = date(2026, 10, 16)


def t(d, h, m=0):
    return datetime(d.year, d.month, d.day, h, m, tzinfo=IST)


def rows_with_ex(ex=EX, **override):
    """The real rows, every ex-date moved to ``ex`` (or per symbol via override)."""
    out = []
    for r in FIX:
        d = override.get(r["symbol"], ex)
        out.append({**r, "exDate": d.strftime("%d-%b-%Y")})
    return out


class FakeNSE:
    def __init__(self, payload):
        self.payload, self.calls = payload, []

    def _get(self, path, params=None, referer=None):
        self.calls.append((path, params, referer))
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


def stored_book(tmp_path, now=None, rows=None):
    now = now or t(FRI, 9)
    book = ActionBook(tmp_path, now_fn=lambda: now)
    book.store(parse_actions(rows or rows_with_ex()), now)
    return book


# --------------------------------------------------------------------------- #
# parsing the real subjects
# --------------------------------------------------------------------------- #
def test_the_real_subject_lines_parse_to_ratios_and_dividends():
    got = {r["symbol"]: r for r in parse_actions(FIX)}
    assert got["SURYAROSNI"]["ratio"] == 2.0 and got["SURYAROSNI"]["label"] == "bonus 1:1"
    assert got["KOTYARK"]["ratio"] == 11.0                                   # 10 new for every 1 held
    assert got["SHRIRAMFIN"]["ratio"] == 5.0 and "split ₹10 → ₹2" in got["SHRIRAMFIN"]["label"]
    assert got["VERTOZ"]["ratio"] == 0.1                                     # Re 1 -> Rs 10: fewer shares
    assert got["INDOBORAX"]["special"] and got["INDOBORAX"]["dividend"] == 40.0 and got["INDOBORAX"]["ratio"] is None
    assert got["HCLTECH"]["dividend"] == 18.0                                # interim 12 + special 6
    assert got["ITC"]["demerger"] and got["ITC"]["ratio"] is None
    assert "CAMLINFINE" not in got and "REDTAPE" not in got                  # rights, an ordinary dividend
    assert got["SHRIRAMFIN"]["ex_date"] == "2025-01-10"
    assert parse_subject("Bonus 3:2") ["ratio"] == 2.5 and parse_subject("Annual General Meeting") is None


# --------------------------------------------------------------------------- #
# fetching
# --------------------------------------------------------------------------- #
def test_the_list_is_asked_for_30_days_ahead_once_a_trading_day(tmp_path):
    book = ActionBook(tmp_path)
    nse = FakeNSE(rows_with_ex())
    assert book.tick(nse, t(FRI, 8)) == 7
    path, params, referer = nse.calls[0]
    assert path == "api/corporates-corporateActions" and referer.endswith("corporate-filings-actions")
    assert params == {"index": "equities", "from_date": "16-10-2026", "to_date": "15-11-2026"}
    assert book.tick(nse, t(FRI, 12)) is None and len(nse.calls) == 1        # once a day
    assert book.tick(nse, t(date(2026, 10, 17), 9)) is None                  # Saturday: nothing
    assert health.read_runs(tmp_path)["corp_actions"]["rows"] == 7


def test_an_empty_or_failed_answer_is_unknown_and_retried_with_back_off(tmp_path):
    book = ActionBook(tmp_path)
    assert book.tick(FakeNSE([]), t(FRI, 8)) is None
    assert book.load()["tries"]["2026-10-16"]["tries"] == 1 and "no rows" in book.load()["tries"]["2026-10-16"]["error"]
    assert not book.due(t(FRI, 8, 4)) and book.due(t(FRI, 8, 5))
    assert book.tick(FakeNSE(RuntimeError("403")), t(FRI, 8, 5)) is None
    assert not book.known(t(FRI, 9)) and book.note(t(FRI, 9)).startswith("corporate actions unknown")


def test_the_list_goes_stale_after_two_trading_days(tmp_path):
    book = stored_book(tmp_path)
    assert book.known(t(EX, 9)) and book.known(t(date(2026, 10, 20), 9))
    assert not book.known(t(date(2026, 10, 21), 9))


def test_past_rows_are_kept_ten_days_so_a_missed_day_can_still_be_normalised(tmp_path):
    book = stored_book(tmp_path, now=t(FRI, 9))
    book.store(parse_actions(rows_with_ex(ex=date(2026, 11, 2))), t(date(2026, 10, 22), 9))
    exs = {r["ex_date"] for r in book.load()["rows"]}
    assert exs == {"2026-10-19", "2026-11-02"}


# --------------------------------------------------------------------------- #
# flags
# --------------------------------------------------------------------------- #
def test_holdings_are_flagged_seven_days_ahead(tmp_path):
    book = stored_book(tmp_path)
    assert book.reasons("SURYAROSNI", t(FRI, 9)) == [
        "corporate action: bonus 1:1, ex-date 19 Oct (the GTT stop is paused over the ex-date and re-set at the adjusted level)"]
    assert book.flags("SHRIRAMFIN", t(FRI, 9)) == ["Split ₹10 → ₹2 face value ex 19 Oct"]
    assert "check the stop by hand" in book.reasons("ITC", t(FRI, 9))[0]
    assert book.reasons("SURYAROSNI", t(date(2026, 10, 9), 9)) == []           # 10 days ahead: not yet
    assert book.reasons("SURYAROSNI", t(date(2026, 10, 20), 9)) == []          # the day after: done


def test_a_special_dividend_counts_only_when_it_is_at_least_2_percent(tmp_path):
    book = stored_book(tmp_path)
    assert book.reasons("INDOBORAX", t(FRI, 9), price=1500.0)               # 40 / 1500 = 2.7%
    assert book.reasons("INDOBORAX", t(FRI, 9), price=4000.0) == []         # 1%
    assert book.reasons("HCLTECH", t(FRI, 9), price=1700.0) == []           # 18 / 1700 = 1.1%


def test_the_email_flag_is_short():
    assert _flags({"reasons": ["corporate action: bonus 1:1, ex-date 19 Oct (...)"]}) == "corp. action"


# --------------------------------------------------------------------------- #
# the GTT guard
# --------------------------------------------------------------------------- #
def test_the_gtt_is_suspended_from_1500_the_session_before_and_waits_for_the_new_shares(tmp_path):
    book = stored_book(tmp_path)
    assert session_before(EX, None) == FRI
    assert book.gtt_step("SURYAROSNI", t(FRI, 14, 59), 10, None)[0] == "normal"
    step, susp = book.gtt_step("SURYAROSNI", t(FRI, 15, 0), 10, None, gtt_qty=10)
    assert step == "suspend" and susp["expected_qty"] == 20 and susp["ratio"] == 2.0
    assert book.gtt_step("SURYAROSNI", t(EX, 8, 45), 10, susp)[0] == "wait"      # Groww still shows 10
    assert book.gtt_step("SURYAROSNI", t(EX, 11, 0), 20, susp)[0] == "resume"    # credited
    assert book.gtt_step("SURYAROSNI", t(date(2026, 10, 20), 8, 45), 10, susp)[0] == "resume"  # the day after, anyway


def test_a_missed_session_before_suspends_on_the_ex_date_and_unknown_never_cancels(tmp_path):
    book = stored_book(tmp_path)
    assert book.gtt_step("SURYAROSNI", t(EX, 8, 31), 10, None)[0] == "suspend"
    assert book.gtt_step("REDTAPE", t(EX, 8, 31), 10, None)[0] == "normal"       # an ordinary dividend
    stale = stored_book(tmp_path / "x", now=t(date(2026, 10, 12), 9))
    assert not stale.known(t(FRI, 15, 30))
    assert stale.gtt_step("SURYAROSNI", t(FRI, 15, 30), 10, None)[0] == "normal"


class FakeLive:
    """A live Groww broker stand-in: positions and GTT calls recorded, nothing else."""
    name, live_orders = "groww", True

    def __init__(self):
        self.calls, self.pos = [], []

    def positions(self):
        return self.pos

    def is_listed(self, s):
        return True

    def gtt_stop_prices(self, sym, stop):
        return round(stop, 2), round(stop * 0.995, 2)

    def create_gtt_stop(self, sym, qty, trigger, limit):
        self.calls.append(("create", sym, qty, trigger))
        return {"smart_order_id": f"S{len(self.calls)}", "reference_id": "SL-X", "status": "ACTIVE"}

    def modify_gtt_stop(self, gid, qty, trigger, limit):
        self.calls.append(("modify", gid, qty, trigger))
        return {"status": "ACTIVE"}

    def cancel_gtt(self, gid):
        self.calls.append(("cancel", gid))
        return {"status": "CANCELLED"}

    def get_gtt(self, gid):
        return {"status": "CANCELLED"}


def test_the_gtt_manager_cancels_before_and_re_creates_at_the_adjusted_level(tmp_path):
    book = stored_book(tmp_path)
    st = State(tmp_path / "state.json")
    st.data["gtt_stops"] = {"SURYAROSNI": {"smart_order_id": "S0", "trigger": 850.0, "limit": 845.0, "qty": 10,
                                           "high_water": 1000.0}}
    g = FakeLive()
    clock = [t(FRI, 15, 5)]
    mgr = GttStopManager(g, st, corp=book, now_fn=lambda: clock[0])
    g.pos = [Position("SURYAROSNI", 10, 700.0, current_price=980.0)]
    acts = mgr.sync()
    assert g.calls == [("cancel", "S0")] and acts[0]["reason"].startswith("corporate action: bonus 1:1")
    assert st.data["gtt_suspended"]["SURYAROSNI"]["high_water"] == 1000.0 and "SURYAROSNI" not in st.data["gtt_stops"]
    clock[0] = t(EX, 8, 45)
    g.pos = [Position("SURYAROSNI", 10, 700.0, current_price=490.0)]         # price halved, shares not credited yet
    assert mgr.sync()[0]["action"] == "wait" and len(g.calls) == 1
    clock[0] = t(EX, 10, 0)
    g.pos = [Position("SURYAROSNI", 20, 350.0, current_price=495.0)]
    acts = mgr.sync()
    assert [a["action"] for a in acts] == ["resume", "create"]
    op, sym, qty, trigger = g.calls[-1]
    assert (op, qty) == ("create", 20) and trigger < 495.0 and trigger == pytest.approx(500.0 * 0.85, rel=0.01)
    assert st.data["gtt_stops"]["SURYAROSNI"]["high_water"] == 500.0 and st.data["gtt_suspended"] == {}


def test_without_a_known_list_the_gtt_manager_changes_nothing_for_corporate_actions(tmp_path):
    st = State(tmp_path / "state.json")
    st.data["gtt_stops"] = {"SURYAROSNI": {"smart_order_id": "S0", "trigger": 850.0, "limit": 845.0, "qty": 10,
                                           "high_water": 1000.0}}
    g = FakeLive()
    g.pos = [Position("SURYAROSNI", 10, 700.0, current_price=980.0)]
    GttStopManager(g, st, corp=ActionBook(tmp_path), now_fn=lambda: t(FRI, 15, 5)).sync()
    assert g.calls == []


# --------------------------------------------------------------------------- #
# the normaliser
# --------------------------------------------------------------------------- #
def practice(path, opened="2026-10-01T05:00:00+00:00"):
    b = LocalPaperBroker(path, starting_cash=100_000, price_fn=lambda s: 1000.0, whole_shares=True,
                         now_fn=lambda: opened)
    b.submit_order("SURYAROSNI", "buy", qty=15)
    b.set_stop("SURYAROSNI", {"type": "fixed", "value": 800.0})
    return b


def test_the_practice_account_is_rescaled_from_0830_on_the_ex_date_once(tmp_path):
    book = stored_book(tmp_path)
    b = practice(tmp_path / "paper_broker.json")
    assert book.normalise_practice(b, t(EX, 8, 29)) == []
    got = book.normalise_practice(b, t(EX, 8, 30))
    assert got == [{"symbol": "SURYAROSNI", "ex_date": "2026-10-19", "label": "bonus 1:1", "qty_from": 15.0,
                    "qty_to": 30.0, "avg_from": 1000.0, "avg_to": 500.0}]
    p = b._state["positions"]["SURYAROSNI"]
    assert p["high_water"] == 500.0 and p["stop"] == {"type": "fixed", "value": 400.0}
    assert book.normalise_practice(b, t(EX, 9, 0)) == []                       # once
    assert b._state["positions"]["SURYAROSNI"]["qty"] == 30.0


def test_a_position_bought_on_the_ex_date_is_left_alone(tmp_path):
    book = stored_book(tmp_path)
    b = practice(tmp_path / "paper_broker.json", opened="2026-10-19T04:00:00+00:00")   # 09:30 IST on the ex-date
    assert book.normalise_practice(b, t(EX, 10, 0)) == []
    assert b._state["positions"]["SURYAROSNI"]["qty"] == 15.0


def test_every_paper_account_is_normalised_and_a_quiet_day_opens_none(tmp_path, settings, monkeypatch):
    settings.state_dir = tmp_path
    book = stored_book(tmp_path)
    practice(tmp_path / "paper_broker.json")
    (tmp_path / "forward").mkdir()
    practice(tmp_path / "forward" / "nifty50_broker.json")
    done = normalise_all(settings, t(EX, 8, 45), book)
    assert sorted(d["account"] for d in done) == ["nifty50_broker", "paper_broker"]
    opened = []
    monkeypatch.setattr("trading_agent.broker.LocalPaperBroker._load", lambda self, cash: opened.append(1) or {})
    assert normalise_all(settings, t(EX, 9, 0), book) == [] and opened == []


def test_stops_are_held_back_until_the_practice_position_is_normalised(tmp_path):
    book = stored_book(tmp_path)
    assert book.held_back("SURYAROSNI", t(EX, 8, 0), account="paper_broker")
    b = practice(tmp_path / "paper_broker.json")
    book.normalise_practice(b, t(EX, 8, 30))
    assert not book.held_back("SURYAROSNI", t(EX, 9, 20), account="paper_broker")
    assert book.held_back("SURYAROSNI", t(EX, 9, 20))                          # a Groww holding: all of the ex-date
    assert not book.held_back("SURYAROSNI", t(date(2026, 10, 20), 9, 20))
    assert book.held_back("ANY", t(FRI, 9), suspended={"ex_date": "2026-10-19"})


def test_the_practice_stop_checker_normalises_first_and_never_sells_on_the_bonus_drop(tmp_path):
    from trading_agent.stops import PracticeStopChecker
    stored_book(tmp_path)
    b = practice(tmp_path / "paper_broker.json")
    b.price_fn = lambda s: 495.0                                             # the ex-date price after a 1:1 bonus
    chk = PracticeStopChecker(lambda: b, tmp_path / "state.json")
    assert chk.check_once(t(EX, 9, 20)) == []
    p = b._state["positions"]["SURYAROSNI"]
    assert p["qty"] == 30.0 and p["stop"]["value"] == 400.0                     # rescaled, still held


# --------------------------------------------------------------------------- #
# the Health row
# --------------------------------------------------------------------------- #
def test_the_health_row(tmp_path, settings):
    settings.state_dir, settings.market, settings.data_source = tmp_path, "in", "nse"
    row = {r["id"]: r for r in health.report(settings, t(FRI, 11))["sources"]}["corp_actions"]
    assert row["status"] == "late" and "corporate actions unknown" in row["detail"]
    ActionBook(tmp_path).tick(FakeNSE(rows_with_ex()), t(FRI, 8))
    row = {r["id"]: r for r in health.report(settings, t(FRI, 11))["sources"]}["corp_actions"]
    assert row["status"] == "ok" and row["rows"] == 7
    settings.market = "us"
    row = {r["id"]: r for r in health.report(settings, t(FRI, 11))["sources"]}["corp_actions"]
    assert row["status"] == "off"


# --------------------------------------------------------------------------- #
# review fixes: one action is handled once; a stale Groww average never lifts the stop above the market
# --------------------------------------------------------------------------- #
def _suspended_bonus(tmp_path):
    book = stored_book(tmp_path)
    st = State(tmp_path / "state.json")
    st.data["gtt_stops"] = {"SURYAROSNI": {"smart_order_id": "S0", "trigger": 850.0, "limit": 845.0, "qty": 10,
                                           "high_water": 1000.0}}
    g = FakeLive()
    clock = [t(FRI, 15, 5)]
    mgr = GttStopManager(g, st, corp=book, now_fn=lambda: clock[0])
    g.pos = [Position("SURYAROSNI", 10, 700.0, current_price=980.0)]
    mgr.sync()
    return st, g, clock, mgr


def test_after_the_resume_the_ex_date_never_suspends_again(tmp_path):
    st, g, clock, mgr = _suspended_bonus(tmp_path)
    g.pos = [Position("SURYAROSNI", 20, 350.0, current_price=495.0)]
    for h, m in ((10, 0), (10, 1), (11, 0), (15, 10), (15, 29)):          # many syncs on the ex-date
        clock[0] = t(EX, h, m)
        mgr.sync()
    assert [c[0] for c in g.calls] == ["cancel", "create"]                  # one cancel, one re-create: no churn
    rec = st.data["gtt_stops"]["SURYAROSNI"]
    assert rec["qty"] == 20 and rec["high_water"] == 500.0 and st.data["gtt_suspended"] == {}
    clock[0] = t(date(2026, 10, 20), 10)                                    # the day after
    mgr.sync()
    assert st.data["gtt_stops"]["SURYAROSNI"]["high_water"] == 500.0         # not divided a second time
    assert [c[0] for c in g.calls] == ["cancel", "create"]


def test_a_special_dividend_is_handled_once(tmp_path):
    book = stored_book(tmp_path)
    st = State(tmp_path / "state.json")
    st.data["gtt_stops"] = {"INDOBORAX": {"smart_order_id": "S0", "trigger": 1300.0, "limit": 1290.0, "qty": 5,
                                          "high_water": 1550.0}}
    g = FakeLive()
    clock = [t(FRI, 15, 5)]
    mgr = GttStopManager(g, st, corp=book, now_fn=lambda: clock[0])
    g.pos = [Position("INDOBORAX", 5, 1200.0, current_price=1500.0)]
    mgr.sync()
    g.pos = [Position("INDOBORAX", 5, 1200.0, current_price=1460.0)]
    for h in (9, 10, 11, 12, 13, 14):
        clock[0] = t(EX, h)
        mgr.sync()
    assert [c[0] for c in g.calls] == ["cancel", "create"]
    assert st.data["gtt_stops"]["INDOBORAX"]["high_water"] == 1510.0        # 1550 - 40, once


def test_a_stale_groww_average_never_puts_the_stop_above_the_market(tmp_path):
    st, g, clock, mgr = _suspended_bonus(tmp_path)
    clock[0] = t(date(2026, 10, 20), 9, 30)                                 # the day after: resume whatever Groww shows
    g.pos = [Position("SURYAROSNI", 10, 1100.0, current_price=600.0)]        # Groww: old quantity, old average
    mgr.sync()
    clock[0] = t(date(2026, 10, 20), 9, 31)
    mgr.sync()
    for op, *rest in g.calls[1:]:
        trigger = rest[-1]
        assert trigger < 600.0, (op, trigger)                               # never at or above the price
    assert st.data["gtt_stops"]["SURYAROSNI"]["high_water"] < 1000.0


def test_a_modify_at_or_above_the_price_is_refused_and_alerted(tmp_path):
    st = State(tmp_path / "state.json")
    st.data["gtt_stops"] = {"AAA": {"smart_order_id": "S0", "trigger": 80.0, "limit": 79.0, "qty": 10,
                                    "high_water": 100.0}}
    g = FakeLive()
    sent = []

    class Notes:
        def send(self, subject, body):
            sent.append(subject)
    mgr = GttStopManager(g, st, notifier=Notes(), bars_fn=lambda s: [])
    g.pos = [Position("AAA", 12, 100.0, current_price=79.5)]                # price under the old trigger, qty changed
    acts = mgr.sync()
    assert g.calls == [] and acts[0]["action"] == "error" and "at or above the price" in acts[0]["error"]
    assert sent == ["[GTT MODIFY FAILED] AAA"]


# --------------------------------------------------------------------------- #
# second review: a long agent run never overwrites what the watch wrote meanwhile
# --------------------------------------------------------------------------- #
def test_an_agent_runs_stale_state_never_creates_a_second_gtt_or_erases_the_watchs(tmp_path, settings, monkeypatch):
    import trading_agent.live as live
    from trading_agent import runner
    settings.state_dir, settings.market = tmp_path, "in"
    settings.broker, settings.groww_live_orders, settings.groww_gtt_stops = "groww", True, True
    st, g, clock, _ = _suspended_bonus(tmp_path)                    # Friday 15:05: GTT cancelled, holding suspended
    st.save(fresh_keys=())
    monkeypatch.setattr(live, "_clock", lambda: clock[0])
    monkeypatch.setattr(runner, "free_prices", lambda s: type("P", (), {"history": lambda self, sym, r: []})())
    agent_state = State(tmp_path / "state.json")                    # the agent run loads state.json ...
    clock[0] = t(EX, 10, 0)
    g.pos = [Position("SURYAROSNI", 20, 350.0, current_price=495.0)]
    live.locked_gtt_sync(settings, g)                               # ... the watch resumes and re-creates meanwhile
    assert [c[0] for c in g.calls] == ["cancel", "create"]
    clock[0] = t(EX, 10, 3)
    runner._after_run_gtt(settings, g, agent_state, None)           # the agent's own GTT sync at the end of its run
    agent_state.save(fresh_keys=live.GTT_KEYS)                      # and its final save
    assert [c[0] for c in g.calls] == ["cancel", "create"]           # no second GTT at Groww
    disk = State(tmp_path / "state.json").data
    assert disk["gtt_stops"]["SURYAROSNI"]["qty"] == 20 and "SURYAROSNI:2026-10-19" in disk["gtt_corp_done"]
    assert disk["gtt_suspended"] == {}


def test_a_first_gtt_on_a_stale_average_is_placed_from_the_price(tmp_path):
    st = State(tmp_path / "state.json")
    st.data["gtt_corp_done"] = {"AAA:2026-10-19": "2026-10-19T10:00:00+05:30"}   # a bonus handled; the create failed
    g = FakeLive()
    mgr = GttStopManager(g, st, bars_fn=lambda s: [], now_fn=lambda: t(date(2026, 10, 20), 10))
    g.pos = [Position("AAA", 20, 1100.0, current_price=600.0)]              # Groww's average not yet adjusted
    acts = mgr.sync()
    assert acts[0]["action"] == "create" and g.calls[0][3] == pytest.approx(510.0)   # 15% under the price
    other = GttStopManager(FakeLive(), State(tmp_path / "other.json"), bars_fn=lambda s: [],
                           now_fn=lambda: t(date(2026, 10, 20), 10))
    other.broker.pos = [Position("AAA", 20, 1100.0, current_price=600.0)]   # no corporate action: under water
    assert other.sync()[0]["action"] == "skip" and other.broker.calls == []  # the old rule: no GTT


def test_a_refused_modify_alerts_once_a_day(tmp_path):
    st = State(tmp_path / "state.json")
    st.data["gtt_stops"] = {"AAA": {"smart_order_id": "S0", "trigger": 80.0, "limit": 79.0, "qty": 10,
                                    "high_water": 100.0}}
    g = FakeLive()
    sent = []

    class Notes:
        def send(self, subject, body):
            sent.append(subject)
    mgr = GttStopManager(g, st, notifier=Notes(), bars_fn=lambda s: [])
    g.pos = [Position("AAA", 12, 100.0, current_price=79.5)]
    for _ in range(3):
        mgr.sync()
    assert g.calls == [] and sent == ["[GTT MODIFY FAILED] AAA"]
