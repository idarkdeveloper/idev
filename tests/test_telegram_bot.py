"""Telegram commands, step 1 (practice only): a fake Telegram API records every call; the practice orders go through a
real dashboard App on a temp state dir. No network, no real Telegram, no Groww, no live order."""
import json
import logging
from types import SimpleNamespace

import pytest

from trading_agent import health, killswitch, telegram_bot
from trading_agent.broker import LocalPaperBroker
from trading_agent.telegram_bot import CONFIRM_TTL_S, CommandBot, TelegramAPI, TelegramError, take_lease
from trading_agent.ui import App

CHAT = "4242"
TOKEN = "123456789:AAH_fake-token-for-tests-0123456789abcd"


class FakeAPI:
    """Answers getUpdates from queued batches; records every call."""

    def __init__(self, *batches):
        self.token = TOKEN
        self.batches = list(batches)
        self.calls = []
        self.n = 0

    def call(self, method, payload, timeout=10.0):
        self.calls.append((method, payload))
        if method == "getUpdates":
            return self.batches.pop(0) if self.batches else []
        if method == "sendMessage":
            self.n += 1
            return {"message_id": self.n}
        return True

    def sent(self):
        return [p for m, p in self.calls if m == "sendMessage"]

    def texts(self):
        return [p["text"] for p in self.sent()]


def msg(text, chat=CHAT, uid=1):
    return {"update_id": uid, "message": {"message_id": uid, "chat": {"id": int(chat)}, "text": text}}


def tap(data, chat=CHAT, uid=50, message_id=1):
    return {"update_id": uid, "callback_query": {"id": f"cb{uid}", "data": data,
                                                 "message": {"message_id": message_id, "chat": {"id": int(chat)}}}}


class Prices:
    def __call__(self, symbol):
        return 100.0

    def history(self, symbol, range_="1y"):
        return []


@pytest.fixture
def s(settings):
    settings.market = "in"
    settings.telegram_bot_token, settings.telegram_chat_id, settings.telegram_commands = TOKEN, CHAT, True
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    return settings


@pytest.fixture
def app(s, monkeypatch):
    from trading_agent.runner import make_practice_broker
    # the real practice account (its HALT / collar / day-brake gate included), priced by the fake: no network
    a = App(s, broker=make_practice_broker(s, price_fn=Prices()), dotenv=None, prices=Prices())
    monkeypatch.setattr(App, "resolve", lambda self, text: (text.strip().upper(), None))
    monkeypatch.setattr(App, "holidays", property(lambda self: None))
    return a


def bot(s, app, api, clock=None):
    t = clock or [1000.0]
    b = CommandBot(s, app, api, clock=lambda: t[0])
    b.t = t
    return b


def nonce_of(api):
    kb = api.sent()[-1]["reply_markup"]["inline_keyboard"][0]
    return kb[0]["callback_data"], kb[1]["callback_data"]


# --------------------------------------------------------------------------- #
# who is obeyed, the offset, the rate limit
# --------------------------------------------------------------------------- #
def test_another_chat_is_ignored_and_logged_without_its_text(s, app, caplog):
    api = FakeAPI([msg("/buy RELIANCE 5 secret-text", chat="999")])
    with caplog.at_level(logging.INFO):
        assert bot(s, app, api).poll_once() == 1
    assert api.sent() == [] and "another chat (999)" in caplog.text and "secret-text" not in caplog.text


def test_the_offset_is_saved_so_a_restart_never_replays_a_command(s, app):
    api = FakeAPI([msg("/help", uid=7), msg("/help", uid=8)])
    bot(s, app, api).poll_once()
    assert json.loads((s.state_dir / "telegram_commands.json").read_text())["offset"] == 9
    api2 = FakeAPI()
    bot(s, app, api2).poll_once()
    assert api2.calls[0] == ("getUpdates", {"offset": 9, "timeout": 25, "allowed_updates": ["message", "callback_query"]})


def test_more_than_20_commands_a_minute_are_refused_once(s, app):
    api = FakeAPI([msg("/help", uid=i) for i in range(1, 26)])
    b = bot(s, app, api)
    b.poll_once()
    texts = api.texts()
    assert len(texts) == 21 and texts[-1] == "Too many commands: wait a minute."
    b.t[0] += 61
    api.batches.append([msg("/help", uid=30)])
    b.poll_once()
    assert api.texts()[-1].startswith("<b>Commands</b>")


def test_help_status_and_an_unknown_command(s, app):
    api = FakeAPI([msg("/help"), msg("/status", uid=2), msg("/frobnicate", uid=3)])
    bot(s, app, api).poll_once()
    help_, status, unknown = api.texts()
    assert "/buy SYMBOL QTY" in help_ and "nothing here places a live order" in help_
    assert "live orders OFF" in status and "Kill switch: not halted." in status and "Day brake: off." in status
    assert unknown == "Unknown command /frobnicate. /help lists them."


# --------------------------------------------------------------------------- #
# practice orders: confirm, cancel, expiry, double tap, nonce, HALT
# --------------------------------------------------------------------------- #
def test_a_confirmed_buy_is_placed_in_the_practice_account_once(s, app):
    api = FakeAPI([msg("/buy RELIANCE 5")])
    b = bot(s, app, api)
    b.poll_once()
    assert "PRACTICE BUY</b> 5 × RELIANCE at about ₹100.00 = ₹500" in api.texts()[0]
    ok, _ = nonce_of(api)
    assert app.demo.practice_broker.positions() == []                        # nothing placed by the command itself
    api.batches.append([tap(ok)])
    b.poll_once()
    assert "<b>Practice BUY</b> 5 × RELIANCE at ₹100.00" in api.texts()[-1]
    assert [(p.symbol, p.qty) for p in app.demo.practice_broker.positions()] == [("RELIANCE", 5.0)]
    assert ("editMessageReplyMarkup", {"chat_id": CHAT, "message_id": 1, "reply_markup": {"inline_keyboard": []}}) in api.calls
    api.batches.append([tap(ok, uid=51)])                                    # the second tap
    b.poll_once()
    assert ("answerCallbackQuery", {"callback_query_id": "cb51",
                                    "text": "Nothing to confirm (already done, cancelled or unknown)."}) in api.calls
    assert [(p.symbol, p.qty) for p in app.demo.practice_broker.positions()] == [("RELIANCE", 5.0)]


def test_an_amount_buy_and_a_sell(s, app):
    api = FakeAPI([msg("/buy TCS ₹1,050")])
    b = bot(s, app, api)
    b.poll_once()
    api.batches.append([tap(nonce_of(api)[0])])
    b.poll_once()
    assert [(p.symbol, p.qty) for p in app.demo.practice_broker.positions()] == [("TCS", 10.0)]
    api.batches.append([msg("/sell TCS 4", uid=60)])
    b.poll_once()
    api.batches.append([tap(nonce_of(api)[0], uid=61)])
    b.poll_once()
    assert [(p.symbol, p.qty) for p in app.demo.practice_broker.positions()] == [("TCS", 6.0)]
    api.batches.append([msg("/sell TCS ₹500", uid=62), msg("/buy TCS 1.5", uid=63), msg("/buy", uid=64)])
    b.poll_once()
    assert api.texts()[-3:] == ["Sell a number of shares: /sell SYMBOL QTY.", "The quantity must be a whole number of shares.",
                                "Usage: /buy SYMBOL QTY or /buy SYMBOL ₹AMOUNT (practice account only)"]


def test_cancel_expiry_wrong_nonce_and_another_chats_tap_place_nothing(s, app):
    api = FakeAPI([msg("/buy RELIANCE 5")])
    b = bot(s, app, api)
    b.poll_once()
    ok, no = nonce_of(api)
    api.batches.append([tap("ok:not-the-nonce"), tap(ok, chat="999", uid=52)])
    b.poll_once()
    assert app.demo.practice_broker.positions() == []
    api.batches.append([tap(no, uid=53)])
    b.poll_once()
    assert api.texts()[-1] == "Cancelled: practice buy of RELIANCE."
    api.batches.append([msg("/buy INFY 2", uid=54)])
    b.poll_once()
    b.t[0] += CONFIRM_TTL_S + 1
    api.batches.append([tap(nonce_of(api)[0], uid=55)])
    b.poll_once()
    assert api.texts()[-1] == "Expired: the practice buy of INFY was not placed."
    assert app.demo.practice_broker.positions() == []


def test_under_halt_orders_refuse_and_read_commands_still_answer(s, app):
    api = FakeAPI([msg("/buy RELIANCE 5")])
    b = bot(s, app, api)
    b.poll_once()
    ok, _ = nonce_of(api)
    killswitch.engage(s, by="test")                                           # halted between the ask and the tap
    api.batches.append([tap(ok), msg("/sell RELIANCE 1", uid=70), msg("/status", uid=71)])
    b.poll_once()
    refused_tap, refused_sell, status = api.texts()[-3:]
    assert refused_tap.startswith("Refused: HALT is set") and refused_sell.startswith("HALT is set")
    assert "HALT is set" in status
    assert app.demo.practice_broker.positions() == []


def test_the_indian_filters_and_the_day_brake_hold_a_buy_back(s, app, monkeypatch):
    class Flagged:
        def refuse_buy(self, symbol):
            return "on NSE's ASM list"

        def note(self):
            return None
    monkeypatch.setattr("trading_agent.indian_filters.filters_for", lambda settings, **k: Flagged())
    api = FakeAPI([msg("/buy RELIANCE 5")])
    b = bot(s, app, api)
    b.poll_once()
    api.batches.append([tap(nonce_of(api)[0])])
    b.poll_once()
    assert api.texts()[-1].startswith("Held back by the Indian-market filters (no override from Telegram)")
    monkeypatch.undo()
    monkeypatch.setattr(App, "resolve", lambda self, text: (text.strip().upper(), None))
    from trading_agent import day_brake
    monkeypatch.setattr(day_brake, "reason", lambda state_dir, now=None: "day brake: down 4.0% today")
    api.batches.append([msg("/buy RELIANCE 5", uid=80)])
    b.poll_once()
    api.batches.append([tap(nonce_of(api)[0], uid=81)])
    b.poll_once()
    assert api.texts()[-1] == "Refused: day brake: down 4.0% today"
    assert app.demo.practice_broker.positions() == []


def test_there_is_no_path_to_a_live_order_even_with_live_orders_on(s, monkeypatch):
    class LiveGroww:
        name, live_orders = "groww", True
        writes = []

        def latest_price(self, symbol):
            return 100.0

        def positions(self):
            return []

        def submit_order(self, *a, **k):
            self.writes.append((a, k))
            raise AssertionError("a live order was sent")
    live = LiveGroww()
    s.broker, s.groww_live_orders, s.groww_access_token = "groww", True, "tok"
    import trading_agent.runner as runner
    monkeypatch.setattr(runner, "make_groww", lambda *a, **k: live)
    monkeypatch.setattr(App, "resolve", lambda self, text: (text.strip().upper(), None))
    monkeypatch.setattr(App, "holidays", property(lambda self: None))
    app = App(s, broker=live, dotenv=None, prices=Prices())
    api = FakeAPI([msg("/buy RELIANCE 3")])
    b = bot(s, app, api)
    b.poll_once()
    api.batches.append([tap(nonce_of(api)[0])])
    b.poll_once()
    assert live.writes == [] and "<b>Practice BUY</b> 3 × RELIANCE" in api.texts()[-1]
    assert isinstance(app.demo.broker, LocalPaperBroker) and app.demo.settings.groww_live_orders is False
    assert [(p.symbol, p.qty) for p in app.demo.practice_broker.positions()] == [("RELIANCE", 3.0)]
    status = api.texts()
    b.cmd_status([])
    assert "live orders ON" in api.texts()[-1] and "practice account only (never live orders)" in api.texts()[-1]
    assert status


def test_hostile_names_are_escaped(s, app, monkeypatch):
    monkeypatch.setattr(App, "resolve", lambda self, text: ("EVIL", "<b>Evil & Co</b> <a href='x'>"))
    api = FakeAPI([msg("/buy evil 1"), msg("/buy <script> 1", uid=2)])
    bot(s, app, api).poll_once()
    first = api.texts()[0]
    assert "(&lt;b&gt;Evil &amp; Co&lt;/b&gt; &lt;a href='x'&gt;)" in first and "<b>Evil" not in first
    assert "<script>" not in "".join(api.texts())


# --------------------------------------------------------------------------- #
# read commands (the App's own answers, formatted)
# --------------------------------------------------------------------------- #
class FakeApp:
    """Stands in for the dashboard App for the read commands; records that nothing forces a Groww login."""

    def __init__(self, s):
        self.settings = s
        self.prices = Prices()
        self.portfolio_calls = []
        self.practice_broker = LocalPaperBroker(s.state_dir / "pb.json", starting_cash=10_000, price_fn=lambda x: 110.0,
                                                whole_shares=True)
        self.practice_broker.submit_order("ABC", "buy", qty=10)

    @property
    def demo(self):
        return self

    def resolve(self, text):
        return text.strip().upper(), "Abc <Industries> Limited"

    def lookup(self, t):
        return {"ticker": t, "name": "Abc <Industries> Limited", "price": 110.0,
                "momentum": {"verdict": "strong", "ret_1m": 0.02, "ret_3m": 0.08, "ret_6m": 0.2, "ret_12_1": 0.3,
                             "above_200dma": True, "pct_from_52w_high": -0.1},
                "history": [{"d": "2026-10-09", "c": 110.0, "ma200": 95.5}], "announcements": [],
                "filter_flags": ["Results due 15 Oct"]}

    def news_for_ticker(self, t):
        return {"items": [{"title": "Abc wins <big> order", "source": "Mint & Co"}]}

    def size_quote(self, t, risk_pct=1.0, max_pct=10.0):
        return {"ticker": t, "qty": 9, "price": 110.0, "notional": 990.0, "stop": 93.5, "basis": "15% below",
                "equity": 10_000.0, "round_trip_cost": {"charges": 3.21}}

    def my_portfolio(self, refresh=False, force=False):
        self.portfolio_calls.append(force)
        return {"linked": True, "source": "saved", "reason": "market closed — holdings saved at the 9 Oct close",
                "value": 5000.0, "pl": 500.0, "pl_pct": 0.111,
                "holdings": [{"symbol": "XYZ", "qty": 5, "avg_price": 900.0, "price": 1000.0, "value": 5000.0}]}

    def digest_email(self, kind, groww=None):
        self.brief_groww = groww
        return {"data": {"kind": kind}, "writer": "rules", "summary": None, "subject": "s", "text": "t", "html": "h"}


def test_lookup_size_portfolio_and_stops(s):
    fa = FakeApp(s)
    api = FakeAPI([msg("/lookup abc"), msg("/size abc 1 5", uid=2), msg("/portfolio", uid=3), msg("/stops", uid=4)])
    bot(s, fa, api).poll_once()
    look, size, port, stops = api.texts()
    assert "<b>ABC</b> · Abc &lt;Industries&gt; Limited" in look and "Momentum: 1m +2.0% · 3m +8.0% · 6m +20.0%" in look
    assert "200-day average ₹95.50 (above it)" in look and "Suggested stop: ₹93.50" in look
    assert "Abc wins &lt;big&gt; order (Mint &amp; Co)" in look and "<b>What this means</b>" in look
    assert "Bottom line" in look and "Filters: Results due 15 Oct" in look
    assert "9 shares × ₹110.00 = ₹990 (risk 1%, cap 5%)" in size and "Round-trip charges about ₹3.21" in size
    assert "<b>Practice account</b>" in port and "• ABC 10 @ ₹110.00" in port
    assert "<b>Groww</b> (market closed — holdings saved at the 9 Oct close)" in port and "(+11.1%)" in port
    assert fa.portfolio_calls == [False]                                      # never forced: no Groww login is forced
    assert "• ABC: trailing stop ₹93.50, 15.0% below ₹110.00" in stops


def test_brief_sends_the_telegram_brief_with_its_button(s, monkeypatch):
    fa = FakeApp(s)
    monkeypatch.setattr("trading_agent.telegram_brief.telegram_brief",
                        lambda email, hosts=None: {"html": "<b>Morning</b> brief", "plain": "x",
                                                   "button": {"inline_keyboard": [[{"text": "📊 Dashboard", "url": "https://x/"}]]}})
    api = FakeAPI([msg("/brief")])
    b = bot(s, fa, api)
    b.poll_once()
    assert api.texts() == ["Building the morning brief…", "<b>Morning</b> brief"]
    assert api.sent()[-1]["reply_markup"]["inline_keyboard"][0][0]["text"] == "📊 Dashboard"
    fa.brief_groww()
    assert fa.portfolio_calls == [False]


def test_a_long_reply_is_cut_at_a_line_end():
    text = "\n".join(f"line {i} <b>x</b>" for i in range(1000))
    out = telegram_bot.fit(text)
    assert len(out) <= 4000 and out.endswith("\n…") and out.count("<b>") == out.count("</b>")


# --------------------------------------------------------------------------- #
# the API wrapper, the lease, the Health row, the setting
# --------------------------------------------------------------------------- #
def test_api_errors_never_carry_the_token(caplog):
    class Session:
        def post(self, url, json=None, timeout=None):
            raise ConnectionError(f"failed to reach {url}")
    api = TelegramAPI(TOKEN, session=Session())
    with pytest.raises(TelegramError) as ei:
        api.call("getUpdates", {})
    assert TOKEN not in str(ei.value) and "bot***" in str(ei.value)

    class Refused:
        def post(self, url, json=None, timeout=None):
            return SimpleNamespace(json=lambda: {"ok": False, "description": "Conflict: terminated by other getUpdates"})
    with pytest.raises(TelegramError, match="Conflict"):
        TelegramAPI(TOKEN, session=Refused()).call("getUpdates", {})


def test_one_poller_at_a_time(tmp_path):
    assert take_lease(tmp_path, "a", now=1000.0) and take_lease(tmp_path, "a", now=1010.0)
    assert not take_lease(tmp_path, "b", now=1050.0)
    assert take_lease(tmp_path, "b", now=1131.0)                             # a's lease ran out


def test_the_health_row_and_the_setting(s, app, monkeypatch, tmp_path):
    from datetime import datetime

    from trading_agent.timezones import IST
    row = {r["id"]: r for r in health.report(s, datetime.now(IST))["sources"]}["telegram_commands"]
    assert row["status"] == "late"
    bot(s, app, FakeAPI([msg("/help")])).poll_once()
    row = {r["id"]: r for r in health.report(s, datetime.now(IST))["sources"]}["telegram_commands"]
    assert row["status"] == "ok" and row["rows"] == 1
    s.telegram_commands = False
    row = {r["id"]: r for r in health.report(s, datetime.now(IST))["sources"]}["telegram_commands"]
    assert row["status"] == "off" and telegram_bot.start(s) is None
    from trading_agent.config import load_settings
    monkeypatch.chdir(tmp_path)
    assert load_settings(None).telegram_commands is False


def test_a_failing_round_is_recorded_and_backed_off(s, app):
    import threading
    stop = threading.Event()

    class Down(FakeAPI):
        def call(self, method, payload, timeout=10.0):
            stop.set()
            raise TelegramError("getUpdates: Bad Gateway")
    bot(s, app, Down()).run(stop)
    data = json.loads((s.state_dir / "telegram_commands.json").read_text())
    assert data["last_error"] == "TelegramError: getUpdates: Bad Gateway"
    assert health.read_runs(s.state_dir)["telegram_commands"]["error"]
