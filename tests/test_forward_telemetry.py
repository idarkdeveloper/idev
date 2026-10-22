"""Forward-test telemetry and the friction gate: turnover, approximate active share, slippage, charges against the
return above the fund, the once-a-month alert and the live-test refusal. Fakes only; no network."""
from datetime import datetime
from types import SimpleNamespace

import pytest

from trading_agent import forward_telemetry as ftm
from trading_agent.forward import IST, format_forward
from trading_agent.forward_schedule import friction_alert, with_caps

from .test_forward import Clock, make


def test_active_share_by_hand():
    members = ["A", "B", "C", "D"]
    caps = {"A": 400, "B": 300, "C": 200, "D": 100}               # fund weights 40/30/20/10
    r = ftm.active_share({"A": 50.0, "D": 50.0}, 100.0, members, caps)
    assert r["value"] == pytest.approx(1 - (0.4 + 0.1)) and r["basis"].startswith("market-cap")
    eq = ftm.active_share({"A": 50.0, "D": 50.0}, 100.0, members, {"A": 1})   # 1 of 4 caps: equal weights
    assert eq["value"] == pytest.approx(1 - 0.5) and eq["basis"].startswith("equal")
    assert ftm.active_share({"A": 1.0}, 0.0, members) is None and ftm.active_share({}, 1.0, []) is None


def test_slippage_counts_against_you_on_both_sides():
    orders = [{"side": "buy", "ltp": 100, "average_fill_price": 100.2, "filled_quantity": 1},     # +20 bps
              {"side": "sell", "ltp": 200, "average_fill_price": 199.8, "filled_quantity": 2},    # +10 bps
              {"side": "buy", "ltp": 100, "average_fill_price": None, "filled_quantity": 0}]      # not filled
    s = ftm.slippage(orders)
    assert s["bps"] == pytest.approx(15.0) and s["orders"] == 2
    none = ftm.slippage([])
    assert none["bps"] is None and "15 bps" in none["basis"]


def state(rebs, last_equity, last_bench, capital=100_000.0):
    return {"benchmark": "MID150BEES", "capital": capital, "rebalances": rebs,
            "history": [{"date": "2026-12-31", "equity": last_equity, "bench": last_bench}]}


def reb(date, traded, equity=100_000.0, charges=50.0):
    return {"date": date, "equity": equity, "charges": charges,
            "trades": [{"symbol": "X", "side": "buy", "qty": 1, "price": traded / 2},
                       {"symbol": "Y", "side": "sell", "qty": 1, "price": traded / 2},
                       {"symbol": "Z", "side": "buy", "qty": 5, "error": "no cash"}]}


def test_turnover_skips_the_build_and_annualises():
    d = state([reb("2026-10-09", 100_000), reb("2026-11-02", 16_000), reb("2026-12-01", 24_000)], 110_000, 105_000)
    t = ftm.telemetry(d, charges_paid=500.0)
    assert t["turnover_year"] == pytest.approx((0.08 + 0.12) / 2 * 12)        # 120% a year: well under the 250% line
    assert t["per_rebalance"][0]["build"] and t["rebalances_after_start"] == 2
    assert next(c for c in t["checks"] if c["name"] == "Turnover a year")["status"] == "ok"
    high = ftm.telemetry(state([reb("2026-10-09", 100_000), reb("2026-11-02", 40_000), reb("2026-12-01", 60_000)],
                               110_000, 105_000), charges_paid=500.0)
    assert high["turnover_year"] == pytest.approx(3.0)                            # 300% a year: over the 250% line
    assert next(c for c in high["checks"] if c["name"] == "Turnover a year")["status"] == "warn"
    assert ftm.TURNOVER_MAX == 2.5 and "under 250%" in next(c for c in high["checks"] if c["name"] == "Turnover a year")["target"]


def test_charge_share_and_the_gate():
    rebs = [reb("2026-10-09", 100_000), reb("2026-11-02", 20_000), reb("2026-12-01", 20_000)]
    clear = ftm.telemetry(state(rebs, 110_000, 100_000), charges_paid=1_000.0)       # 1,000 of 11,000 = 9%
    assert clear["charge_share"] == pytest.approx(1_000 / 11_000, abs=1e-4) and clear["gate"]["armed"]
    assert not clear["gate"]["tripped"] and "clear" in clear["gate"]["text"]
    high = ftm.telemetry(state(rebs, 101_000, 100_000), charges_paid=1_000.0)        # 1,000 of 2,000 = 50%
    assert high["gate"]["tripped"] and "50%" in high["gate"]["text"] and "--confirm-friction-override" in high["gate"]["text"]
    behind = ftm.telemetry(state(rebs, 95_000, 100_000), charges_paid=1_000.0)       # no return above the fund
    assert behind["charge_share"] is None and behind["gate"]["tripped"] and "no return above" in behind["gate"]["text"]
    early = ftm.telemetry(state(rebs[:2], 101_000, 100_000), charges_paid=1_000.0)   # one monthly rebalance so far
    assert not early["gate"]["armed"] and not early["gate"]["tripped"] and "arms after 2" in early["gate"]["text"]
    assert "WARN" in ftm.format_telemetry(high) and "!!!" in ftm.format_telemetry(high)


def screen_with_members(*syms):
    members = list(syms) + ["M1", "M2", "M3", "M4"]
    caps = {s: 1_000.0 for s in members}
    return lambda: {"top": [{"symbol": s} for s in syms], "eligible": 8, "all": [{"symbol": s} for s in members],
                    "caps": caps}


def test_forward_test_records_equity_and_active_share_and_reports_them(tmp_path):
    prices = {"MID150BEES": 200.0, "A": 100.0, "B": 250.0, "C": 1000.0, "D": 40.0, "E": 500.0}
    clock = Clock(2026, 10, 9, 16, 0)
    ft = make(tmp_path, prices, clock)
    s = ft.run(screen_with_members("A", "B", "C", "D"))
    e = s["rebalanced"]
    assert e["equity"] == pytest.approx(100_000) and e["active_share"]["basis"].startswith("market-cap")
    assert 0.45 < e["active_share"]["value"] < 0.55   # 4 of 8 equal-cap members, nearly fully invested
    clock.t = datetime(2026, 11, 2, 16, 0, tzinfo=IST)
    s = ft.run(screen_with_members("A", "B", "C", "E"))
    t = s["telemetry"]
    assert t["rebalances_after_start"] == 1 and t["turnover_year"] > 0 and not t["gate"]["armed"]
    text = format_forward(s)
    assert "Friction telemetry" in text and "Active share" in text and "arms after 2" in text


class Notes:
    def __init__(self):
        self.sent = []

    def send(self, subject, body, idempotency_key=None):
        self.sent.append((subject, body, idempotency_key))


def tripped_forward(tmp_path):
    prices = {"MID150BEES": 200.0, "A": 100.0, "B": 250.0, "C": 1000.0, "D": 40.0, "E": 500.0}
    clock = Clock(2026, 10, 9, 16, 0)
    ft = make(tmp_path, prices, clock)
    for when, picks in [((2026, 10, 9), "ABCD"), ((2026, 11, 2), "ABCE"), ((2026, 12, 1), "ABCD")]:
        clock.t = datetime(*when, 16, 0, tzinfo=IST)
        s = ft.run(screen_with_members(*picks))
    return ft, s, clock


def test_alert_once_a_month_and_no_setting_changes(tmp_path):
    ft, s, clock = tripped_forward(tmp_path)
    g = s["telemetry"]["gate"]
    assert g["armed"] and g["tripped"]          # flat prices: charges with no return above the fund
    settings = SimpleNamespace(state_dir=tmp_path, auto_trade=False, groww_live_orders=False)
    before = dict(vars(settings))
    notes = Notes()
    assert friction_alert(settings, ft, s, notes)
    assert not friction_alert(settings, ft, s, notes)             # same month: no second alert
    assert len(notes.sent) == 1 and notes.sent[0][2] == "friction:2026-12" and "Friction telemetry" in notes.sent[0][1]
    assert vars(settings) == before
    clock.t = datetime(2027, 1, 4, 16, 0, tzinfo=IST)
    assert friction_alert(settings, ft, s, notes) and len(notes.sent) == 2
    assert ftm.gate(tmp_path)["tripped"]                         # read back from disk for the live test


def test_alert_works_with_a_plain_notifier(tmp_path):
    ft, s, _ = tripped_forward(tmp_path)
    sent = []

    class Plain:
        def send(self, subject, body):
            sent.append(subject)
    assert friction_alert(SimpleNamespace(state_dir=tmp_path), ft, s, Plain()) and sent


def test_gate_without_an_account_and_when_unreadable(tmp_path):
    assert ftm.gate(tmp_path) is None
    (tmp_path / "forward").mkdir()
    (tmp_path / "forward" / "niftymidcap150.json").write_text("{broken", encoding="utf-8")
    g = ftm.gate(tmp_path)
    assert g["tripped"] and "unreadable" in g["text"]


def test_with_caps_uses_the_provider_and_survives_failures():
    class Funds:
        def get(self, s):
            if s == "BAD":
                raise RuntimeError("down")
            return {"market_cap": 5.0}
    r = with_caps(SimpleNamespace(state_dir=None), {"all": [{"symbol": "A"}, {"symbol": "BAD"}]}, provider=Funds())
    assert r["caps"] == {"A": 5.0, "BAD": None}


def test_live_test_refuses_while_the_gate_is_tripped(tmp_path, monkeypatch, capsys):
    from trading_agent import cli, runner
    settings = SimpleNamespace(state_dir=tmp_path, has_groww_credentials=True, groww_live_orders=True,
                               groww_access_token="x", groww_api_key=None)
    monkeypatch.setattr(cli, "_settings", lambda a: settings)
    monkeypatch.setattr(ftm, "gate", lambda d: {"tripped": True, "text": "Friction gate: test."})

    class Reached(Exception):
        pass

    def no_groww(*a, **k):
        raise Reached
    monkeypatch.setattr(runner, "make_groww", no_groww)
    monkeypatch.setattr(runner, "token_cache", lambda s: {})
    args = SimpleNamespace(symbol=None, ip=False, live_test="ITC", i_understand_real_orders=True, force=False,
                           offset_pct=3.0, confirm_friction_override=False)
    assert cli.cmd_groww_check(args) == 1
    assert "Friction gate: test." in capsys.readouterr().out
    args.confirm_friction_override = True
    with pytest.raises(Reached):                                   # past the gate, at the (faked) broker
        cli.cmd_groww_check(args)
    args.live_test, args.confirm_friction_override = None, False   # read-only checks never look at the gate
    with pytest.raises(Reached):
        cli.cmd_groww_check(args)
