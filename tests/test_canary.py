"""The pre-market canary failsafe: a failed (or missing) integration check pauses automated BUYS, never sells.
Fakes only: no network, no orders, no real keys."""
import dataclasses
import json
from datetime import date

import pytest

from trading_agent import integration
from trading_agent.groww import GrowwBroker
from trading_agent.integration import buy_block, mark_overdue, premarket_line, run_and_record

from tests.test_integration import (FakeBSE, FakeGrowwReads, FakeNSE, FakePrices, FakeSession, Holidays, MON, Notifier,
                                    at)  # noqa: F401


@pytest.fixture
def s(settings):
    return dataclasses.replace(settings, market="in", resend_api_key="re_fake", notify_email_to="me@example.com")


def run(s, notifier, now=MON, nse=None, **kw):
    return run_and_record(s, notifier=notifier, now=now, holidays=Holidays(), session=FakeSession(), bse=FakeBSE(),
                          prices=FakePrices(), nse=nse or FakeNSE(), groww=lambda: FakeGrowwReads(), timeout=5, **kw)


def test_a_failed_run_pauses_buys_and_a_passing_run_resumes_them(s):
    n = Notifier()
    assert buy_block(s) is None
    run(s, n, nse=FakeNSE(deals_error="NSE changed"))
    assert buy_block(s) == "pre-market check failed: NSE deals"
    assert len(n.sent) == 1 and "new automated buys are paused" in n.sent[0][1]
    run(s, n)                                                   # the CLI / button / next morning, now passing
    assert buy_block(s) is None and not (s.state_dir / "canary_failed.json").exists()


def test_the_failsafe_is_off_with_the_switch_and_for_the_us_market(s):
    run(s, Notifier(), nse=FakeNSE(deals_error="x"))
    s.integration_check = False
    assert buy_block(s) is None
    s.integration_check, s.market = True, "us"
    assert buy_block(s) is None


def test_no_run_by_0910_on_a_trading_day_sets_the_flag_and_alerts_once(s):
    n = Notifier()
    h = Holidays({date(2026, 10, 20)})
    assert mark_overdue(s, n, at(2026, 10, 12, 9, 9), h) is False and buy_block(s) is None
    assert mark_overdue(s, n, at(2026, 10, 12, 9, 10), h) is True
    assert buy_block(s) == "pre-market check failed: did not run by 09:10"
    assert mark_overdue(s, n, at(2026, 10, 12, 9, 20), h) is False and len(n.sent) == 1
    run(s, n, now=at(2026, 10, 12, 9, 30))                       # a pass clears it
    assert buy_block(s) is None


def test_overdue_does_not_fire_on_holidays_weekends_or_after_a_run_today(tmp_path, s):
    n = Notifier()
    h = Holidays({date(2026, 10, 20)})
    assert not mark_overdue(s, n, at(2026, 10, 20, 10, 0), h) and not mark_overdue(s, n, at(2026, 10, 17, 10, 0), h)
    run(s, n, now=at(2026, 10, 12, 8, 30))
    assert not mark_overdue(s, n, at(2026, 10, 12, 10, 0), h) and buy_block(s) is None


def test_the_scheduler_ticks_the_overdue_check(s):
    n = Notifier()
    sch = integration.make_scheduler(s, n, Holidays())
    sch.threaded = False
    sch.run_fn = lambda: None                                    # the 08:30 run never produced a result
    out = sch.tick(at(2026, 10, 12, 9, 15))
    assert out.get("overdue") is True and buy_block(s)


class Boom:
    def request(self, *a, **k):
        raise AssertionError("no network before the gate")


def test_a_live_groww_buy_is_refused_by_the_gate_but_a_sell_is_not(s):
    asked = []
    g = GrowwBroker("tok", live_orders=True, session=Boom(), allowed_ip=None, buy_gate=lambda: asked.append(1) or "pre-market check failed: x")
    with pytest.raises(PermissionError, match="pre-market check failed: x"):
        g.submit_order("ITC", "buy", qty=1)
    assert asked == [1]
    asked.clear()
    with pytest.raises(AssertionError):                          # a sell goes on to touch the network (the Boom session)
        g.submit_order("ITC", "sell", qty=1)
    assert asked == []                                           # the gate was never consulted


def test_make_groww_wires_the_gate(s, monkeypatch):
    from trading_agent import runner
    s.groww_access_token = "tok"
    g = runner.make_groww(s)
    assert g.buy_gate() is None
    integration.set_flag(s.state_dir, ["NSE deals"], MON)
    assert g.buy_gate() == "pre-market check failed: NSE deals"


def test_agent_auto_buys_are_refused_but_the_message_names_the_steps(s, sample_rows):
    from tests.test_agent import FakeRunner, _make
    from trading_agent.runner import check
    trades, broker, notifier = _make(s, sample_rows, auto_trade=True)
    integration.set_flag(s.state_dir, ["Yahoo prices", "NSE bhavcopy"], MON)
    script = [("send_recommendation", {"action": "buy", "ticker": "NVDA", "headline": "h", "rationale": "r",
                                       "confidence": "high", "suggested_notional_usd": 5000}),
              ("place_paper_order", {"symbol": "NVDA", "side": "buy", "notional_usd": 5000}),
              ("place_paper_order", {"symbol": "NVDA", "side": "sell", "notional_usd": 5000})]
    holder = {}

    def factory(**kw):
        holder["r"] = FakeRunner(script, **kw)
        return holder["r"]

    result = check(s, trades=trades, broker=broker, notifier=notifier, runner_factory=factory)
    log = holder["r"].tool_log
    assert log[1][1]["error"] == "pre-market check failed: Yahoo prices, NSE bhavcopy" and result.orders == []
    assert "pre-market" not in log[2][1].get("error", "")        # the sell is judged on its own merits


def test_morning_line_and_the_summary_check(s):
    assert premarket_line(s, MON) is None                        # nothing yet today
    run(s, Notifier())
    assert premarket_line(s, MON) == "Pre-market check: ok 9/9"
    run(s, Notifier(), nse=FakeNSE(deals_error="x"))
    assert premarket_line(s, MON) == "Pre-market check: FAILED: NSE deals. New buys are paused."
    s.integration_check = False
    assert premarket_line(s, MON) is None
    # it reaches the email's gauge block and the summary writer's facts and allowlist
    from trading_agent import digest_render, digest_writer
    g = {"gauges": [], "warnings": [], "warning_texts": [], "skipped": 0, "note": "n",
         "premarket_line": "Pre-market check: ok 9/9"}
    assert "Pre-market check: ok 9/9" in json.dumps(digest_render._gauge_block(g))
    facts = digest_writer.summary_facts("morning", {"kind": "morning", "date": "2026-10-12", "gauges": g})
    assert facts["gauges"]["premarket_line"] == "Pre-market check: ok 9/9"
    ok, why = digest_writer.validate_summary("The pre-market check passed 9/9 checks.", facts, set())
    assert ok, why


def test_the_real_groww_client_reads_cash_from_the_margin_call_only():
    from tests.conftest import FakeResponse
    calls = []

    class Sess:
        def request(self, method, url, **kw):
            calls.append((method, url.rsplit("/", 3)[-3:]))
            return FakeResponse({"payload": MARGIN[0]})

    MARGIN = [{"clear_cash": 2500.5}]
    g = GrowwBroker("tok", session=Sess())
    assert g.available_cash() == 2500.5 and calls == [("GET", ["margins", "detail", "user"])]
    MARGIN[0] = {"nothing": 1}
    with pytest.raises(ValueError, match="none of"):
        g.available_cash()
