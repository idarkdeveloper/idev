"""Sector concentration diagnostic. Synthetic prices and fake NSE lists only; no network."""
from types import SimpleNamespace

from trading_agent import research_sectors as rs
from trading_agent.costs import IndianDeliveryCosts

from .test_research_sim import synthetic  # noqa: F401 - the fixture


def industries(m):
    names = ("Banks", "IT", "Metals", "Pharma", "Autos")
    return {s: names[i % 5] if i % 3 else "Metals" for i, s in enumerate(sorted(m.raw))}


def test_the_industry_map_merges_lists_and_survives_a_failed_one():
    lists = {"NIFTY500": [{"symbol": "A", "industry": "Banks"}, {"symbol": "B", "industry": ""}],
             "NIFTYMIDCAP150": [{"symbol": "A", "industry": "Other"}, {"symbol": "B", "industry": "IT"}]}

    def load(name):
        if name not in lists:
            raise OSError("NSE refused")
        return lists[name]
    got, failed = rs.industry_map(load)
    assert got == {"A": "Banks", "B": "IT"} and failed == ["NIFTYSMALLCAP250"]


def test_crowding_counts_the_largest_known_industry():
    c = rs.crowding(["A", "B", "C", "D", "E"], {"A": "IT", "B": "IT", "C": "IT", "D": "Banks"})
    assert c["top_industry"] == "IT" and c["top_count"] == 3 and c["industries"] == 2 and c["unknown"] == 1
    assert abs(c["hhi"] - (0.36 + 0.04 + 0.04)) < 1e-12
    assert rs.band_of(3) == "at most 3 in one industry" and rs.band_of(5) == "4 or 5 in one industry"
    assert rs.band_of(9) == "6 or more in one industry" and rs.crowding([], {})["top_count"] == 0


def test_sector_diagnostic_end_to_end(synthetic):  # noqa: F811
    m = synthetic
    r = rs.sector_diagnostic(m, industries(m), cost_model=IndianDeliveryCosts())
    assert r["rebalances"] and 1 <= r["avg_top_count"] <= 20 and r["max_top"]["top_count"] >= r["avg_top_count"]
    labelled = sum(x["months"] for x in r["by_band"].values())
    assert labelled > 30 and len(r["worst_months"]) == 5
    assert [x["excess"] for x in r["worst_months"]] == sorted(x["excess"] for x in r["worst_months"])
    assert r["unknown_stocks"] == []
    text = rs.format_sectors(r)
    assert "Sector concentration of the adopted rules" in text and "A description, not a filter" in text
    assert "The 5 worst months against the fund" in text and "as of today" in text


def test_unknown_industries_are_reported(synthetic):  # noqa: F811
    r = rs.sector_diagnostic(synthetic, {}, cost_model=IndianDeliveryCosts())
    assert r["unknown_stocks"] and r["avg_top_count"] == 0
    assert "had no industry in today's NSE lists" in rs.format_sectors(r)


def test_research_sectors_command(synthetic, tmp_path, monkeypatch, capsys):  # noqa: F811
    from trading_agent import cli, screen
    m = synthetic
    monkeypatch.setattr(cli, "_settings", lambda a: SimpleNamespace(state_dir=tmp_path, paper_starting_cash=500_000.0))
    monkeypatch.setattr(cli, "_research_market", lambda a, s: m)
    rows = [{"symbol": s, "industry": i} for s, i in industries(m).items()]
    monkeypatch.setattr(screen, "load_universe", lambda name: rows if name == "NIFTY500" else [])
    assert cli.cmd_research_sectors(SimpleNamespace(json=False)) == 0
    assert "Sector concentration" in capsys.readouterr().out
    monkeypatch.setattr(screen, "load_universe", lambda name: (_ for _ in ()).throw(OSError("refused")))
    assert cli.cmd_research_sectors(SimpleNamespace(json=False)) == 1
    assert "No industry list" in capsys.readouterr().out and "research-sectors" in cli.HALT_ALLOWED
