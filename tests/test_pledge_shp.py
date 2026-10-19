"""Promoter pledges from NSE's quarterly shareholding filings (pledge_shp.py), the fallback used when NSE's pledge list
is empty. Fake NSE client and fake XBRL: parsing, the newest filing, the per-company cache, the link-host check, and the
filter book using it (stored with its source, progress kept for the Health page). No network."""
from datetime import date, datetime, timedelta

import pytest

from trading_agent import indian_filters as fi
from trading_agent.health import _filters_row
from trading_agent.pledge_shp import ShpPledges, latest_filing, parse_shp_pledge
from trading_agent.timezones import IST

MON = datetime(2026, 10, 19, 10, 0, tzinfo=IST)


def xbrl(promoter_shares, pledged):
    """The tags NSE's shareholding XBRL carries, for the promoter group and the whole company."""
    return (f'<xbrli:xbrl><in-bse-shp:NumberOfShares contextRef="ShareholdingOfPromoterAndPromoterGroup_ContextI" '
            f'decimals="INF" unitRef="shares">{promoter_shares}</in-bse-shp:NumberOfShares>'
            f'<in-bse-shp:NumberOfShares contextRef="ShareholdingPattern_ContextI" decimals="INF" unitRef="shares">'
            f'{promoter_shares * 2}</in-bse-shp:NumberOfShares>'
            f'<in-bse-shp:NumberOfSharesEncumberedUnderPledged contextRef="OtherIndianShareholders_ContextI" '
            f'decimals="INF" unitRef="shares">{pledged}</in-bse-shp:NumberOfSharesEncumberedUnderPledged>'
            f'<in-bse-shp:NumberOfSharesEncumberedUnderPledged contextRef="ShareholdingOfPromoterAndPromoterGroup_ContextI" '
            f'decimals="INF" unitRef="shares">{pledged}</in-bse-shp:NumberOfSharesEncumberedUnderPledged></xbrli:xbrl>')


def filing(sym, quarter, filed, rec, host="nsearchives.nseindia.com"):
    return {"symbol": sym, "date": quarter, "broadcastDate": filed, "submissionDate": filed[:11], "recordId": rec,
            "xbrl": f"https://{host}/corporate/xbrl/SHP_{rec}.xml"}


class Resp:
    def __init__(self, content):
        self.content = content.encode()

    def raise_for_status(self):
        pass


class FakeClient:
    """_get answers the filing list per symbol; session.get answers an XBRL by record id. Records both."""

    def __init__(self, lists, files, extra=None):
        self.lists, self.files, self.extra = lists, files, extra or {}
        self.gets, self.downloads = [], []
        self.timeout = 5
        self.session = self

    def _get(self, path, params=None, referer=None):
        if path == "api/corporate-share-holdings-master":
            self.gets.append(params["symbol"])
            a = self.lists[params["symbol"]]
            if isinstance(a, BaseException):
                raise a
            return a
        a = self.extra[path]
        if isinstance(a, BaseException):
            raise a
        return a

    def get(self, url, headers=None, timeout=None):
        assert headers and "Mozilla" in headers["User-Agent"]
        self.downloads.append(url)
        return Resp(self.files[url.rsplit("SHP_", 1)[1][:-4]])


def test_parse_reads_the_promoter_groups_pledged_share():
    # Adani Enterprises, 30 Sep 2026 filing: 77,00,000 of 97,42,34,554 promoter shares pledged
    assert parse_shp_pledge(xbrl(974234554, 7700000)) == {"pct": 0.79, "pledged": 7700000, "promoter_shares": 974234554}
    assert parse_shp_pledge(xbrl(0, 0))["pct"] == 0.0                 # promoters hold nothing: nothing pledged
    assert parse_shp_pledge("<xbrl>no promoter tags</xbrl>") is None


def test_the_newest_quarter_wins_and_a_revision_within_it():
    rows = [filing("A", "30-JUN-2026", "14-JUL-2026 10:00:00", "1"),
            filing("A", "30-SEP-2026", "12-OCT-2026 18:00:00", "2"),
            filing("A", "30-SEP-2026", "15-OCT-2026 11:00:00", "3"),        # revised filing for the same quarter
            {"date": "31-DEC-2026", "xbrl": None}]                          # no file: ignored
    assert latest_filing(rows)["recordId"] == "3"
    assert latest_filing([]) is None and latest_filing({"error": 1}) is None


def test_build_caches_each_filing_and_asks_again_only_after_a_week(tmp_path):
    lists = {"HI": [filing("HI", "30-SEP-2026", "12-OCT-2026 18:00:00", "10")],
             "LO": [filing("LO", "30-SEP-2026", "10-OCT-2026 18:00:00", "20")]}
    files = {"10": xbrl(1000, 300), "20": xbrl(1000, 0)}
    day = [date(2026, 10, 19)]
    c = FakeClient(lists, files)
    sp = ShpPledges(tmp_path, today=lambda: day[0])
    res = sp.build(c, ["hi", "LO"])
    assert res["ready"] and res["done"] == 2 and res["symbols"]["HI"] == {"pct": 30.0, "filed": "2026-10-12T18:00:00"}
    assert len(c.downloads) == 2
    sp.build(c, ["HI", "LO"])                                   # same day: nothing asked again
    assert c.gets == ["HI", "LO"] and len(c.downloads) == 2
    day[0] += timedelta(days=7)
    sp.build(c, ["HI", "LO"])                                   # a week on: the list again, the same filing not re-downloaded
    assert c.gets == ["HI", "LO", "HI", "LO"] and len(c.downloads) == 2


def test_a_link_to_another_host_is_refused_and_one_failure_does_not_stop_the_rest(tmp_path):
    lists = {"BAD": [filing("BAD", "30-SEP-2026", "12-OCT-2026 18:00:00", "1", host="evil.example.com")],
             "DOWN": ConnectionError("x"), "OK": [filing("OK", "30-SEP-2026", "12-OCT-2026 18:00:00", "2")]}
    c = FakeClient(lists, {"1": xbrl(10, 10), "2": xbrl(10, 1)})
    res = ShpPledges(tmp_path, today=lambda: date(2026, 10, 19)).build(c, ["BAD", "DOWN", "OK"])
    assert set(res["symbols"]) == {"OK"} and res["errors"] == 2 and not res["ready"]
    assert all("evil" not in u for u in c.downloads)


def test_the_deadline_stops_between_companies(tmp_path):
    lists = {s: [filing(s, "30-SEP-2026", "12-OCT-2026 18:00:00", s)] for s in ("A", "B", "C")}
    c = FakeClient(lists, {s: xbrl(10, 0) for s in "ABC"})
    clock = iter([0.0, 0.0, 99.0, 99.0])
    res = ShpPledges(tmp_path, today=lambda: date(2026, 10, 19), monotonic=lambda: next(clock)).build(c, "ABC", deadline=10.0)
    assert res["done"] == 2 and res["total"] == 3 and c.gets == ["A", "B"]


EMPTY = {"api/reportASM": {"data": []}, "api/reportGSM": {"data": []}, "api/corporate-board-meetings": [],
         "api/corporate-pledgedata": {"comNameList": [], "data": []},
         fi.PLEDGE_COUNT_PATH: {"data": [{"NUMBER_OF_COMPANY": "0"}]}}


class Cal:
    def is_trading_day(self, d):
        return d.weekday() < 5


def test_the_filter_book_falls_back_when_nses_pledge_list_is_empty(tmp_path):
    lists = {"HI": [filing("HI", "30-SEP-2026", "12-OCT-2026 18:00:00", "10")],
             "LO": [filing("LO", "30-SEP-2026", "10-OCT-2026 18:00:00", "20")]}
    c = FakeClient(lists, {"10": xbrl(1000, 300), "20": xbrl(1000, 50)}, extra=EMPTY)
    b = fi.FilterBook(tmp_path, now_fn=lambda: MON, calendar=Cal())
    got = b.tick(c, MON, pledge_symbols=lambda: ["HI", "LO"])
    d = b.load()
    assert got.get("pledge") == 2 and d["pledge"]["source"] == fi.SHP_SOURCE and d["pledge_fallback"] == "2026-10-19"
    assert "pledge" not in ((d.get("tries") or {}).get("2026-10-19") or {}).get("errors", {})
    assert b.check("HI")["block"] and "30.0%" in b.refuse_buy("HI")
    assert not b.check("LO")["block"] and b.check("LO")["flags"] == ["Pledge 5%"]
    row = _filters_row(type("S", (), {"market": "in"})(), tmp_path, MON, Cal(), None)
    assert fi.SHP_SOURCE in row["detail"]


def test_progress_is_kept_and_the_next_tick_goes_straight_to_the_filings(tmp_path):
    lists = {"HI": [filing("HI", "30-SEP-2026", "12-OCT-2026 18:00:00", "10")], "LO": ConnectionError("refused")}
    c = FakeClient(lists, {"10": xbrl(1000, 300)}, extra=EMPTY)
    b = fi.FilterBook(tmp_path, now_fn=lambda: MON, calendar=Cal())
    assert "pledge" not in b.tick(c, MON, pledge_symbols=lambda: ["HI", "LO"])      # 1 of 2: not enough yet
    d = b.load()
    assert d["pledge_progress"]["done"] == 1 and d["pledge_progress"]["total"] == 2 and "pledge" not in d
    row = _filters_row(type("S", (), {"market": "in"})(), tmp_path, MON, Cal(), None)
    assert "1 of 2 stocks so far" in row["detail"] and "pledge" not in (row.get("error") or "")
    c.extra = {**EMPTY, "api/corporate-pledgedata": AssertionError("NSE's list is not asked again today")}
    c.lists["LO"] = [filing("LO", "30-SEP-2026", "10-OCT-2026 18:00:00", "20")]
    c.files["20"] = xbrl(1000, 0)
    later = MON + timedelta(minutes=1)
    assert b.tick(c, later, pledge_symbols=lambda: ["HI", "LO"]).get("pledge") == 2


def test_without_the_symbol_list_the_old_error_stays(tmp_path):
    b = fi.FilterBook(tmp_path, now_fn=lambda: MON, calendar=Cal())
    b.tick(FakeClient({}, {}, extra=EMPTY), MON)
    assert "NSE is publishing no pledge data" in b.load()["tries"]["2026-10-19"]["errors"]["pledge"]


@pytest.mark.parametrize("bad", ["<x/>", ""])
def test_unreadable_xbrl_is_an_error_for_that_company_only(tmp_path, bad):
    c = FakeClient({"A": [filing("A", "30-SEP-2026", "12-OCT-2026 18:00:00", "1")]}, {"1": bad})
    res = ShpPledges(tmp_path, today=lambda: date(2026, 10, 19)).build(c, ["A"])
    assert res["done"] == 0 and res["errors"] == 1


def test_a_company_with_no_promoters_pledges_nothing(tmp_path):
    """IDFC First Bank's filing: its promoters were dissolved in a merger, so the XBRL has no promoter section and the
    filing list says 0% promoter holding."""
    row = {**filing("IDFCFIRSTB", "30-JUN-2026", "18-JUL-2026 16:15:39", "7"), "pr_and_prgrp": "0"}
    c = FakeClient({"IDFCFIRSTB": [row]}, {"7": "<xbrl>public only</xbrl>"})
    res = ShpPledges(tmp_path, today=lambda: date(2026, 10, 19)).build(c, ["IDFCFIRSTB"])
    assert res["symbols"]["IDFCFIRSTB"]["pct"] == 0.0 and res["ready"]
