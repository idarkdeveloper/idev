"""Following several investors at once: settings, matching, checks, alerts, backtests, the CLI and the page."""
import argparse
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from trading_agent.backtest import format_summary, run_backtest_followed
from trading_agent.broker import LocalPaperBroker
from trading_agent.cli import _investor_names, build_parser
from trading_agent.config import MAX_INVESTORS, load_settings, parse_investors
from trading_agent.notify import Notifier
from trading_agent.quiver import DisclosedTrade, fetch_followed, filter_by_investors, followed_names
from trading_agent.runner import check
from trading_agent.state import State
from trading_agent.ui import App

from .test_agent import FakeRunner
from .test_backtest import FakePrices
from .test_ui import _get, _post, server, two_pages  # noqa: F401  (fixtures)


def _deal(ticker, investor, side="Purchase", date="2026-01-10", size="1 sh"):
    return DisclosedTrade(source="bulk", investor=investor, ticker=ticker, transaction=side,
                          transaction_date=date, report_date=date, size=size, raw={})


KACHOLIA = _deal("SENCO", "KACHOLIA ASHISH", size="100 sh")
KEDIA = _deal("TCS", "VIJAY KEDIA", size="200 sh")
BOTH = _deal("BOTH", "VIJAY KEDIA AND ASHISH KACHOLIA", size="9 sh")
OTHER = _deal("NOPE", "SOMEONE ELSE", size="1 sh")
NAMES = ["ASHISH KACHOLIA", "VIJAY KEDIA"]


# -- settings ---------------------------------------------------------------------------------------------
def test_parse_investors_trims_dedupes_and_keeps_order():
    assert parse_investors(" Ashish Kacholia , VIJAY KEDIA,ashish  kacholia,, ") == ["Ashish Kacholia", "VIJAY KEDIA"]
    assert parse_investors("A\nB\r\nC") == ["A", "B", "C"]          # one per line works too
    assert parse_investors(["A,B", "c"]) == ["A", "B", "c"]


def test_parse_investors_refuses_empty_and_too_many():
    for bad in ("", " , ,", [], None):
        with pytest.raises(ValueError, match="at least one investor"):
            parse_investors(bad)
    assert len(parse_investors(",".join(f"N{i}" for i in range(MAX_INVESTORS)))) == MAX_INVESTORS
    with pytest.raises(ValueError, match="at most 10"):
        parse_investors(",".join(f"N{i}" for i in range(MAX_INVESTORS + 1)))


@pytest.fixture
def clean_env(monkeypatch):
    for k in ("INVESTORS", "WATCH_INVESTOR", "MARKET", "BROKER", "DATA_SOURCE", "GROWW_ACCESS_TOKEN", "GROWW_API_KEY",
              "ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY"):
        monkeypatch.setenv(k, "x")      # so the undo removes anything load_settings(dotenv=...) sets later
        monkeypatch.delenv(k)
    return monkeypatch


def test_load_settings_reads_investors_list(clean_env):
    clean_env.setenv("INVESTORS", "Ashish Kacholia, Vijay Kedia ,ASHISH KACHOLIA")
    s = load_settings(dotenv=None)
    assert s.investors == ["Ashish Kacholia", "Vijay Kedia"] and s.watch_investor == "Ashish Kacholia"


def test_load_settings_falls_back_to_the_single_investor(clean_env):
    clean_env.setenv("WATCH_INVESTOR", "MUKUL AGRAWAL")
    s = load_settings(dotenv=None)
    assert s.investors == ["MUKUL AGRAWAL"] and s.watch_investor == "MUKUL AGRAWAL"
    clean_env.delenv("WATCH_INVESTOR")
    assert load_settings(dotenv=None).investors == ["ASHISH KACHOLIA"]          # the market's default
    clean_env.setenv("INVESTORS", "")                                         # blank is the same as unset
    assert load_settings(dotenv=None).investors == ["ASHISH KACHOLIA"]


def test_load_settings_refuses_an_empty_or_oversized_list(clean_env):
    clean_env.setenv("INVESTORS", " , ")
    with pytest.raises(SystemExit, match="INVESTORS"):
        load_settings(dotenv=None)
    clean_env.setenv("INVESTORS", ",".join(f"N{i}" for i in range(11)))
    with pytest.raises(SystemExit, match="at most 10"):
        load_settings(dotenv=None)


def test_investors_follow_watch_investor_until_a_list_is_set(settings):
    assert settings.investors == ["Nancy Pelosi"]
    settings.watch_investor = "Dan Crenshaw"
    assert settings.investors == ["Dan Crenshaw"]
    settings.investors = ["A", "B"]
    assert settings.investors == ["A", "B"] and settings.watch_investor == "A"


def test_env_example_and_readme_document_investors():
    root = Path(__file__).resolve().parents[1]
    assert "INVESTORS=" in (root / ".env.example").read_text(encoding="utf-8")
    assert "INVESTORS" in (root / "README.md").read_text(encoding="utf-8")


# -- matching ---------------------------------------------------------------------------------------------
def test_each_deal_is_attributed_to_the_names_it_matches():
    assert followed_names(KACHOLIA.investor, NAMES) == ["ASHISH KACHOLIA"]       # word order does not matter
    assert followed_names(KEDIA.investor, NAMES) == ["VIJAY KEDIA"]
    assert followed_names(BOTH.investor, NAMES) == NAMES                          # a deal matching two: both
    assert followed_names(OTHER.investor, NAMES) == []


def test_filter_by_investors_shows_a_shared_deal_once():
    rows = filter_by_investors([KACHOLIA, KEDIA, BOTH, OTHER], NAMES)
    assert sorted(t.ticker for t in rows) == ["BOTH", "SENCO", "TCS"]


def test_fetch_followed_merges_clients_that_only_know_one_name():
    class OneName:
        def trades_for_investor(self, investor, source, **kw):
            return [t for t in (KACHOLIA, KEDIA, BOTH, OTHER) if followed_names(t.investor, [investor])]

    got = fetch_followed(OneName(), NAMES, "deals")
    assert sorted(t.ticker for t in got) == ["BOTH", "SENCO", "TCS"]


def test_nse_client_fetches_once_for_all_names():
    from trading_agent.nse import NSEClient

    calls = []

    class C(NSEClient):
        def _rows(self, source, days):
            calls.append((source, days))
            return [KACHOLIA, KEDIA, BOTH, OTHER]

    c = object.__new__(C)
    got = c.trades_for_investors(NAMES, "deals", days=30)
    assert sorted(t.ticker for t in got) == ["BOTH", "SENCO", "TCS"] and calls == [("deals", 30)]


# -- the check --------------------------------------------------------------------------------------------
class OldFake:
    """A data client that only has the one-name call."""
    def __init__(self):
        self.asked = []

    def trades_for_investor(self, investor, source, **kw):
        self.asked.append(investor)
        return [t for t in (KACHOLIA, KEDIA, BOTH, OTHER) if followed_names(t.investor, [investor])]

    def history_for_ticker(self, investor, ticker, days=365):
        self.asked.append(("history", investor, ticker))
        return [_deal(ticker, "KACHOLIA ASHISH" if "KACHOLIA" in investor else "VIJAY KEDIA", date="2025-12-01")]


def _two(settings):
    settings.investors = list(NAMES)
    settings.market = "in"
    return LocalPaperBroker(settings.state_dir / "pb.json", starting_cash=100_000, price_fn=lambda s: 100.0,
                            whole_shares=True)


def test_dry_run_reads_old_state_and_counts_a_shared_deal_once(settings):
    broker = _two(settings)
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    # an old state.json: one deal already seen, written before this feature existed
    (settings.state_dir / "state.json").write_text(json.dumps(
        {"seen": {KACHOLIA.key: {"at": "2026-01-01T00:00:00+00:00", "summary": KACHOLIA.summary()}},
         "runs": [], "recommendations": []}))
    data = OldFake()
    result = check(settings, dry_run=True, data=data, broker=broker, notifier=Notifier())
    assert sorted(t.ticker for t in result.new_trades) == ["BOTH", "TCS"]       # SENCO was seen, BOTH is one deal
    assert result.skipped and result.investors == NAMES and result.investor == "ASHISH KACHOLIA, VIJAY KEDIA"
    assert data.asked == NAMES and result.to_dict()["investors"] == NAMES


def test_a_deal_seen_once_is_seen_for_every_investor(settings):
    broker = _two(settings)
    data = OldFake()
    factory = lambda **kw: FakeRunner([], **kw)  # noqa: E731
    first = check(settings, data=data, broker=broker, notifier=Notifier(), runner_factory=factory)
    assert len(first.new_trades) == 3
    second = check(settings, data=data, broker=broker, notifier=Notifier(), runner_factory=factory)
    assert second.skipped and second.new_trades == []
    assert State(settings.state_dir / "state.json").seen_count == 3


def test_one_run_covers_both_investors_and_the_recommendation_names_its_investor(settings):
    broker = _two(settings)
    notifier = Notifier()
    holder = {}
    script = [
        ("get_investor_trade_history", {"ticker": "TCS"}),
        ("get_investor_trade_history", {"ticker": "SENCO", "investor": "ASHISH KACHOLIA"}),
        ("send_recommendation", {"action": "buy", "ticker": "TCS", "headline": "Kedia added", "rationale": "r",
                                 "confidence": "high"}),                                     # investor inferred
        ("send_recommendation", {"action": "watch", "ticker": "SENCO", "headline": "Kacholia added", "rationale": "r",
                                 "confidence": "low", "investor": "kacholia ashish"}),      # named, matched to the list
        ("send_recommendation", {"action": "hold", "ticker": "BOTH", "headline": "both", "rationale": "r",
                                 "confidence": "low"}),
    ]

    def factory(**kw):
        holder["r"] = FakeRunner(script, **kw)
        return holder["r"]

    data = OldFake()
    result = check(settings, data=data, broker=broker, notifier=notifier, runner_factory=factory)
    msg = holder["r"].kwargs["messages"][0]["content"]
    assert "Followed investors (2): ASHISH KACHOLIA, VIJAY KEDIA" in msg
    for t in ("SENCO", "TCS", "BOTH"):
        assert f'"ticker": "{t}"' in msg
    assert '"followed_investor": "VIJAY KEDIA"' in msg and '"followed_investor": "ASHISH KACHOLIA"' in msg
    assert '"followed_investor": "ASHISH KACHOLIA, VIJAY KEDIA"' in msg           # the shared deal names both
    assert "NOPE" not in msg
    assert [r["investor"] for r in result.recommendations] == ["VIJAY KEDIA", "ASHISH KACHOLIA",
                                                                "ASHISH KACHOLIA, VIJAY KEDIA"]
    recorded = State(settings.state_dir / "state.json").data["recommendations"]
    assert [r["investor"] for r in recorded][0] == "VIJAY KEDIA"
    # the history tool: all followed by default, one when asked
    log = holder["r"].tool_log
    assert log[0][1]["investors"] == NAMES and {t["followed_investor"] for t in log[0][1]["trades"]} <= set(NAMES)
    assert log[1][1]["investors"] == ["ASHISH KACHOLIA"]
    assert ("history", "ASHISH KACHOLIA", "TCS") in data.asked and ("history", "VIJAY KEDIA", "TCS") in data.asked
    assert ("history", "VIJAY KEDIA", "SENCO") not in data.asked
    # alerts name the investor in subject and body
    assert notifier.sent[0]["subject"] == "[DEAL] VIJAY KEDIA: BUY TCS - Kedia added"
    assert "Investor: VIJAY KEDIA" in notifier.sent[0]["body"]
    assert notifier.sent[1]["subject"].startswith("[DEAL] ASHISH KACHOLIA: WATCH SENCO")


def test_single_investor_prompt_keeps_its_wording(settings):
    broker = _two(settings)
    settings.investors = ["ASHISH KACHOLIA"]
    holder = {}

    def factory(**kw):
        holder["r"] = FakeRunner([("send_recommendation", {"action": "buy", "ticker": "SENCO", "headline": "h",
                                                           "rationale": "r", "confidence": "high"})], **kw)
        return holder["r"]

    result = check(settings, data=OldFake(), broker=broker, notifier=Notifier(), runner_factory=factory)
    assert "Watched investor: ASHISH KACHOLIA" in holder["r"].kwargs["messages"][0]["content"]
    assert result.recommendations[0]["investor"] == "ASHISH KACHOLIA"


def test_scorecard_rows_carry_the_investor():
    from trading_agent.scorecard import score_recommendations
    rows = score_recommendations([{"ticker": "UP", "action": "buy", "at": "2026-01-10T00:00:00+00:00",
                                   "investor": "VIJAY KEDIA"}], FakePrices(), horizons=(5,))["rows"]
    assert rows[0]["investor"] == "VIJAY KEDIA"


# -- backtest ---------------------------------------------------------------------------------------------
def test_all_followed_backtest_gives_per_investor_rows_and_the_pooled_result():
    deals = [_deal("UP", "KACHOLIA ASHISH"), _deal("UP", "VIJAY KEDIA", date="2026-01-12"),
             _deal("UP", "VIJAY KEDIA AND ASHISH KACHOLIA", date="2026-01-14")]
    r = run_backtest_followed(NAMES, deals, FakePrices(), horizons=(5,), cost_bps=0)
    s = r.summary()
    assert s["investor"] == "All followed" and s["deals"] == 3                    # pooled: every deal once
    assert set(s["investors"]) == set(NAMES)
    assert s["investors"]["ASHISH KACHOLIA"]["deals"] == 2 and s["investors"]["VIJAY KEDIA"]["deals"] == 2
    assert s["by_side"]["Purchase"]["5"]["n"] == 3
    assert s["investors"]["VIJAY KEDIA"]["by_side"]["Purchase"]["5"]["n"] == 2
    text = format_summary(s)
    assert "All followed" in text and "ASHISH KACHOLIA: 2/2 deals priced" in text
    assert r.to_dict()["summary"]["investors"]["VIJAY KEDIA"]["deals"] == 2


def test_backtest_api_all_followed_one_name_and_free_text(server):
    base, app = server
    app.settings.investors = ["Nancy Pelosi", "Dan Crenshaw"]
    assert app.backtest_names("") == ["Nancy Pelosi", "Dan Crenshaw"]
    assert app.backtest_names("All followed") == ["Nancy Pelosi", "Dan Crenshaw"]
    assert app.backtest_names("Someone Unfollowed") == ["Someone Unfollowed"]    # free text still allowed
    assert app.backtest_names("A, B") == ["A", "B"]
    status, job = _post(base + "/api/backtest", {"investor": "", "days": 365, "horizons": "5,20", "cost_bps": 50})
    assert status == 202
    for _ in range(100):
        _, st = _get(base + "/api/state")
        if not st["busy"]:
            break
        time.sleep(0.05)
    assert st["jobs"][-1]["ok"] is True, st["jobs"][-1]
    sm = st["backtest"]["summary"]
    assert sm["investor"] == "All followed" and set(sm["investors"]) == {"Nancy Pelosi", "Dan Crenshaw"}
    assert sm["deals"] == 3 and sm["investors"]["Nancy Pelosi"]["deals"] == 3 and sm["investors"]["Dan Crenshaw"]["deals"] == 0


# -- the dashboard API ------------------------------------------------------------------------------------
def test_deals_api_names_who_and_shows_a_shared_deal_once(server):
    base, app = server
    app.settings.investors = ["Nancy Pelosi", "Pelosi"]          # both match every Pelosi deal
    _, st = _get(base + "/api/state")
    assert st["settings"]["investors"] == ["Nancy Pelosi", "Pelosi"] and st["settings"]["watch_investor"] == "Nancy Pelosi"
    assert len(st["deals"]) == 3 and all(d["who"] == ["Nancy Pelosi", "Pelosi"] for d in st["deals"])
    app.settings.investors = ["Nancy Pelosi"]
    assert _get(base + "/api/state")[1]["deals"][0]["who"] == ["Nancy Pelosi"]


def test_live_fetch_uses_every_followed_name(settings):
    data = OldFake()
    settings.investors = list(NAMES)
    app = App(settings, data=data, dotenv=None)
    assert sorted(t.ticker for t in app.deals()) == ["BOTH", "SENCO", "TCS"] and data.asked == NAMES


def test_settings_post_writes_investors_to_env_on_live(server):
    base, app = server
    status, j = _post(base + "/api/settings", {"watch_investors": "Ashish Kacholia,vijay kedia,ASHISH KACHOLIA"})
    assert status == 200 and j["applied"]["INVESTORS"] == "Ashish Kacholia,vijay kedia"
    assert "INVESTORS=Ashish Kacholia,vijay kedia" in (app.settings.state_dir / ".env").read_text()
    assert app.settings.investors == ["Ashish Kacholia", "vijay kedia"]
    _, st = _get(base + "/api/state")
    assert st["settings"]["investors"] == ["Ashish Kacholia", "vijay kedia"]
    status, j = _post(base + "/api/settings", {"watch_investors": " , "})
    assert status == 400 and "at least one investor" in j["error"]
    assert app.settings.investors == ["Ashish Kacholia", "vijay kedia"]
    status, j = _post(base + "/api/settings", {"watch_investors": ",".join(f"N{i}" for i in range(11))})
    assert status == 400 and "at most 10" in j["error"]
    # the older one-name form still works and replaces the list
    status, j = _post(base + "/api/settings", {"watch_investor": "Dan Crenshaw"})
    assert status == 200 and app.settings.investors == ["Dan Crenshaw"]
    assert "INVESTORS=Dan Crenshaw" in (app.settings.state_dir / ".env").read_text()


def test_demo_page_refuses_to_change_the_followed_list(two_pages):
    base, app, fake, settings = two_pages
    before = list(settings.investors)
    status, j = _post(base + "/demo/api/settings", {"watch_investors": "A\nB"})
    assert status == 403 and "Live page" in j["error"] and settings.investors == before


def test_demo_child_follows_the_same_list_without_sharing_it(two_pages):
    base, app, fake, settings = two_pages
    app.update_settings({"watch_investors": "A,B"})
    child = app.demo
    assert child.settings.investors == ["A", "B"] and child.settings.watch_investors is not settings.watch_investors


# -- the command line -------------------------------------------------------------------------------------
def test_cli_investor_flags_take_a_comma_list_or_repeat():
    p = build_parser()
    ns = p.parse_args(["check", "--investor", "A B,C", "--investor", "d", "--investor", "c"])
    assert _investor_names(ns) == ["A B", "C", "d"]
    assert _investor_names(p.parse_args(["backtest", "--investor", "X,Y"])) == ["X", "Y"]
    assert _investor_names(p.parse_args(["check"])) is None                       # default: all followed
    with pytest.raises(SystemExit, match="--investor"):
        _investor_names(p.parse_args(["backtest", "--investor", " , "]))
    with pytest.raises(SystemExit, match="at most 10"):
        _investor_names(argparse.Namespace(investor=[",".join(f"N{i}" for i in range(11))]))


def test_cli_demo_inputs_follow_every_name(settings):
    from trading_agent import cli
    settings.market = "in"
    settings.investors = ["ASHISH KACHOLIA"]
    one, _ = cli._demo_inputs(settings)
    settings.investors = ["ASHISH KACHOLIA", "RARE ENTERPRISES"]
    both, _ = cli._demo_inputs(settings)
    assert one and len(both) >= len(one)
    assert all(followed_names(t.investor, settings.investors) for t in both)


# -- the page ---------------------------------------------------------------------------------------------
NODE = shutil.which("node")
HARNESS = Path(__file__).resolve().parent / "ui_mode_harness.js"


def _page(mode, arg=""):
    cmd = [NODE, str(HARNESS), mode] + ([arg] if arg else [])
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr + r.stdout
    return json.loads(r.stdout)


@pytest.mark.skipif(NODE is None, reason="node not installed")
@pytest.mark.parametrize("mode", ["live", "demo"])
def test_heading_for_one_investor_and_for_several(mode):
    one = _page(mode)
    assert one["heading"] == "Ashish Kacholia" and one["investor_list_hidden"] is True and one["filter_hidden"] is True
    many = _page(mode, "multi")
    assert many["heading"] == "3 investors" and many["investor_list_hidden"] is False
    assert many["investor_list"] == "ASHISH KACHOLIA · VIJAY KEDIA · DOLLY KHANNA"
    # the counts are across all followed investors, whatever the filter shows
    assert many["summary"].startswith("2 new disclosures") and many["summary"].endswith("3 deals in window")
    assert many["newcount"] == "2 new"


@pytest.mark.skipif(NODE is None, reason="node not installed")
@pytest.mark.parametrize("mode", ["live", "demo"])
def test_filter_toggles_hide_and_show_rows_and_persist(mode):
    out = _page(mode, "multi")
    assert out["filter_hidden"] is False
    assert out["deals_all"] == ["SENCO", "TCS", "BOTH"]
    assert 'data-deal-filter="" aria-pressed="true"' in out["filter_all"]
    assert 'data-deal-filter="VIJAY KEDIA" aria-pressed="false"' in out["filter_all"]
    assert out["deals_kedia"] == ["TCS", "BOTH"]                                  # a deal matching two shows under either
    assert 'data-deal-filter="VIJAY KEDIA" aria-pressed="true"' in out["filter_kedia"]
    assert 'data-deal-filter="" aria-pressed="false"' in out["filter_kedia"]
    assert json.loads(out["stored_after_kedia"]) == ["VIJAY KEDIA"]
    assert out["deals_two"] == ["SENCO", "TCS", "BOTH"]
    assert out["deals_back"] == ["SENCO", "TCS", "BOTH"] and json.loads(out["stored_after_all"]) == []
    # a saved choice is applied on load
    saved = _page(mode, "multi-saved")
    assert saved["deals_all"] == ["TCS", "BOTH"] and 'data-deal-filter="VIJAY KEDIA" aria-pressed="true"' in saved["filter_all"]
    # the Who column names the followed investor, the shared deal names both
    assert "VIJAY KEDIA, DOLLY KHANNA" in out["deals_html"] and ">ASHISH KACHOLIA<" in out["deals_html"]


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_backtest_select_lists_all_followed_and_each_name():
    out = _page("live", "multi")
    assert out["bt_options"].startswith('<option value="">All followed</option>')
    for n in ("ASHISH KACHOLIA", "VIJAY KEDIA", "DOLLY KHANNA"):
        assert f'<option value="{n}">{n}</option>' in out["bt_options"]


def test_page_has_the_investor_controls():
    html = (Path(__file__).resolve().parents[1] / "trading_agent" / "ui" / "index.html").read_text(encoding="utf-8")
    assert 'id="f-investors"' in html and 'name="watch_investors"' in html and 'id="deal-filter"' in html
    assert 'id="bt-other"' in html and '<select id="bt-investor"' in html
