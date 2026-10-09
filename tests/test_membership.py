from trading_agent.costs import IndianDeliveryCosts
from trading_agent.factor_backtest import format_factor_backtest, run_factor_backtest
from trading_agent.membership import NIFTY50_CHANGES, Membership, load_changes_csv, membership_for
from trading_agent.state import equity_stats

from .test_scorecard_factor import Prices

TODAY_NIFTY50 = ["ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK", "BAJAJ-AUTO", "BAJAJFINSV",
                 "BAJFINANCE", "BEL", "BHARTIARTL", "BSE", "CIPLA", "COALINDIA", "DRREDDY", "EICHERMOT", "ETERNAL",
                 "GRASIM", "HCLTECH", "HDFCBANK", "HDFCLIFE", "HINDALCO", "HINDUNILVR", "ICICIBANK", "INDIGO",
                 "INFY", "ITC", "JIOFIN", "JSWSTEEL", "KOTAKBANK", "LT", "M&M", "MARUTI", "MAXHEALTH", "NESTLEIND",
                 "NTPC", "ONGC", "POWERGRID", "RELIANCE", "SBILIFE", "SBIN", "SHRIRAMFIN", "SUNPHARMA",
                 "TATACONSUM", "TATASTEEL", "TCS", "TECHM", "TITAN", "TMPV", "TRENT", "ULTRACEMCO"]


def test_nifty50_has_fifty_members_on_every_day():
    m = membership_for("NIFTY 50", TODAY_NIFTY50)
    assert m is not None and m.known_since == "2021-03-31"
    for day in ["2021-03-30", "2021-06-01", "2022-10-03", "2023-07-12", "2023-07-13", "2024-06-03",
                "2024-09-30", "2025-04-01", "2025-10-01", "2026-09-29", "2026-09-30"]:
        assert len(m.members_on(day)) == 50, day
    assert "HDFC" in m.members_on("2023-07-12") and "HDFC" not in m.members_on("2023-07-13")
    assert "LTM" in m.members_on("2024-01-02") and "LTM" not in m.members_on("2024-10-01")
    assert "WIPRO" in m.members_on("2026-09-29") and "BSE" in m.members_on("2026-09-30")
    assert "GAIL" in m.members_on("2021-01-01") and "TATACONSUM" not in m.members_on("2021-01-01")
    ever = m.ever_members("2022-01-03")
    assert {"IOC", "SHREECEM", "HDFC", "UPL", "DIVISLAB", "LTM", "BPCL", "BRITANNIA", "WIPRO"} <= ever
    assert "GAIL" not in ever
    assert [c["date"] for c in m.changes_between("2025-01-01", "2026-12-31")] == ["2025-03-28", "2025-09-30",
                                                                                  "2026-09-30"]
    assert membership_for("NIFTY200", TODAY_NIFTY50) is None
    assert all(d < e for (d, _, _), (e, _, _) in zip(NIFTY50_CHANGES, NIFTY50_CHANGES[1:]))


def test_renames_and_csv(tmp_path):
    m = Membership(frozenset(["zomato", "LTIM"]), [("2025-01-01", ("ZOMATO",), ("OLD",))])
    assert m.members_on("2024-12-31") == {"LTM", "OLD"}
    f = tmp_path / "changes.csv"
    f.write_text("date,added,removed\n2024-03-28,SHRIRAMFIN,UPL\n2024-09-30,BEL TRENT,DIVISLAB;LTM\n")
    rows = load_changes_csv(f)
    assert rows[1] == ("2024-09-30", ("BEL", "TRENT"), ("DIVISLAB", "LTM"))
    assert membership_for("NIFTY200", ["BEL"], f).source == str(f)


def test_factor_backtest_includes_dropped_stocks():
    prices = Prices(900)
    uni = [{"symbol": s} for s in ("UP", "DOWN", "FLAT")]
    cut = prices.dates[700]
    # GONE was a member until `cut`; NONE was a member too but has no price history (delisted)
    m = Membership(frozenset(["UP", "DOWN", "FLAT"]),
                   [("2020-01-01", (), ()), (cut, ("FLAT",), ("GONE", "NONE"))])
    kw = dict(top=2, years=2, cost_model=IndianDeliveryCosts(), capital=500_000, min_turnover=0, workers=2)
    biased = run_factor_backtest(uni, prices, **kw)
    pit = run_factor_backtest(uni, prices, membership=m, **kw)
    assert pit["point_in_time"] is True and pit["former_members"] == ["GONE", "NONE"]
    assert pit["missing_history"] == [{"symbol": "NONE", "why": "no price history found"}]
    assert pit["universe_size"] == 5 and pit["with_history"] == 4
    assert pit["index_changes"][0]["removed"] == ["GONE", "NONE"]
    # GONE outran everything while it was a member, so the honest test differs from today's-members-only
    assert pit["stats"]["strategy"]["total_return"] != biased["stats"]["strategy"]["total_return"]
    assert all("GONE" not in p["picks"] for p in pit["picks"] if p["date"] >= cut)
    assert "no survivorship bias" in pit["caveat"] and "NIFTYBEES" in pit["caveat"]
    text = format_factor_backtest(pit)
    assert "point-in-time" in text and "-GONE -NONE" in text


def test_equity_stats():
    assert equity_stats([]) is None
    h = [{"at": f"t{i}", "equity": v} for i, v in enumerate([100, 120, 90, 110, 130, 117])]
    s = equity_stats(h)
    assert s["peak"] == 130 and abs(s["max_drawdown"] - (-0.25)) < 1e-9 and s["max_drawdown_at"] == "t2"
    assert abs(s["drawdown_now"] - (-0.1)) < 1e-9 and s["points"] == 6
