"""Fix round 1 for following several investors: workflow, .env injection, fetch counts, demo backtests, alert text."""
import time
from pathlib import Path

import pytest

from trading_agent.cli import build_parser
from trading_agent.config import load_settings
from trading_agent.notify import Notifier, clean_text
from trading_agent.runner import check
from trading_agent.ui import App, EDITABLE_ENV_KEYS, _write_env

from .test_agent import FakeRunner
from .test_followed import KACHOLIA, KEDIA, NAMES, _deal, _two, clean_env  # noqa: F401  (fixture)
from .test_ui import _get, _post, server  # noqa: F401  (fixture)

ROOT = Path(__file__).resolve().parents[1]
NASTY = ("Z\nGROWW_LIVE_ORDERS=true", "Z\rGROWW_LIVE_ORDERS=true", "Z GROWW_LIVE_ORDERS=true",
         "Z\x85X", "Z\x0bX", "Z\x00X")


def test_routine_workflow_passes_investors():
    yml = (ROOT / ".github" / "workflows" / "routine.yml").read_text(encoding="utf-8")
    assert "INVESTORS: ${{ vars.INVESTORS }}" in yml and "WATCH_INVESTOR:" in yml


def _env_app(settings):
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    env = settings.state_dir / ".env"
    env.write_bytes("# keep\nINVESTORS=Old Name\n".encode("utf-8"))
    return App(settings, dotenv=env), env


@pytest.mark.parametrize("key", ["watch_investor", "watch_investors", "watch_source", "notify_email_to",
                                 "notify_webhook_url"])
def test_line_breaks_in_any_form_string_are_refused_and_nothing_changes(settings, key):
    app, env = _env_app(settings)
    before_bytes, live, trade, inv = env.read_bytes(), settings.groww_live_orders, settings.auto_trade, settings.investors
    src, email = settings.watch_source, settings.notify_email_to
    for bad in NASTY:
        with pytest.raises(ValueError):
            app.update_settings({key: bad, "auto_trade": True, "paper_starting_cash": 1234})
        assert env.read_bytes() == before_bytes
        assert settings.groww_live_orders is live and settings.auto_trade is trade
        assert settings.investors == inv and settings.watch_source == src and settings.notify_email_to == email
        assert settings.paper_starting_cash != 1234


def test_a_bad_value_after_a_good_one_leaves_memory_and_env_alone(settings):
    app, env = _env_app(settings)
    before = env.read_bytes()
    with pytest.raises(ValueError):
        app.update_settings({"watch_investors": "A,B", "notify_webhook_url": "x\nAUTO_TRADE=true"})
    assert env.read_bytes() == before and settings.investors == ["Nancy Pelosi"]


def test_nothing_on_the_page_can_write_live_orders(settings):
    assert "GROWW_LIVE_ORDERS" not in EDITABLE_ENV_KEYS.values()
    app, env = _env_app(settings)
    app.update_settings({"groww_live_orders": True, "GROWW_LIVE_ORDERS": "true"})
    assert settings.groww_live_orders is False and b"GROWW_LIVE_ORDERS" not in env.read_bytes()


def test_write_env_refuses_unsafe_values_directly(tmp_path):
    p = tmp_path / ".env"
    p.write_text("A=1\n", encoding="utf-8")
    for bad in NASTY:
        with pytest.raises(ValueError, match="one line"):
            _write_env(p, {"X": bad})
    assert p.read_text(encoding="utf-8") == "A=1\n"


def test_legacy_watch_investor_goes_through_parse_investors(settings):
    app, env = _env_app(settings)
    app.update_settings({"watch_investor": " Dan  Crenshaw "})
    assert settings.investors == ["Dan Crenshaw"]
    with pytest.raises(ValueError, match="at least one investor"):
        app.update_settings({"watch_investor": "  "})
    app.update_settings({"watch_investor": "A, B"})
    assert settings.investors == ["A", "B"]


def test_non_cp1252_names_round_trip_through_env_and_back(settings, clean_env):
    app, env = _env_app(settings)
    app.update_settings({"watch_investors": "ŞAH ALI,VIJAY KEDIA"})
    assert "INVESTORS=ŞAH ALI,VIJAY KEDIA" in env.read_text(encoding="utf-8")
    s = load_settings(dotenv=env)
    assert s.investors == ["ŞAH ALI", "VIJAY KEDIA"]


def test_history_tool_fetches_once_for_all_names(settings):
    broker = _two(settings)
    calls = []

    class Many:
        def trades_for_investors(self, investors, source, **kw):
            return [KACHOLIA, KEDIA]

        def history_for_ticker_many(self, investors, ticker):
            calls.append(list(investors))
            return [_deal(ticker, "KACHOLIA ASHISH", date="2025-12-01"), _deal(ticker, "VIJAY KEDIA", date="2025-11-01")]

        def history_for_ticker(self, investor, ticker):
            raise AssertionError("per-name fetch used")

    holder = {}

    def factory(**kw):
        holder["r"] = FakeRunner([("get_investor_trade_history", {"ticker": "TCS"})], **kw)
        return holder["r"]

    check(settings, data=Many(), broker=broker, notifier=Notifier(), runner_factory=factory)
    assert calls == [NAMES]
    assert len(__import__("trading_agent.untrusted", fromlist=["x"]).unwrap_json(holder["r"].tool_log[0][1]["trades"])) == 2


def test_nse_history_many_is_one_fetch():
    from trading_agent.nse import NSEClient
    n = []

    class C(NSEClient):
        def historical_deals(self, days, kind):
            n.append(kind)
            return [_deal("TCS", "KACHOLIA ASHISH"), _deal("TCS", "VIJAY KEDIA", size="2"), _deal("OTHER", "VIJAY KEDIA")]

    got = object.__new__(C).history_for_ticker_many(NAMES, "tcs")
    assert sorted(t.investor for t in got) == ["KACHOLIA ASHISH", "VIJAY KEDIA"] and n == ["bulk", "block"]


def _run_backtest(base, body):
    _post(base + "/api/backtest", {**body, "days": 365, "horizons": "5", "cost_bps": 50})
    for _ in range(100):
        _, st = _get(base + "/api/state")
        if not st["busy"]:
            break
        time.sleep(0.05)
    return st["backtest"]["summary"]


def test_demo_backtest_uses_only_the_chosen_names(server):
    base, app = server
    app.settings.investors = ["Nancy Pelosi", "Dan Crenshaw"]
    sm = _run_backtest(base, {"investor": "Dan Crenshaw"})
    assert sm["investor"] == "Dan Crenshaw" and sm["deals"] == 0
    sm = _run_backtest(base, {"investor": "Nancy Pelosi"})
    assert sm["deals"] == 3


def test_cli_demo_backtest_filters_by_chosen_names(settings, monkeypatch, capsys):
    from trading_agent import cli
    settings.market = "in"
    settings.investors = ["ASHISH KACHOLIA", "RARE ENTERPRISES"]
    monkeypatch.setattr(cli, "_settings", lambda args: settings)
    args = build_parser().parse_args(["backtest", "--demo", "--investor", "ASHISH KACHOLIA"])
    cli.cmd_backtest(args)
    out = capsys.readouterr().out
    assert "disclosed deals by ASHISH KACHOLIA" in out
    only = int(out.split(" disclosed deals")[0].strip().splitlines()[-1])
    args = build_parser().parse_args(["backtest", "--demo"])
    cli.cmd_backtest(args)
    both = int(capsys.readouterr().out.split(" disclosed deals")[0].strip().splitlines()[-1])
    assert both >= only


def test_backtest_label_is_all_followed_only_for_the_whole_list(server):
    base, app = server
    app.settings.investors = ["Nancy Pelosi", "Dan Crenshaw"]
    assert _run_backtest(base, {"investor": ""})["investor"] == "All followed"
    assert _run_backtest(base, {"investor": "Nancy Pelosi,Someone"})["investor"] == "Nancy Pelosi, Someone"


def test_recommendation_investor_is_always_a_followed_name(settings):
    from trading_agent.agent import AgentContext, RunResult, _rec_investor
    settings.investors = list(NAMES)
    ctx = AgentContext(settings=settings, broker=None, data=None, notifier=Notifier(), state=None,
                       result=RunResult("x", [KEDIA]))
    assert _rec_investor(ctx, "vijay kedia, ashish kacholia", "TCS") == "ASHISH KACHOLIA, VIJAY KEDIA"
    assert _rec_investor(ctx, "<!channel> pwn", "TCS") == "VIJAY KEDIA"          # not followed: inferred from the trades
    assert _rec_investor(ctx, "<!channel> pwn", "ZZZ") == ""
    assert "pwn" not in _rec_investor(ctx, "pwn\nINJECT", "TCS")


def test_alert_text_is_neutralised_and_capped(settings):
    t = clean_text("hey <!channel> <@U1> @everyone @here\nline2 " + "x" * 500, 100)
    assert "\n" not in t and "<!" not in t and "<@" not in t and "@everyone" not in t and "@here" not in t
    assert len(t) <= 100
    broker = _two(settings)
    settings.investors = ["Evil <!channel>"]
    notifier = Notifier()
    script = [("send_recommendation", {"action": "buy", "ticker": "TCS", "headline": "@everyone\nbuy <@U9> " + "y" * 400,
                                       "rationale": "r", "confidence": "high"})]
    check(settings, trades=[_deal("TCS", "Evil <!channel>")], broker=broker, notifier=notifier,
          runner_factory=lambda **kw: FakeRunner(script, **kw))
    subj = notifier.sent[0]["subject"]
    assert "\n" not in subj and "<!" not in subj and "<@" not in subj and "@everyone" not in subj and len(subj) < 400


def test_per_investor_backtest_empty_cell_has_no_down_class():
    html = (ROOT / "trading_agent" / "ui" / "index.html").read_text(encoding="utf-8")
    assert 'buy && buy.mean_excess>=0?"up":"down"' not in html and '${!buy ? "" :' in html
