"""Survivorship repairs (survivorship-2026-10-22.md): the NSE gap fill on fakes only, no network."""
import pytest

from trading_agent import survivorship as sv
from trading_agent.research_sim import load_market

from .test_research_india import _days


def _actions(**splits):
    return {"splits": {k: v for k, v in splits.items()}, "dividends": {}, "split_rows": 0, "split_read": 0,
            "split_records": []}


def test_fill_from_files_follows_renames_adjusts_splits_and_names_what_is_still_missing():
    days = _days(10, "2021-01-04")
    files = {}
    for d in days:
        old = d < days[5]
        files[d] = [{"symbol": "OLDCO" if old else "NEWCO", "close": 200.0 if old else 100.0, "volume": 1000,
                     "value": 1e5, "isin": "INE1"},
                    {"symbol": "OTHER", "close": 50.0, "volume": 10, "value": 500, "isin": "INE2"}]
    out = sv.fill_from_files(["NEWCO", "GHOST"], days, files, [("OLDCO", "NEWCO", days[5])],
                             _actions(NEWCO=[(days[5], 2.0)]))
    assert list(out["series"]) == ["NEWCO"] and out["still_missing"] == ["GHOST"] and out["stocks"] == 2
    bars = out["series"]["NEWCO"]
    assert [b["date"] for b in bars] == days and all(b["close"] == pytest.approx(100.0) for b in bars)
    assert bars[0]["volume"] == 2000 and bars[-1]["volume"] == 1000 and bars[0]["adj_close"] == pytest.approx(100.0)


class _Client:
    def __init__(self, rows=None, fail=False):
        self.rows, self.fail = rows or [], fail

    def _get(self, path, params=None):
        if self.fail:
            raise RuntimeError("403 refused")
        return {"data": self.rows}


def test_the_gate_stops_the_fill_before_any_claim(tmp_path, monkeypatch):
    days = _days(10, "2021-01-04")
    monkeypatch.setattr(sv, "load_files", lambda d, cache, sf, **k: {x: [{"symbol": "X", "close": 1.0, "volume": 1,
                                                                        "value": 1.0, "isin": ""}] for x in d[:5]})
    fill = sv.nse_filler(tmp_path, renames=[], client_factory=_Client)
    with pytest.raises(sv.FillError, match="5 of 10"):
        fill(["X"], days)
    monkeypatch.setattr(sv, "load_files", lambda d, cache, sf, **k: {x: [{"symbol": "X", "close": 1.0, "volume": 1,
                                                                        "value": 1.0, "isin": ""}] for x in d})
    with pytest.raises(sv.FillError, match="corporate actions"):
        sv.nse_filler(tmp_path, renames=[], client_factory=lambda: _Client(fail=True))(["X"], days)
    assert fill([], days) == {"series": {}, "still_missing": [], "files_loaded": 0, "files_wanted": 0,
                              "dividends_skipped": 0, "stocks": 0}


def test_load_market_fills_missing_members_and_reports_them(tmp_path, monkeypatch):
    days = _days(12, "2021-01-04")
    rows = [{"symbol": "NEWCO", "close": 100.0, "volume": 1000, "value": 1e5, "isin": "INE1"}]
    monkeypatch.setattr(sv, "load_files", lambda d, cache, sf, **k: {x: rows for x in d})
    fill = sv.nse_filler(tmp_path, renames=[], client_factory=_Client, progress=lambda s: None)

    class Prices:
        def history(self, sym, rng):
            if sym in ("FUND", "A"):
                return [{"date": d, "close": 10.0, "adj_close": 10.0, "volume": 1e6} for d in days]
            raise KeyError(sym)
    m = load_market(["A", "NEWCO", "GHOST"], Prices(), regime="FUND", years=1, until=None, fill=fill)
    assert m.filled == ["NEWCO"] and m.missing == ["GHOST"] and len(m.raw["NEWCO"]) == 12
    assert m.fill_report["files_loaded"] == 12 and m.fill_report["files_wanted"] == 12
    assert (tmp_path / "research" / f"corporate_actions_{days[0]}_{days[-1]}.json").exists()
    note = sv.fill_note(m)
    assert "1 member(s)" in note and "GHOST" in note and "12 of 12" in note
    bare = load_market(["A", "GHOST"], Prices(), regime="FUND", years=1, until=None)
    assert bare.filled == [] and bare.missing == ["GHOST"] and "no member filled" in sv.fill_note(bare)


def test_research_market_stops_before_the_claim_when_the_fill_gate_fails(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from trading_agent import cli, index_history, research_sim, runner, screen
    monkeypatch.setattr(screen, "load_universe", lambda u: [{"symbol": "A"}])
    monkeypatch.setattr(index_history, "point_in_time", lambda *a, **k: None)
    monkeypatch.setattr(runner, "free_prices", lambda s: None)
    seen = {}

    def boom(*a, **k):
        seen["fill"] = k.get("fill")
        raise sv.FillError("only 3 of 10 trading days' NSE files loaded")
    monkeypatch.setattr(research_sim, "load_market", boom)
    args = SimpleNamespace(universe="NIFTYMIDCAP150", years=5, until="2025-10-01", fill_nse=True)
    with pytest.raises(SystemExit, match="Stopped before any claim"):
        cli._research_market(args, SimpleNamespace(state_dir=tmp_path))
    assert callable(seen["fill"])
