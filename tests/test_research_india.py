"""The India 2007-2016 test's data plumbing (research_india.py), on fakes only: no network, no real prices."""
from datetime import date, timedelta

import pytest

from trading_agent import research_india as ri


def _days(n, start="2007-01-01"):
    d, out = date.fromisoformat(start), []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def test_bhav_url_and_parse_keep_eq_rows_only():
    assert ri.bhav_url("2008-01-01").endswith("/2008/JAN/cm01JAN2008bhav.csv.zip")
    text = ("SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,TIMESTAMP,\n"
            "INFOSYSTCH,EQ,1,1,1,1700.5,1,1,100,170050,1-JAN-2008,\n"
            "ABC,BE,1,1,1,10,1,1,5,50,1-JAN-2008,\n"
            "ZERO,EQ,1,1,1,10,1,1,0,0,1-JAN-2008,\n")
    rows = ri.parse_bhav(text)
    assert [r["symbol"] for r in rows] == ["INFOSYSTCH"]
    assert rows[0]["close"] == 1700.5 and rows[0]["value"] == 170050


@pytest.mark.parametrize("subject, ratio", [
    ("Fv Split Rs.10/- To Rs.2/", 5.0), ("Fv Split-Rs10/- To Re.1/-", 10.0), ("Fv Split Rs.10 To Rs.5", 2.0),
    ("Bonus - 1:1", 2.0), ("Bonus-1:2", 1.5), ("Bonus - 2:7", 9 / 7), ("Bonus 1:1", 2.0),
    ("Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share", 5.0),
    ("Sch Of Agmt- Bonus Deb1:1", None), ("Annual General Meeting / Dividend - Rs 2 Per Share", None)])
def test_split_ratio_reads_old_and_new_wording(subject, ratio):
    got = ri.split_ratio(subject)
    assert (got is None) if ratio is None else got == pytest.approx(ratio, rel=1e-6)


def test_bonus_debentures_are_not_counted_as_share_bonuses():
    assert not ri.is_split_or_bonus("Sch Of Agmt- Bonus Deb1:1")
    assert ri.is_split_or_bonus("Bonus - 1:1") and ri.is_split_or_bonus("Fv Split Rs.10/- To Rs.2/")


@pytest.mark.parametrize("subject, amount", [
    ("Int Dividend- Rs.3 Per Sh", 3.0), ("3rd Int Div-Rs.2/- Pr Shr", 2.0),
    ("Final Dividend Rs.4/- Per Share And Special Dividend Rs.10/- Per Share", 14.0),
    ("Dividend - 50%", None), ("Egm-Election Of Directors", None)])
def test_dividend_amount(subject, amount):
    assert ri.dividend_amount(subject) == amount


def _row(sym, ex, subject):
    d = date.fromisoformat(ex)
    return {"symbol": sym, "exDate": d.strftime("%d-%b-%Y"), "subject": subject}


def test_parse_actions_counts_reads_and_dedupes():
    a = ri.parse_actions([_row("X", "2008-03-03", "Bonus - 1:1"), _row("X", "2008-03-03", "Bonus - 1:1"),
                          _row("Y", "2008-04-01", "Bonus odd wording"), _row("Z", "2008-05-02", "Int Div-Rs.2/-")])
    assert a["split_rows"] == 2 and a["split_read"] == 1
    assert a["splits"] == {"X": [("2008-03-03", 2.0)]}
    assert a["dividends"] == {"Z": [("2008-05-02", 2.0)]}


def test_build_series_adjusts_splits_volume_and_dividends_in_todays_symbols():
    days = _days(4)
    files = {days[0]: [{"symbol": "OLD", "close": 100.0, "volume": 10, "value": 1000, "isin": ""}],
             days[1]: [{"symbol": "OLD", "close": 104.0, "volume": 10, "value": 1040, "isin": ""}],
             days[2]: [{"symbol": "NEW", "close": 50.0, "volume": 20, "value": 1000, "isin": ""}],
             days[3]: [{"symbol": "NEW", "close": 48.0, "volume": 20, "value": 960, "isin": "INF000"}]}
    rename = lambda s, d: "NEW"  # noqa: E731
    acts = {"splits": {"NEW": [(days[2], 2.0)]}, "dividends": {"NEW": [(days[3], 2.0)]}}
    series, info = ri.build_series(days, files, rename, acts)
    bars = series["NEW"]
    assert [b["close"] for b in bars] == [50.0, 52.0, 50.0, 48.0]
    assert [b["volume"] for b in bars] == [20, 20, 20, 20]
    f = 1 - 2.0 / 50.0                       # the dividend on day 4, against day 3's close
    assert [round(b["adj_close"], 6) for b in bars] == [round(x, 6) for x in (50 * f, 52 * f, 50 * f, 48.0)]
    assert info["funds"] == {"NEW"} and info["dividends_skipped"] == 0


def test_build_series_skips_a_dividend_that_reads_as_a_huge_share_of_the_price():
    days = _days(2)
    files = {d: [{"symbol": "A", "close": 10.0, "volume": 1, "value": 10, "isin": ""}] for d in days}
    series, info = ri.build_series(days, files, lambda s, d: s, {"splits": {}, "dividends": {"A": [(days[1], 5.0)]}})
    assert [b["adj_close"] for b in series["A"]] == [10.0, 10.0] and info["dividends_skipped"] == 1


def _series(days, sym, value, every=1, until=None):
    return [{"date": d, "close": 10.0, "adj_close": 10.0, "volume": 1, "value": value, "raw_close": 10.0}
            for k, d in enumerate(days) if k % every == 0 and (until is None or d < until)]


def test_universe_rule_needs_history_recent_trade_no_funds_and_ranks_by_value(monkeypatch):
    monkeypatch.setattr(ri, "UNIVERSE_SIZE", 2)
    days = _days(300)
    series = {"BIG": _series(days, "BIG", 300.0), "MID": _series(days, "MID", 200.0),
              "SMALL": _series(days, "SMALL", 100.0), "SPARSE": _series(days, "SPARSE", 900.0, every=2),
              "NIFTYBEES": _series(days, "NIFTYBEES", 999.0), "FUNDX": _series(days, "FUNDX", 999.0),
              "DEAD": _series(days, "DEAD", 999.0, until=days[150])}
    u = ri.universe_by_month(days, series, funds={"FUNDX"})
    first = days[ri.month_starts(days)[0]][:7]
    assert u[first] == set()                                     # no 126-session history yet
    last = days[ri.month_starts(days)[-1]][:7]
    assert u[last] == {"BIG", "MID"}                             # SPARSE traded too rarely, funds and DEAD are out
    mid = next(d for d in (days[t] for t in ri.month_starts(days)) if days[140] <= d <= days[160])
    assert "DEAD" in u[mid[:7]] or mid > days[150]


def test_unrecorded_split_like_moves_are_found_unless_recorded():
    days = _days(10)
    bars = [{"date": d, "raw_close": 100.0 if k < 5 else 50.0, "close": 0, "value": 1} for k, d in enumerate(days)]
    crash = [{"date": d, "raw_close": 100.0 if k < 5 else 22.0, "close": 0, "value": 1} for k, d in enumerate(days)]
    members = {days[0][:7]: {"S", "C"}}
    none = {"split_records": []}
    found = ri.unrecorded_splits(days, {"S": bars, "C": crash}, members, none)
    assert [(x["symbol"], x["date"]) for x in found] == [("S", days[5])]   # a -78% crash is not split-like
    rec = {"split_records": [("S", days[6])]}
    assert ri.unrecorded_splits(days, {"S": bars}, members, rec) == []


def test_gate_reports_every_check_and_fails_on_any():
    days = _days(30)
    series = {f"S{k}": _series(days, f"S{k}", 1.0) for k in range(3)}
    members = {d[:7]: set(series) for d in days}
    acts = {"split_rows": 100, "split_read": 99, "split_records": [("S0", days[3])]}
    months = sorted({d[:7] for d in days})
    g = ri.gate(days=days, present=set(days), actions=acts, series=series, members=members, trading_months=months)
    assert not g["passes"]                                   # 3 members < 400
    failed = [k for k, ok in g["checks"].items() if not ok]
    assert len(failed) == 1 and failed[0].startswith("universe >= 400")
    assert "DATA GATE" in ri.format_gate(g)


def test_constants_match_the_preregistration():
    text = open("docs/research/preregistered-india-2007-2016.md", encoding="utf-8").read()
    for needle in ("1 Jul 2006", "1 Oct 2007", "1 Apr 2016", "0.20% a year", "**500**", "× 2", "**98%**", "**400**",
                   "**99%**", "**95%**", "**15**"):
        assert needle in text
    assert (ri.DATA_FROM, ri.TRADING_FROM, ri.WINDOW_END) == ("2006-07-01", "2007-10-01", "2016-04-01")
    assert (ri.UNIVERSE_SIZE, ri.COST_MULT_PRIMARY, ri.GATE_MAX_UNRECORDED) == (500, 2.0, 15)


def test_rule_sets_are_the_preregistered_seven():
    from trading_agent.research_studies import FINALIST
    sets = {k: r for k, _, r, _ in ri.rule_sets()}
    assert list(sets) == ["A0", "B", "E", "BRK", "STP", "IVW", "PLN"]
    assert sets["A0"] is FINALIST and sets["E"] is FINALIST
    assert (sets["BRK"].brake, sets["STP"].stop, sets["IVW"].weights) == ("sma100", "atr3", "inverse_vol")
    assert sets["PLN"].buffer is None and not sets["PLN"].tax_aware and sets["PLN"].floor == FINALIST.floor
    add = open("docs/research/preregistered-india-2007-2016-addendum.md", encoding="utf-8").read()
    for needle in ('brake="sma100"', 'stop="atr3"', 'weights="inverse_vol"', "buffer=None, tax_aware=False", "7 trials"):
        assert needle in add


def _bse(rows_by_day):
    head = ["SC_CODE", "SC_NAME", "SC_GROUP", "SC_TYPE", "OPEN", "HIGH", "LOW", "CLOSE", "LAST", "PREVCLOSE",
            "NO_TRADES", "NO_OF_SHRS", "NET_TURNOV", "TDCLOINDI"]
    return lambda d: [head] + [["1", "X", "A", "Q", "0", "0", "0", str(c), "0", "0", "0", "0", "0", f]
                               for c, f in rows_by_day.get(d, [])]


def test_addendum_2_classifies_crash_days_flags_and_mends():
    days = _days(12)
    up = lambda k: 100.0 if k < 6 else 75.0  # noqa: E731
    series = {s: [{"date": d, "raw_close": up(k), "close": up(k), "value": 1} for k, d in enumerate(days)]
              for s in ("CRASH", "RIGHTS", "BONUS", "ODD")}
    series["BONUS"] = [{"date": d, "raw_close": 100.0 if k < 6 else 20.0, "close": 0, "value": 1}
                       for k, d in enumerate(days)]
    flagged = [{"symbol": "CRASH", "date": days[6], "ratio": 0.75}, {"symbol": "RIGHTS", "date": days[6], "ratio": 0.75},
               {"symbol": "BONUS", "date": days[6], "ratio": 0.2}, {"symbol": "ODD", "date": days[6], "ratio": 0.75}]
    calm = {d: 100.0 for d in days}
    crash = dict(calm, **{days[6]: 94.0})
    bse = _bse({days[6]: [(75.2, "XR"), (20.1, "XB")]})
    cls, mends = ri.classify_flagged(flagged[:1], days, crash, series, bse)
    assert cls == {("CRASH", days[6]): "crash day"} and mends == []
    cls, mends = ri.classify_flagged(flagged[1:], days, calm, series, bse)
    assert cls[("RIGHTS", days[6])].startswith("rights or scheme")   # its close matches the XR row
    assert cls[("BONUS", days[6])].startswith("mended") and mends == [("BONUS", days[6], 5.0)]
    assert cls[("ODD", days[6])].startswith("rights") or cls[("ODD", days[6])] == "unexplained"


def test_a_flag_on_another_stock_at_a_different_price_does_not_count():
    days = _days(8)
    series = {"S": [{"date": d, "raw_close": 100.0 if k < 4 else 50.0, "close": 0, "value": 1}
                    for k, d in enumerate(days)]}
    cls, mends = ri.classify_flagged([{"symbol": "S", "date": days[4], "ratio": 0.5}], days, {d: 1.0 for d in days},
                                     series, _bse({days[4]: [(900.0, "XB")]}))
    assert cls == {("S", days[4]): "unexplained"} and mends == []


def test_with_mends_adds_the_split_and_records_it():
    acts = {"splits": {"A": [("2008-01-01", 2.0)]}, "dividends": {}, "split_records": [("A", "2008-01-01")],
            "split_rows": 1, "split_read": 1}
    out = ri.with_mends(acts, [("B", "2009-05-06", 5.0)])
    assert out["splits"]["B"] == [("2009-05-06", 5.0)] and ("B", "2009-05-06") in out["split_records"]
    assert "B" not in acts["splits"]                       # the input is not changed


def test_gate_counts_only_unexplained_moves():
    days = _days(30)
    series = {f"S{k}": _series(days, f"S{k}", 1.0) for k in range(3)}
    series["S0"] = [dict(b, raw_close=10.0 if k < 10 else 7.5) for k, b in enumerate(series["S0"])]
    members = {d[:7]: set(series) for d in days}
    acts = {"split_rows": 100, "split_read": 100, "split_records": [("S1", days[3])]}
    months = sorted({d[:7] for d in days})
    plain = ri.gate(days=days, present=set(days), actions=acts, series=series, members=members, trading_months=months)
    assert len(plain["unrecorded"]) == 1
    crash = ri.gate(days=days, present=set(days), actions=acts, series=series, members=members,
                    trading_months=months, classified={("S0", days[10]): "crash day"})
    assert crash["unrecorded"] == [] and crash["flagged"][0]["class"] == "crash day"
    assert "(crash day)" in ri.format_gate(crash)


def test_held_through_counts_positions_held_across_a_move():
    run = {"trades": [{"symbol": "X", "entry": "2010-01-01", "exit": "2010-03-01"},
                      {"symbol": "Y", "entry": "2010-02-15", "exit": "2010-03-01"}]}
    moves = [{"symbol": "X", "date": "2010-02-10"}, {"symbol": "Y", "date": "2010-02-10"}]
    assert ri.held_through(run, moves, "2016-03-31") == 1


def test_addendum_2_constants_match_the_document():
    text = open("docs/research/preregistered-india-2007-2016-addendum-2.md", encoding="utf-8").read()
    for needle in ("**5% or more**", "`XR`", "`SA`", "`XB`, `XS`, `SS` or `CS`", "stays at **15**"):
        assert needle in text
    assert ri.CRASH_DAY_FALL == -0.05 and ri.BSE_GENUINE_FLAGS == ("XR", "SA")
    assert ri.BSE_SPLIT_FLAGS == ("XB", "XS", "SS", "CS") and ri.GATE_MAX_UNRECORDED == 15


# --------------------------------------------------------------------------- #
# NSE's new daily file (UDiFF), from 8 Jul 2024
# --------------------------------------------------------------------------- #
UDIFF = ("TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,OpnPric,HghPric,LwPric,ClsPric,LastPric,"
         "PrvsClsgPric,TtlTradgVol,TtlTrfVal\n"
         "2025-01-15,2025-01-15,CM,NSE,STK,1,INE216P01012,AAVAS,EQ,1,1,1,1638.40,1,1,149384,244623421.10\n"
         "2025-01-15,2025-01-15,CM,NSE,STK,2,INE000000001,SOMEBE,BE,1,1,1,10,1,1,5,50\n"
         "2025-01-15,2025-01-15,CM,NSE,STK,3,INE000000002,ZEROQTY,EQ,1,1,1,10,1,1,0,0\n"
         "2025-01-15,2025-01-15,CM,NSE,STK,4,INF000000003,NIFTYBEES,EQ,1,1,1,260.5,1,1,1000,260500\n")


def test_udiff_url_and_parse_keep_eq_rows_in_the_same_shape():
    assert ri.udiff_url("2025-01-15") == \
        "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_20250115_F_0000.csv.zip"
    rows = ri.parse_udiff(UDIFF)
    assert [r["symbol"] for r in rows] == ["AAVAS", "NIFTYBEES"]
    assert rows[0] == {"symbol": "AAVAS", "close": 1638.40, "volume": 149384.0, "value": 244623421.10, "isin": "INE216P01012"}
    assert set(rows[0]) == set(ri.parse_bhav("SYMBOL,SERIES,CLOSE,TOTTRDQTY,TOTTRDVAL,ISIN\nA,EQ,1,1,1,X\n")[0])


class _Resp:
    def __init__(self, status, content=b""):
        self.status_code, self.content = status, content


class _Session:
    def __init__(self, answers):
        self.answers, self.urls = answers, []

    def get(self, url, headers=None, timeout=None):
        self.urls.append(url)
        return self.answers(url)


def _zip(name, text):
    import io
    import zipfile
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr(name, text)
    return b.getvalue()


def test_fetch_bhav_reads_the_new_file_from_8_jul_2024_and_the_old_one_before(tmp_path):
    legacy = ("SYMBOL,SERIES,CLOSE,TOTTRDQTY,TOTTRDVAL,ISIN\nOLDCO,EQ,50,10,500,INE1\n")

    def answers(url):
        if "BhavCopy_NSE_CM" in url:
            return _Resp(200, _zip("x.csv", UDIFF))
        return _Resp(200, _zip("y.csv", legacy))
    s = _Session(answers)
    assert ri.UDIFF_FROM == "2024-07-08"
    old = ri.fetch_bhav("2024-07-05", tmp_path, s)
    new = ri.fetch_bhav("2024-07-08", tmp_path, s)
    assert [r["symbol"] for r in old] == ["OLDCO"] and [r["symbol"] for r in new] == ["AAVAS", "NIFTYBEES"]
    assert s.urls[0].endswith("/2024/JUL/cm05JULbhav.csv.zip".replace("cm05JULbhav", "cm05JUL2024bhav")) and "BhavCopy_NSE_CM_0_0_0_20240708" in s.urls[1]
    assert (tmp_path / "2024-07-05.csv").exists() and (tmp_path / "2024-07-08.udiff.csv").exists()
    n = len(s.urls)
    assert [r["symbol"] for r in ri.fetch_bhav("2024-07-08", tmp_path, s)] == ["AAVAS", "NIFTYBEES"] and len(s.urls) == n   # cached
    gone = _Session(lambda url: _Resp(404, b"<!"))
    assert ri.fetch_bhav("2025-01-01", tmp_path, gone) is None and (tmp_path / "2025-01-01.udiff.none").exists()
    assert ri.fetch_bhav("2025-01-01", tmp_path, gone) is None and len(gone.urls) == 1                                   # the miss is cached
