"""The Telegram look of the alerts (telegram_cards.py, news_story.render_telegram, telegram_brief): golden cards from the owner's XPROINDIA
examples, length limits, escaping, link buttons, the expandable quote and its plain fallback, the ranking of grouped filings, the
brief's sections, the notifier's 400 fallback, and that nothing acts. Fakes only: no message is ever sent."""
from __future__ import annotations

import re

import pytest
import requests

from trading_agent import telegram_cards as tc
from trading_agent import telegram_preview as pv
from trading_agent.config import _dashboard_url
from trading_agent.notify import Notifier

BASE = "https://box.tailnet.ts.net"
TAGS_OK = {"b", "i", "u", "s", "code", "pre", "a", "blockquote", "tg-spoiler"}


def visible(html: str) -> str:
    return html.split("<blockquote")[0]


def only_telegram_tags(html: str) -> None:
    for m in re.finditer(r"</?([a-zA-Z-]+)([^>]*)>", html):
        assert m.group(1) in TAGS_OK, m.group(0)
    assert html.count("<blockquote expandable>") == html.count("</blockquote>") <= 1


# ---- the DEAL card ----
def deal(**over):
    return tc.deal_card(over.pop("rec", pv.SAMPLE_REC), deal=over.pop("deal", pv.SAMPLE_DEAL), holding_qty=over.pop("held", None), market_wait=over.pop("wait", True),
                        regime=over.pop("regime", "risk_off"), base_url=over.pop("base", BASE))


def test_the_xproindia_deal_card_is_a_scannable_card_not_a_wall_of_text():
    c = deal()
    h, plain = c["html"], c["plain"]
    lines = visible(h).strip().split("\n")
    assert lines[0] == "<b>▼ SOLD XPROINDIA · Kacholia</b>" and len(re.sub("<[^>]+>", "", lines[0])) <= 70
    assert "<code>Rs21.5 cr</code> · <code>2.22 lakh sh @ Rs970</code>" in h
    assert "deal disclosed · 19 Oct" in h and "You hold: none · Agent: <code>WATCH (medium)</code>" in h and "Market: risk-off · no new buys" in h
    why = next(x for x in lines if x.startswith("<b>Why</b>"))
    assert why.count(". ") <= 1 and len(why) <= 260 and "Avoid for now" not in why
    assert len(visible(h)) <= tc.VISIBLE_LIMIT and len(h) <= tc.TOTAL_LIMIT and len([x for x in lines if x.strip()]) <= 12
    assert "<blockquote expandable>" in h and "Avoid for now" in h.split("<blockquote")[1]       # the original text, collapsed
    assert "Watch for a second disclosure" in h.split("<blockquote")[1]                           # the long paragraph is all in there
    assert "http" not in visible(h)
    only_telegram_tags(h)
    assert "<" not in plain and "Watch for a second disclosure" in plain and "Avoid for now" in plain and plain.startswith("▼ SOLD XPROINDIA · Kacholia")


def test_tone_markers_always_come_with_words():
    bought = deal(deal={**pv.SAMPLE_DEAL, "side": "Purchase"}, rec={**pv.SAMPLE_REC, "action": "buy"})
    assert bought["html"].startswith("<b>▲ BOUGHT XPROINDIA")
    no_deal = tc.deal_card({**pv.SAMPLE_REC, "action": "watch", "investor": ""}, deal=None)
    assert no_deal["html"].startswith("<b>● WATCH XPROINDIA</b>")
    sell = tc.deal_card({**pv.SAMPLE_REC, "action": "sell"}, deal=None)
    assert sell["html"].startswith("<b>▼ SELL XPROINDIA")
    for c in (bought, no_deal, sell):
        assert re.match(r"<b>[▲▼●] [A-Z]+ ", c["html"])                                           # a marker is never alone: a word follows
    assert "🔴" not in bought["html"] and "🟢" not in bought["html"]


def test_you_hold_and_missing_facts_are_left_out_not_invented():
    held = deal(held=150.0)
    assert "You hold: <code>150 sh</code>" in held["html"]
    bare = tc.deal_card({"ticker": "TCS", "action": "hold", "headline": "Note", "rationale": ""}, deal=None, holding_known=False)
    assert "You hold" not in bare["html"] and "deal disclosed" not in bare["html"] and "Rs" not in bare["html"]
    assert tc.deal_card({"ticker": "TCS", "action": "watch", "headline": "H" * 400, "rationale": ""}, deal=None)["html"].count("<b>Why</b>") == 1


def test_escaping_of_every_outside_value_and_no_raw_urls_in_the_visible_text():
    rec = {**pv.SAMPLE_REC, "ticker": "A&B", "investor": "Mr <script>alert(1)</script> \"Q\" O'Neil", "headline": "x <b>bold</b> & more",
           "rationale": "See https://evil.example/path?a=1&b=2 now. Second <i>sentence</i> 'quoted' \"too\". Third."}
    c = tc.deal_card(rec, deal=pv.SAMPLE_DEAL, base_url=BASE)
    h = c["html"]
    assert "<script>" not in h and "&lt;script&gt;" in h and "<b>bold</b>" not in h and "&lt;b&gt;bold&lt;/b&gt;" in h
    assert "A&amp;B" in h and "&amp; more" in h
    only_telegram_tags(h)
    news = {**pv.SAMPLE_NEWS, "headline": "<img src=x onerror=1> & \"q\" 'a'", "source": "<b>Src</b>", "takeaways": ["<u>t</u> & u"], "context": "<a href='x'>c</a>"}
    n = tc.news_card(news, BASE)
    assert "<img" not in n["html"] and "&lt;img" in n["html"] and "<a href" not in n["html"] and "&lt;a href" in n["html"]
    only_telegram_tags(n["html"])
    f = tc.filings_card([{"at": "2026-10-19 10:00:00", "symbol": "X<Y", "category": "<b>Cat</b>", "text": "has informed the Exchange about <i>x</i> & y"}], BASE)
    assert "<b>Cat" not in f["html"] and "X&lt;Y" in f["html"] and "&amp; y" in f["html"]
    only_telegram_tags(f["html"])


def test_length_limits_hold_for_enormous_inputs_and_the_visible_part_is_never_cut_for_the_quote():
    rec = {**pv.SAMPLE_REC, "rationale": "A long sentence about the company. " * 600, "headline": "H " * 400}
    c = tc.deal_card(rec, deal=pv.SAMPLE_DEAL, base_url=BASE)
    assert len(c["html"]) <= 4096 and len(c["plain"]) <= 4096 and len(visible(c["html"])) <= 900
    assert c["html"].startswith("<b>▼ SOLD XPROINDIA") and "<b>Why</b>" in visible(c["html"])
    only_telegram_tags(c["html"])
    assert c["html"].endswith("</blockquote>")
    esc_heavy = {**pv.SAMPLE_REC, "rationale": "<>&" * 3000}
    e = tc.deal_card(esc_heavy, deal=pv.SAMPLE_DEAL)
    assert len(e["html"]) <= 4096 and e["html"].endswith("</blockquote>")
    long_news = tc.news_card({**pv.SAMPLE_NEWS, "story": "Paragraph. " * 2000, "headline": "Z" * 500}, BASE)
    assert len(long_news["html"]) <= 4096 and len(visible(long_news["html"])) <= 900 and len(re.sub("<[^>]+>", "", visible(long_news["html"]).split("\n")[1])) <= 121


def test_the_first_line_is_at_most_seventy_characters():
    c = tc.deal_card({**pv.SAMPLE_REC, "ticker": "VERYLONGSYMBOLNAME", "investor": "SOMEBODY WITH AN EXTREMELY LONG INVESTOR NAME LIMITED"}, deal=pv.SAMPLE_DEAL)
    first = re.sub("<[^>]+>", "", c["html"].split("\n")[0])
    assert len(first) <= 70 and first.startswith("▼ SOLD VERYLONGSYMBOLNAME")


# ---- the buttons ----
def test_buttons_are_links_only_with_validated_urls():
    c = deal()
    row = c["button"]["inline_keyboard"]
    assert len(row) == 1 and [b["text"] for b in row[0]] == ["Open XPROINDIA", "All deals"]
    assert row[0][0]["url"] == f"{BASE}/?stock=XPROINDIA" and row[0][1]["url"] == f"{BASE}/#deals"
    for b in row[0]:
        assert set(b) == {"text", "url"}                                                         # no callback_data: nothing acts
    assert deal(base=None)["button"] is None
    assert deal(base="")["button"] is None
    n = tc.news_card({**pv.SAMPLE_NEWS, "url": "javascript:alert(1)"}, BASE)
    assert [b["text"] for b in n["button"]["inline_keyboard"][0]] == ["Open HDFCBANK"]
    assert tc.news_card({**pv.SAMPLE_NEWS, "url": "ftp://x/y"}, None)["button"] is None
    assert [b["text"] for b in tc.news_card(pv.SAMPLE_NEWS, None)["button"]["inline_keyboard"][0]] == ["Read on Moneycontrol"]
    assert tc.button_row(("a", "https://x.example/1"), ("b", "http://x.example/2"), ("c", "https://x.example/3"), ("d", "https://x.example/4"))["inline_keyboard"][0].__len__() == 3
    assert tc.button_row(("a", "not a url"), ("b", None), ("c", "javascript:x"), ("d", "https://x y")) is None
    assert tc.stock_url(BASE, "bad sym; drop") is None and tc.stock_url(BASE, "RELIANCE.NS") == f"{BASE}/?stock=RELIANCE" and tc.stock_url(None, "TCS") is None
    assert tc.stock_url(BASE, "M&M") == f"{BASE}/?stock=M%26M"


def test_dashboard_url_setting_and_the_allowed_hosts_fallback():
    class S:
        dashboard_url = ""
        allowed_hosts = ["127.0.0.1", "10.0.0.5", "box.tailnet.ts.net"]
    assert tc.dashboard_base(S()) == "https://box.tailnet.ts.net"
    S.dashboard_url = "https://my.example.org/"
    assert tc.dashboard_base(S()) == "https://my.example.org"
    S.dashboard_url, S.allowed_hosts = "", ["127.0.0.1"]
    assert tc.dashboard_base(S()) is None
    S.dashboard_url = "javascript:alert(1)"
    assert tc.dashboard_base(S()) is None
    assert _dashboard_url("https://a.b.ts.net/") == "https://a.b.ts.net" and _dashboard_url("http://192.168.1.5:8765") == "http://192.168.1.5:8765"
    assert _dashboard_url("") == "" and _dashboard_url("box.ts.net") == "" and _dashboard_url("https://x.y/path") == "" and _dashboard_url("javascript:1") == ""


def test_config_reads_dashboard_url(monkeypatch, tmp_path):
    from trading_agent.config import load_settings
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    assert load_settings().dashboard_url == ""
    monkeypatch.setenv("DASHBOARD_URL", "https://box.tailnet.ts.net/")
    assert load_settings().dashboard_url == "https://box.tailnet.ts.net"


# ---- NEWS ----
def test_the_news_card():
    n = tc.news_card(pv.SAMPLE_NEWS, BASE)
    h = n["html"]
    lines = visible(h).split("\n")
    assert lines[0] == "<b>▼ NEWS · HDFCBANK · Negative</b>" and lines[1].startswith("<b>HDFC Bank slips as RBI flags") and lines[2] == "<i>Moneycontrol · 19 Oct 09:41 IST</i>"
    assert "• RBI flagged rising stress" in h and "• HDFC Bank fell 1.8% in early trade." in h and "Analysts see a limited effect" in h.split("<blockquote")[1]
    assert "<b>For you</b> Held in your portfolio" in visible(h)
    assert len(visible(h)) <= 900
    quiet = tc.news_card({**pv.SAMPLE_NEWS, "context": "", "impact": "POSITIVE"}, BASE)
    assert "For you" not in quiet["html"] and quiet["html"].startswith("<b>▲ NEWS · HDFCBANK · Positive</b>")
    assert tc.news_card({k: v for k, v in pv.SAMPLE_NEWS.items() if k != "impact"} | {"sentiment": ""}, None)["html"].startswith("<b>● NEWS · HDFCBANK · Neutral</b>")


# ---- grouped filings ----
def test_grouped_filings_rank_by_category_and_cap_the_list():
    rows = pv.sample_filings(20)
    f = tc.filings_card(rows, BASE)
    h = f["html"]
    lines = visible(h).split("\n")
    assert lines[0] == "<b>● XPROINDIA · 20 new filings (15-19 Oct)</b>"
    top = [x for x in lines[2:5]]
    assert "Board Meeting Intimation" in top[0] and "Outcome of Board Meeting" in top[1] and "Appointment of Director" in top[2]    # board meeting, dividend outcome, appointment
    assert lines[5] == "<i>+17 more</i>" and "Newspaper" not in "".join(top) and "Trading Window" not in "".join(top)
    quote = h.split("<blockquote expandable>")[1]
    assert quote.count("\n") + 1 <= 25 and "Newspaper" in quote and len(h) <= 4096 and len(visible(h)) <= 900
    assert f["button"]["inline_keyboard"][0][0]["url"] == f"{BASE}/?stock=XPROINDIA"
    assert "&" not in re.sub(r"&(?:amp|lt|gt);", "", h)
    # the newest repeat of a duplicate ranks last, results first
    mixed = [{"at": "2026-10-19 09:00:00", "symbol": "ABC", "category": "Updates", "text": "has informed the Exchange about x"},
             {"at": "2026-10-19 10:00:00", "symbol": "ABC", "category": "Updates", "text": "has informed the Exchange about x"},
             {"at": "2026-10-18 10:00:00", "symbol": "ABC", "category": "Financial Results", "text": "has informed the Exchange about the quarterly results"},
             {"at": "2026-10-18 09:00:00", "symbol": "ABC", "category": "Pledge", "text": "pledge of shares"}]
    out = visible(tc.filings_card(mixed, None)["html"]).split("\n")
    assert "Financial Results" in out[2] and "Pledge" in out[3] and out[4].startswith("18 Oct") is False and "Updates" in out[4]
    assert tc.filing_rank("Financial Results") == 0 < tc.filing_rank("Board Meeting") < tc.filing_rank("Dividend") < tc.filing_rank("Order Received") < tc.filing_rank("Pledge") \
        < tc.filing_rank("Penalty imposed") < tc.filing_rank("SEBI order") < tc.filing_rank("Appointment of Director") < tc.filing_rank("Updates") < tc.filing_rank("Copy of Newspaper Publication") == 9
    assert tc.filing_rank("Trading Window") == 9 and tc.filing_rank("Refinery update") == 8                  # "fine" inside a word is not a fine
    many = tc.filings_card(pv.sample_filings(60), BASE)
    assert many["html"].split("<blockquote expandable>")[1].count("\n") + 1 <= 26 and "…and " in many["html"] and len(many["html"]) <= 4096


def test_filings_from_several_stocks_name_them_and_have_a_dashboard_button():
    rows = [{"at": f"2026-10-19 1{i}:00:00", "symbol": s, "category": "Board Meeting", "text": "x"} for i, s in enumerate(["INFY", "TCS", "ITC", "SBIN", "WIPRO"])]
    f = tc.filings_card(rows, BASE)
    assert f["html"].startswith("<b>● INFY, TCS, ITC +2 · 5 new filings (19 Oct)</b>") and "INFY · " in f["html"]
    assert f["button"]["inline_keyboard"][0][0] == {"text": "Open dashboard", "url": BASE}
    assert tc.filings_card(rows, None)["button"] is None
    one = tc.filings_card(rows[:1], BASE)
    assert "1 new filing (19 Oct)" in one["html"]


# ---- the brief ----
def test_the_brief_has_three_sections_and_a_collapsed_detail():
    b = pv.telegram_brief.telegram_brief(pv.sample_brief(), ["box.tailnet.ts.net"])
    h = b["html"]
    vis = visible(h)
    assert vis.startswith("<b>Evening close · Mon 19 Oct IST</b>") and len(vis) <= 900
    for name in ("<b>Market</b>", "<b>Your portfolio</b>", "<b>News and deals</b>"):
        section = vis.split(name)[1].split("<b>")[0].strip("\n")
        assert name in vis and len(section.split("\n")) <= 3, name
    assert "▼ RISK-OFF (-2)" in vis and "No new buys" in vis                                                 # the marker has its word
    assert "<blockquote expandable>" in h and "Holdings" not in vis and len(h) <= 4096
    only_telegram_tags(h)
    assert "<" not in b["plain"] and "Portfolio" in b["plain"] and "Deals:" in b["plain"]
    assert b["button"]["inline_keyboard"][0][0]["url"] == "https://box.tailnet.ts.net/"
    morning = {"summary": "", "writer": "rules", "data": {"kind": "morning", "date": "2026-10-19", "mood": {"regime": "risk_on", "score": 3, "why": []},
                                                         "buy_ideas": {"ideas": [{"symbol": "AAA"}]}, "watch": {"portfolio": {"value": 450000.0, "pl": 12000.0, "pl_pct": 2.7, "holdings": 9}, "total": 0, "items": []}}}
    m = pv.telegram_brief.telegram_brief(morning)
    assert m["html"].startswith("<b>Morning brief · Mon 19 Oct IST</b>") and "▲ RISK-ON (+3)" in m["html"] and "1 buy idea: AAA" in m["html"]
    assert "Value <code>₹4,50,000</code>" in m["html"] and m["button"] is None


# ---- the notifier: 400 on the HTML, then plain ----
class Resp:
    def __init__(self, code):
        self.status_code = code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError("bad", response=self)


class Sess:
    def __init__(self, first=200):
        self.calls, self.first = [], first

    def post(self, url, **kw):
        self.calls.append((url, kw))
        return Resp(self.first if len(self.calls) == 1 else 200)


@pytest.mark.parametrize("kind", ["deal", "news", "group", "brief"])
def test_telegram_gets_the_card_and_a_400_falls_back_to_the_plain_text_with_everything(kind):
    card = dict(pv.previews(kind, BASE)[0][1])
    sess = Sess(first=400)
    n = Notifier(session=sess, telegram_token="123:SECRET", telegram_chat_id="42")
    delivered = n.send("S", "body", telegram_html=card["html"], telegram_plain=card["plain"], telegram_button=card["button"], only=("telegram",))
    assert "telegram" in delivered and len(sess.calls) == 2
    first, second = sess.calls[0][1]["json"], sess.calls[1][1]["json"]
    assert first["parse_mode"] == "HTML" and first["text"] == card["html"] and first["reply_markup"] == card["button"] and first["disable_web_page_preview"] is True
    assert "parse_mode" not in second and second["text"] == card["plain"] and "<" not in second["text"]
    if kind == "deal":
        assert "Watch for a second disclosure" in second["text"] and "Avoid for now" in second["text"]            # the plain fallback keeps the whole reasoning
    ok = Sess()
    Notifier(session=ok, telegram_token="123:SECRET", telegram_chat_id="42").send("S", "b", telegram_html=card["html"], telegram_plain=card["plain"], only=("telegram",))
    assert len(ok.calls) == 1


def test_send_with_html_hands_the_telegram_card_to_a_notifier_that_takes_it_and_spares_one_that_does_not():
    from trading_agent.agent import send_with_html
    got: dict = {}

    class Takes:
        def send(self, subject, body, html=None, telegram_html=None, telegram_plain=None, telegram_button=None):
            got.update(subject=subject, html=html, th=telegram_html, tp=telegram_plain, tb=telegram_button)
            return ["x"]

    c = {"html": "<b>h</b>", "plain": "h", "button": {"inline_keyboard": []}}
    assert send_with_html(Takes(), "S", "B", "<p>e</p>", c) == ["x"] and got == {"subject": "S", "html": "<p>e</p>", "th": "<b>h</b>", "tp": "h", "tb": {"inline_keyboard": []}}

    class Old:
        def send(self, subject, body):
            got["old"] = (subject, body)

    send_with_html(Old(), "S", "B", "<p>e</p>", c)
    assert got["old"] == ("S", "B")
    class OnlyHtml:
        def send(self, subject, body, html=None):
            got["only"] = html
    send_with_html(OnlyHtml(), "S", "B", "<p>e</p>", c)
    assert got["only"] == "<p>e</p>"


def test_the_preview_prints_every_type_without_settings_or_network(capsys):
    from trading_agent.cli import main
    assert pv.run("all") == 0
    out = capsys.readouterr().out
    for name in ("DEAL", "NEWS", "GROUP", "BRIEF"):
        assert f"===== {name}: HTML" in out and f"===== {name}: plain fallback" in out
    assert "<blockquote expandable>" in out and "SECRET" not in out
    assert main is not None
