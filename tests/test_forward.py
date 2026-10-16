from datetime import datetime

from trading_agent.costs import cost_model_for
from trading_agent.forward import ForwardTest, format_forward
from trading_agent.forward import IST


class Clock:
    def __init__(self, *a):
        self.t = datetime(*a, tzinfo=IST)

    def __call__(self):
        return self.t


def make(tmp_path, prices, clock, **kw):
    return ForwardTest(tmp_path, universe="NIFTYMIDCAP150", top=kw.pop("top", 4), capital=kw.pop("capital", 100_000),
                       price_fn=lambda s: prices[s.upper()], cost_model=kw.pop("cost_model", cost_model_for("in")),
                       now=clock, **kw)


def screen_of(*syms, eligible=50):
    return lambda: {"top": [{"symbol": s} for s in syms], "eligible": eligible}


def test_first_run_buys_equal_weights_and_the_index_fund(tmp_path):
    prices = {"MID150BEES": 200.0, "A": 100.0, "B": 250.0, "C": 1000.0, "D": 40.0}
    clock = Clock(2026, 10, 9, 16, 0)
    ft = make(tmp_path, prices, clock)
    assert ft.data["benchmark"] == "MID150BEES"
    s = ft.run(screen_of("A", "B", "C", "D"))
    assert s["rebalanced"]["picks"] == ["A", "B", "C", "D"] and s["rebalanced"]["charges"] > 0
    held = {h["symbol"]: h for h in s["holdings"]}
    assert set(held) == {"A", "B", "C", "D"}
    for h in held.values():  # about a quarter each, whole shares, after charges
        assert 0.22 < h["weight"] <= 0.25
    # benchmark: same capital into the fund, buy charges deducted
    assert 499 < ft.data["bench_units"] < 500 and ft.data["bench_cost"] > 0
    assert s["days"] == 1 and s["history"][0]["bench"] < 100_000
    assert -0.01 < s["strategy_return"] < 0 and -0.01 < s["benchmark_return"] < 0


def test_monthly_rebalance_sells_dropped_names_and_keeps_winners(tmp_path):
    prices = {"MID150BEES": 200.0, "A": 100.0, "B": 250.0, "C": 1000.0, "D": 40.0, "E": 500.0}
    clock = Clock(2026, 10, 9, 16, 0)
    ft = make(tmp_path, prices, clock)
    ft.run(screen_of("A", "B", "C", "D"))
    a_qty = next(p.qty for p in ft.broker.positions() if p.symbol == "A")

    # same month: no rebalance, even if the screen would now pick E
    clock.t = datetime(2026, 10, 12, 16, 0, tzinfo=IST)
    s = ft.run(screen_of("A", "B", "C", "E"))
    assert s["rebalanced"] is None and "E" not in {h["symbol"] for h in s["holdings"]}

    # next month: D dropped (sold), E added, A up 10% is kept untouched (under the trim line)
    prices.update(A=110.0, MID150BEES=210.0)
    clock.t = datetime(2026, 11, 2, 16, 0, tzinfo=IST)
    s = ft.run(screen_of("A", "B", "C", "E"))
    r = s["rebalanced"]
    assert {t["symbol"] for t in r["trades"] if t["side"] == "sell"} == {"D"}
    assert "E" in {t["symbol"] for t in r["trades"] if t["side"] == "buy"}
    assert next(p.qty for p in ft.broker.positions() if p.symbol == "A") == a_qty
    assert s["benchmark_return"] > 0.04 and s["days"] == 3 and s["rebalances"] == 2


def test_trims_a_name_far_above_its_weight(tmp_path):
    """The plain top-20 rules (before 16 Oct 2026); the research rules never trim a kept name."""
    prices = {"MID150BEES": 200.0, "A": 100.0, "B": 100.0, "C": 100.0, "D": 100.0}
    clock = Clock(2026, 10, 9, 16, 0)
    ft = make(tmp_path, prices, clock, cost_model=None, rules=None)
    ft.run(screen_of("A", "B", "C", "D"))
    prices["A"] = 200.0  # A doubles: now ~40% of equity, target 25%
    clock.t = datetime(2026, 11, 2, 16, 0, tzinfo=IST)
    r = ft.run(screen_of("A", "B", "C", "D"))["rebalanced"]
    assert [t["symbol"] for t in r["trades"] if t["side"] == "sell"] == ["A"]
    a = next(h for h in ft.summary()["holdings"] if h["symbol"] == "A")
    assert a["weight"] < 0.26


def test_due_only_on_weekdays_after_the_close_once_a_day(tmp_path):
    prices = {"MID150BEES": 200.0, "A": 100.0}
    clock = Clock(2026, 10, 9, 15, 0)  # Friday before the close
    ft = make(tmp_path, prices, clock, top=1)
    assert not ft.due()
    clock.t = datetime(2026, 10, 9, 15, 45, tzinfo=IST)
    assert ft.due()
    ft.run(screen_of("A"))
    assert not ft.due()  # today's point recorded, month done
    clock.t = datetime(2026, 10, 10, 16, 0, tzinfo=IST)  # Saturday
    assert not ft.due()
    clock.t = datetime(2026, 10, 12, 16, 0, tzinfo=IST)  # Monday
    assert ft.due() and not ft.rebalance_due()


def test_state_survives_a_restart_and_reports(tmp_path):
    prices = {"MID150BEES": 200.0, "A": 100.0, "B": 50.0}
    clock = Clock(2026, 10, 9, 16, 0)
    make(tmp_path, prices, clock, top=2).run(screen_of("A", "B"))
    again = make(tmp_path, prices, clock, top=9, capital=1)  # first-run settings are kept
    s = again.summary()
    assert s["top"] == 2 and s["capital"] == 100_000 and s["last_rebalance"] == "2026-10"
    assert {h["symbol"] for h in s["holdings"]} == {"A", "B"}
    text = format_forward(s)
    assert "top 2 of NIFTYMIDCAP150 vs MID150BEES" in text and "Too early to judge" in text


def test_cli_status_and_if_due_without_network(tmp_path, monkeypatch, capsys):
    from trading_agent import cli
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("STATE_DIR", str(tmp_path / "state"))
    for k in ("GROWW_ACCESS_TOKEN", "GROWW_API_KEY", "BROKER", "MARKET"):
        monkeypatch.delenv(k, raising=False)
    assert cli.main(["forward", "--status"]) == 0
    assert "not yet" in capsys.readouterr().out
    import trading_agent.forward as fwd
    monkeypatch.setattr(fwd, "_now", lambda: datetime(2026, 10, 10, 12, 0, tzinfo=IST))  # Saturday
    assert cli.main(["forward", "--if-due"]) == 0
    assert "nothing due" in capsys.readouterr().out


def ranked_screen(*ranked, top=4):
    """A screen result whose eligible names are ``ranked`` best first (as run_screen's "all" rows)."""
    return lambda: {"top": [{"symbol": s} for s in ranked[:top]], "eligible": len(ranked),
                    "all": [{"symbol": s, "eligible": True} for s in ranked]}


def test_research_rules_keep_names_inside_the_buffer_and_fill_only_empty_slots(tmp_path):
    names = [f"S{i:02d}" for i in range(1, 41)]
    prices = {"MID150BEES": 200.0, **{n: 100.0 for n in names}}
    clock = Clock(2026, 10, 9, 16, 0)
    ft = make(tmp_path, prices, clock, top=4)
    s = ft.run(ranked_screen(*names))
    assert s["rules"]["since"] == "2026-10-09" and s["rules"]["buffer"] == 35
    assert {h["symbol"] for h in s["holdings"]} == {"S01", "S02", "S03", "S04"}
    a_qty = next(p.qty for p in ft.broker.positions() if p.symbol == "S01")
    # next month: S01 fell to rank 30 (inside 35: kept, untouched), S02 to rank 36 (sold); S03, S04 stay top
    prices["S01"] = 150.0   # far above its weight, but the buffer never trims
    order = ["S03", "S04", "S05", "S06"] + [n for n in names if n not in ("S01", "S02", "S03", "S04", "S05", "S06")]
    order.insert(29, "S01")
    order.insert(35, "S02")
    clock.t = datetime(2026, 11, 2, 16, 0, tzinfo=IST)
    s = ft.run(ranked_screen(*order))
    r = s["rebalanced"]
    assert r["kept"] == ["S01", "S03", "S04"]
    assert {t["symbol"] for t in r["trades"] if t["side"] == "sell"} == {"S02"}
    assert {t["symbol"] for t in r["trades"] if t["side"] == "buy"} == {"S05"}   # one empty slot, the best new name
    assert next(p.qty for p in ft.broker.positions() if p.symbol == "S01") == a_qty
    assert "Research rules since 2026-10-09" in format_forward(s) and "kept 3 still ranked" in format_forward(s)


def test_tax_aware_wait_keeps_a_profitable_holding_near_one_year(tmp_path):
    names = [f"S{i:02d}" for i in range(1, 61)]
    prices = {"MID150BEES": 200.0, **{n: 100.0 for n in names}}
    clock = Clock(2026, 10, 9, 16, 0)
    ft = make(tmp_path, prices, clock, top=2)
    ft.run(ranked_screen(*names, top=2))
    prices["S01"], prices["S02"] = 130.0, 130.0
    clock.t = datetime(2027, 9, 13, 16, 0, tzinfo=IST)   # 339 days after the first buy: profitable, short-term
    order = [n for n in names if n not in ("S01", "S02")]
    order.insert(44, "S01")   # rank 45: outside the buffer, inside 50 -> waits for long-term tax
    order.insert(55, "S02")   # rank 56: below 50 -> sold
    s = ft.run(ranked_screen(*order, top=2), force_rebalance=True)
    r = s["rebalanced"]
    assert r["tax_waits"] == ["S01"] and "S01" in r["kept"]
    assert {t["symbol"] for t in r["trades"] if t["side"] == "sell"} == {"S02"}
    assert "1 waiting for long-term tax" in format_forward(s)


def test_disaster_floor_sells_a_holding_25_percent_down_and_nothing_else(tmp_path):
    prices = {"MID150BEES": 200.0, "A": 100.0, "B": 100.0, "C": 100.0, "D": 100.0}
    clock = Clock(2026, 10, 9, 16, 0)
    ft = make(tmp_path, prices, clock)
    ft.run(screen_of("A", "B", "C", "D"))
    prices.update(A=76.0, B=74.0)   # A -24%: kept; B -26%: floor
    clock.t = datetime(2026, 10, 12, 16, 0, tzinfo=IST)
    s = ft.run(screen_of("A", "B", "C", "D"))
    assert s["rebalanced"] is None and [f["symbol"] for f in s["floor_sales"]] == ["B"]
    assert {h["symbol"] for h in s["holdings"]} == {"A", "C", "D"}
    assert "Disaster floor: sold B" in format_forward(s)


def test_an_existing_account_records_the_rules_from_its_next_run(tmp_path):
    prices = {"MID150BEES": 200.0, "A": 100.0, "B": 100.0}
    clock = Clock(2026, 10, 9, 16, 0)
    old = make(tmp_path, prices, clock, top=2, rules=None)
    old.run(screen_of("A", "B"))
    assert "rules" not in old.data
    clock.t = datetime(2026, 10, 19, 16, 0, tzinfo=IST)
    s = make(tmp_path, prices, clock, top=2).run(screen_of("A", "B"))
    assert s["rules"]["since"] == "2026-10-19"
