"""Flows, breadth, price bands and the closed-bar archive. No network: NSE files are fixtures captured from the public
pages (FII/DII trade report, full bhavcopy, price band list), trimmed."""

import dataclasses
import json
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from trading_agent import breadth, flows
from trading_agent.bands import BandBook, parse_sec_list
from trading_agent.membership import Membership
from trading_agent.price_archive import PriceArchive, detect_split, find_split
from trading_agent.prices import YahooPrices
from trading_agent.screen import run_screen
from trading_agent.timezones import IST

FX = Path(__file__).parent / "fixtures"


class Resp:
    def __init__(self, payload, status=200):
        self.status_code = status
        self.content = payload if isinstance(payload, bytes) else (payload.encode() if isinstance(payload, str) else json.dumps(payload).encode())
        self._payload = payload

    def json(self):
        return self._payload if not isinstance(self._payload, (bytes, str)) else json.loads(self.content)

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


class Session:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, **kw):
        self.calls.append(url)
        for key, val in self.routes.items():
            if key in url:
                if isinstance(val, Exception):
                    raise val
                return val if isinstance(val, Resp) else Resp(val)
        return Resp("", 404)


class Client:
    """Stands in for NSEClient: ._get(path) JSON and .session."""

    def __init__(self, payload=None, session=None):
        self.payload, self.session, self.gets = payload, session, 0

    def _get(self, path, params=None, referer=None):
        self.gets += 1
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


# -- FII / DII ------------------------------------------------------------------------
def test_parse_real_fii_dii_fixture():
    row = flows.parse_fii_dii(json.loads((FX / "fiidii_sample.json").read_text()))
    assert row == {"date": "2026-10-09", "dii_buy": 15626.53, "dii_sell": 10883.27, "dii_net": 4743.26,
                   "fii_buy": 10373.99, "fii_sell": 13942.89, "fii_net": -3568.9}


@pytest.mark.parametrize("bad", [{}, [], [{"category": "DII", "date": "09-Oct-2026", "buyValue": "1", "sellValue": "1"}],
                                 [{"category": "FII/FPI", "date": "bad", "buyValue": "1", "sellValue": "1"}]])
def test_parse_rejects_other_payloads(bad):
    with pytest.raises(ValueError):
        flows.parse_fii_dii(bad)


def _weekdays(n, start=date(2026, 9, 28)):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _rows(nets):
    return [{"date": d.isoformat(), "fii_net": f, "dii_net": dd, "fii_buy": 0, "fii_sell": 0, "dii_buy": 0, "dii_sell": 0}
            for d, (f, dd) in zip(_weekdays(len(nets)), nets)]


def test_summary_five_day_and_negative_streak():
    rows = _rows([(500, 100), (-2000, 1000), (-1500, 900), (-1200, 800), (-1000, 700), (-2340, 1980)])
    s = flows.summarize(rows)
    assert s["fii_net_5d"] == -8040 + 0 and s["days"] == 5     # last five sessions only
    assert s["dii_net_5d"] == 5380 and s["combined_net_5d"] == -2660
    assert s["negative_streak"] == 5                            # every day since the first is net negative combined
    rows2 = _rows([(-100, -50), (-100, 300)])
    assert flows.summarize(rows2)["negative_streak"] == 0


def test_flows_line_wording_and_staleness():
    rows = _rows([(-2000, 1000), (-1500, 900), (-1200, 800), (-1000, 700), (-2400, 1980)])
    line = flows.flows_line(rows, today=date(2026, 10, 3))
    assert line == "FII −₹2,400 cr, DII +₹1,980 cr on Fri 2 Oct (provisional NSE figures); 5-day net FII −₹8,100 cr"
    assert flows.flows_line(rows, today=date(2026, 10, 20)) is None
    assert flows.flows_line([]) is None


def test_store_is_idempotent_and_fetches_through_the_client(tmp_path):
    payload = json.loads((FX / "fiidii_sample.json").read_text())
    store = flows.FlowStore(tmp_path)
    c = Client(payload)
    row = store.fetch(c)
    assert row["date"] == "2026-10-09" and len(store.rows()) == 1
    store.fetch(c)
    assert len(store.rows()) == 1                               # the same session is not stored twice


def test_flows_fetch_once_per_trading_day_after_19(tmp_path):
    payload = json.loads((FX / "fiidii_sample.json").read_text())
    store, c = flows.FlowStore(tmp_path), Client(payload)
    for r in _rows([(1, 1)] * 5):                                  # earlier sessions stored: no morning catch-up
        store.add(r)
    at = lambda h, m, day=9: datetime(2026, 10, day, h, m, tzinfo=IST)   # noqa: E731
    assert store.tick(c, at(18, 59)) is None and c.gets == 0     # before 19:00
    assert store.tick(c, at(19, 0))["date"] == "2026-10-09"
    assert store.tick(c, at(19, 30)) is None and c.gets == 1     # done for the day
    assert store.tick(c, at(19, 0, day=10)) is None and c.gets == 1   # Saturday
    # a day on which NSE still serves yesterday's session is retried, not marked done
    c2, store2 = Client(payload), flows.FlowStore(tmp_path / "b")
    assert store2.tick(c2, at(19, 0, day=12)) is None and c2.gets == 1    # Monday: payload still says 9 Oct
    assert store2.tick(c2, at(19, 5, day=12)) is None and c2.gets == 1    # too soon to retry
    assert store2.tick(c2, at(19, 25, day=12)) is None and c2.gets == 2   # retried after 20 minutes
    # a failure never raises and never repeats every tick
    c3 = Client(RuntimeError("blocked"))
    store3 = flows.FlowStore(tmp_path / "c")
    assert store3.tick(c3, at(19, 0)) is None and store3.tick(c3, at(19, 1)) is None and c3.gets == 1


# -- price bands ----------------------------------------------------------------------------
def test_parse_real_price_band_fixture():
    bands = parse_sec_list((FX / "price_bands_sample.csv").read_text())
    assert bands["ABAN"] == 2 and bands["RELIANCE"] is None and bands["TCS"] is None
    assert set(v for v in bands.values() if v is not None) >= {2, 5, 10, 20}


def _book(tmp_path, on=True):
    b = BandBook(tmp_path, enabled=on, now_fn=lambda: datetime(2026, 10, 9, 12, 0, tzinfo=IST))
    b.save({"AAA": 2, "BBB": 5, "CCC": 10, "DDD": 20, "EEE": None}, date(2026, 10, 9))
    return b


def test_band_rules(tmp_path):
    b = _book(tmp_path)
    assert b.rule("AAA")["skip"] and b.rule("BBB")["reason"] == "price band 5%: liquidity can vanish in a fall; not bought"
    assert not b.rule("CCC")["skip"] and b.rule("CCC")["caution"] and "caution" in b.rule("CCC")["reason"]
    for sym in ("DDD", "EEE", "UNKNOWN"):
        r = b.rule(sym)
        assert not r["skip"] and not r["caution"] and r["reason"] is None
    assert b.refuse_buy("bbb").startswith("BBB: price band 5%")
    assert b.refuse_buy("ccc") is None


def test_missing_file_or_filter_off_means_no_filtering(tmp_path):
    empty = BandBook(tmp_path / "none")
    assert empty.rule("AAA")["skip"] is False and empty.refuse_buy("AAA") is None
    assert _book(tmp_path / "off", on=False).refuse_buy("AAA") is None


def test_bands_fetched_before_nine_once_per_day(tmp_path):
    sess = Session({"sec_list.csv": (FX / "price_bands_sample.csv").read_bytes()})
    client = Client(session=sess)
    b = BandBook(tmp_path)
    assert b.tick(client, datetime(2026, 10, 12, 8, 30, tzinfo=IST)) > 20
    assert b.tick(client, datetime(2026, 10, 12, 8, 31, tzinfo=IST)) is None       # already stored for today
    assert len(sess.calls) == 1
    late = BandBook(tmp_path / "late")                                              # missing at 11:40: fetched then
    assert late.tick(client, datetime(2026, 10, 12, 11, 40, tzinfo=IST)) > 20
    assert b.tick(client, datetime(2026, 10, 11, 8, 30, tzinfo=IST)) is None       # Sunday
    assert BandBook(tmp_path / "sun").tick(client, datetime(2026, 10, 11, 8, 30, tzinfo=IST)) is None
    assert BandBook(tmp_path / "x").tick(Client(session=Session({"sec_list": Resp("", 503)})),
                                         datetime(2026, 10, 13, 8, 0, tzinfo=IST)) is None   # failure: no raise


def _bars(n=300, start=100.0, step=0.5):
    d0 = date(2025, 1, 1)
    return [{"date": (d0 + timedelta(days=i)).isoformat(), "close": start + i * step, "adj_close": start + i * step,
             "volume": 1e6} for i in range(n)]


class Prices:
    def history(self, sym, rng="2y"):
        return _bars()


def test_screen_skips_band_stocks_and_shows_why(tmp_path):
    book = _book(tmp_path)
    members = [{"symbol": s, "name": s, "industry": ""} for s in ("AAA", "BBB", "CCC", "DDD", "EEE")]
    res = run_screen(members, Prices(), top=10, bands=book, min_turnover=0)
    eligible = {r["symbol"] for r in res["top"]}
    assert "AAA" not in eligible and "BBB" not in eligible and {"CCC", "DDD", "EEE"} <= eligible
    assert res["band_skipped"] == 2
    by = {r["symbol"]: r for r in res["all"]}
    assert by["BBB"]["band_skipped"] and "not bought" in by["BBB"]["band_note"]
    assert by["CCC"]["band"] == "10%" and "caution" in by["CCC"]["band_note"]
    assert run_screen(members, Prices(), top=10, min_turnover=0)["band_skipped"] == 0       # off by default


# -- breadth -----------------------------------------------------------------------------------
def test_parse_real_bhavcopy_fixture():
    day, closes = breadth.parse_bhavcopy((FX / "bhavcopy_sample.csv").read_text())
    assert day == "2026-10-09"
    assert closes["20MICRONS"] == (194.81, 196.12) and "RELIANCE" in closes
    assert all(p > 0 and c > 0 for p, c in closes.values())
    with pytest.raises(ValueError):
        breadth.parse_bhavcopy("a,b\n1,2\n")


def test_compute_day_counts_and_ratio():
    closes = {"A": (100, 101), "B": (100, 99), "C": (100, 100), "D": (50, 52), "OUT": (10, 11)}
    r = breadth.compute_day("2026-10-09", closes, ["A", "B", "C", "D", "MISSING"])
    assert (r["advances"], r["declines"], r["unchanged"], r["n"]) == (2, 1, 1, 4)
    assert r["ratio"] == round(2 / 3, 4) and r["above_50dma_pct"] is None


def test_above_ma_needs_fifty_bars():
    assert breadth.above_ma(_bars(49), "2030-01-01") is None
    assert breadth.above_ma(_bars(60), "2030-01-01") is True
    assert breadth.above_ma(_bars(60, step=-0.5), "2030-01-01") is False


def _row(day, ratio, above=None):
    return {"date": day, "ratio": ratio, "above_50dma_pct": above, "advances": 0, "declines": 0, "unchanged": 0, "n": 0}


def test_breadth_line_streak_and_broad_selling():
    rows = [_row("2026-10-07", 0.30), _row("2026-10-08", 0.31), _row("2026-10-09", 0.38 - 0.10, 41.2)]
    line = breadth.breadth_line(rows, today=date(2026, 10, 10))
    assert line == ("Breadth (NIFTY 500): 28% of stocks rose on Fri 9 Oct (below 35% is weak); 3rd day below 35%, "
                    "broad selling; 41% are above their 50-day average.")
    ok = breadth.breadth_line([_row("2026-10-09", 0.38)], today=date(2026, 10, 10))
    assert ok == "Breadth (NIFTY 500): 38% of stocks rose on Fri 9 Oct (below 35% is weak)."
    two = breadth.breadth_line([_row("2026-10-08", 0.3), _row("2026-10-09", 0.2)])
    assert "2nd day below 35%" in two and "broad selling" not in two
    assert breadth.breadth_line(rows, today=date(2026, 11, 1)) is None


def test_breadth_fetch_day_stores_and_retries(tmp_path):
    sess = Session({"sec_bhavdata_full_09102026.csv": (FX / "bhavcopy_sample.csv").read_bytes()})
    client, store = Client(session=sess), breadth.BreadthStore(tmp_path)
    store.add_many(_prior_rows())                                   # nothing to backfill: only today's fetch is tested here
    at = lambda h, m: datetime(2026, 10, 9, h, m, tzinfo=IST)   # noqa: E731
    assert store.tick(client, at(18, 0), lambda: ["20MICRONS"]) is None and not sess.calls
    row = store.tick(client, at(19, 1), lambda: ["20MICRONS", "21STCENMGM", "360ONE"])
    assert row["date"] == "2026-10-09" and row["n"] == 3 and row["advances"] >= 1
    assert store.tick(client, at(19, 40), lambda: ["20MICRONS"]) is None and len(sess.calls) == 1   # done
    # not posted yet (404): tried again later, at most every 20 minutes
    s2, st2 = Session({}), breadth.BreadthStore(tmp_path / "b")
    st2.add_many(_prior_rows())
    c2 = Client(session=s2)
    assert st2.tick(c2, at(19, 0), lambda: ["A"]) is None and st2.tick(c2, at(19, 10), lambda: ["A"]) is None
    assert len(s2.calls) == 1
    st2.tick(c2, at(19, 21), lambda: ["A"])
    assert len(s2.calls) == 2


def _prior_rows():
    """Stored sessions for the ten trading days before Fri 9 Oct 2026 (so the backfill has nothing to do)."""
    days, d = [], date(2026, 10, 8)
    while len(days) < 10:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return [_row(x.isoformat(), 0.5) for x in days]


def _archive_with_members(tmp_path, n=25, days=60):
    arch = PriceArchive(tmp_path / "a.sqlite", now_fn=lambda: datetime(2027, 1, 1, 12, 0, tzinfo=IST))
    d0 = date(2026, 6, 1)
    syms = [f"S{i:02d}" for i in range(n)]
    for i, s in enumerate(syms):
        sign = 1 if i % 2 else -1       # odd names rise every day, even names fall
        bars = [{"date": (d0 + timedelta(days=k)).isoformat(), "close": 200 + sign * k * 0.1,
                 "adj_close": 200 + sign * k * 0.1, "volume": 1e5} for k in range(days)]
        arch.merge(s + ".NS", bars, "max")
    return arch, syms, d0


def test_history_from_archive_and_point_in_time_members(tmp_path):
    arch, syms, d0 = _archive_with_members(tmp_path)
    mem = Membership(current=frozenset(syms), changes=[])
    rows = breadth.history((d0 + timedelta(days=55)).isoformat(), (d0 + timedelta(days=59)).isoformat(), arch, mem)
    assert [r["date"] for r in rows] == [(d0 + timedelta(days=k)).isoformat() for k in range(55, 60)]
    r = rows[-1]
    assert r["advances"] == 12 and r["declines"] == 13 and r["n"] == 25            # odd indices 1..23 rise
    assert r["ratio"] == round(12 / 25, 4)
    assert r["n50"] == 25 and r["above_50dma_pct"] == 48.0                          # the 12 risers are above their mean
    # a name that joined the index later does not count before it joined
    late = Membership(current=frozenset(syms), changes=[("2026-07-24", (syms[0],), ())])
    on_55 = breadth.history((d0 + timedelta(days=55)).isoformat(), (d0 + timedelta(days=55)).isoformat(), arch, late)
    assert on_55[0]["n"] == 25                                                       # 07-26 is after it joined (07-24) ...
    before = breadth.history((d0 + timedelta(days=50)).isoformat(), (d0 + timedelta(days=50)).isoformat(), arch, late)
    assert before[0]["n"] == 24                                                      # ... 07-21 predates it
    # too few archived names: no number rather than a misleading one
    thin = Membership(current=frozenset(syms[:5]), changes=[])
    assert breadth.history("2026-06-01", "2026-08-01", arch, thin) == []


# -- price archive --------------------------------------------------------------------------------
NOW = datetime(2026, 10, 9, 17, 0, tzinfo=IST)


def _arch(tmp_path, now=NOW):
    return PriceArchive(tmp_path / "p.sqlite", now_fn=lambda: now)


def _b(day, close, adj=None, vol=100.0):
    return {"date": day, "close": close, "adj_close": adj if adj is not None else close, "volume": vol}


def test_archive_never_overwritten_by_a_later_fetch(tmp_path):
    a = _arch(tmp_path)
    a.merge("X.NS", [_b("2026-10-05", 100), _b("2026-10-06", 101), _b("2026-10-07", 102)])
    out = a.merge("X.NS", [_b("2026-10-05", 100), _b("2026-10-06", 55), _b("2026-10-07", 102)])   # Yahoo rewrote one bar
    assert [b["close"] for b in out] == [100, 101, 102]
    assert [b["close"] for b in a.bars("X.NS", raw=True)] == [100, 101, 102]
    assert a.conflicts == 1 and a.splits("X.NS") == []


def test_archive_served_when_yahoo_is_empty_or_errors(tmp_path):
    a = _arch(tmp_path)
    ok = {"chart": {"result": [{"timestamp": [1759622400 + 86400 * i for i in range(5)],
                                "indicators": {"quote": [{"close": [10, 11, 12, 13, 14], "volume": [1] * 5}],
                                               "adjclose": [{"adjclose": [10, 11, 12, 13, 14]}]}}]}}
    empty = {"chart": {"result": [{"timestamp": [], "indicators": {"quote": [{"close": [], "volume": []}]}}]}}
    y = YahooPrices(".NS", session=Session({"/X.NS": ok}), archive=a)
    first = y.history("X")
    assert len(first) == 5
    for sess in (Session({"/X.NS": empty}), Session({"/X.NS": Resp("", 404)}), Session({"/X.NS": OSError("down")})):
        y2 = YahooPrices(".NS", session=sess, archive=a)
        assert [b["close"] for b in y2.history("X")] == [10, 11, 12, 13, 14]
    with pytest.raises(LookupError):                                      # nothing archived, nothing from Yahoo
        YahooPrices(".NS", session=Session({"/Y.NS": Resp("", 404)}), archive=a).history("Y")


def test_split_detected_and_both_views_kept(tmp_path):
    a = _arch(tmp_path)
    old = [_b(f"2026-09-{d:02d}", 1000 + d, vol=100) for d in range(21, 31)] + [_b("2026-10-01", 1031, vol=100)]
    a.merge("S.NS", old)
    # a 5-for-1 split: Yahoo now serves every date at one fifth, volumes times five
    new = [_b(b["date"], b["close"] / 5, vol=500) for b in old] + [_b("2026-10-02", 207.0, vol=500)]
    assert detect_split({b["date"]: b["close"] for b in old}, {b["date"]: b["close"] for b in new}) == 5.0
    out = a.merge("S.NS", new)
    sp = a.splits("S.NS")
    assert len(sp) == 1 and sp[0]["factor"] == 5.0 and sp[0]["overlap"] == len(old)
    assert out[0]["close"] == pytest.approx(1021 / 5) and out[0]["volume"] == 500          # split-adjusted view
    assert a.bars("S.NS", raw=True)[0]["close"] == 1021 and a.bars("S.NS", raw=True)[0]["volume"] == 100   # as first seen
    assert out[-1]["date"] == "2026-10-02" and out[-1]["close"] == 207.0
    a.merge("S.NS", new)
    assert len(a.splits("S.NS")) == 1                                       # not detected again


def test_not_a_split_when_ratios_differ_or_overlap_is_tiny():
    base = {f"d{i}": 100.0 for i in range(6)}
    assert detect_split(base, {k: v / 5 for k, v in base.items()}) == 5.0
    noisy = {k: (v / 5 if i % 2 else v / 3) for i, (k, v) in enumerate(base.items())}
    assert detect_split(base, noisy) is None
    assert detect_split(base, {k: v * 1.01 for k, v in base.items()}) is None
    assert detect_split({"a": 100.0, "b": 100.0}, {"a": 20.0, "b": 20.0}) is None


def test_todays_open_bar_is_not_archived(tmp_path):
    a = _arch(tmp_path, now=datetime(2026, 10, 9, 11, 0, tzinfo=IST))
    out = a.merge("X.NS", [_b("2026-10-08", 100), _b("2026-10-09", 101)])
    assert [b["date"] for b in out] == ["2026-10-08", "2026-10-09"]        # served ...
    assert [b["date"] for b in a.bars("X.NS")] == ["2026-10-08"]           # ... but not kept
    after = _arch(tmp_path, now=datetime(2026, 10, 9, 18, 0, tzinfo=IST))
    after.merge("X.NS", [_b("2026-10-09", 101)])
    assert [b["date"] for b in after.bars("X.NS")] == ["2026-10-08", "2026-10-09"]


def test_adj_close_follows_the_fresh_fetch_after_a_dividend(tmp_path):
    a = _arch(tmp_path)
    a.merge("D.NS", [_b("2026-10-05", 100, 100), _b("2026-10-06", 100, 100)])
    out = a.merge("D.NS", [_b("2026-10-06", 100, 98), _b("2026-10-07", 100, 98)])   # dividend: earlier adj rescaled by 0.98
    assert [round(b["adj_close"], 2) for b in out] == [98.0, 98.0, 98.0]
    assert [b["close"] for b in out] == [100, 100, 100]                              # levels untouched


def test_concurrent_writers_do_not_corrupt(tmp_path):
    a = _arch(tmp_path)
    errs = []

    def work(i):
        try:
            for k in range(5):
                a.merge(f"T{i}.NS", [_b(f"2026-10-0{k + 1}", 100 + k)])
        except Exception as e:   # noqa: BLE001
            errs.append(e)
    ts = [threading.Thread(target=work, args=(i,)) for i in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs and all(len(a.bars(f"T{i}.NS")) == 5 for i in range(6))


# -- settings + email line ---------------------------------------------------------------------------
def test_settings_flags_default_on_and_are_editable(settings, monkeypatch):
    from trading_agent import ui
    from trading_agent.config import load_settings
    assert settings.flows_breadth is True and settings.price_band_filter is True
    assert ui.EDITABLE_ENV_KEYS["flows_breadth"] == "FLOWS_BREADTH" and ui.EDITABLE_ENV_KEYS["price_band_filter"] == "PRICE_BAND_FILTER"
    assert dataclasses.replace(settings, flows_breadth=False).flows_breadth is False
    monkeypatch.setenv("FLOWS_BREADTH", "false")
    monkeypatch.setenv("PRICE_BAND_FILTER", "false")
    s = load_settings(None)
    assert s.flows_breadth is False and s.price_band_filter is False


def test_gauge_section_carries_flow_and_breadth_lines(settings, tmp_path):
    from trading_agent.digest import DigestContext, _flow_breadth_lines
    s = dataclasses.replace(settings, market="in", state_dir=tmp_path)
    store = flows.FlowStore(tmp_path)
    for r in _rows([(-2000, 1000), (-2400, 1980)]):
        store.add(r)
    breadth.BreadthStore(tmp_path).add_many([_row("2026-09-29", 0.3, 40.0)])
    ctx = DigestContext(settings=s, now=lambda: datetime(2026, 9, 30, 8, 0, tzinfo=IST))
    out = _flow_breadth_lines(ctx)
    assert out["flows_line"].startswith("FII −₹2,400 cr, DII +₹1,980 cr on Tue 29 Sep (provisional NSE figures)")
    assert out["breadth_line"].startswith("Breadth (NIFTY 500): 30% of stocks rose on Tue 29 Sep")
    off = _flow_breadth_lines(DigestContext(settings=dataclasses.replace(s, flows_breadth=False)))
    assert off == {}


def test_summary_may_quote_the_flow_and_breadth_lines_but_not_other_numbers():
    from trading_agent.digest_writer import summary_facts, validate_summary
    data = {"kind": "morning", "date": "2026-10-10", "gauges": {
        "gauges": [], "warnings": [], "note": "Readings from fixed rules.",
        "flows_line": "FII −₹3,569 cr, DII +₹4,743 cr on Fri 9 Oct (provisional NSE figures); 5-day net FII −₹8,100 cr",
        "breadth_line": "Breadth (NIFTY 500): 28% of stocks rose on Fri 9 Oct (below 35% is weak); 3rd day below 35%, broad selling."}}
    assert "flows_line" in summary_facts("morning", data)["gauges"]
    ok, why = validate_summary("Foreign investors sold about ₹3,569 cr on Friday and only 28% of NIFTY 500 stocks rose.", data)
    assert ok, why
    bad, _ = validate_summary("Foreign investors sold about ₹9,999 cr on Friday.", data)
    assert not bad


# -- fix round 1 -------------------------------------------------------------------------------------------
def test_every_production_price_source_has_the_archive(settings, tmp_path):
    from trading_agent.runner import free_prices
    s = dataclasses.replace(settings, market="in", state_dir=tmp_path)
    y = free_prices(s)
    assert y.archive is not None and y.archive.path == tmp_path / "prices" / "archive.sqlite"
    assert (tmp_path / "prices" / "archive.sqlite").exists()


def test_late_split_only_older_bars_are_rescaled(tmp_path):
    a = _arch(tmp_path)
    old = [_b(f"2026-09-{d:02d}", 1000 + d, vol=100) for d in range(14, 26)]            # 12 bars in old share units
    a.merge("L.NS", old)
    # the split took effect on 26 Sep; the archive then captured 26-30 Sep already in NEW units
    a.merge("L.NS", [_b("2026-09-26", 200.0, vol=500), _b("2026-09-29", 201.0, vol=500)], "max")
    assert a.splits("L.NS") == []                                  # a lone pair of new-unit bars is no overlap to judge yet
    fresh = [_b(b["date"], b["close"] / 5, vol=500) for b in old] + [_b("2026-09-26", 200.0, vol=500), _b("2026-09-29", 201.0, vol=500)]
    arch_close = {b["date"]: b["close"] for b in a.bars("L.NS")}
    f = find_split(arch_close, {b["date"]: b["close"] for b in fresh})
    assert f == (5.0, "2026-09-26")
    out = a.merge("L.NS", fresh, "max")
    sp = a.splits("L.NS")
    assert len(sp) == 1 and sp[0]["factor"] == 5.0 and sp[0]["effective"] == "2026-09-26"
    by = {b["date"]: b for b in out}
    assert by["2026-09-14"]["close"] == pytest.approx(1014 / 5) and by["2026-09-14"]["volume"] == 500
    assert by["2026-09-26"]["close"] == 200.0 and by["2026-09-29"]["close"] == 201.0     # the new-unit bars untouched
    assert a.bars("L.NS", raw=True)[0]["close"] == 1014                                  # as first seen
    a.merge("L.NS", fresh + [_b("2026-09-30", 202.0, vol=500)], "max")
    assert len(a.splits("L.NS")) == 1


def test_find_split_rejects_a_step_that_is_not_clean():
    arch = {f"d{i}": 100.0 for i in range(8)}
    clean = {k: (20.0 if i < 5 else 100.0) for i, k in enumerate(arch)}
    assert find_split(arch, clean) == (5.0, "d5")
    messy = {k: (20.0 if i < 5 else 130.0) for i, k in enumerate(arch)}
    assert find_split(arch, messy) is None
    assert find_split(arch, {k: 100.0 for k in arch}) is None


def test_unchanged_fetch_is_not_written_again(tmp_path):
    a = _arch(tmp_path)
    bars = [_b("2026-10-05", 100), _b("2026-10-06", 101)]
    a.merge("W.NS", bars)
    n = a.writes
    for _ in range(5):
        a.merge("W.NS", [dict(b) for b in bars])
    assert a.writes == n
    a2 = PriceArchive(tmp_path / "p.sqlite", now_fn=lambda: NOW)                    # a new process: reads, writes nothing
    a2.merge("W.NS", bars)
    assert a2.writes == 0
    a.merge("W.NS", bars + [_b("2026-10-07", 102)])
    assert a.writes == n + 1


def test_foreign_markets_are_archived_only_when_two_days_old(tmp_path):
    a = _arch(tmp_path)
    a.merge("^GSPC", [_b("2026-10-06", 1), _b("2026-10-07", 2), _b("2026-10-08", 3), _b("2026-10-09", 4)])
    assert [b["date"] for b in a.bars("^GSPC")] == ["2026-10-06", "2026-10-07"]      # 8 and 9 Oct may still be partial
    a.merge("^NSEI", [_b("2026-10-08", 3), _b("2026-10-09", 4)])
    assert [b["date"] for b in a.bars("^NSEI")] == ["2026-10-08"]                    # Indian: 9 Oct is final only after 18:00


def test_bar_not_final_before_eighteen(tmp_path):
    a = _arch(tmp_path, now=datetime(2026, 10, 9, 17, 59, tzinfo=IST))
    a.merge("X.NS", [_b("2026-10-09", 1)])
    assert a.bars("X.NS") == []
    assert _arch(tmp_path / "x", now=datetime(2026, 10, 9, 18, 0, tzinfo=IST)).is_closed("2026-10-09", "X.NS")


def test_stale_band_list_means_no_filtering(tmp_path):
    def book(now):
        b = BandBook(tmp_path, now_fn=lambda: now)
        b.save({"AAA": 2}, date(2026, 10, 7))                                        # Wednesday's list
        return b
    assert book(datetime(2026, 10, 9, 12, tzinfo=IST)).refuse_buy("AAA")             # 2 trading days old: still used
    stale = book(datetime(2026, 10, 12, 12, tzinfo=IST))                              # Monday: 3 trading days old
    assert stale.refuse_buy("AAA") is None and not stale.active() and stale.rule("AAA")["band"] == "unknown"
    assert book(datetime(2026, 10, 9, 12, tzinfo=IST)).active()


def test_band_fetch_retries_are_capped(tmp_path):
    client = Client(session=Session({"sec_list": Resp("", 503)}))
    b = BandBook(tmp_path)
    t0 = datetime(2026, 10, 12, 8, 0, tzinfo=IST)
    for i in range(30):
        b.tick(client, t0 + timedelta(minutes=21 * i))
    assert len(client.session.calls) == 6


def test_morning_catch_up_when_the_evening_fetch_was_missed(tmp_path):
    payload = json.loads((FX / "fiidii_sample.json").read_text())          # the session of Fri 9 Oct
    store, c = flows.FlowStore(tmp_path), Client(payload)
    mon = lambda h, m: datetime(2026, 10, 12, h, m, tzinfo=IST)            # noqa: E731
    assert store.tick(c, mon(8, 0))["date"] == "2026-10-09" and len(store.rows()) == 1       # Monday morning, Friday missing
    assert store.tick(c, mon(8, 30)) is None and c.gets == 1                                 # stored: no more asking
    assert flows.FlowStore(tmp_path / "n").tick(Client(payload), mon(9, 30)) is None         # not in the morning window


def test_five_day_net_and_streaks_count_trading_days_only():
    d = _weekdays(8)
    rows = [{"date": x.isoformat(), "fii_net": -100.0, "dii_net": 10.0} for x in d]
    del rows[5]                                                                             # one session missing
    s = flows.summarize(rows)
    assert s["negative_streak"] == 2 and s["window_label"] == "5 of the last 6 sessions"
    assert "net FII over 5 of the last 6 sessions" in flows.flows_line(rows)
    full = [{"date": x.isoformat(), "fii_net": -100.0, "dii_net": 10.0} for x in d]
    assert flows.summarize(full)["negative_streak"] == 8 and "5-day net FII" in flows.flows_line(full)
    # a market holiday is not a gap when the calendar knows it
    class Cal:
        def is_trading_day(self, day):
            return day.weekday() < 5 and day != d[5]
    assert flows.summarize(rows, calendar=Cal())["negative_streak"] == 7
    br = [_row(x.isoformat(), 0.2) for x in d]
    del br[5]
    assert breadth.weak_streak(br) == 2 and breadth.weak_streak(br, calendar=Cal()) == 7


def test_breadth_backfills_missed_sessions_from_the_bhavcopy(tmp_path):
    csv_bytes = (FX / "bhavcopy_sample.csv").read_bytes()
    # the sample is the 9 Oct file: serve it for the days asked and check only the day it really is gets stored
    sess = Session({"sec_bhavdata_full_09102026.csv": csv_bytes})
    store = breadth.BreadthStore(tmp_path)
    c = Client(session=sess)
    sleeps = []
    store.tick(c, datetime(2026, 10, 12, 8, 0, tzinfo=IST), lambda: ["20MICRONS", "360ONE"], calendar=None, sleep=sleeps.append)
    asked = [u.rsplit("_", 1)[1] for u in sess.calls]
    assert asked == ["09102026.csv", "08102026.csv", "07102026.csv"]                       # newest first, three a tick
    assert {r["date"] for r in store.rows()} == {"2026-10-09"}                              # 8 and 7 Oct are not served: 404
    assert len(sleeps) == 2                                                              # polite pauses between requests
    for _ in range(4):
        store.tick(c, datetime(2026, 10, 12, 8, 0, tzinfo=IST) + timedelta(minutes=25 * (_ + 1)), lambda: ["A"], sleep=lambda s: None)
    assert len(sess.calls) <= 20                                                           # each missing day is tried at most twice


def test_forward_screen_filter_is_recorded_with_a_date(tmp_path):
    from trading_agent.costs import cost_model_for
    from trading_agent.forward import ForwardTest, format_forward
    ft = ForwardTest(tmp_path, universe="NIFTYMIDCAP150", top=2, capital=100_000, price_fn=lambda s: 100.0,
                     cost_model=cost_model_for("in"), now=lambda: datetime(2026, 10, 9, 16, 0, tzinfo=IST))
    ft.run(lambda: {"top": [{"symbol": "A"}, {"symbol": "B"}], "eligible": 5, "bands_applied": False})
    assert ft.data.get("band_filter_since") is None
    ft.run(lambda: {"top": [{"symbol": "A"}], "eligible": 5, "bands_applied": True}, force_rebalance=True)
    assert ft.data["band_filter_since"] == "2026-10-09"
    s = ft.summary()
    assert s["band_filter_since"] == "2026-10-09" and "Price-band filter on since 2026-10-09" in format_forward(s)


def test_morning_email_deals_are_consolidated_events(settings, tmp_path):
    from trading_agent.deal_events import consolidate_deals
    from trading_agent.digest import DigestContext, _deals
    from trading_agent.quiver import DisclosedTrade

    def deal(ex, client, qty, px):
        return DisclosedTrade("bulk", client, "ABC", "Purchase", "2026-10-08", "2026-10-08", f"{qty} sh @ ₹{px}",
                              {"qty": str(qty), "watp": str(px), "nse_symbol": "ABC"}, exchange=ex)

    class Data:
        def trades_for_investors(self, names, source, **kw):
            return [deal("NSE", "HRTI PRIVATE LIMITED", 1_500_000, 250.0), deal("BSE", "HRTI PVT LTD", 610_000, 272.0)]

    s = dataclasses.replace(settings, market="in", state_dir=tmp_path, watch_investors=["HRTI"], watch_source="deals")
    ctx = DigestContext(settings=s, data=Data(), now=lambda: datetime(2026, 10, 9, 8, 0, tzinfo=IST))
    out = _deals(ctx, "morning", date(2026, 10, 9))
    assert len(out["deals"]) == 1 and out["deals"][0]["exchange"] == "NSE + BSE"
    assert out["deals"][0]["size"].startswith("2,110,000 sh @ ₹") and out["total"] == 1
    assert len(consolidate_deals(Data().trades_for_investors([], ""))) == 1
