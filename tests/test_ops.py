"""One engine, heartbeat, Telegram alerts, email headers, forward-test rebuild. Fakes only: no network."""
from __future__ import annotations

import dataclasses
import os
import logging
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
import requests

from trading_agent import digest_render
from trading_agent.config import (load_settings, parse_heartbeat_url, parse_telegram_chat, parse_telegram_token)
from trading_agent.costs import cost_model_for
from trading_agent.forward import AsOfPrices, ForwardTest, IST, rebuild_from
import threading
import time
import json
from types import SimpleNamespace
from trading_agent.forward_schedule import MAX_TRIES, ForwardScheduler, run_forward_due
from trading_agent.heartbeat import Heartbeat, fail_url, stall_after
from trading_agent.notify import Notifier, custom_domain, redact, telegram_chunks
from trading_agent.watch import Watcher

from .conftest import FakeResponse

TOKEN = "123456789:AAH_fake-token-for-tests-0123456789abcd"
HB_URL = "https://hc-ping.com/0123abcd-secret-path-token"
ROOT = Path(__file__).resolve().parents[1]


class Session:
    """Records every request; answers 200 unless told to fail by URL substring."""

    def __init__(self, fail: tuple[str, ...] = (), exc: Exception | None = None):
        self.calls: list[tuple[str, str, dict]] = []
        self.fail, self.exc = fail, exc

    def _do(self, method, url, **kw):
        self.calls.append((method, url, kw))
        if self.exc is not None:
            raise self.exc
        if any(f in url for f in self.fail):
            raise requests.ConnectionError("boom")
        return FakeResponse({"ok": True})

    def get(self, url, **kw):
        return self._do("GET", url, **kw)

    def post(self, url, **kw):
        return self._do("POST", url, **kw)


# ---------------------------------------------------------------- settings
def test_setting_validators():
    assert parse_heartbeat_url("") is None and parse_heartbeat_url(HB_URL) == HB_URL
    for bad in ("http://hc-ping.com/x", "hc-ping.com/x", "https://", "https://a b/c"):
        with pytest.raises(ValueError) as e:
            parse_heartbeat_url(bad)
        assert bad not in str(e.value) or bad == "https://"
    assert parse_telegram_token(TOKEN) == TOKEN and parse_telegram_token(None) is None
    for bad in ("123:short", "abc:AAH_fake-token-for-tests-0123456789abcd", "nocolon" * 6):
        with pytest.raises(ValueError) as e:
            parse_telegram_token(bad)
        assert bad not in str(e.value)
    assert parse_telegram_chat("-1001234567") == "-1001234567" and parse_telegram_chat("@my_channel") == "@my_channel"
    with pytest.raises(ValueError):
        parse_telegram_chat("12ab")


def test_load_settings_reads_and_validates_the_new_values(monkeypatch):
    for k in ("HEARTBEAT_URL", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_ALERTS", "FORWARD_UNIVERSE"):
        monkeypatch.delenv(k, raising=False)
    s = load_settings(dotenv=None)
    assert s.heartbeat_url is None and not s.telegram_on and s.forward_universe == "NIFTYMIDCAP150"
    monkeypatch.setenv("HEARTBEAT_URL", HB_URL)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    s = load_settings(dotenv=None)
    assert s.heartbeat_url == HB_URL and s.telegram_on
    monkeypatch.setenv("TELEGRAM_ALERTS", "false")
    assert not load_settings(dotenv=None).telegram_on
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bad-token")
    with pytest.raises(SystemExit) as e:
        load_settings(dotenv=None)
    assert "bad-token" not in str(e.value)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("HEARTBEAT_URL", "http://insecure.example/ping-secret")
    with pytest.raises(SystemExit) as e:
        load_settings(dotenv=None)
    assert "ping-secret" not in str(e.value)


def test_dashboard_settings_write_env_validate_and_never_echo_secrets(settings):
    from trading_agent.ui import App
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    env = settings.state_dir / ".env"
    app = App(settings, dotenv=env)
    with pytest.raises(ValueError) as e:
        app.update_settings({"telegram_bot_token": "oops-not-a-token"})
    assert "oops-not-a-token" not in str(e.value) and not env.exists()
    with pytest.raises(ValueError):
        app.update_settings({"heartbeat_url": "http://not-https.example/x"})
    app.update_settings({"telegram_bot_token": TOKEN, "telegram_chat_id": "42", "telegram_alerts": True,
                         "heartbeat_url": HB_URL})
    text = env.read_text(encoding="utf-8")
    assert f"TELEGRAM_BOT_TOKEN={TOKEN}" in text and "TELEGRAM_CHAT_ID=42" in text and f"HEARTBEAT_URL={HB_URL}" in text
    assert settings.telegram_on and settings.heartbeat_url == HB_URL
    snap = app.snapshot()["settings"]
    assert snap["telegram_token_set"] is True and snap["heartbeat_set"] is True and snap["telegram_chat_set"] is True
    assert TOKEN not in repr(snap) and "secret-path" not in repr(snap)
    assert "telegram_chat_id" not in snap        # the chat id is never sent back to the page either
    app.update_settings({"telegram_bot_token": "", "heartbeat_url": ""})
    assert settings.telegram_bot_token is None and settings.heartbeat_url is None and not settings.telegram_on


# ---------------------------------------------------------------- telegram
def tg_notifier(session, **kw):
    return Notifier(session=session, telegram_token=TOKEN, telegram_chat_id="42", **kw)


def test_telegram_is_off_unless_token_and_chat_are_both_set():
    s = Session()
    n = Notifier(session=s, telegram_token=TOKEN)
    assert "telegram" not in n.channels
    n.send("[STOP] x", "body")
    assert s.calls == []
    assert "telegram" in tg_notifier(s).channels


def test_telegram_message_shape_and_escaping():
    s = Session()
    n = tg_notifier(s)
    delivered = n.send("[NEWS] A&B <script>", "line one <b>x</b> & more\nsecond")
    assert "telegram" in delivered
    method, url, kw = s.calls[0]
    assert url == f"https://api.telegram.org/bot{TOKEN}/sendMessage" and kw["timeout"] == 30
    j = kw["json"]
    assert j["chat_id"] == "42" and j["parse_mode"] == "HTML" and j["disable_web_page_preview"] is True
    assert j["text"] == "<b>[NEWS] A&amp;B &lt;script&gt;</b>\nline one &lt;b&gt;x&lt;/b&gt; &amp; more\nsecond"


def test_telegram_splits_long_text_under_4000_without_breaking_entities():
    long = "\n".join("a & b < c " * 20 for _ in range(60))   # about 12,000 characters, all of them escaped
    chunks = telegram_chunks(long)
    assert len(chunks) >= 3 and all(len(c) <= 4000 for c in chunks)
    assert all("&amp" not in c.replace("&amp;", "") for c in chunks)   # no cut entity
    one_line = "&" * 9000   # a single line longer than a message
    assert all(len(c) <= 4000 for c in telegram_chunks(one_line)) and len(telegram_chunks(one_line)) >= 3
    s = Session()
    tg_notifier(s).send("S", long)
    sent = [c for c in s.calls if c[1].endswith("/sendMessage")]
    assert len(sent) == len(telegram_chunks("S\n" + long)) and all(len(c[2]["json"]["text"]) <= 4010 for c in sent)


def test_telegram_album_and_fallbacks():
    imgs = [{"cid": "a", "filename": "a.png", "content": b"\x89PNGa"}, {"cid": "b", "filename": "b.png", "content": b"\x89PNGb"}]
    s = Session()
    tg_notifier(s).send("Close", "text", images=imgs, telegram_text="short summary")
    assert s.calls[0][2]["json"]["text"].endswith("short summary")
    method, url, kw = s.calls[1]
    assert url.endswith("/sendMediaGroup") and set(kw["files"]) == {"p0", "p1"}
    assert '"attach://p0"' in kw["data"]["media"] and kw["data"]["chat_id"] == "42"
    # one picture goes as a single photo
    s1 = Session()
    tg_notifier(s1).send("Close", "t", images=imgs[:1])
    assert s1.calls[1][1].endswith("/sendPhoto")
    # the album fails: the text still went and nothing raises
    s2 = Session(fail=("sendMediaGroup",))
    delivered = tg_notifier(s2).send("Close", "t", images=imgs)
    assert "telegram" in delivered and s2.calls[0][1].endswith("/sendMessage")


def test_telegram_failure_never_fails_the_email_and_the_token_is_redacted(caplog):
    caplog.set_level(logging.DEBUG)

    class S(Session):
        def post(self, url, **kw):
            if "telegram" in url:
                raise ConnectionError(f"HTTPSConnectionPool: Max retries with url: /bot{TOKEN}/sendMessage ({TOKEN})")
            return super().post(url, **kw)

    s = S()
    n = Notifier(resend_api_key="rk", email_to="me@example.com", session=s, telegram_token=TOKEN, telegram_chat_id="42")
    delivered = n.send("Subject", "body")
    assert "email" in delivered and "telegram" not in delivered
    assert TOKEN not in caplog.text and "AAH_fake" not in caplog.text and "bot***" in caplog.text
    assert redact(f"GET https://api.telegram.org/bot{TOKEN}/getMe") == "GET https://api.telegram.org/bot***/getMe"
    assert TOKEN not in redact(f"token {TOKEN} here", TOKEN)
    # HTTP errors raised by raise_for_status are redacted too
    caplog.clear()
    s2 = Session(fail=("api.telegram.org",))
    tg_notifier(s2).send("S", "b")
    assert TOKEN not in caplog.text


def test_daily_email_gives_telegram_the_summary_and_key_lines():
    from trading_agent.digest_schedule import send_digest, telegram_summary
    email = {"subject": "Today: bullish · 2 buy ideas · 1 to watch", "text": "FULL EMAIL TEXT " * 50, "html": "<p>x</p>",
             "summary": "A calm start.", "images": [{"cid": "c", "filename": "c.png", "content": b"1"}],
             "data": {"buy_ideas": {"ideas": [{"symbol": "AAA", "price": 100.0, "qty": 5, "stop": 90.0}]},
                      "watch": {"items": [{"symbol": "BBB", "reasons": ["down 12%", "results due"]}]}}}
    text = telegram_summary(email)
    assert "A calm start." in text and "AAA at 100.0" in text and "BBB: down 12%; results due" in text
    s = Session()
    send_digest(tg_notifier(s), email, "morning", "2026-10-12")
    sent = [c for c in s.calls if c[1].endswith("/sendMessage")]
    assert "FULL EMAIL TEXT" not in sent[0][2]["json"]["text"] and "Holdings to review (1 flagged)" in sent[0][2]["json"]["text"] and "BBB" in sent[0][2]["json"]["text"]
    assert s.calls[-1][1].endswith("/sendPhoto")


# ---------------------------------------------------------------- email headers and subjects
def test_list_unsubscribe_only_for_a_custom_sender_domain():
    assert custom_domain("Agent <agent@mail.example.com>") and not custom_domain("Trading Agent <onboarding@resend.dev>")
    assert not custom_domain("") and not custom_domain("x@send.resend.dev")
    for sender, expect in (("Agent <agent@mail.example.com>", True), ("Trading Agent <onboarding@resend.dev>", False)):
        s = Session()
        Notifier(resend_api_key="rk", email_to="me@example.com", email_from=sender, session=s).send("Subj", "b")
        payload = s.calls[0][2]["json"]
        if expect:
            assert payload["headers"] == {"List-Unsubscribe": "<mailto:me@example.com?subject=unsubscribe>",
                                          "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}
        else:
            assert "headers" not in payload


PROMO = re.compile(r"\b(free|offer|discount|winner|act now|limited time|guarantee[d]?|urgent|buy now|click here|"
                   r"earn|cash|prize|100%)\b|!!|\$\$|₹₹", re.I)


def test_current_subjects_have_no_promotional_words():
    g = {"day_pl": 1200.0, "day_pct": 0.8, "pl": 30000.0, "pl_pct": 4.2}
    subjects = [
        digest_render.subject({"kind": "morning", "mood": {"regime": "risk_on"}, "buy_ideas": {"ideas": [{}, {}]},
                               "watch": {"items": [], "total": 3}}),
        digest_render.subject({"kind": "morning", "mood": {"unavailable": "x"}, "buy_ideas": {"wait": True},
                               "watch": {"unavailable": "x"}}),
        digest_render.subject({"kind": "evening", "groww": g, "practice": {"unavailable": "x"}}),
        digest_render.subject({"kind": "evening", "groww": {"unavailable": "x"}, "practice": {"unavailable": "x"}}),
        "[DEAL] ASHISH KACHOLIA: BUY ABC - strong accumulation", "[STOP] 2 position(s) hit their stop",
        "[NEWS] 3 new announcement(s)", "[NEWS] ABC: results beat", "[ORDER FILLED] BUY ABC",
        "[GTT CREATED FAILED] ABC",
    ]
    assert len(subjects) >= 10
    for sub in subjects:
        assert not PROMO.search(sub), sub
    src = " ".join(p.read_text(encoding="utf-8") for p in (ROOT / "trading_agent").glob("*.py"))
    for fixed in re.findall(r'send\(f?"(\[[A-Z ]+[^"]*)"', src):
        assert not PROMO.search(fixed), fixed


# ---------------------------------------------------------------- forward: rebuild
def weekdays(start: date, n: int):
    d, out = start, []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


DAYS = weekdays(date(2026, 9, 1), 30)   # to mid October: the future of 9 Oct is in the data
ASOF = "2026-10-09"


def make_bars(base, drift):
    return [{"date": d.isoformat(), "close": round(base * (1 + drift) ** i, 2), "adj_close": round(base * (1 + drift) ** i, 2),
             "volume": 1e6} for i, d in enumerate(DAYS)]


FUTURE = {"A": make_bars(100, 0.004), "B": make_bars(250, 0.003), "C": make_bars(1000, 0.002),
          "D": make_bars(40, 0.001), "E": make_bars(500, -0.002), "MID150BEES": make_bars(200, 0.001)}
# E is the weakest as of 9 Oct but explodes afterwards: a screen that peeked at the future would pick it
FUTURE["E"][-1]["close"] = 99999.0


class FakePrices:
    def __init__(self, data):
        self.data = data

    def history(self, sym, range_="2y"):
        return list(self.data[sym.upper()])

    def latest_price(self, sym):
        return float(self.data[sym.upper()][-1]["close"])


def screen_by_last_close_momentum(prices, members=None):
    rows = []
    for sym in [m["symbol"] for m in members] if members else ("A", "B", "C", "D", "E"):
        bars = prices.history(sym)
        rows.append({"symbol": sym, "score": bars[-1]["close"] / bars[0]["close"]})
    rows.sort(key=lambda r: -r["score"])
    return {"top": rows[:4], "eligible": len(rows)}


def test_forward_cli_has_the_rebuild_options():
    from trading_agent.cli import build_parser
    a = build_parser().parse_args(["forward", "--rebuild-from", "2026-10-09", "--universe", "NIFTYMIDCAP150", "--force"])
    assert a.rebuild_from == "2026-10-09" and a.force is True and a.top == 20
    assert build_parser().parse_args(["forward", "--if-due"]).rebuild_from is None


# ---------------------------------------------------------------- forward: scheduler on the server
class Holidays:
    def __init__(self, closed=()):
        self.closed = set(closed)

    def is_trading_day(self, d):
        return d.weekday() < 5 and d not in self.closed


def at(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=IST)


def test_scheduler_runs_once_per_trading_day_after_1610(tmp_path):
    runs = []
    sch = ForwardScheduler(tmp_path, lambda: runs.append(1), holidays=Holidays(), threaded=False)
    assert sch.tick(at(2026, 10, 12, 16, 9))["due"] is False and runs == []
    assert sch.tick(at(2026, 10, 12, 16, 10))["started"] is True and len(runs) == 1
    sch.tick(at(2026, 10, 12, 16, 11))
    sch.tick(at(2026, 10, 12, 18, 0))
    assert len(runs) == 1
    # a restart (a new scheduler on the same state directory) cannot run it again
    again = ForwardScheduler(tmp_path, lambda: runs.append(2), holidays=Holidays(), threaded=False)
    assert again.tick(at(2026, 10, 12, 17, 0)) == {"due": True, "claimed": False} and len(runs) == 1
    # the next trading day runs; old claims are cleaned
    assert again.tick(at(2026, 10, 13, 16, 30))["started"] is True and len(runs) == 2
    assert sorted(f.name for f in tmp_path.glob("forward_*.claim")) == ["forward_2026-10-12.claim", "forward_2026-10-13.claim"]


def test_scheduler_skips_weekends_holidays_and_late_nights(tmp_path):
    runs = []
    sch = ForwardScheduler(tmp_path, lambda: runs.append(1), holidays=Holidays({date(2026, 10, 20)}), threaded=False)
    for when in (at(2026, 10, 10, 17, 0), at(2026, 10, 11, 17, 0), at(2026, 10, 20, 17, 0), at(2026, 10, 12, 8, 0),
                 at(2026, 10, 12, 23, 45)):
        assert sch.tick(when)["due"] is False
    assert runs == []


def test_scheduler_retries_a_failed_run_a_few_times_then_stops(tmp_path):
    now = [0.0]
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("Yahoo down")

    sch = ForwardScheduler(tmp_path, boom, holidays=Holidays(), threaded=False, clock=lambda: now[0])
    t = at(2026, 10, 12, 16, 30)
    sch.tick(t)
    assert len(calls) == 1 and not list(tmp_path.glob("forward_*.claim"))   # released for a retry
    assert sch.tick(t) == {"due": True, "waiting": True} and len(calls) == 1
    for n in range(2, MAX_TRIES + 3):
        now[0] += 601
        sch.tick(t)
    assert len(calls) == MAX_TRIES


def test_scheduler_runs_in_a_background_thread_and_the_watch_tick_never_raises(tmp_path, settings):
    done = []
    sch = ForwardScheduler(tmp_path, lambda: done.append(1), holidays=Holidays())
    out = sch.tick(at(2026, 10, 12, 16, 20))
    sch.wait()
    assert out["started"] is True and done == [1]

    class Broken:
        def tick(self, now):
            raise RuntimeError("x")

    w = Watcher(settings, every=15, awake=None, forward=Broken())
    assert w.tick(force=True)["at"]


# ---------------------------------------------------------------- fix round 1
# -- Telegram never counts as the digest being delivered
def test_telegram_success_does_not_hide_a_failed_email_and_telegram_is_not_repeated(settings, tmp_path):
    from trading_agent.digest_schedule import DigestScheduler
    from trading_agent.digest import read_digest_state
    s = dataclasses.replace(settings, market="in", state_dir=tmp_path / "st")
    sess = Session(fail=("api.resend.com",))
    n = Notifier(resend_api_key="rk", email_to="me@example.com", session=sess, telegram_token=TOKEN,
                 telegram_chat_id="42", telegram_state_dir=s.state_dir / "telegram_sent")
    mail = lambda kind, cancel=None: {"subject": f"{kind} subject", "text": "t", "html": "<p>t</p>", "writer": "none"}  # noqa: E731
    sc = DigestScheduler(s, lambda: None, n, holidays=Holidays(), retry_after=0, build_fn=mail)
    when = at(2026, 10, 12, 9, 30)
    sc.tick(when)
    sc.wait()
    assert (read_digest_state(s.state_dir).get("sent") or {}).get("morning") != "2026-10-12"
    assert sc.last["outcome"] == "failed"
    tg1 = [c for c in sess.calls if "telegram" in c[1]]
    assert len(tg1) == 1
    assert (s.state_dir / "telegram_sent" / "morning_2026-10-12").exists()
    sc.tick(when)   # the retry
    sc.wait()
    assert len([c for c in sess.calls if "resend" in c[1]]) >= 2   # the email was tried again
    assert len([c for c in sess.calls if "telegram" in c[1]]) == 1  # Telegram was not sent twice


# -- rebuild: point in time, strict bars, holidays, stamps
class Mem:
    def __init__(self, members, known_since="2021-01-01"):
        self.members, self.known_since = set(members), known_since

    def members_on(self, day):
        return set(self.members)


def rebuild(tmp_path, data=None, mem=None, holidays=None, **kw):
    seen = {}

    def screen(prices, members):
        seen["members"] = [m["symbol"] for m in members]
        return screen_by_last_close_momentum(prices, members)
    out = rebuild_from(tmp_path, ASOF, top=4, capital=100_000, prices=FakePrices(data or FUTURE),
                       cost_model=cost_model_for("in"), screen_fn=screen,
                       members_loader=lambda: [{"symbol": x, "name": x, "industry": ""} for x in "ABCD"],
                       membership=mem or Mem("ABCDE"), holidays=holidays, **kw)
    return out, seen


def test_asof_prices_need_a_bar_dated_exactly_that_day():
    p = AsOfPrices(FakePrices(FUTURE), ASOF)
    assert p.latest_price("A") == next(b["close"] for b in FUTURE["A"] if b["date"] == ASOF)
    assert p.latest_price("E") < 1000
    gap = {"Z": [b for b in FUTURE["A"] if b["date"] < ASOF]}
    with pytest.raises(LookupError):
        AsOfPrices(FakePrices(gap), ASOF).latest_price("Z")
    assert not AsOfPrices(FakePrices(gap), ASOF).has_bar("Z")


def test_rebuild_equals_a_fresh_run_and_uses_point_in_time_members(tmp_path):
    cm = cost_model_for("in")
    rebuilt, seen = rebuild(tmp_path / "r")
    assert seen["members"] == list("ABCDE") and rebuilt["members_on_asof"] == 5
    past = {k: [b for b in bars if b["date"] <= ASOF] for k, bars in FUTURE.items()}
    fp = FakePrices(past)
    clock = datetime(2026, 10, 9, 16, 0, tzinfo=IST)
    fresh = ForwardTest(tmp_path / "f", universe="NIFTYMIDCAP150", top=4, capital=100_000, price_fn=fp.latest_price,
                        cost_model=cm, now=lambda: clock)
    fs = fresh.run(lambda: screen_by_last_close_momentum(fp))
    loaded = ForwardTest(tmp_path / "r", universe="NIFTYMIDCAP150", price_fn=lambda s: 1.0, cost_model=cm)
    assert loaded.data == fresh.data and rebuilt["holdings"] == fs["holdings"] and rebuilt["skipped_no_bar"] == []
    assert "E" not in rebuilt["last_picks"]


def test_rebuild_stamps_the_asof_date_not_the_wall_clock(tmp_path):
    rebuild(tmp_path)
    bro = json.loads((tmp_path / "forward" / "niftymidcap150_broker.json").read_text())
    assert bro["created_at"].startswith("2026-10-09")
    assert bro["orders"] and all(o["filled_at"].startswith("2026-10-09") for o in bro["orders"])
    assert json.loads((tmp_path / "forward" / "niftymidcap150.json").read_text())["started"].startswith("2026-10-09")


def test_rebuild_refuses_unknown_late_or_holiday_cases_with_clear_messages(tmp_path):
    with pytest.raises(ValueError, match="only known from 2026-11-01"):
        rebuild(tmp_path / "a", mem=Mem("ABCDE", known_since="2026-11-01"))
    with pytest.raises(ValueError, match="no point-in-time membership"):
        rebuild_from(tmp_path / "b", ASOF, prices=FakePrices(FUTURE), cost_model=None, screen_fn=None,
                     members_loader=lambda: [], membership=None)
    with pytest.raises(ValueError, match="holiday"):
        rebuild(tmp_path / "c", holidays=Holidays({date(2026, 10, 9)}))
    with pytest.raises(ValueError, match="weekend"):
        rebuild_from(tmp_path / "d", "2026-10-10", prices=FakePrices(FUTURE), cost_model=None, screen_fn=None,
                     members_loader=lambda: [], membership=Mem("A"))
    no_bench = {k: v for k, v in FUTURE.items() if k != "MID150BEES"}
    no_bench["MID150BEES"] = [b for b in FUTURE["MID150BEES"] if b["date"] < ASOF]
    with pytest.raises(ValueError, match="benchmark"):
        rebuild(tmp_path / "e", data=no_bench)
    assert not (tmp_path / "e" / "forward").exists()


def test_rebuild_leaves_out_a_pick_without_a_bar_and_says_so(tmp_path):
    data = dict(FUTURE)
    data["A"] = [b for b in FUTURE["A"] if b["date"] != ASOF]   # A has no bar on the day (older closes exist)
    out, _ = rebuild(tmp_path, data=data)
    assert out["skipped_no_bar"] == ["A"] and "A" not in out["last_picks"]
    from trading_agent.forward import format_rebuild
    assert "Left out" in format_rebuild(out) and "A" in format_rebuild(out)


def test_rebuild_refuses_an_existing_account_unless_forced(tmp_path):
    rebuild(tmp_path)
    path = tmp_path / "forward" / "niftymidcap150.json"
    before = path.read_text(encoding="utf-8")
    with pytest.raises(FileExistsError, match="--force"):
        rebuild(tmp_path)
    assert path.read_text(encoding="utf-8") == before
    path.unlink()
    with pytest.raises(FileExistsError):
        rebuild(tmp_path)
    out, _ = rebuild(tmp_path, force=True)
    assert out["days"] == 1 and len(out["holdings"]) == 4


# -- no silent fresh account on the server
def server_settings(settings, tmp_path, start=None):
    return dataclasses.replace(settings, market="in", state_dir=tmp_path / "st", forward_start=start)


def test_server_refuses_to_start_a_fresh_account_and_alerts_once_a_day(settings, tmp_path):
    s = server_settings(settings, tmp_path)
    sess = Session()
    n = Notifier(session=sess, webhook_url="https://hooks.example/x")
    msg = run_forward_due(s, FakePrices(FUTURE), Holidays(), n, today=date(2026, 10, 12))
    assert msg == "no forward account; run forward --rebuild-from DATE or set FORWARD_START"
    run_forward_due(s, FakePrices(FUTURE), Holidays(), n, today=date(2026, 10, 12))
    assert len(n.sent) == 1 and n.sent[0]["subject"].startswith("[FORWARD]")
    run_forward_due(s, FakePrices(FUTURE), Holidays(), n, today=date(2026, 10, 13))
    assert len(n.sent) == 2   # once per day
    assert not (s.state_dir / "forward").exists()


def test_server_rebuilds_from_forward_start_then_runs_the_normal_flow(settings, tmp_path, monkeypatch):
    from trading_agent import forward_schedule
    s = server_settings(settings, tmp_path, start=ASOF)
    called = {}

    def fake_rebuild(settings_, prices, holidays, asof, **kw):
        called["asof"] = asof
        out, _ = rebuild(tmp_path / "st")
        return out
    monkeypatch.setattr(forward_schedule, "rebuild_for_settings", fake_rebuild)
    monkeypatch.setattr(forward_schedule, "datetime", SimpleNamespace(now=lambda tz=None: datetime(2026, 10, 12, 16, 30, tzinfo=IST)))
    monkeypatch.setattr("trading_agent.screen.load_universe", lambda u: [{"symbol": x, "name": x, "industry": ""} for x in "ABCD"])
    monkeypatch.setattr("trading_agent.screen.run_screen", lambda members, prices, top=20: screen_by_last_close_momentum(prices))
    text = run_forward_due(s, FakePrices(FUTURE), Holidays(), None, today=date(2026, 10, 12))
    assert called["asof"] == ASOF and "Forward test" in text
    # a start date that is today or later is not "earlier than today": refuse
    s2 = server_settings(settings, tmp_path / "other", start="2026-10-12")
    assert "no forward account" in run_forward_due(s2, FakePrices(FUTURE), Holidays(), None, today=date(2026, 10, 12))


def test_forward_start_setting_is_validated():
    from trading_agent.config import parse_forward_start
    assert parse_forward_start("2026-10-09") == "2026-10-09" and parse_forward_start("") is None
    for bad in ("09/10/2026", "2026-13-40", "x"):
        with pytest.raises(ValueError):
            parse_forward_start(bad)


def test_forward_claim_stale_takeover(tmp_path):
    sch = ForwardScheduler(tmp_path, lambda: None, holidays=Holidays(), threaded=False)
    day = "2026-10-12"
    claim = sch._claim_path(day)
    assert sch._claim(day) and not sch._claim(day)
    pid, _ = claim.read_text().split()
    assert pid == str(os.getpid())
    # an old claim from a process that is gone is taken over; a fresh one, or one of a live process, is not
    claim.write_text(f"999999 {int(time.time()) - 3600}")
    sch._pid_gone = staticmethod(lambda p: True)
    assert sch._claim(day)
    claim.write_text(f"999999 {int(time.time()) - 60}")
    assert not sch._claim(day)
    claim.write_text(f"999999 {int(time.time()) - 3600}")
    sch._pid_gone = staticmethod(lambda p: False)
    assert not sch._claim(day)


# -- heartbeat on its own thread, tied to the loop's progress
def test_fail_url_keeps_the_query_and_goes_on_the_path():
    assert fail_url("https://hc-ping.com/abc") == "https://hc-ping.com/abc/fail"
    assert fail_url("https://hc-ping.com/abc/?create=1&x=2") == "https://hc-ping.com/abc/fail?create=1&x=2"


def hb(session, clock, progress, **kw):
    h = Heartbeat(HB_URL, session=session, clock=lambda: clock[0], **kw)
    h._progress, h._stall = progress, stall_after(60)
    return h


def test_beat_ok_stalled_fail_and_immediate_recovery():
    now = [10_000.0]
    stamp = [now[0]]
    s = Session()
    h = hb(s, now, lambda: stamp[0])
    assert h.beat() == "ok" and s.calls[-1][0] == "GET" and s.calls[-1][2]["timeout"] == 10
    now[0] += 1300   # no progress for 21 minutes (limit max(3*60, 1200))
    assert h.beat() == "fail"
    method, url, kw = s.calls[-1]
    assert method == "POST" and url == HB_URL + "/fail" and kw["data"] == b"watch loop stalled 21 min"
    now[0] += 30
    assert h.beat() == "skip" and len(s.calls) == 2   # still stalled: no fail spam inside the interval
    stamp[0] = now[0]   # the loop is back
    assert h.beat() == "ok" and s.calls[-1][0] == "GET"   # healthy again: ok at once


def test_reported_error_pings_fail_with_the_text_and_url_off_means_nothing():
    now = [0.0]
    s = Session()
    h = hb(s, now, lambda: now[0])
    h.report_error("ValueError: tick exploded")
    assert h.beat() == "fail" and s.calls[-1][2]["data"] == b"ValueError: tick exploded"
    off = Session()
    assert Heartbeat(None, session=off).beat() == "skip" and off.calls == []


def test_fail_goes_only_to_hc_ping_unless_enabled():
    now = [0.0]
    other = "https://uptime.example.com/api/push/TOKEN"
    s = Session()
    h = Heartbeat(other, session=s, clock=lambda: now[0])
    h._progress, h._stall = (lambda: now[0]), 1200
    h.report_error("boom")
    h.beat()
    assert s.calls == []   # no /fail to another provider: it only sees the pings stop
    h.beat()
    assert s.calls == []   # and no ok ping until a tick has finished cleanly
    h.report_clean()
    h.beat()
    assert s.calls[-1][0] == "GET"
    s2 = Session()
    h2 = Heartbeat(other, session=s2, clock=lambda: now[0], fail_enabled=True)
    h2._progress, h2._stall = (lambda: now[0]), 1200
    h2.report_error("boom")
    h2.beat()
    assert s2.calls[-1][1] == other + "/fail"


def wait_for(cond, secs=3.0):
    end = time.time() + secs
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def test_heartbeat_thread_pings_on_its_own_and_fails_at_once_on_a_reported_error():
    s = Session()
    h = Heartbeat(HB_URL, session=s, every=0.05)
    stop = threading.Event()
    h.start(lambda: time.monotonic(), stall_s=1200, stop=stop)
    try:
        assert wait_for(lambda: len([c for c in s.calls if c[0] == "GET"]) >= 3)
        h.report_error("RuntimeError: x")
        assert wait_for(lambda: any(c[0] == "POST" for c in s.calls))
        n = len(s.calls)
        time.sleep(0.2)
        assert not [c for c in s.calls[n:] if c[0] == "GET"]   # no ok ping until a tick finished cleanly
        h.report_clean()
        assert wait_for(lambda: len([c for c in s.calls[n:] if c[0] == "GET"]) >= 1)   # recovered
    finally:
        stop.set()
        h.stop()


def test_watch_loop_stamps_progress_reports_tick_errors_and_never_depends_on_the_ping(settings):
    log: list = []

    class HB:
        def start(self, progress, *, stall_s, stop, **kw):
            log.append(("start", stall_s, progress() > 0))

        def report_error(self, text):
            log.append(("error", text))
            w._stop.set()

        def stop(self):
            pass

    w = Watcher(settings, every=15, awake=None, heartbeat=HB())
    calls = {"n": 0}

    def tick(force=False):
        calls["n"] += 1
        raise RuntimeError("tick blew up")
    w.tick = tick
    before = w._progress
    w.run_forever()
    assert log[0] == ("start", 1200, True) and log[1][0] == "error" and log[1][1].startswith("RuntimeError: tick blew up")
    assert w._progress >= before


# -- logging never carries secrets
def test_log_redaction_covers_bot_tokens_and_the_heartbeat_path_even_at_debug(caplog):
    from trading_agent.notify import install_log_redaction
    install_log_redaction()
    Heartbeat(HB_URL, session=Session())   # registers its secrets
    caplog.set_level(logging.DEBUG)
    logging.getLogger("some.library").debug("GET https://api.telegram.org/bot%s/getMe", TOKEN)
    logging.getLogger("other").warning("ping %s failed", HB_URL)
    logging.getLogger("other").info("path /0123abcd-secret-path-token seen")
    text = caplog.text
    assert TOKEN not in text and "AAH_fake" not in text and "secret-path" not in text and "hc-ping.com/0123" not in text
    assert "bot***" in text
    assert logging.getLogger("urllib3").level == logging.WARNING


# -- state/watch_alive.json
def test_watch_alive_file_is_written_atomically_throttled_and_never_breaks_the_loop(settings, monkeypatch):
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    w = Watcher(settings, every=15, awake=None)
    w._tick_started = "2026-10-12T10:00:00+05:30"
    w._write_alive("boom")
    path = settings.state_dir / "watch_alive.json"
    d = json.loads(path.read_text())
    assert set(d) == {"at", "tick_started", "tick_finished", "last_error", "every", "market_window"}
    assert d["last_error"] == "boom" and d["every"] == 15 and d["tick_finished"] is None
    assert d["at"].endswith("+05:30") and isinstance(d["market_window"], bool)
    path.unlink()
    w._write_alive(None)   # inside 30 s: skipped
    assert not path.exists()
    w._write_alive(None, force=True)
    assert json.loads(path.read_text())["last_error"] is None
    # a write failure is swallowed
    import trading_agent.state as st
    monkeypatch.setattr(st, "atomic_write", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    w._write_alive("x", force=True)
    # and the loop writes it around each tick
    path.unlink()
    monkeypatch.undo()
    w2 = Watcher(settings, every=15, awake=None)
    w2.tick = lambda force=False: (w2._stop.set(), {})[1]
    w2.run_forever()
    d = json.loads(path.read_text())
    assert d["tick_started"] and d["last_error"] is None


# -- fix round 2
def test_ten_check_errors_give_no_fail_a_raising_tick_gives_one_and_a_clean_tick_gives_ok(settings):
    sess = Session()
    now = [0.0]
    h = Heartbeat(HB_URL, session=sess, clock=lambda: now[0])
    w = Watcher(settings, every=15, awake=None, heartbeat=h)
    h._progress, h._stall = (lambda: now[0]), 1200
    seq = [{"check_error": "GrowwTokenUnavailable: down"}] * 10 + [RuntimeError("bug")] + [{}] * 2
    n = {"i": 0}

    def tick(force=False):
        item = seq[n["i"]]
        n["i"] += 1
        if n["i"] >= len(seq):
            w._stop.set()
        if isinstance(item, Exception):
            raise item
        return item
    w.tick = tick
    # run the loop without waiting between iterations, beating the heartbeat by hand after each one
    w._stop.wait = lambda t=None: None
    beats = []
    orig = h.report_error, h.report_clean

    def rec_err(t):
        orig[0](t)
        beats.append(("err", h.beat()))

    def rec_clean():
        orig[1]()
        beats.append(("clean", h.beat()))
    h.report_error, h.report_clean = rec_err, rec_clean
    w._stop = threading.Event()
    w.run_forever()
    posts = [c for c in sess.calls if c[0] == "POST"]
    assert len(posts) == 1 and posts[0][2]["data"].startswith(b"RuntimeError: bug")
    kinds = [b for b in beats]
    first_err = next(i for i, b in enumerate(kinds) if b[0] == "err")
    assert all(b[0] == "clean" for b in kinds[:first_err])   # the ten check errors only ever produced clean ticks
    assert not [c for c in sess.calls[:sess.calls.index(posts[0])] if c[0] == "POST"]
    # after the raising tick: no ok until a clean tick, then ok
    after = sess.calls[sess.calls.index(posts[0]) + 1:]
    assert after and after[0][0] == "GET"


def test_heartbeat_fail_is_rate_limited_to_one_per_five_minutes():
    now = [0.0]
    s = Session()
    h = hb(s, now, lambda: now[0])
    h.report_error("one")
    assert h.beat() == "fail"
    now[0] += 120
    h.report_error("two")
    assert h.beat() == "skip" and len([c for c in s.calls if c[0] == "POST"]) == 1
    now[0] += 200
    h.report_error("three")
    assert h.beat() == "fail" and len([c for c in s.calls if c[0] == "POST"]) == 2


def test_the_end_of_iteration_alive_write_is_forced(settings):
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    w = Watcher(settings, every=15, awake=None)

    def tick(force=False):
        w._stop.set()
        raise RuntimeError("late failure")
    w.tick = tick
    w.run_forever()
    d = json.loads((settings.state_dir / "watch_alive.json").read_text())
    assert d["tick_finished"] and d["last_error"].startswith("RuntimeError: late failure")   # inside the 30 s throttle


def test_old_telegram_markers_and_forward_claims_are_pruned_once_a_day(tmp_path):
    (tmp_path / "telegram_sent").mkdir()
    old, new = tmp_path / "telegram_sent" / "morning_2026-09-01", tmp_path / "telegram_sent" / "morning_2026-10-11"
    for f in (old, new):
        f.write_text("sent")
    t = time.time() - 9 * 86400
    os.utime(old, (t, t))
    (tmp_path / "forward_2026-10-01.claim").write_text("1 1")
    (tmp_path / "forward_2026-10-10.claim").write_text("1 1")
    sch = ForwardScheduler(tmp_path, lambda: None, holidays=Holidays(), threaded=False)
    sch.tick(at(2026, 10, 12, 9, 0))
    assert not old.exists() and new.exists()
    assert not (tmp_path / "forward_2026-10-01.claim").exists() and (tmp_path / "forward_2026-10-10.claim").exists()
    again = tmp_path / "telegram_sent" / "x"
    again.write_text("s")
    os.utime(again, (t, t))
    sch.tick(at(2026, 10, 12, 9, 5))
    assert again.exists()   # the prune ran once for that day
    sch.tick(at(2026, 10, 13, 9, 5))
    assert not again.exists()


def test_log_redaction_scrubs_exception_text_and_is_installed_in_every_entry_point(caplog, monkeypatch):
    import io
    from trading_agent import cli, notify
    notify.install_log_redaction()
    notify.install_log_redaction()   # the guard: no second wrap
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    lg = logging.getLogger("exc.test")
    lg.addHandler(handler)
    lg.propagate = False
    try:
        try:
            raise requests.ConnectionError(f"HTTPSConnectionPool: Max retries with url: /bot{TOKEN}/sendMessage")
        except requests.ConnectionError:
            lg.exception("telegram call failed")
    finally:
        lg.removeHandler(handler)
    out = stream.getvalue()
    assert "bot***" in out and TOKEN not in out and "AAH_fake" not in out and "Traceback" in out
    import inspect
    from trading_agent import ui
    assert "install_log_redaction()" in inspect.getsource(cli.main) and "install_log_redaction()" in inspect.getsource(ui.serve)
