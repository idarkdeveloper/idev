"""Optional TradingView columns: off by default, refused on the server role, one request per list per 15 minutes,
a 24 hour back-off on any error or HTTP 403/429, parsing, and display-only. A fake HTTP session only: no network."""
import re
from pathlib import Path

import pytest

from trading_agent import tvscreener as tv
from trading_agent.config import Settings, load_settings

from .conftest import FakeResponse

ROOT = Path(__file__).resolve().parents[1]


class FakeSession:
    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []
        self.cookies = type("Jar", (), {"cleared": 0, "clear": lambda s: setattr(s, "cleared", s.cleared + 1)})()

    def post(self, url, **kw):
        self.calls.append((url, kw))
        a = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(a, BaseException):
            raise a
        return a

    def get(self, *a, **k):   # pragma: no cover - the module must only ever POST its one request
        raise AssertionError("no GET expected")


def ok(rows):
    return FakeResponse({"totalCount": len(rows), "data": rows}, 200)


GOOD = ok([{"s": "NSE:TCS", "d": [0.62, "Technology Services", "IT Services & Consulting", 8.4567]},
           {"s": "NSE:BAJAJ_AUTO", "d": [-0.2, "Consumer Durables", "Motorcycles", None]},
           {"s": "NSE:UNKNOWN", "d": [0.1, "x", "y", 1]}])


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def make(tmp_path, session, *, enabled=True, role="", clock=None):
    return tv.TradingViewColumns(tmp_path, enabled=lambda: enabled, role=lambda: role, session=session, clock=clock or Clock())


def test_off_by_default_in_settings_and_does_nothing_when_off(tmp_path):
    from dataclasses import fields
    assert {f.name: f.default for f in fields(Settings)}["tradingview_screener"] is False
    s = FakeSession(GOOD)
    t = make(tmp_path, s, enabled=False)
    assert t.fetch("NIFTY50", ["TCS"]) is None and s.calls == [] and t.status()["state"] == "off"


def test_env_defaults_and_switches(tmp_path, monkeypatch):
    for k in ("TRADINGVIEW_SCREENER", "TA_ROLE"):
        monkeypatch.delenv(k, raising=False)
    s = load_settings(dotenv=None)
    assert s.tradingview_screener is False and s.ta_role == ""
    monkeypatch.setenv("TRADINGVIEW_SCREENER", "true")
    monkeypatch.setenv("TA_ROLE", " Server ")
    s = load_settings(dotenv=None)
    assert s.tradingview_screener is True and s.ta_role == "server"


def test_refused_on_the_server_role_even_when_switched_on(tmp_path):
    s = FakeSession(GOOD)
    t = make(tmp_path, s, enabled=True, role="server")
    assert t.fetch("NIFTY50", ["TCS"]) is None
    assert s.calls == [] and t.requests_made == 0
    assert t.status() == {"state": "refused_server", "until": None}
    assert make(tmp_path, FakeSession(GOOD), enabled=True, role="SERVER").status()["state"] == "refused_server"
    assert make(tmp_path, s, enabled=True, role="laptop").status()["state"] == "on"     # any other role is the laptop


def test_the_server_role_check_comes_before_any_network_object_is_made(tmp_path, monkeypatch):
    import requests

    def boom(*a, **k):
        raise AssertionError("a session was built on the server")
    monkeypatch.setattr(requests, "Session", boom)
    t = tv.TradingViewColumns(tmp_path, enabled=lambda: True, role=lambda: "server")
    assert t.fetch("NIFTY50", ["TCS"]) is None


def test_parsing_maps_symbols_and_reads_every_column(tmp_path):
    s = FakeSession(GOOD)
    t = make(tmp_path, s)
    got = t.fetch("NIFTY50", ["TCS", "BAJAJ-AUTO", "INFY"])
    assert set(got) == {"TCS", "BAJAJ-AUTO"}                     # a symbol we did not ask about is ignored
    assert got["TCS"] == {"summary": 0.62, "summary_label": "Strong buy", "sector": "Technology Services",
                          "industry": "IT Services & Consulting", "eps_growth": 8.46}
    assert got["BAJAJ-AUTO"]["summary_label"] == "Sell" and got["BAJAJ-AUTO"]["eps_growth"] is None
    url, kw = s.calls[0]
    assert url == "https://scanner.tradingview.com/india/scan"
    assert kw["json"]["symbols"]["tickers"] == ["NSE:TCS", "NSE:BAJAJ_AUTO", "NSE:INFY"]
    assert kw["json"]["columns"] == tv.COLUMNS
    assert "Cookie" not in kw["headers"] and "Authorization" not in kw["headers"]       # no login, no cookies
    assert s.cookies.cleared >= 1


def test_summary_labels():
    assert [tv.summary_label(v) for v in (0.8, 0.5, 0.3, 0.1, 0.0, -0.09, -0.1, -0.3, -0.5, -0.9)] == [
        "Strong buy", "Strong buy", "Buy", "Buy", "Neutral", "Neutral", "Sell", "Sell", "Strong sell", "Strong sell"]
    assert tv.summary_label(None) is None and tv.summary_label("x") is None and tv.summary_label(float("nan")) is None
    assert tv.summary_label(True) is None


def test_parse_scan_rejects_the_wrong_shape_and_pads_short_rows():
    with pytest.raises(ValueError):
        tv.parse_scan({"nope": 1}, ["TCS"])
    with pytest.raises(ValueError):
        tv.parse_scan([], ["TCS"])
    out = tv.parse_scan({"data": [{"s": "NSE:TCS", "d": [0.2]}, {"s": "NSE:INFY"}, "junk"]}, ["TCS", "INFY"])
    assert out["TCS"]["summary_label"] == "Buy" and out["TCS"]["sector"] is None and "INFY" not in out


def test_one_request_per_list_cached_for_15_minutes(tmp_path):
    clock, s = Clock(), FakeSession(GOOD)
    t = make(tmp_path, s, clock=clock)
    a = t.fetch("NIFTY50", ["TCS"])
    assert t.fetch("NIFTY50", ["TCS"]) is a and len(s.calls) == 1
    clock.t += tv.CACHE_SECONDS - 1
    assert t.fetch("NIFTY50", ["TCS"]) is a and len(s.calls) == 1
    assert t.fetch("nifty50", ["TCS"]) is a and len(s.calls) == 1       # same list, however it is spelled
    clock.t += 2
    t.fetch("NIFTY50", ["TCS"])
    assert len(s.calls) == 2                                           # at most once every 15 minutes
    t.fetch("NIFTY100", ["TCS"])
    assert len(s.calls) == 3                                           # another list is another load


def test_http_403_and_429_back_off_for_24_hours_and_survive_a_restart(tmp_path):
    for code in (403, 429):
        d = tmp_path / str(code)
        clock, s = Clock(), FakeSession(FakeResponse({}, code), GOOD)
        t = make(d, s, clock=clock)
        assert t.fetch("NIFTY50", ["TCS"]) is None and len(s.calls) == 1
        st = t.status()
        assert st["state"] == "backoff" and st["until"] is not None
        clock.t += tv.CACHE_SECONDS + 1
        assert t.fetch("NIFTY50", ["TCS"]) is None and t.fetch("NIFTY100", ["TCS"]) is None and len(s.calls) == 1   # silent
        t2 = make(d, FakeSession(GOOD), clock=clock)                  # a restart reads the saved back-off
        assert t2.fetch("NIFTY50", ["TCS"]) is None and t2.status()["state"] == "backoff"
        clock.t += tv.BACKOFF_SECONDS - tv.CACHE_SECONDS - 2          # a moment before 24 hours after the refusal
        assert t.status()["state"] == "backoff"
        clock.t += 2
        assert t.status()["state"] == "on"
        assert t.fetch("NIFTY50", ["TCS"]) is not None and len(s.calls) == 2


def test_any_error_backs_off_too(tmp_path):
    for i, bad in enumerate((ConnectionError("down"), TimeoutError("slow"), FakeResponse({"oops": True}, 200),
                             FakeResponse({}, 500), FakeResponse("not json", 200))):
        d = tmp_path / str(i)
        s = FakeSession(bad)
        t = make(d, s)
        assert t.fetch("NIFTY50", ["TCS"]) is None, bad
        assert t.status()["state"] == "backoff", bad
        t.fetch("NIFTY50", ["TCS"])
        assert len(s.calls) == 1, bad


def test_a_turned_off_switch_or_empty_list_makes_no_request(tmp_path):
    s = FakeSession(GOOD)
    on = {"v": True}
    t = tv.TradingViewColumns(tmp_path, enabled=lambda: on["v"], role=lambda: "", session=s, clock=Clock())
    assert t.fetch("NIFTY50", []) is None and s.calls == []
    on["v"] = False
    assert t.fetch("NIFTY50", ["TCS"]) is None and s.calls == []
    on["v"] = True
    assert t.fetch("NIFTY50", ["TCS"]) is not None and len(s.calls) == 1


def test_a_real_run_builds_a_fresh_cookieless_session_per_request(tmp_path, monkeypatch):
    import requests
    made = []

    class Sess(FakeSession):
        def __init__(self):
            super().__init__(GOOD)
            self.closed = False
            made.append(self)

        def close(self):
            self.closed = True
    monkeypatch.setattr(requests, "Session", Sess)
    t = tv.TradingViewColumns(tmp_path, enabled=lambda: True, role=lambda: "", clock=Clock())
    assert t.fetch("NIFTY50", ["TCS"]) is not None
    assert len(made) == 1 and made[0].closed and made[0].cookies.cleared >= 1


def test_display_only_nothing_outside_the_page_layer_imports_it():
    users = []
    for p in (ROOT / "trading_agent").rglob("*.py"):
        if p.name in ("tvscreener.py", "__init__.py"):
            continue
        text = p.read_text(encoding="utf-8")
        if re.search(r"^\s*(from\s+\S*tvscreener|import\s+\S*tvscreener|from\s+\S+\s+import\s+.*tvscreener)", text, re.M) \
                or "scanner.tradingview" in text:
            users.append(p.name)
    assert users == ["ui.py"]
    # the screener service only passes the columns through to the page; the agent, emails and Claude never see them
    for name in ("agent.py", "digest.py", "digest_writer.py", "digest_render.py", "notify.py", "telegram_brief.py", "bulletin.py", "momentum.py", "watch.py"):
        assert "tv_" not in (ROOT / "trading_agent" / name).read_text(encoding="utf-8"), name
    ui = (ROOT / "trading_agent" / "ui.py").read_text(encoding="utf-8")
    assert ui.count("tvscreener") == 1                                  # only the screener property builds it
