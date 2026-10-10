"""Fix round 1 for the scorecard: assumed membership, late fund history, US mode, the membership cache."""
from trading_agent.membership import Membership
from trading_agent.scorecard import format_scorecard, score_recommendations

from .test_scorecard_benchmark import FundPrices, _mem, _rec


def test_day_before_recorded_history_is_noted_and_warnings_surface():
    p = FundPrices()
    m = Membership(frozenset(["MIDCO"]), [(p.dates[50], ("MIDCO",), ())], warnings=["2 inconsistencies remain"])
    mem = {"NIFTY50": _mem([]), "NIFTY100": _mem([]), "NIFTYMIDCAP150": m, "NIFTYSMALLCAP250": _mem([])}
    assert m.known_since > p.dates[p.start - 1]
    r = score_recommendations([_rec(p, "MIDCO")], p, memberships=mem)
    assert r["rows"][0]["benchmark_note"] == "membership assumed (before recorded history)"
    assert r["summary"]["assumed_membership"] == 1 and r["summary"]["membership_warnings"] == ["2 inconsistencies remain"]
    text = format_scorecard(r)
    assert "before recorded history" in text and "2 inconsistencies remain" in text


def test_benchmark_history_starting_after_the_call_falls_back_to_niftybees():
    class Late(FundPrices):
        def history(self, sym, range_="2y"):
            bars = super().history(sym, range_)
            return bars[self.start + 5:] if sym == "MID150BEES" else bars

    p = Late()
    mem = {"NIFTY50": _mem([]), "NIFTY100": _mem([]), "NIFTYMIDCAP150": _mem(["MIDCO"]), "NIFTYSMALLCAP250": _mem([])}
    row = score_recommendations([_rec(p, "MIDCO")], p, memberships=mem)["rows"][0]
    assert row["benchmark"] == "NIFTYBEES" and "history starts" in row["benchmark_note"]
    assert abs(row["excess"]["60"] - 0.05) < 1e-9


def test_single_benchmark_mode_leaves_excess_nifty_empty():
    p = FundPrices()
    row = score_recommendations([_rec(p, "LARGECO")], p, benchmark="^NSEI")["rows"][0]
    assert all(v is None for v in row["excess_nifty"].values()) and row["excess"]["60"] is not None


def test_membership_cache_keeps_successes_per_day_and_retries_failures_after_ten_minutes(monkeypatch, tmp_path):
    from trading_agent import index_history, scorecard, screen
    scorecard.clear_membership_cache()
    loads, fail = [], {"on": True}

    def load_universe(key):
        loads.append(key)
        if fail["on"] and key == "NIFTY100":
            raise RuntimeError("down")
        return [{"symbol": "X"}]

    monkeypatch.setattr(screen, "load_universe", load_universe)
    monkeypatch.setattr(index_history, "point_in_time", lambda key, cur, sd, progress=None: _mem(cur))
    clock = {"t": 1000.0, "day": "2026-10-09"}
    kw = dict(now_fn=lambda: clock["t"], today_fn=lambda: clock["day"])
    a = scorecard.build_memberships(tmp_path, use_cache=True, **kw)
    assert a["NIFTY100"] is None and a["NIFTY50"] is not None and len(loads) == 4
    scorecard.build_memberships(tmp_path, use_cache=True, **kw)
    assert len(loads) == 4                                  # all cached, the failure too, for now
    clock["t"] += 601
    fail["on"] = False
    b = scorecard.build_memberships(tmp_path, use_cache=True, **kw)
    assert loads[4:] == ["NIFTY100"] and b["NIFTY100"] is not None   # only the failed one is retried
    clock["day"] = "2026-10-10"
    scorecard.build_memberships(tmp_path, use_cache=True, **kw)
    assert len(loads) == 9                                  # a new IST day rebuilds everything
    scorecard.build_memberships(tmp_path, use_cache=False, **kw)
    assert len(loads) == 13                                 # no cache: fresh each time
    scorecard.clear_membership_cache()
