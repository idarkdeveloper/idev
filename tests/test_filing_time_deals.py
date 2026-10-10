from datetime import date, datetime

import pytest

from trading_agent.backtest import run_backtest, visible_after
from trading_agent.deal_events import consolidate_deals, event_text, normalise_client
from trading_agent.filing_time import usable_by, usable_from
from trading_agent.fundamentals_history import point_in_time
from trading_agent.quiver import DisclosedTrade
from trading_agent.replay.clock import ReplayClock
from trading_agent.replay.news import ClockedNews


# -- usable_from -----------------------------------------------------------------
@pytest.mark.parametrize("stamp,expected", [
    ("2026-10-07T14:59:00", date(2026, 10, 7)),     # Wed, before 15:00: same day
    ("2026-10-07T15:00:00", date(2026, 10, 8)),     # at 15:00: next day
    ("2026-10-07T15:28:00", date(2026, 10, 8)),
    ("2026-10-07T18:00:00", date(2026, 10, 8)),
    ("2026-10-09T18:00:00", date(2026, 10, 12)),    # Friday evening -> Monday
    ("2026-10-10T11:00:00", date(2026, 10, 12)),    # Saturday morning -> Monday
    ("08-Oct-2026 14:59:59", date(2026, 10, 8)),    # NSE style
    ("2026-10-07", date(2026, 10, 8)),              # bare date: time unknown, after the close
    ("2026-10-07T09:30:00Z", date(2026, 10, 7)),    # 15:00 IST exactly... 09:30Z is 15:00 IST -> next day
])
def test_usable_from(stamp, expected):
    if stamp.endswith("Z"):
        expected = date(2026, 10, 8)
    assert usable_from(stamp) == expected


def test_usable_from_skips_holidays():
    class Cal:
        def is_trading_day(self, d):
            return d.weekday() < 5 and d != date(2026, 10, 8)
    assert usable_from("2026-10-07T18:00:00", Cal()) == date(2026, 10, 9)
    assert usable_from("2026-10-08T10:00:00", Cal()) == date(2026, 10, 9)   # holiday morning
    assert usable_from(datetime(2026, 10, 7, 10, 0)) == date(2026, 10, 7)


def test_unreadable_stamp_is_not_usable():
    with pytest.raises(ValueError):
        usable_from("soon")
    assert usable_by("soon", "2030-01-01") is False


# -- point-in-time results ---------------------------------------------------------
def test_result_filed_after_cutoff_is_known_next_day_only():
    rec = {"period_end": "2026-09-30", "available": "2026-10-07T18:43:50", "profit_q": 1.0, "profit_ytd": 1.0}
    assert point_in_time([rec], "2026-10-07") is None
    assert point_in_time([rec], "2026-10-08")["latest_quarter"] == "2026-09-30"
    early = dict(rec, available="2026-10-07T14:59:00")
    assert point_in_time([early], "2026-10-07") is not None


# -- Replay announcements --------------------------------------------------------
def test_replay_hides_late_announcements_on_their_own_day():
    class C:
        def announcement_history(self, s):
            return [{"at": "2026-10-07 15:00:00", "text": "late"}, {"at": "2026-10-07 14:59:00", "text": "early"}]
    got = ClockedNews(C(), ReplayClock("2026-10-07")).for_symbol("X")["items"]
    assert [a["text"] for a in got] == ["early"]


# -- insider backtest entry ----------------------------------------------------------
class Px:
    def history(self, symbol, range_="2y"):
        return [{"date": f"2026-01-{d:02d}", "close": 100.0 + d, "adj_close": 100.0 + d, "volume": 1}
                for d in range(1, 28)]


def _insider(filed):
    return DisclosedTrade(source="insider", investor="A PROMOTER", ticker="UP", transaction="Purchase",
                          transaction_date="2026-01-10", report_date="2026-01-12", size="1 sh",
                          raw={"filed_at": filed} if filed else {})


def test_insider_entry_waits_for_the_filing():
    r = run_backtest("x", [_insider("2026-01-14T12:00:00")], Px(), horizons=(1,))
    assert r.outcomes[0].entry_date == "2026-01-14"          # filed before 15:00: that day's close
    r = run_backtest("x", [_insider("2026-01-14T15:28:00")], Px(), horizons=(1,))
    assert r.outcomes[0].entry_date == "2026-01-15"
    r = run_backtest("x", [_insider(None)], Px(), horizons=(1,))
    assert r.outcomes[0].entry_date == "2026-01-13"           # report date, strictly after


def test_bulk_deal_entry_unchanged():
    d = DisclosedTrade("bulk", "C", "UP", "Purchase", "2026-01-10", "2026-01-10", "1 sh", {})
    assert visible_after(d) == "2026-01-10"


# -- cross-exchange events ----------------------------------------------------------
def _deal(ex, client, side="Purchase", qty=100000, px=250.0, day="2026-01-12", sym="ABC", kind="bulk"):
    raw = {"qty": str(qty), "watp": str(px), "clientName": client}
    if ex == "BSE":
        raw["nse_symbol"] = sym
    return DisclosedTrade(source=kind, investor=client, ticker=sym, transaction=side, transaction_date=day,
                          report_date=day, size=f"{qty} sh @ ₹{px}", raw=raw, exchange=ex)


@pytest.mark.parametrize("a,b,expected", [
    ("HRTI PRIVATE LIMITED", "HRTI PVT LTD", True),
    ("ABC LLP", "ABC LIMITED LIABILITY PARTNERSHIP", True),
    ("ABC LTD", "ABC PVT LTD", False),
    ("ABC PVT. LTD.", "ABC PRIVATE LIMITED", True),
    ("A-B & C (INDIA) LTD", "A B C INDIA LIMITED", True),
])
def test_client_normalisation(a, b, expected):
    assert (normalise_client(a) == normalise_client(b)) is expected


def test_same_client_two_exchanges_is_one_event_with_vwap():
    nse = _deal("NSE", "HRTI PRIVATE LIMITED", qty=1_500_000, px=250.0)
    bse = _deal("BSE", "HRTI PVT LTD", qty=610_000, px=272.0)
    ev = consolidate_deals([nse, bse])
    assert len(ev) == 1
    e = ev[0]
    assert e.exchange == "NSE + BSE" and e.raw["exchanges"] == ["NSE", "BSE"]
    assert e.raw["qty"] == 2_110_000
    assert e.raw["watp"] == round((1_500_000 * 250 + 610_000 * 272) / 2_110_000, 2)
    assert "NSE + BSE" in e.summary()
    assert event_text(e).startswith("HRTI bought 21.1L shares of ABC across NSE + BSE (₹")
    assert nse.key != bse.key and e.raw["members"] == [nse.key, bse.key]   # originals keep their seen-keys


def test_different_side_client_suffix_or_day_stay_separate():
    base = _deal("NSE", "ABC LTD")
    assert len(consolidate_deals([base, _deal("BSE", "ABC LTD", side="Sale")])) == 2
    assert len(consolidate_deals([base, _deal("BSE", "ABC PVT LTD")])) == 2
    assert len(consolidate_deals([base, _deal("BSE", "XYZ LTD")])) == 2
    assert len(consolidate_deals([base, _deal("BSE", "ABC LTD", day="2026-01-13")])) == 2
    assert consolidate_deals([base])[0] is base
    ins = _deal("NSE", "ABC LTD", kind="insider")
    assert consolidate_deals([ins, ins]) == [ins, ins]


def test_backtest_counts_a_two_exchange_event_once():
    nse, bse = _deal("NSE", "HRTI PRIVATE LIMITED", sym="UP"), _deal("BSE", "HRTI PVT LTD", sym="UP")
    r = run_backtest("x", [nse, bse], Px(), horizons=(1,))
    assert len(r.outcomes) == 1 and r.outcomes[0].deal.exchange == "NSE + BSE"
    assert r.summary()["by_side"]["Purchase"]["1"]["n"] == 1
