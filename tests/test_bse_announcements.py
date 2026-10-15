"""BSE announcements for BSE-only stocks (<scrip code>.BO): the per-company API, the RSS fallback, the cache, and the
wiring through NSEClient.announcements to the watch's alert. Fakes only, no network."""
import json
from datetime import datetime

import pytest
import requests

from trading_agent import bse_announcements as ba
from trading_agent.bse import BSEClient
from trading_agent.timezones import IST

NOW = datetime(2026, 10, 16, 10, 0, tzinfo=IST)

API_JSON = {"Table": [
    {"NEWSID": "n-1", "SCRIP_CD": 543287, "SLONGNAME": "XYZ Specialty Chemicals Ltd-$", "DT_TM": "2026-10-15T18:42:10.25",
     "CATEGORYNAME": "Company Update", "SUBCATNAME": "Acquisition", "HEADLINE": "XYZ acquires 51% of ABC Pvt Ltd",
     "ATTACHMENTNAME": "aaaa-1111.pdf"},
    {"NEWSID": "n-2", "SCRIP_CD": 543287, "SLONGNAME": "XYZ Specialty Chemicals Ltd", "DT_TM": "2026-10-09T11:00:00",
     "CATEGORYNAME": "Board Meeting", "SUBCATNAME": "-", "NEWSSUB": "Board meeting on 24 Oct for Q2 results",
     "ATTACHMENTNAME": ""},
    {"NEWSID": "", "DT_TM": "", "HEADLINE": "broken row"}], "Table1": [{"ROWCNT": 3}]}

RSS = """<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel><title>BSE - Latest Corporate Announcements</title>
<item><title>XYZ Specialty Chemicals Ltd-$ (543287)</title>
<link>https://www.bseindia.com/xml-data/corpfiling/AttachLive/bbbb-2222.pdf</link><scripcode>543287</scripcode>
<description>Allotment of 5,00,000 warrants</description><pubDate>16-Oct-2026 09:31:05</pubDate></item>
<item><title>XYZ Specialty Chemicals Ltd (543287)</title>
<link>https://www.bseindia.com/xml-data/corpfiling/AttachLive/aaaa-1111.pdf</link><scripcode>543287</scripcode>
<description>same filing as the API's</description><pubDate>15-Oct-2026 18:42:10</pubDate></item>
<item><title>Aegis Logistics Ltd (500003)</title>
<link>https://www.bseindia.com/xml-data/corpfiling/AttachLive/cccc-3333.pdf</link><scripcode>500003</scripcode>
<description>Investor Presentation is attached.</description><pubDate>16-Oct-2026 00:17:25</pubDate></item>
</channel></rss>"""


class Resp:
    def __init__(self, status=200, text="", data=None):
        self.status_code, self.text, self._data = status, text, data
        self.content = (text or json.dumps(data or {})).encode()

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def json(self):
        if self._data is None:
            raise ValueError("not json")
        return self._data


class Session:
    def __init__(self, api=None, rss=None):
        self.api = api if api is not None else Resp(data=API_JSON)
        self.rss = rss if rss is not None else Resp(text=RSS)
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params))
        out = self.api if url == ba.API_URL else self.rss if url == ba.RSS_URL else requests.ConnectionError(url)
        if isinstance(out, Exception):
            raise out
        return out


def reader(tmp_path, sess, t=None):
    clock = t or [0.0]
    r = ba.BSEAnnouncements(sess, tmp_path / "cache", clock=lambda: NOW, monotonic=lambda: clock[0])
    r.t = clock
    return r


def test_scrip_codes():
    assert ba.scrip_code("543287.BO") == "543287" and ba.scrip_code("500325") == "500325"
    assert ba.scrip_code("RELIANCE") is None and ba.scrip_code("1234.BO") is None and ba.scrip_code("../x.BO") is None


def test_api_rows_are_normalised_and_merged_with_the_rss_feed(tmp_path):
    s = Session()
    rows = reader(tmp_path, s).announcements("543287.BO")
    assert [r["id"] for r in rows] == ["bse:bbbb-2222", "bse:aaaa-1111", "bse:n-2"]      # newest first, one per filing
    acq = rows[1]
    assert acq == {"id": "bse:aaaa-1111", "symbol": "543287.BO", "company": "XYZ Specialty Chemicals Ltd",
                   "at": "2026-10-15 18:42:10", "category": "Acquisition", "text": "XYZ acquires 51% of ABC Pvt Ltd",
                   "file": "https://www.bseindia.com/xml-data/corpfiling/AttachLive/aaaa-1111.pdf"}   # the API's copy wins
    assert rows[2]["category"] == "Board Meeting" and rows[2]["text"] == "Board meeting on 24 Oct for Q2 results"
    assert rows[0]["category"] == "Announcement" and rows[0]["company"] == "XYZ Specialty Chemicals Ltd"
    url, params = s.calls[0]
    assert url == ba.API_URL and params["strScrip"] == "543287" and params["strPrevDate"] == "20260916"
    assert params["strToDate"] == "20261016"
    assert "500003" not in json.dumps(rows)                                                 # other companies left out


def test_a_refused_api_falls_back_to_rss_and_is_left_alone_for_hours(tmp_path):
    s = Session(api=Resp(403, text="Access Denied"))
    r = reader(tmp_path, s)
    rows = r.announcements("543287.BO")
    assert [x["id"] for x in rows] == ["bse:bbbb-2222", "bse:aaaa-1111"] and "403" in r.last_error
    r.t[0] += 3600
    r.announcements("543287.BO")
    assert sum(1 for u, _ in s.calls if u == ba.API_URL) == 1                               # not asked again within 6 h
    r.t[0] += ba.API_BLOCK_S
    s.api = Resp(data=API_JSON)
    assert len(r.announcements("543287.BO")) == 3


def test_the_cache_keeps_what_the_rss_feed_showed(tmp_path):
    s = Session(api=Resp(403, text="Access Denied"))
    reader(tmp_path, s).announcements("543287.BO")
    s2 = Session(api=Resp(403, text="Access Denied"), rss=Resp(text="<rss><channel></channel></rss>"))
    later = reader(tmp_path, s2).announcements("543287.BO")                                  # a new process, the feed moved on
    assert [x["id"] for x in later] == ["bse:bbbb-2222", "bse:aaaa-1111"]


def test_reads_are_spaced_out(tmp_path):
    s = Session()
    r = reader(tmp_path, s)
    r.announcements("543287.BO")
    r.announcements("543287.BO")
    r.announcements("500003.BO")
    api = [p["strScrip"] for u, p in s.calls if u == ba.API_URL]
    assert api == ["543287", "500003"] and sum(1 for u, _ in s.calls if u == ba.RSS_URL) == 1   # RSS once a minute
    r.t[0] += ba.API_TTL_S
    r.announcements("543287.BO")
    assert [p["strScrip"] for u, p in s.calls if u == ba.API_URL] == ["543287", "500003", "543287"]


def test_nothing_read_and_nothing_cached_is_an_error_but_an_empty_answer_is_not(tmp_path):
    down = Session(api=requests.ConnectionError("down"), rss=requests.ConnectionError("down"))
    with pytest.raises(ba.BSEAnnouncementError):
        reader(tmp_path, down).announcements("543287.BO")
    empty = Session(api=Resp(data={"Table": []}), rss=Resp(text="<rss><channel></channel></rss>"))
    assert reader(tmp_path, empty).announcements("999999.BO") == []
    with pytest.raises(ValueError):
        reader(tmp_path, Session()).announcements("RELIANCE")


def test_hostile_feeds_are_refused(tmp_path):
    with pytest.raises(ValueError):
        ba.parse_rss('<!DOCTYPE x [<!ENTITY a "b">]><rss/>')
    s = Session(api=Resp(403, text="x"), rss=Resp(text="<rss><channel><item>broken"))
    with pytest.raises(ba.BSEAnnouncementError):
        reader(tmp_path, s).announcements("543287.BO")


def test_old_rows_are_pruned(tmp_path):
    old = {"Table": [{"NEWSID": "old", "DT_TM": "2026-05-01T10:00:00", "CATEGORYNAME": "x", "HEADLINE": "old"},
                     {"NEWSID": "new", "DT_TM": "2026-10-10T10:00:00", "CATEGORYNAME": "x", "HEADLINE": "new"}]}
    rows = reader(tmp_path, Session(api=Resp(data=old), rss=Resp(text="<rss/>"))).announcements("543287.BO")
    assert [r["id"] for r in rows] == ["bse:new"]


def test_health_records_whether_the_api_or_only_the_feed_answered(settings):
    from trading_agent import health
    settings.market, settings.data_source = "in", "nse"
    cache = settings.state_dir / "cache"
    r = ba.BSEAnnouncements(Session(api=Resp(403, text="Access Denied")), cache, clock=lambda: NOW, monotonic=lambda: 0.0)
    r.announcements("543287.BO")
    row = next(x for x in health.report(settings, NOW)["sources"] if x["id"] == "bse_ann")
    assert row["status"] == "ok" and "RSS feed only" in row["detail"]
    r2 = ba.BSEAnnouncements(Session(), cache, clock=lambda: NOW, monotonic=lambda: 0.0)
    r2.announcements("543287.BO")
    row = next(x for x in health.report(settings, NOW)["sources"] if x["id"] == "bse_ann")
    assert "per-company API" in row["detail"]


# --------------------------------------------------------------------------- #
# wiring: NSEClient.announcements and the watch
# --------------------------------------------------------------------------- #
def test_nse_client_hands_bse_only_codes_to_bse(tmp_path):
    from trading_agent.nse import NSEClient
    nse = NSEClient(cache_dir=tmp_path / "cache")
    assert nse.announcements("543287.BO") == []                                             # BSE off: nothing, no NSE call
    nse.bse = BSEClient(session=Session(), cache_dir=tmp_path / "cache", clock=lambda: NOW)
    rows = nse.announcements("543287.BO", limit=2)
    assert [r["id"] for r in rows] == ["bse:bbbb-2222", "bse:aaaa-1111"]


def test_the_watch_alerts_a_bse_only_holdings_filing_with_its_name(settings):
    from trading_agent.notify import Notifier
    from trading_agent.state import State
    from trading_agent.watch import Watcher
    from trading_agent.nse import NSEClient

    class Recorder(Notifier):
        def __init__(self):
            super().__init__()
            self.kw = []

        def send(self, subject, body, **kw):
            self.kw.append({"subject": subject, "body": body, **kw})
            return ["console"]
    nse = NSEClient(cache_dir=settings.state_dir / "cache")
    nse.bse = BSEClient(session=Session(), cache_dir=settings.state_dir / "cache", clock=lambda: NOW)
    st = State(settings.state_dir / "state.json")
    st.record_recommendation({"action": "watch", "ticker": "543287.BO"})
    st.save()
    rec = Recorder()
    w = Watcher(settings, every=60, data=nse, notifier=rec)
    fresh = w.poll_announcements()
    assert len(fresh) == 3 and all(a["symbol"] == "543287.BO" for a in fresh)
    (sent,) = rec.kw
    assert sent["subject"] == "[NEWS] 3 new announcements: XYZ Specialty Chemicals · BSE"
    assert "XYZ Specialty Chemicals · BSE" in sent["html"] and "<b>Acquisition:</b> XYZ acquires 51%" in sent["html"]
    assert w.poll_announcements() == [] and len(rec.kw) == 1                                # each filing once
