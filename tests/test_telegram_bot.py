"""Telegram commands, step 1 (practice only): a fake Telegram API records every call; the practice orders go through a
real dashboard App on a temp state dir. No network, no real Telegram, no Groww, no live order."""
import json
import logging
from types import SimpleNamespace

import pytest

from trading_agent import health, killswitch, telegram_bot
from trading_agent.broker import LocalPaperBroker
from trading_agent.telegram_bot import (CONFIRM_TTL_S, HALT_TTL_S, CommandBot, TelegramAPI, TelegramError, lookup_buttons,
                                        take_lease)
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


def msg(text, chat=CHAT, uid=1, sender=None):
    return {"update_id": uid, "message": {"message_id": uid, "chat": {"id": int(chat)},
                                          "from": {"id": int(sender or chat)}, "text": text}}


def tap(data, chat=CHAT, uid=50, message_id=1, sender=None):
    return {"update_id": uid, "callback_query": {"id": f"cb{uid}", "data": data, "from": {"id": int(sender or chat)},
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


EQUITY_CSV = """SYMBOL,NAME OF COMPANY, SERIES, FACE VALUE
RELIANCE,Reliance Industries Limited,EQ,10
TCS,Tata Consultancy Services Limited,EQ,1
INFY,Infosys Limited,EQ,5
TATASTEEL,Tata Steel Limited,EQ,1
TATAMOTORS,Tata Motors Limited,EQ,2
HDFCBANK,HDFC Bank Limited,EQ,1
HDFCLIFE,HDFC Life Insurance Company Limited,EQ,10
HDFCAMC,HDFC Asset Management Company Limited,EQ,5
COALINDIA,Coal India Limited,EQ,10
EVIL,<b>Evil & Co</b> <a href='x'> Limited,EQ,1
"""


def seed_names(state_dir):
    """The NSE equity list as the dashboard caches it, so names resolve with no network."""
    cache = state_dir / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "nse_equity_list.csv").write_text(EQUITY_CSV, encoding="utf-8")


@pytest.fixture
def app(s, monkeypatch):
    seed_names(s.state_dir)
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
    assert api.sent() == [] and "another chat or sender (999)" in caplog.text and "secret-text" not in caplog.text


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
    assert "/buy NAME QTY" in help_ and "nothing here places a live order" in help_
    assert "live orders OFF" in status and "Kill switch: not halted." in status and "Day brake: off." in status
    assert unknown == "Unknown command /frobnicate. /help lists them."


# --------------------------------------------------------------------------- #
# practice orders: confirm, cancel, expiry, double tap, nonce, HALT
# --------------------------------------------------------------------------- #
def test_a_confirmed_buy_is_placed_in_the_practice_account_once(s, app):
    api = FakeAPI([msg("/buy RELIANCE 5")])
    b = bot(s, app, api)
    b.poll_once()
    assert "PRACTICE BUY</b> 5 × Reliance Industries (RELIANCE) at about ₹100.00 = ₹500" in api.texts()[0]
    assert "Company: <b>Reliance Industries (RELIANCE)</b>. Is that the one you meant?" in api.texts()[0]
    ok, _ = nonce_of(api)
    assert app.demo.practice_broker.positions() == []                        # nothing placed by the command itself
    api.batches.append([tap(ok)])
    b.poll_once()
    assert "<b>Practice BUY</b> 5 × Reliance Industries (RELIANCE) at ₹100.00" in api.texts()[-1]
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
    assert api.texts()[-3:] == ["Sell a number of shares: /sell NAME QTY.", "The quantity must be a whole number of shares.",
                                "Usage: /buy NAME QTY or /buy NAME ₹AMOUNT (practice account only)"]


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
    assert api.texts()[-1] == "Cancelled: practice buy of Reliance Industries (RELIANCE)."
    api.batches.append([msg("/buy INFY 2", uid=54)])
    b.poll_once()
    b.t[0] += CONFIRM_TTL_S + 1
    api.batches.append([tap(nonce_of(api)[0], uid=55)])
    b.poll_once()
    assert api.texts()[-1] == "Expired: the practice buy of Infosys (INFY) was not placed."
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
    seed_names(s.state_dir)
    app = App(s, broker=live, dotenv=None, prices=Prices())
    api = FakeAPI([msg("/buy RELIANCE 3")])
    b = bot(s, app, api)
    b.poll_once()
    api.batches.append([tap(nonce_of(api)[0])])
    b.poll_once()
    assert live.writes == [] and "<b>Practice BUY</b> 3 × Reliance Industries (RELIANCE)" in api.texts()[-1]
    assert isinstance(app.demo.broker, LocalPaperBroker) and app.demo.settings.groww_live_orders is False
    assert [(p.symbol, p.qty) for p in app.demo.practice_broker.positions()] == [("RELIANCE", 3.0)]
    status = api.texts()
    b.cmd_status([])
    assert "live orders ON" in api.texts()[-1] and "practice account only (never live orders)" in api.texts()[-1]
    assert status


def test_hostile_names_are_escaped(s, app, monkeypatch):
    api = FakeAPI([msg("/buy EVIL 1"), msg("/buy <script> 1", uid=2)])
    bot(s, app, api).poll_once()
    first = api.texts()[0]
    assert "&lt;b&gt;Evil &amp; Co&lt;/b&gt; &lt;a href='x'&gt; (EVIL)" in first and "<b>Evil" not in first
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
    assert "<b>Abc &lt;Industries&gt; (ABC)</b>" in look and "Momentum: 1m +2.0% · 3m +8.0% · 6m +20.0%" in look
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


# --------------------------------------------------------------------------- #
# follow-up: only you, never a group; the practice account never builds the live broker
# --------------------------------------------------------------------------- #
def test_only_the_chat_owner_is_obeyed_never_another_sender(s, app):
    api = FakeAPI([msg("/portfolio", sender="777")])
    b = bot(s, app, api)
    b.poll_once()
    assert api.sent() == []
    api.batches.append([msg("/buy RELIANCE 2", uid=2)])
    b.poll_once()
    ok, _ = nonce_of(api)
    api.batches.append([tap(ok, sender="777")])                              # someone else presses Confirm
    b.poll_once()
    assert app.demo.practice_broker.positions() == [] and ok.split(":")[1] in b.pending


def test_commands_are_never_taken_from_a_group_or_channel(s):
    for chat in ("-1001234567890", "-42", "@mychannel", "0"):
        s.telegram_chat_id = chat
        assert not telegram_bot.commands_configured(s) and telegram_bot.start(s) is None
    s.telegram_chat_id = CHAT
    assert telegram_bot.commands_configured(s)


def test_the_practice_account_never_builds_the_live_broker(s, monkeypatch):
    import trading_agent.runner as runner
    import trading_agent.ui as ui
    s.broker, s.groww_live_orders, s.groww_access_token = "groww", True, "tok"

    built = []

    def no_live(*a, **k):
        built.append(1)   # recorded, not raised: the old code swallowed any error from building it
        raise RuntimeError("the live broker was built")
    monkeypatch.setattr(ui, "make_broker", no_live)
    monkeypatch.setattr(runner, "make_groww", lambda settings, *a, **k: (_ for _ in ()).throw(AssertionError(
        "a live-capable client")) if settings.groww_live_orders else _ReadOnly())
    app = App(s, dotenv=None, prices=Prices())
    pb = app.demo.practice_broker
    assert isinstance(pb, LocalPaperBroker) and app._broker is None and built == []


class _ReadOnly:
    name, live_orders = "groww", False

    def latest_price(self, symbol):
        return 100.0


# --------------------------------------------------------------------------- #
# company names everywhere
# --------------------------------------------------------------------------- #
def test_names_of_any_length_with_the_numbers_after_them(s, app, monkeypatch):
    seen = []
    real = App.size_quote

    def spy(self, ticker, equity=None, risk_pct=1.0, max_pct=10.0):
        seen.append((ticker, risk_pct, max_pct))
        return real(self, ticker, equity, risk_pct, max_pct)
    monkeypatch.setattr(App, "size_quote", spy)
    api = FakeAPI([msg("/size tata steel 1 10"), msg("/size coal india", uid=2), msg("/buy tata steel 5", uid=3)])
    b = bot(s, app, api)
    b.poll_once()
    assert seen == [("TATASTEEL", 1.0, 10.0), ("COALINDIA", 1.0, 10.0)]
    size1, size2, confirm = api.texts()
    assert size1.startswith("<b>Tata Steel (TATASTEEL)</b>") and "(risk 1%, cap 10%)" in size1
    assert size2.startswith("<b>Coal India (COALINDIA)</b>")
    assert "5 × Tata Steel (TATASTEEL)" in confirm
    api.batches.append([tap(nonce_of(api)[0]), msg("/sell tata steel 2", uid=4)])
    b.poll_once()
    api.batches.append([tap(nonce_of(api)[0], uid=51)])
    b.poll_once()
    assert [(p.symbol, p.qty) for p in app.demo.practice_broker.positions()] == [("TATASTEEL", 3.0)]
    api.batches.append([msg("/portfolio", uid=5), msg("/stops", uid=6)])
    b.poll_once()
    port, stops = api.texts()[-2:]
    assert "• Tata Steel (TATASTEEL) 3 @ ₹100.00" in port and "• Tata Steel (TATASTEEL): trailing stop" in stops


def test_an_ambiguous_name_asks_which_company_and_runs_the_command_for_the_choice(s, app):
    api = FakeAPI([msg("/buy hdfc 2")])
    b = bot(s, app, api)
    b.poll_once()
    ask = api.sent()[-1]
    assert ask["text"].startswith("“hdfc” matches several companies. Which one?") and "(for /buy 2)" in ask["text"]
    rows = ask["reply_markup"]["inline_keyboard"]
    assert [r[0]["text"] for r in rows] == ["HDFC Bank (HDFCBANK)", "HDFC Life Insurance Company (HDFCLIFE)",
                                            "HDFC Asset Management Company (HDFCAMC)"]
    life = rows[1][0]["callback_data"]
    api.batches.append([tap(life, sender="777")])                             # not you: ignored
    b.poll_once()
    assert app.demo.practice_broker.positions() == [] and len(b.picks) == 1
    api.batches.append([tap(life, uid=52)])
    b.poll_once()
    assert "2 × HDFC Life Insurance Company (HDFCLIFE)" in api.texts()[-1]   # the same /buy, for the chosen one
    api.batches.append([tap(life, uid=53)])                                   # a second tap on the choice
    b.poll_once()
    assert ("answerCallbackQuery", {"callback_query_id": "cb53", "text": "Nothing to choose (already chosen or unknown)."}) in api.calls
    api.batches.append([tap(nonce_of(api)[0], uid=54)])
    b.poll_once()
    assert [(p.symbol, p.qty) for p in app.demo.practice_broker.positions()] == [("HDFCLIFE", 2.0)]


def test_a_choice_expires_after_two_minutes(s, app):
    api = FakeAPI([msg("/size hdfc 1 5")])
    b = bot(s, app, api)
    b.poll_once()
    pick = api.sent()[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    b.t[0] += CONFIRM_TTL_S + 1
    api.batches.append([tap(pick)])
    b.poll_once()
    assert ("answerCallbackQuery", {"callback_query_id": "cb50", "text": "Expired: send the command again."}) in api.calls
    assert len(api.sent()) == 1


def test_no_match_suggests_the_closest_names(s, app):
    api = FakeAPI([msg("/lookup relianse industries"), msg("/buy qqqqzzzz 1", uid=2), msg("/buy tata steel five", uid=3)])
    bot(s, app, api).poll_once()
    sugg, none, bad = api.sent()
    assert sugg["text"].startswith("No company is called “relianse industries”. Did you mean:")
    assert sugg["reply_markup"]["inline_keyboard"][0][0]["text"] == "Reliance Industries (RELIANCE)"
    assert none["text"] == "No NSE company matches “qqqqzzzz”. Try the company's name or its NSE code."
    assert bad["text"].startswith("Usage: /buy NAME QTY") and "reply_markup" not in bad   # caught before any choice


def test_the_command_list_is_registered_when_the_poller_starts(s, app):
    import threading
    stop = threading.Event()

    class Once(FakeAPI):
        def call(self, method, payload, timeout=10.0):
            out = super().call(method, payload, timeout)
            if method == "getUpdates":
                stop.set()
            return out
    api = Once()
    bot(s, app, api).run(stop)
    assert api.calls[0] == ("setMyCommands", {"commands": telegram_bot.COMMANDS})
    assert {c["command"] for c in telegram_bot.COMMANDS} == {"lookup", "size", "buy", "sell", "portfolio", "stops",
                                                              "brief", "status", "halt", "help"}
    assert {"command": "halt", "description": "Emergency kill switch: stop watch and trading"} in telegram_bot.COMMANDS
    assert all(c["command"].islower() and len(c["description"]) <= 256 for c in telegram_bot.COMMANDS)



# --------------------------------------------------------------------------- #
# the /lookup buttons: Size (1%), Stops, Practice Buy
# --------------------------------------------------------------------------- #
def answers(api):
    return [p["text"] for m, p in api.calls if m == "answerCallbackQuery"]


def test_lookup_reply_carries_the_three_buttons(s):
    api = FakeAPI([msg("/lookup abc")])
    bot(s, FakeApp(s), api).poll_once()
    assert api.sent()[0]["reply_markup"] == {"inline_keyboard": [[
        {"text": "📏 Size (1%)", "callback_data": "sz:ABC:1"},
        {"text": "🛡️ Stops", "callback_data": "st:ABC"},
        {"text": "🟢 Practice Buy", "callback_data": "by:ABC"}]]}
    assert lookup_buttons("M&M")["inline_keyboard"][0][0]["callback_data"] == "sz:M&M:1"
    assert lookup_buttons("BAJAJ-AUTO") is not None
    for bad in ("", "abc", "A:B", "A B", "<b>", "X" * 21):
        assert lookup_buttons(bad) is None
    assert all(len(b["callback_data"].encode()) <= 64 for b in lookup_buttons("X" * 20)["inline_keyboard"][0])


def test_size_button_runs_size_for_that_company_and_risk(s):
    fa = FakeApp(s)
    seen = []
    fa.size_quote = lambda t, risk_pct=1.0, max_pct=10.0: seen.append((t, risk_pct, max_pct)) or FakeApp.size_quote(fa, t)
    api = FakeAPI([tap("sz:ABC:1"), tap("sz:ABC:2.5", uid=51), tap("sz:ABC:9", uid=52)])
    bot(s, fa, api).poll_once()
    assert seen == [("ABC", 1.0, 10.0), ("ABC", 2.5, 10.0)]
    first, second, refused = api.texts()
    assert "9 shares × ₹110.00 = ₹990 (risk 1%, cap 10%)" in first and "(risk 2.5%, cap 10%)" in second
    assert refused.startswith("Risk must be above 0 and at most 5%")
    assert answers(api)[:2] == ["Size at 1% risk", "Size at 2.5% risk"]


def test_stops_button_answers_for_that_company_only(s):
    from trading_agent.state import State
    fa = FakeApp(s)
    fa.practice_broker.submit_order("XYZ", "buy", qty=1)
    st = State(s.state_dir / "state.json")
    st.data["gtt_stops"] = {"ABC": {"qty": 10, "trigger": 90.0, "status": "active"},
                            "OTHER": {"qty": 1, "trigger": 5.0, "status": "active"}}
    st.save(fresh_keys=())
    api = FakeAPI([tap("st:ABC"), tap("st:NEW", uid=51)])
    bot(s, fa, api).poll_once()
    abc, new = api.texts()
    assert abc.startswith("🛡️ <b>Stops · ABC</b>") and "• ABC: trailing stop ₹93.50" in abc
    assert "<b>GTT stop-loss at Groww</b>\n• ABC qty 10 trigger ₹90.00 (active)" in abc
    assert "XYZ" not in abc and "OTHER" not in abc
    assert "Not held in the practice account" in new and "Suggested stop: ₹93.50 (15% below)" in new


def test_practice_buy_button_only_shows_the_command_and_places_nothing(s, app):
    api = FakeAPI([tap("by:RELIANCE")])
    bot(s, app, api).poll_once()
    (text,) = api.texts()
    assert "<code>/buy RELIANCE QTY</code>" in text and "<code>/buy RELIANCE ₹AMOUNT</code>" in text
    assert "Reliance Industries (RELIANCE)" in text and "reply_markup" not in api.sent()[0]
    assert app.demo.practice_broker.positions() == []


def test_lookup_buttons_refuse_bad_data_other_chats_and_floods(s):
    fa = FakeApp(s)
    api = FakeAPI([tap("sz:abc:1"), tap("sz:ABC", uid=51), tap("st:ABC:1", uid=52), tap("sz:ABC:1e9", uid=53),
                   tap("by:<b>", uid=54), tap("st:ABC", chat="999", uid=55), tap("st:ABC", sender="777", uid=56)])
    b = bot(s, fa, api)
    b.poll_once()
    assert api.texts() == [] and answers(api) == ["Unknown button."] * 5
    api.batches.append([tap("st:ABC", uid=60 + i) for i in range(25)])
    b.poll_once()
    assert len(api.texts()) == 20 and answers(api)[-1] == "Too many commands: wait a minute."


# --------------------------------------------------------------------------- #
# /halt: the emergency kill switch
# --------------------------------------------------------------------------- #
class HaltApp(FakeApp):
    def __init__(self, s):
        super().__init__(s)
        self.halts = []

    def kill_switch(self, confirm, cancel_gtt=False, by="dashboard"):
        self.halts.append((confirm, cancel_gtt, by))
        return {"ok": True, "summary": "Trading HALTED (since now, by telegram).", "report": {}, "halt": {}}


def halt_buttons(api):
    kb = api.sent()[-1]["reply_markup"]["inline_keyboard"][0]
    assert [b["text"] for b in kb] == ["⛔ ENGAGE HALT", "Cancel"]
    return kb[0]["callback_data"], kb[1]["callback_data"]


def test_halt_asks_first_and_a_confirm_engages_the_kill_switch_once(s):
    ha = HaltApp(s)
    api = FakeAPI([msg("/halt")])
    b = bot(s, ha, api)
    b.poll_once()
    ask = api.texts()[0]
    assert ask.startswith("⛔ <b>Emergency stop</b>") and "Confirm within 60 seconds." in ask and ha.halts == []
    go, no = halt_buttons(api)
    assert go.startswith("halt_confirm:") and no == "no:" + go.split(":", 1)[1]
    api.batches.append([tap(go)])
    b.poll_once()
    assert ha.halts == [("HALT", False, "telegram")]
    assert api.texts()[-1] == "⛔ <b>HALT engaged</b>\nTrading HALTED (since now, by telegram)."
    assert ("editMessageReplyMarkup", {"chat_id": CHAT, "message_id": 1, "reply_markup": {"inline_keyboard": []}}) in api.calls
    api.batches.append([tap(go, uid=51)])   # a second tap
    b.poll_once()
    assert ha.halts == [("HALT", False, "telegram")] and answers(api)[-1].startswith("Nothing to confirm")


def test_halt_cancel_expiry_wrong_nonce_and_other_senders_engage_nothing(s):
    ha = HaltApp(s)
    api = FakeAPI([msg("/halt")])
    b = bot(s, ha, api)
    b.poll_once()
    go, no = halt_buttons(api)
    api.batches.append([tap("halt_confirm:forged"), tap(go, chat="999", uid=51), tap(go, sender="777", uid=52)])
    b.poll_once()
    assert ha.halts == []
    api.batches.append([tap(no, uid=53)])
    b.poll_once()
    assert api.texts()[-1] == "Cancelled: the kill switch was not engaged."
    api.batches.append([tap(go, uid=54)])   # cancelled: the confirm is gone too
    b.poll_once()
    assert ha.halts == []
    api.batches.append([msg("/halt", uid=55)])
    b.poll_once()
    b.t[0] += HALT_TTL_S
    api.batches.append([tap(halt_buttons(api)[0], uid=56)])
    b.poll_once()
    assert api.texts()[-1] == "Expired: the kill switch was not engaged. Send /halt again." and ha.halts == []


def test_an_order_nonce_never_engages_halt_and_a_halt_nonce_never_places_an_order(s, app):
    calls = []
    app.kill_switch = lambda *a, **k: calls.append((a, k))
    api = FakeAPI([msg("/buy RELIANCE 5"), msg("/halt", uid=2)])
    b = bot(s, app, api)
    b.poll_once()
    order_ok = api.sent()[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    halt_go = api.sent()[1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    api.batches.append([tap("halt_confirm:" + order_ok.split(":", 1)[1]), tap("ok:" + halt_go.split(":", 1)[1], uid=51)])
    b.poll_once()
    assert calls == [] and app.demo.practice_broker.positions() == []


def test_halt_is_never_rate_limited(s):
    ha = HaltApp(s)
    api = FakeAPI([msg("/help", uid=i) for i in range(1, 22)] + [msg("/halt", uid=30)])
    bot(s, ha, api).poll_once()
    assert api.texts()[-2] == "Too many commands: wait a minute."
    assert api.texts()[-1].startswith("⛔ <b>Emergency stop</b>")


def test_a_failed_halt_says_it_was_not_engaged(s):
    ha = HaltApp(s)

    def boom(*a, **k):
        raise PermissionError("The kill switch stops the real agent: use the Live page")
    ha.kill_switch = boom
    api = FakeAPI([msg("/halt")])
    b = bot(s, ha, api)
    b.poll_once()
    api.batches.append([tap(halt_buttons(api)[0])])
    b.poll_once()
    assert api.texts()[-1].startswith("⚠️ The kill switch was NOT engaged: PermissionError")


def test_halt_through_the_real_dashboard_kill_switch_writes_halt_and_blocks_orders(s, app, monkeypatch):
    sent = []

    class Note:
        def send(self, subject, body):
            sent.append(subject)
            return ["fake"]
    monkeypatch.setattr("trading_agent.ui.make_notifier", lambda settings: Note())
    api = FakeAPI([msg("/halt")])
    b = bot(s, app, api)
    b.poll_once()
    api.batches.append([tap(halt_buttons(api)[0])])
    b.poll_once()
    assert (s.state_dir / "HALT").exists() and killswitch.status(s.state_dir)["by"] == "telegram"
    assert sent == ["[HALT] kill switch engaged"] and api.texts()[-1].startswith("⛔ <b>HALT engaged</b>\nTrading HALTED")
    api.batches.append([msg("/buy RELIANCE 1", uid=60), msg("/halt", uid=61)])
    b.poll_once()
    assert api.texts()[-2].startswith("HALT is set") and "HALT is set" in api.texts()[-1]   # the ask says it is already on
