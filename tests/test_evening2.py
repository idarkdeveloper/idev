"""Evening email and evening Telegram, second layout pass: no duplicate prose, the shared world table, portfolio column
names, the levels-and-momentum matrix, one header subtitle, the order of the sections, chart axes, the Telegram money
line and cheat sheet, and the one-bubble album. Fakes only, no network."""
import re
from datetime import datetime

import pytest
import requests

from trading_agent import bulletin, charts
from trading_agent.digest_schedule import build_digest
from trading_agent.groww import IST
from trading_agent.notify import CAPTION_LIMIT, Notifier
from trading_agent.telegram_brief import telegram_brief

from .conftest import FakeResponse
from .test_bulletin import daily_bars, evening_ctx, sessions
from .test_digest import s  # noqa: F401  (a fixture)

TOKEN = "123456789:AAH_fake-token-for-tests-0123456789abcd"
SAT = datetime(2026, 10, 10, 18, 0, tzinfo=IST)


def section(text: str, title: str) -> str:
    """The text of one block, up to the next all-capitals heading line."""
    start = text.index(title)
    m = re.search(r"\n\n[A-Z][A-Z' :,&]+\n", text[start:])
    return text[start:start + m.start()] if m else text[start:]


@pytest.fixture
def mail(s):  # noqa: F811
    s.digest_writer = "rules"
    return build_digest("evening", evening_ctx(s))


# ---------- email ----------
def test_commodities_and_global_markets_do_not_repeat_the_table_in_prose(mail):
    text = mail["text"]
    assert "Gold rose" not in text and "Gold fell" not in text and "on the day" not in text
    com = section(text, "COMMODITIES CORNER")
    assert "Commodity" in com and "Gold" in com and "50-day average" in com.split("Not a forecast")[0]   # the table and the one rule note
    assert "rose" not in com and "higher over 5 sessions" not in com
    glob = section(text, "GLOBAL MARKETS")
    assert "S&P 500" in glob and not re.search(r"S&P 500 [\d,]+\.\d\d, [+−][\d.]+% on the day", glob)
    assert "Overnight futures:" not in glob and "VIX " not in glob.split("\n  Index")[0]
    assert "In the news: Nikkei: Nikkei slips as exporters fall (ET)" in glob
    assert glob.count("In the news:") == 1


def test_global_markets_in_the_evening_is_the_morning_table(mail):
    glob = section(mail["text"], "GLOBAL MARKETS")
    head = next(x for x in glob.split("\n") if x.strip().startswith("Index"))
    assert head.split() == ["Index", "Close", "1d", "5d", "20d", "Trend"]
    html = mail["html"]
    g = html[html.index("Global markets"):html.index("Commodities corner")]
    assert all(f">{h}<" in g for h in ("Index", "1d", "5d", "20d", "Trend")) and "<table" in g


def test_portfolio_columns_are_named_and_value_is_quantity_times_price(s, mail):  # noqa: F811
    text = mail["text"]
    port = section(text, "YOUR GROWW PORTFOLIO")
    head = next(x for x in port.split("\n") if x.strip().startswith("Stock"))
    assert head.split() == ["Stock", "Price", "Today", "Today", "₹", "Value", "₹", "P&L", "₹", "P&L", "%"]
    assert "Total ₹" not in text and "Total %" not in text and "Qty" not in port
    row = next(x for x in port.split("\n") if x.strip().startswith("AAA"))
    assert "₹1,100" in row                                   # 10 shares x ₹110
    h = mail["html"]
    start = h.index(">Your Groww portfolio<")
    phone = h[start:h.index(">Practice account<", start)]
    assert re.findall(r"<th[^>]*>([^<]*)</th>", phone) == ["Stock", "Price", "Today", "P&amp;L %"]
    s.digest_writer = "rules"
    stale = build_digest("evening", evening_ctx(s, now=lambda: SAT))
    port = section(stale["text"], "YOUR GROWW PORTFOLIO")
    head = next(x for x in port.split("\n") if x.strip().startswith("Stock"))
    assert head.split() == ["Stock", "Qty", "Price", "Value", "₹", "P&L", "₹", "P&L", "%"]
    assert "Total ₹" not in stale["text"] and "Today ₹" not in stale["text"] and "Today ₹" not in stale["html"]


def test_levels_and_momentum_matrix_replaces_the_loose_lines(mail):
    text = mail["text"]
    nifty = section(text, "MARKET BULLETIN: NIFTY 50")
    assert re.search(r"Metric\s+Value\s+Reading", nifty)
    for metric in ("Session", "Pivots", "Watch levels", "Trend strength", "RSI(14)", "Candle", "Time above 21 EMA"):
        assert re.search(rf"^\s+{re.escape(metric)}\s", nifty, re.M), metric
    assert re.search(r"Session\s+[\d,]+ – [\d,]+ · gap [+−]?[\d,]+", nifty)
    assert re.search(r"Pivots\s+S1 [\d,]+ · P [\d,]+ · R1 [\d,]+\s+next session", nifty)
    assert re.search(r"Daily ADX \d+ · 4-hour ADX \d+", nifty) and "swing" in nifty
    assert nifty.count("not a forecast") == 1 and nifty.index("Time above 21 EMA") < nifty.index("not a forecast")
    for gone in ("Watch levels:", "Pivot points from", "Daily candle:", "Daily RSI(14) is", "4-hour candles:"):
        assert gone not in text
    html = mail["html"]
    m = html[html.rindex("<table", 0, html.index(">Metric<")):html.index("Global markets")]
    assert m.count("<th") == 2 and "Trend strength" in m          # phone: two columns, the reading under the value


def test_matrix_says_when_daily_and_four_hour_disagree(monkeypatch):
    monkeypatch.setattr(bulletin, "wilder_adx", lambda bars, n=14: {
        "adx": [30.0] * len(bars), "plus_di": [25.0 if len(bars) > 40 else 10.0] * len(bars), "minus_di": [10.0 if len(bars) > 40 else 25.0] * len(bars)})
    rows = {r["metric"]: r for r in bulletin.analyse_nifty(daily_bars(), sessions(), sessions(days=10, step=60))["matrix"]}
    assert "point in different directions" in rows["Trend strength"]["reading"]
    assert rows["Trend strength"]["value"] == "Daily ADX 30 · 4-hour ADX 30"


def test_the_header_has_one_subtitle_and_no_trading_today_appears_once(s):  # noqa: F811
    s.digest_writer = "rules"
    mail = build_digest("evening", evening_ctx(s, now=lambda: SAT))
    text = mail["text"]
    lines = text.split("\n")
    assert lines[0].startswith("Close: your portfolio") and lines[2] == "Snapshot: Fri 9 Oct close (no trading today)"
    assert text.lower().count("no trading today") == 1 and "figures are from the last close" not in text
    assert mail["html"].count("Snapshot: Fri 9 Oct close") == 1
    live = build_digest("evening", evening_ctx(s))["text"]
    assert "Snapshot:" not in live


def test_the_order_of_the_evening_email(mail):
    titles = ["IN SHORT", "MARKET BULLETIN: NIFTY 50", "GLOBAL MARKETS", "COMMODITIES CORNER", "YOUR GROWW PORTFOLIO", "PRACTICE ACCOUNT",
              "NEWS FOR YOUR STOCKS TODAY", "TODAY'S DEALS BY FOLLOWED INVESTORS", "CONCEPT OF THE DAY"]
    pos = [mail["text"].index(t) for t in titles]
    assert pos == sorted(pos)
    assert mail["text"].index("CONCEPT OF THE DAY") > mail["text"].index("TODAY'S DEALS")


# ---------- charts ----------
def test_chart_price_axes_are_plain_and_no_axes_has_stray_tick_labels():
    import matplotlib
    from matplotlib.ticker import ScalarFormatter
    r = bulletin.analyse_nifty(daily_bars(), sessions(), sessions(days=22, step=60))["chart_input"]
    levels = [{"label": "Resistance", "value": 22500.0}, {"label": "Support", "value": 22360.0}, {"label": "Pivot", "value": 22465.0}]
    with charts._LOCK, matplotlib.rc_context(charts.RC):
        figs = [charts._draw_intraday(r["bars15"], r["ema21"], levels, 22420.0), charts._draw_4h(r["bars4h"], r["adx4h"])]
        for fig in figs:
            fig.canvas.draw()
            for ax in fig.axes:
                fmt = ax.yaxis.get_major_formatter()
                assert not isinstance(fmt, ScalarFormatter) and fmt.get_offset() == ""
                assert not ax.yaxis.get_offset_text().get_visible() and not ax.xaxis.get_offset_text().get_visible()
                labels = [t.get_text() for t in ax.get_yticklabels() if t.get_visible()]
                assert labels and all(re.fullmatch(r"[\d,]+", x) for x in labels), labels      # plain numbers: no "0" column, no 1e4
        a, b = figs
        assert len(a.axes) == 1                                                                   # no twin axis on the 15-minute chart
        assert len(b.axes) == 2
        top, bottom = b.axes
        assert not any(t.get_visible() and t.get_text() for t in top.get_xticklabels(which="both")) or not top.xaxis.get_tick_params()["labelbottom"]
        for fig in figs:                                                                          # no two axes occupy the same box (a twin)
            boxes = [tuple(round(x, 4) for x in ax.get_position().bounds) for ax in fig.axes]
            assert len(set(boxes)) == len(boxes)
        # labels stay inside the frame
        ax = a.axes[0]
        frame = a.bbox
        for t in ax.texts:
            bb = t.get_window_extent()
            assert frame.x0 <= bb.x0 and bb.x1 <= frame.x1 and frame.y0 <= bb.y0 and bb.y1 <= frame.y1, t.get_text()
        assert len(ax.texts) == 4                                                                 # prev close + three levels, all drawn


def test_charts_keep_their_size_and_byte_limits():
    r = bulletin.analyse_nifty(daily_bars(), sessions(), sessions(days=22, step=60))["chart_input"]
    for png in (charts.nifty_intraday_png(r["bars15"], r["ema21"], r["levels"], r["prev_close"]), charts.nifty_4h_png(r["bars4h"], r["adx4h"])):
        assert png and png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) <= charts.MAX_BYTES
        assert int.from_bytes(png[16:20], "big") == 1280 and int.from_bytes(png[20:24], "big") == 720


# ---------- Telegram ----------
def brief_for(s, **kw):  # noqa: F811
    s.digest_writer = "rules"
    m = build_digest("evening", evening_ctx(s, **kw))
    return telegram_brief(m)


def test_telegram_money_line_always_shows_today_and_the_cheat_sheet(s):  # noqa: F811
    h = brief_for(s)["html"]
    money = next(x for x in h.split("\n") if x.startswith("💼"))
    assert re.fullmatch(r"💼 <b>₹[\d,]+</b> · Today [+−]₹[\d,]+ \([+−][\d.]+%\) · Total [+−]₹[\d,]+ \([+−][\d.]+%\)", money), money
    assert re.search(r"^📊 Session: range [\d,]+ – [\d,]+ · gap [+−]?\d+$", h, re.M)
    assert re.search(r"^📐 Next pivots: S1 [\d,]+ · P [\d,]+ · R1 [\d,]+$", h, re.M)
    assert re.search(r"^⚡ Daily ADX \d+ · 4h ADX \d+ · RSI \d+$", h, re.M)
    stale = next(x for x in brief_for(s, now=lambda: SAT)["html"].split("\n") if x.startswith("💼"))
    assert re.fullmatch(r"💼 <b>₹[\d,]+</b> · Today: no trading · Total [+−]₹[\d,]+ \([+−][\d.]+%\)", stale), stale


def test_telegram_cheat_sheet_lines_appear_only_when_their_data_exists():
    def ev(nifty):
        return {"summary": "", "data": {"kind": "evening", "date": "2026-10-12", "groww": {"value": 1000.0, "pl": -10.0, "pl_pct": -1.0, "day_pl": -5.0, "day_pct": -0.5},
                                        "bulletin": {"nifty": nifty}}}
    full = {"close": 22500.0, "change_pct": 0.3, "low": 22295.0, "high": 22581.0, "gap": 83.0, "pivots": {"S1": 22350.0, "P": 22465.0, "R1": 22636.0},
            "adx": 44.2, "rsi": 37.4, "four_hour": {"adx": 30.1}}
    h = telegram_brief(ev(full))["html"]
    assert "📊 Session: range 22,295 – 22,581 · gap +83" in h and "📐 Next pivots: S1 22,350 · P 22,465 · R1 22,636" in h
    assert "⚡ Daily ADX 44 · 4h ADX 30 · RSI 37" in h
    h = telegram_brief(ev({"close": 22500.0, "change_pct": 0.3, "adx": 20.0}))["html"]
    assert "📊" not in h and "📐" not in h and "⚡ Daily ADX 20" in h and "4h ADX" not in h and "RSI" not in h


class Session:
    """Records every request; fails when the URL contains one of ``fail``."""

    def __init__(self, fail=()):
        self.calls, self.fail = [], fail

    def post(self, url, **kw):
        self.calls.append((url.rsplit("/", 1)[-1], kw))
        if any(f in url for f in self.fail):
            err = requests.HTTPError("boom")
            err.response = FakeResponse({"ok": False}, 400)
            raise err
        return FakeResponse({"ok": True})


IMGS = [{"cid": "a", "filename": "a.png", "content": b"\x89PNGa"}, {"cid": "b", "filename": "b.png", "content": b"\x89PNGb"}]


def tg(session):
    return Notifier(session=session, telegram_token=TOKEN, telegram_chat_id="42")


def test_one_bubble_short_text_is_the_caption_of_the_first_photo():
    html = "<b>🌆 Close</b>\n💼 short"
    sess = Session()
    assert "telegram" in tg(sess).send("Close", "body", images=IMGS, telegram_html=html, telegram_plain="plain")
    assert [c[0] for c in sess.calls] == ["sendMediaGroup"]                      # one request, the text is not sent twice
    import json
    media = json.loads(sess.calls[0][1]["data"]["media"])
    assert media[0]["caption"] == html and media[0]["parse_mode"] == "HTML" and "caption" not in media[1]
    one = Session()
    tg(one).send("Close", "body", images=IMGS[:1], telegram_html=html)
    assert [c[0] for c in one.calls] == ["sendPhoto"] and one.calls[0][1]["data"]["caption"] == html and one.calls[0][1]["data"]["parse_mode"] == "HTML"


def test_long_text_keeps_the_text_message_then_the_album():
    html = "x" * (CAPTION_LIMIT + 1)
    sess = Session()
    tg(sess).send("Close", "body", images=IMGS, telegram_html=html)
    assert [c[0] for c in sess.calls] == ["sendMessage", "sendMediaGroup"]
    assert sess.calls[0][1]["json"]["text"] == html and "caption" not in sess.calls[1][1]["data"]["media"]
    edge = Session()
    tg(edge).send("Close", "body", images=IMGS, telegram_html="y" * CAPTION_LIMIT)
    assert [c[0] for c in edge.calls] == ["sendMediaGroup"]                       # exactly 1,024 characters still fits


def test_album_error_falls_back_to_the_text_message_alone():
    sess = Session(fail=("sendMediaGroup",))
    assert "telegram" in tg(sess).send("Close", "body", images=IMGS, telegram_html="short <b>text</b>", telegram_plain="short text")
    assert [c[0] for c in sess.calls] == ["sendMediaGroup", "sendMessage"]        # no second try at the pictures, the text once
    assert sess.calls[1][1]["json"]["text"] == "short <b>text</b>"
    solo = Session(fail=("sendPhoto",))
    tg(solo).send("Close", "body", images=IMGS[:1], telegram_html="t")
    assert [c[0] for c in solo.calls] == ["sendPhoto", "sendMessage"]


def test_no_images_is_the_plain_text_message_as_before():
    sess = Session()
    tg(sess).send("Close", "body", telegram_html="hello", telegram_button={"inline_keyboard": []})
    assert [c[0] for c in sess.calls] == ["sendMessage"] and sess.calls[0][1]["json"]["reply_markup"] == {"inline_keyboard": []}
