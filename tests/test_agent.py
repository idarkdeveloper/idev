"""End-to-end run with a scripted fake Claude runner (no network)."""
import json
from types import SimpleNamespace

from trading_agent.broker import LocalPaperBroker
from trading_agent.notify import Notifier
from trading_agent.quiver import _norm_congress, filter_by_investor
from trading_agent.runner import check
from trading_agent.state import State


class FakeRunner:
    """Mimics BetaToolRunner: calls tools in a script, then ends the turn."""

    def __init__(self, script, **kwargs):
        self.kwargs = kwargs
        self.tools = {t.name: t for t in kwargs["tools"]}
        self.script = script
        self.tool_log = []

    def __iter__(self):
        for name, inputs in self.script:
            if name not in self.tools:
                self.tool_log.append((name, {"error": "tool not available"}))
                continue
            out = self.tools[name].call(inputs)
            self.tool_log.append((name, json.loads(out)))
        yield SimpleNamespace(
            model="claude-opus-5-5", stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text="Done: sent recommendations.")],
        )


def _make(settings, sample_rows, auto_trade=False):
    settings.auto_trade = auto_trade
    trades = filter_by_investor([_norm_congress(r) for r in sample_rows], "Nancy Pelosi")
    prices = {"NVDA": 180.0, "AVGO": 350.0, "AAPL": 250.0}
    broker = LocalPaperBroker(settings.state_dir / "pb.json", starting_cash=80_000,
                              price_fn=lambda s: prices[s])
    return trades, broker, Notifier()


def test_recommendation_only_flow(settings, sample_rows):
    trades, broker, notifier = _make(settings, sample_rows)
    script = [
        ("get_portfolio", {}),
        ("get_latest_price", {"symbol": "NVDA"}),
        ("send_recommendation", {"action": "buy", "ticker": "NVDA",
                                 "headline": "Pelosi bought NVDA; you hold none",
                                 "rationale": "Large purchase.", "confidence": "high",
                                 "suggested_notional_usd": 5000}),
        ("place_paper_order", {"symbol": "NVDA", "side": "buy", "notional_usd": 5000}),
    ]
    holder = {}

    def factory(**kw):
        holder["r"] = FakeRunner(script, **kw)
        return holder["r"]

    result = check(settings, trades=trades, broker=broker, notifier=notifier,
                   runner_factory=factory)
    assert len(result.new_trades) == 3
    assert result.recommendations[0]["ticker"] == "NVDA"
    assert result.orders == []  # auto_trade off -> no order tool
    assert holder["r"].tool_log[-1] == ("place_paper_order", {"error": "tool not available"})
    assert notifier.sent[0]["subject"].startswith("[BUY NVDA]")
    # request shape
    kw = holder["r"].kwargs
    assert kw["model"] == "claude-opus-5-5" and kw["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in kw["betas"]
    assert "NVDA" in kw["messages"][0]["content"]
    # state: trades now remembered, second run is a no-op
    again = check(settings, trades=trades, broker=broker, notifier=notifier,
                  runner_factory=factory)
    assert again.skipped and again.new_trades == []
    assert State(settings.state_dir / "state.json").seen_count == 3


def test_auto_trade_places_paper_order_within_limits(settings, sample_rows):
    trades, broker, notifier = _make(settings, sample_rows, auto_trade=True)
    script = [
        ("send_recommendation", {"action": "buy", "ticker": "NVDA", "headline": "h",
                                 "rationale": "r", "confidence": "high",
                                 "suggested_notional_usd": 5000}),
        ("place_paper_order", {"symbol": "NVDA", "side": "buy", "notional_usd": 50_000}),  # >10%
        ("place_paper_order", {"symbol": "NVDA", "side": "buy", "notional_usd": 5000}),
    ]
    runner = {}

    def factory(**kw):
        runner["r"] = FakeRunner(script, **kw); return runner["r"]

    result = check(settings, trades=trades, broker=broker, notifier=notifier,
                   runner_factory=factory)
    log = runner["r"].tool_log
    assert "error" in log[1][1] and "10%" in log[1][1]["error"]
    assert log[2][1]["ok"] is True
    assert len(result.orders) == 1 and result.orders[0]["symbol"] == "NVDA"
    assert broker.account().cash == 75_000


def test_dry_run_does_not_call_claude(settings, sample_rows):
    trades, broker, notifier = _make(settings, sample_rows)
    called = []
    result = check(settings, trades=trades, broker=broker, notifier=notifier, dry_run=True,
                   runner_factory=lambda **kw: called.append(kw))
    assert result.skipped and len(result.new_trades) == 3 and called == []
    assert State(settings.state_dir / "state.json").seen_count == 0


def test_refusal_is_reported(settings, sample_rows):
    trades, broker, notifier = _make(settings, sample_rows)

    class RefusingRunner(FakeRunner):
        def __iter__(self):
            yield SimpleNamespace(model="m", stop_reason="refusal", content=[])

    result = check(settings, trades=trades, broker=broker, notifier=notifier,
                   runner_factory=lambda **kw: RefusingRunner([], **kw))
    assert result.refusal and "declined" in result.final_text


def _usage(inp, out, read=0, write=0, iterations=None):
    return SimpleNamespace(input_tokens=inp, output_tokens=out, cache_read_input_tokens=read,
                           cache_creation_input_tokens=write, iterations=iterations)


def test_usage_cost_caching_and_step_limit(settings, sample_rows):
    trades, broker, notifier = _make(settings, sample_rows)
    seen = {}

    class MeteredRunner(FakeRunner):
        def __iter__(self):
            seen.update(self.kwargs)
            yield SimpleNamespace(model="claude-opus-5-5", stop_reason="tool_use", content=[],
                                  usage=_usage(2000, 300, write=8000))
            yield SimpleNamespace(model="claude-opus-5-5", stop_reason="tool_use",
                                  content=[SimpleNamespace(type="text", text="Still checking.")],
                                  usage=_usage(500, 200, read=9000))

    result = check(settings, trades=trades, broker=broker, notifier=notifier,
                   runner_factory=lambda **kw: MeteredRunner([], **kw))
    assert seen["cache_control"] == {"type": "ephemeral"} and seen["max_iterations"] == 20
    assert seen["output_config"] == {"effort": "high"} and seen["fallbacks"] == "default"
    u = result.usage.to_dict()
    assert u["requests"] == 2 and u["input_tokens"] == 2500 and u["cache_read_tokens"] == 9000
    # 2500*4 + 500*20 + 9000*0.2 + 8000*4*1.25 = 10000 + 10000 + 1800 + 40000 per million
    assert abs(u["cost_usd"] - 0.0618) < 1e-9
    assert result.stop == "step_limit" and "Stopped after 20 steps" in result.final_text
    run = State(settings.state_dir / "state.json").data["runs"][-1]
    assert run["usage"]["cost_usd"] == u["cost_usd"] and run["stop"] == "step_limit"


def test_fallback_and_unknown_model_cost(settings, sample_rows):
    trades, broker, notifier = _make(settings, sample_rows)

    class FallbackRunner(FakeRunner):
        def __iter__(self):
            its = [SimpleNamespace(type="message"), SimpleNamespace(type="fallback_message")]
            yield SimpleNamespace(model="claude-some-future-model", stop_reason="max_tokens",
                                  content=[SimpleNamespace(type="text", text="Partial")], usage=_usage(10, 10, iterations=its))

    result = check(settings, trades=trades, broker=broker, notifier=notifier,
                   runner_factory=lambda **kw: FallbackRunner([], **kw))
    assert result.fallback_used and result.stop == "max_tokens" and "cut short" in result.final_text
    assert result.usage.to_dict()["cost_usd"] is None and result.usage.output_tokens == 10
