"""One engine, heartbeat, Telegram alerts, email headers, forward-test rebuild. Fakes only: no network."""
from __future__ import annotations

import dataclasses
import logging
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from trading_agent import digest_render, notify
from trading_agent.config import (load_settings, parse_heartbeat_url, parse_telegram_chat, parse_telegram_token)
from trading_agent.costs import cost_model_for
from trading_agent.forward import AsOfPrices, ForwardTest, IST, rebuild_from
from trading_agent.forward_schedule import MAX_TRIES, ForwardScheduler
from trading_agent.heartbeat import Heartbeat
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
        return FakeResponse({"ok": True}, 500 if any(f in url for f in self.fail) else 200)

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
    assert snap["telegram_token_set"] is True and snap["heartbeat_set"] is True
    assert TOKEN not in repr(snap) and "secret-path" not in repr(snap)
    app.update_settings({"telegram_bot_token": "", "heartbeat_url": ""})
    assert settings.telegram_bot_token is None and settings.heartbeat_url is None and not settings.telegram_on


# ---------------------------------------------------------------- heartbeat
def test_heartbeat_pings_every_five_minutes_and_not_without_a_url():
    now = [1000.0]
    s = Session()
    hb = Heartbeat(HB_URL, session=s, clock=lambda: now[0])
    hb.tick()
    assert [c[:2] for c in s.calls] == [("GET", HB_URL)] and s.calls[0][2]["timeout"] == 10
    now[0] += 299
    hb.tick()
    assert len(s.calls) == 1
    now[0] += 2
    hb.tick()
    assert len(s.calls) == 2
    off = Session()
    Heartbeat(None, session=off).tick()
    Heartbeat("", session=off).tick("boom")
    assert off.calls == []


def test_heartbeat_failure_goes_to_fail_with_the_error_text_at_once():
    now = [0.0]
    s = Session()
    hb = Heartbeat(HB_URL + "/", session=s, clock=lambda: now[0])
    hb.tick()
    now[0] += 1   # not due, but a failed tick is reported on the spot
    hb.tick("ValueError: tick exploded")
    method, url, kw = s.calls[-1]
    assert method == "POST" and url == HB_URL + "/fail" and kw["data"] == b"ValueError: tick exploded" and kw["timeout"] == 10


def test_heartbeat_never_raises_and_never_logs_the_url(caplog):
    caplog.set_level(logging.DEBUG)
    s = Session(exc=ConnectionError(f"HTTPSConnectionPool: Max retries exceeded with url: {HB_URL}"))
    hb = Heartbeat(HB_URL, session=s, clock=lambda: 0.0)
    assert hb.ping() is False and hb.ping("err") is False
    ok = Heartbeat(HB_URL, session=Session(), clock=lambda: 0.0)
    assert ok.ping() is True
    text = caplog.text
    assert "heartbeat failed" in text and "heartbeat ok" in text
    assert "secret-path" not in text and "hc-ping" not in text
    bad_status = Heartbeat(HB_URL, session=Session(fail=("hc-ping",)))
    assert bad_status.ping() is False and "secret-path" not in caplog.text


def test_watch_loop_pings_after_each_tick_and_reports_a_raised_tick(settings):
    seen: list = []

    class HB:
        def __init__(self, w):
            self.w = w

        def tick(self, error=None):
            seen.append(error)
            if len(seen) == 2:
                self.w._stop.set()

    w = Watcher(settings, every=15, awake=None)
    calls = {"n": 0}

    def tick(force=False):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("tick blew up")
        return {}

    w.tick = tick
    w._heartbeat = HB(w)
    w.run_forever()
    assert seen[0] is None and seen[1].startswith("RuntimeError: tick blew up")


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
    assert "FULL EMAIL TEXT" not in sent[0][2]["json"]["text"] and "AAA at 100.0" in sent[0][2]["json"]["text"]
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


def screen_by_last_close_momentum(prices):
    rows = []
    for sym in ("A", "B", "C", "D", "E"):
        bars = prices.history(sym)
        rows.append({"symbol": sym, "score": bars[-1]["close"] / bars[0]["close"]})
    rows.sort(key=lambda r: -r["score"])
    return {"top": rows[:4], "eligible": len(rows)}


def test_asof_prices_stop_at_the_date():
    p = AsOfPrices(FakePrices(FUTURE), ASOF)
    assert p.history("A")[-1]["date"] == ASOF and p.latest_price("A") == next(
        b["close"] for b in FUTURE["A"] if b["date"] == ASOF)
    assert p.latest_price("E") < 1000   # not the exploding last bar
    with pytest.raises(LookupError):
        AsOfPrices(FakePrices({"Z": [{"date": "2026-12-01", "close": 1.0}]}), ASOF).latest_price("Z")


def test_rebuild_equals_a_fresh_run_on_that_date(tmp_path):
    cm = cost_model_for("in")
    rebuilt = rebuild_from(tmp_path / "r", ASOF, top=4, capital=100_000, prices=FakePrices(FUTURE), cost_model=cm,
                           screen_fn=screen_by_last_close_momentum)
    # the fresh run: the routine ran that evening, when the data simply ended at that day
    past = {s: [b for b in bars if b["date"] <= ASOF] for s, bars in FUTURE.items()}
    fresh_prices = FakePrices(past)
    clock = datetime(2026, 10, 9, 16, 0, tzinfo=IST)
    fresh = ForwardTest(tmp_path / "f", universe="NIFTYMIDCAP150", top=4, capital=100_000, price_fn=fresh_prices.latest_price,
                        cost_model=cm, now=lambda: clock)
    fresh_summary = fresh.run(lambda: screen_by_last_close_momentum(fresh_prices))
    assert "E" not in rebuilt["last_picks"] and rebuilt["last_picks"] == fresh_summary["last_picks"]
    assert rebuilt["holdings"] == fresh_summary["holdings"] and rebuilt["history"] == fresh_summary["history"]
    loaded = ForwardTest(tmp_path / "r", universe="NIFTYMIDCAP150", price_fn=lambda s: 1.0, cost_model=cm)
    assert loaded.data == fresh.data
    assert [(p.symbol, p.qty) for p in loaded.broker.positions()] == [(p.symbol, p.qty) for p in fresh.broker.positions()]
    assert loaded.data["started"].startswith(ASOF) and loaded.data["last_rebalance"] == "2026-10"
    assert rebuilt["rebalanced"]["charges"] > 0 and loaded.data["bench_cost"] > 0


def test_rebuild_refuses_an_existing_account_unless_forced(tmp_path):
    kw = dict(top=4, capital=100_000, prices=FakePrices(FUTURE), cost_model=cost_model_for("in"),
              screen_fn=screen_by_last_close_momentum)
    rebuild_from(tmp_path, ASOF, **kw)
    path = tmp_path / "forward" / "niftymidcap150.json"
    before = path.read_text(encoding="utf-8")
    with pytest.raises(FileExistsError) as e:
        rebuild_from(tmp_path, ASOF, **kw)
    assert "--force" in str(e.value) and path.read_text(encoding="utf-8") == before
    # a half-existing account (only the broker file) also counts
    path.unlink()
    with pytest.raises(FileExistsError):
        rebuild_from(tmp_path, ASOF, **kw)
    summary = rebuild_from(tmp_path, ASOF, force=True, **kw)
    assert summary["days"] == 1 and len(summary["holdings"]) == 4
    with pytest.raises(ValueError):
        rebuild_from(tmp_path / "w", "2026-10-10", **kw)   # a Saturday
    with pytest.raises(ValueError):
        rebuild_from(tmp_path / "x", "yesterday", **kw)


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
    assert [f.name for f in tmp_path.glob("forward_*.claim")] == ["forward_2026-10-13.claim"]


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
