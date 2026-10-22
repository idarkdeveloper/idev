"""Pre-registered batch 4 (quarterly rebalance H, volatility scaling V). Synthetic prices only; no network."""
import json
from types import SimpleNamespace

import pytest

from trading_agent import research_batch4 as b4
from trading_agent.costs import cost_model_for
from trading_agent.research_sim import Market

from .test_research_leakage import make


def test_rule_sets_follow_the_plan():
    r = b4.rule_sets()
    c, h, v = r["control"], r["H"], r["V"]
    assert c.delist_haircut == 0.30 and c.rebalance == "monthly" and not c.vol_scale and c.buffer == 35 and c.tax
    assert h.rebalance == "quarterly" and h.tax_aware_from == 273 and h.buffer == 35 and h.floor == 0.25
    assert v.vol_scale and (v.vol_recent, v.vol_ref, v.vol_min_days, v.vol_min_names, v.vol_band) == (126, 756, 378, 10, 0.1)
    assert v.rebalance == "monthly" and v.tax_aware_from == 335 and v.brake == "none"
    assert "quarterly" in h.label() and "vol-scaled 126/756" in v.label() and "delisting haircut 30%" in c.label()


def test_checks_follow_the_pass_rules():
    c = {"excess": 0.5, "cagr": 0.20, "max_drawdown": -0.30, "turnover": 1.8}
    h_ok = {"excess": 0.4, "cagr": 0.195, "max_drawdown": -0.33, "turnover": 1.1}
    assert all(b4.checks_for("H", h_ok, c).values())
    assert not b4.checks_for("H", {**h_ok, "turnover": 1.3}, c)["turnover at most two-thirds of the control's"]
    assert not b4.checks_for("H", {**h_ok, "cagr": 0.185}, c)["CAGR within 1 point of the control's"]
    assert not b4.checks_for("H", {**h_ok, "max_drawdown": -0.36}, c)["worst fall within 5 points of the control's"]
    v_ok = {"excess": 0.3, "cagr": 0.192, "max_drawdown": -0.24, "turnover": 2.5}
    assert all(b4.checks_for("V", v_ok, c).values())
    shallower = "worst fall at least 5 points shallower than the control's"
    assert not b4.checks_for("V", {**v_ok, "max_drawdown": -0.27}, c)[shallower]
    assert not b4.checks_for("V", {**v_ok, "turnover": 2.8}, c)["turnover at most 1.5x the control's"]
    assert not b4.checks_for("V", {**v_ok, "excess": -0.1}, c)["beats the fund"]
    assert not any(b4.checks_for("V", {"excess": None, "cagr": None, "max_drawdown": None, "turnover": None}, c).values())


def _with_delisting(m, sym="S03", last=600):
    """The same market, but ``sym`` rises steadily and stops trading after session ``last``."""
    series = dict(m.raw)
    series[sym] = [{**b, "close": 100 * 1.001 ** i, "adj_close": 100 * 1.001 ** i} for i, b in enumerate(m.raw[sym][:last + 1])]
    out = Market(m.dates, series, m.regime, m.members_on, min_turnover=0, flows=m.flows)
    out.filled, out.missing = [sym], []
    return out


def test_batch4_end_to_end_on_synthetic_prices():
    plain = make(n=900)
    m = _with_delisting(make(n=900))
    r = b4.batch4(m, plain, cost_model=cost_model_for("in"), capital=500_000.0, extra_trials=3)
    res = r["results"]
    assert list(res) == ["control", "H", "V"] and r["start"] == r["diagnostic"]["start"]
    assert set(res["H"]["checks"]) == {"beats the fund", "CAGR within 1 point of the control's",
                                       "worst fall within 5 points of the control's",
                                       "turnover at most two-thirds of the control's"}
    assert set(res["V"]["checks"]) == {"beats the fund", "worst fall at least 5 points shallower than the control's",
                                       "CAGR within 1 point of the control's", "turnover at most 1.5x the control's"}
    assert res["control"]["skill"]["trials"] == 6 and all(isinstance(x["passes"], bool) for k, x in res.items() if k != "control")
    assert res["H"]["completed_trades"] <= res["control"]["completed_trades"]
    assert res["V"]["scaling"]["rebalances"] >= 12 and res["V"]["scaling"]["mean_share"] <= 1.0
    cut = res["control"]["delisted"]
    assert cut and cut[0]["symbol"] == "S03" and cut[0]["date"] == m.dates[601] and res["control"]["delistings"] == 1
    assert not any(tr["symbol"] == "S03" and tr["entry"] > m.dates[601] for tr in [])   # never re-bought (see sim test)
    d = r["diagnostic"]
    assert d["filled"] == ["S03"] and d["plain"]["total_return"] is not None and d["start_plain"] == d["start"]
    assert r["pbo"] is None or 0.0 <= r["pbo"]["pbo"] <= 1.0
    text = b4.format_batch4(r, "Survivorship: the note")
    assert "BATCH 4, run once" in text and "Survivorship diagnostic" in text and "Survivorship: the note" in text
    assert "cut 30% for stopping trading: S03" in text and "V's share" in text and "Verdict:" in text
    for k in ("H", "V"):
        assert (k + " misses:" in text) != res[k]["passes"]


def test_the_command_needs_confirm_runs_once_and_is_never_claimed_when_the_data_step_stops(tmp_path, monkeypatch, capsys):
    from trading_agent import cli
    monkeypatch.setattr(cli, "_settings", lambda a: SimpleNamespace(state_dir=tmp_path, paper_starting_cash=100_000.0))
    args = SimpleNamespace(confirm=False, json=False)
    assert cli.cmd_research_batch4(args) == 2 and "Run again with --confirm" in capsys.readouterr().out
    args.confirm = True

    def gate(a, s):
        raise SystemExit("Stopped before any claim: only 3 of 10 trading days' NSE files loaded")
    monkeypatch.setattr(cli, "_research_market", gate)
    with pytest.raises(SystemExit, match="Stopped before any claim"):
        cli.cmd_research_batch4(args)
    assert not (tmp_path / "research" / "once" / "Batch4.claimed").exists()

    m = make(n=900)
    seen = {}
    monkeypatch.setattr(cli, "_research_market",
                        lambda a, s: seen.update(until=a.until, years=a.years, fill=a.fill_nse, uni=a.universe) or m)
    assert cli.cmd_research_batch4(args) == 0
    out = capsys.readouterr().out
    assert "Claimed Batch4" in out and "BATCH 4, run once" in out
    assert seen == {"until": "2025-10-01", "years": 5, "fill": True, "uni": "NIFTYMIDCAP150"}
    rows = [json.loads(x) for x in (tmp_path / "research" / "trials.jsonl").read_text().splitlines()]
    assert [r["params"]["variant"] for r in rows] == ["control", "H", "V"]
    assert rows[1]["params"]["rules"].count("quarterly") == 1 and "vol-scaled" in rows[2]["params"]["rules"]
    assert cli.cmd_research_batch4(args) == 2 and "used once only" in capsys.readouterr().out
    assert "research-batch4" in cli.HALT_ALLOWED
