"""Each recommendation is scored against the index fund of its own universe on the day it was made."""
from trading_agent.membership import Membership
from trading_agent.scorecard import format_scorecard, score_recommendations


def _dates(n):
    import datetime as dt
    d, out = dt.date(2025, 1, 6), []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += dt.timedelta(days=1)
    return out


class FundPrices:
    """Over 60 trading bars after the call: MIDCO +15%, LARGECO +10%, NIFTYBEES +10%, MID150BEES +18%."""
    def __init__(self, n=120, start=20):
        self.dates, self.start, self.calls = _dates(n), start, []

    def _path(self, total):
        out = []
        for i, d in enumerate(self.dates):
            k = min(max(i - self.start, 0), 60) / 60
            px = 100 * (1 + total * k)
            out.append({"date": d, "close": px, "adj_close": px, "volume": 1e6})
        return out

    def history(self, sym, range_="2y"):
        self.calls.append(sym)
        totals = {"MIDCO": 0.15, "LARGECO": 0.10, "NIFTYBEES": 0.10, "MID150BEES": 0.18, "HDFCSML250": 0.20,
                  "^NSEI": 0.09, "LATECO": 0.12}
        if sym not in totals:
            raise RuntimeError("no data " + sym)
        return self._path(totals[sym])


def _mem(current, changes=(), **kw):
    # a recorded (empty) change on an early date: the history is known for the days these tests use
    return Membership(frozenset(current), list(changes) or [("2000-01-01", (), ())], **kw)


def _rec(p, ticker, action="buy"):
    return {"at": p.dates[p.start - 1] + "T10:00:00+00:00", "action": action, "ticker": ticker}


def test_midcap_buy_scored_against_mid150_fund_is_incorrect():
    p = FundPrices()
    mem = {"NIFTY50": _mem([]), "NIFTY100": _mem([]), "NIFTYMIDCAP150": _mem(["MIDCO"]),
           "NIFTYSMALLCAP250": _mem([])}
    r = score_recommendations([_rec(p, "MIDCO")], p, memberships=mem)
    row = r["rows"][0]
    assert row["benchmark"] == "MID150BEES"
    assert abs(row["excess"]["60"] - (-0.03)) < 1e-9
    assert row["correct"]["60"] is False
    assert abs(row["excess_nifty"]["60"] - 0.05) < 1e-9   # against the Nifty it would have looked like alpha
    s = r["summary"]
    assert s["by_benchmark"]["MID150BEES"]["buy"]["60"]["hit_rate"] == 0.0
    text = format_scorecard(r)
    assert "index fund" in text and "MID150BEES for mid caps" in text


def test_large_cap_uses_niftybees_and_fetches_each_benchmark_once():
    p = FundPrices()
    mem = {"NIFTY50": _mem(["LARGECO"]), "NIFTY100": _mem(["LARGECO"]), "NIFTYMIDCAP150": _mem(["MIDCO"]),
           "NIFTYSMALLCAP250": _mem([])}
    r = score_recommendations([_rec(p, "LARGECO"), _rec(p, "LARGECO"), _rec(p, "MIDCO")], p, memberships=mem)
    assert [x["benchmark"] for x in r["rows"]] == ["NIFTYBEES", "NIFTYBEES", "MID150BEES"]
    assert abs(r["rows"][0]["excess"]["60"]) < 1e-9
    assert p.calls.count("NIFTYBEES") == 1 and p.calls.count("MID150BEES") == 1


def test_membership_is_as_of_recommendation_date_not_today():
    p = FundPrices()
    joined = p.dates[p.start + 5]  # joined the Nifty 100 AFTER the call, so it was a mid cap at the time
    nifty100 = _mem(["LATECO"], [(joined, ("LATECO",), ())])
    mid = _mem([], [(joined, (), ("LATECO",))])  # left the midcap index the day it joined the Nifty 100
    mem = {"NIFTY50": _mem([]), "NIFTY100": nifty100, "NIFTYMIDCAP150": mid, "NIFTYSMALLCAP250": _mem([])}
    day = p.dates[p.start - 1]
    assert "LATECO" in mid.members_on(day) and "LATECO" not in nifty100.members_on(day)
    r = score_recommendations([_rec(p, "LATECO")], p, memberships=mem)
    assert r["rows"][0]["benchmark"] == "MID150BEES"
    late = {"at": joined + "T10:00:00+00:00", "action": "buy", "ticker": "LATECO"}
    assert score_recommendations([late], p, memberships=mem)["rows"][0]["benchmark"] == "NIFTYBEES"


def test_small_cap_fund_and_unknown_membership_fallback():
    p = FundPrices()
    mem = {"NIFTY50": _mem([]), "NIFTY100": _mem([]), "NIFTYMIDCAP150": _mem([]),
           "NIFTYSMALLCAP250": _mem(["MIDCO"])}
    assert score_recommendations([_rec(p, "MIDCO")], p, memberships=mem)["rows"][0]["benchmark"] == "HDFCSML250"
    mem["NIFTYMIDCAP150"] = None  # lookup failed
    r = score_recommendations([_rec(p, "LARGECO")], p, memberships=mem)
    row = r["rows"][0]
    assert row["benchmark"] == "NIFTYBEES" and row["benchmark_note"] == "membership unknown"
    assert r["summary"]["unknown_membership"] == 1
    assert "membership unknown" in format_scorecard(r)


def test_not_in_any_index_defaults_to_niftybees_without_note():
    p = FundPrices()
    mem = {k: _mem([]) for k in ("NIFTY50", "NIFTY100", "NIFTYMIDCAP150", "NIFTYSMALLCAP250")}
    row = score_recommendations([_rec(p, "LARGECO")], p, memberships=mem)["rows"][0]
    assert row["benchmark"] == "NIFTYBEES" and "benchmark_note" not in row


def test_without_memberships_single_benchmark_as_before():
    p = FundPrices()
    r = score_recommendations([_rec(p, "LARGECO")], p, benchmark="^NSEI")
    assert r["rows"][0]["benchmark"] == "^NSEI" and abs(r["rows"][0]["excess"]["60"] - 0.01) < 1e-9
    assert r["summary"]["benchmark"] == "^NSEI"
