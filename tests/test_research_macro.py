"""Macro diagnostic: oil, rupee and RBI labels and the holdings' tilt. Synthetic prices and fakes only; no network."""
import math
import random
from types import SimpleNamespace

import pytest

from trading_agent import research_macro as rm
from trading_agent.costs import IndianDeliveryCosts
from trading_agent.prices import YahooPrices
from trading_agent.research_sim import simulate

from .test_research_sim import synthetic  # noqa: F401 - the fixture


def factor(dates, seed, vol, shock=None):
    rng, p, out = random.Random(seed), 100.0, []
    for i, d in enumerate(dates):
        p *= math.exp(vol * rng.gauss(0, 1) + (shock(i) if shock else 0))
        out.append({"date": d, "close": p, "adj_close": p, "volume": 0})
    return out


def test_futures_and_currency_symbols_are_not_given_the_nse_suffix():
    y = YahooPrices(suffix=".NS")
    assert y.yahoo_symbol("BZ=F") == "BZ=F" and y.yahoo_symbol("INR=X") == "INR=X"
    assert y.yahoo_symbol("RELIANCE") == "RELIANCE.NS"


def test_rbi_labels_use_only_changes_announced_before_the_day():
    assert rm.rbi_state("2022-05-20") == "after a hike"        # 4 May 2022 +40
    assert rm.rbi_state("2025-06-30") == "after a cut"         # 6 Jun 2025 -50
    assert rm.rbi_state("2024-01-02") == "no change"           # held at 6.50% all of 2023-24
    assert rm.rbi_state("2022-05-03") == "no change"            # the day before the hike knows nothing of it
    assert rm.rbi_state("2023-04-01") == "no change"            # 52 days after the last hike


def test_oil_and_rupee_states_and_beta():
    s = [100.0] * 21 + [111.0]
    assert rm.oil_state(s, 21) == "oil up >10%" and rm.oil_state([100.0] * 21 + [95.0], 21) == "oil within 10%"
    assert rm.rupee_state([80.0] * 21 + [82.0], 21) == "rupee weaker >2%"
    assert rm.oil_state(s, 5) is None
    rng = random.Random(1)
    f, g = [100.0], [50.0]
    for _ in range(300):
        x = rng.gauss(0, 0.01)
        f.append(f[-1] * math.exp(x))
        g.append(g[-1] * math.exp(2 * x))
    assert rm.beta(g, f, 300) == pytest.approx(2.0)


def test_the_simulator_records_holdings_at_each_rebalance(synthetic):  # noqa: F811
    from trading_agent.research_studies import FINALIST
    run = simulate(synthetic, FINALIST, 300, 700)
    assert run["held"] and all(len(names) <= 20 for _, names in run["held"])
    assert [d for d, _ in run["held"]] == sorted(d for d, _ in run["held"])


def test_macro_diagnostic_end_to_end(synthetic):  # noqa: F811
    m = synthetic
    oil = factor(m.dates, 2, 0.02, shock=lambda i: 0.006 if i % 200 < 21 else 0.0)
    rupee = factor(m.dates, 3, 0.003)
    r = rm.macro_diagnostic(m, oil, rupee, cost_model=IndianDeliveryCosts())
    labelled = [x for x in r["months"] if x["oil"]]
    assert sum(x["months"] for x in r["oil"].values()) == len(labelled) and "oil up >10%" in r["oil"]
    assert sum(x["months"] for x in r["rbi"].values()) == len(r["months"])
    assert r["oil_exposure"]["rebalances"] > 10 and 0 <= r["oil_exposure"]["avg_share_most_hurt"] <= 1
    text = rm.format_macro(r)
    assert "Brent crude over the month before" in text and "RBI repo rate" in text
    assert "A description, not a filter" in text and "20% = no tilt" in text


def test_research_macro_command_keeps_the_holdout_sealed(synthetic, tmp_path, monkeypatch, capsys):  # noqa: F811
    from trading_agent import cli, runner
    m = synthetic
    seen = {}
    later = [{"date": "2026-01-05", "close": 1.0, "adj_close": 1.0, "volume": 0}]

    def history(sym, rng):
        seen[sym] = rng
        return factor(m.dates, 4 if sym == rm.OIL else 5, 0.01) + later
    monkeypatch.setattr(cli, "_settings", lambda a: SimpleNamespace(state_dir=tmp_path, paper_starting_cash=500_000.0))
    monkeypatch.setattr(cli, "_research_market", lambda a, s: m)
    monkeypatch.setattr(runner, "free_prices", lambda s: SimpleNamespace(history=history))
    got = {}
    real = rm.macro_diagnostic
    monkeypatch.setattr(rm, "macro_diagnostic", lambda mk, o, r, **kw: got.update(o=o) or real(mk, o, r, **kw))
    assert cli.cmd_research_macro(SimpleNamespace(json=False, until="2025-10-01")) == 0
    assert set(seen) == {"BZ=F", "INR=X"} and all(b["date"] < "2025-10-01" for b in got["o"])
    assert "Macro diagnostic" in capsys.readouterr().out and "research-macro" in cli.HALT_ALLOWED
