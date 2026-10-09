import json
import re
from types import SimpleNamespace

from trading_agent.replay.claude import TOOL, ask, build_context
from trading_agent.replay.trial import Trial
from .replay_fakes import FakeUniverse, market, top_by_6m


class FakeNews:
    def for_symbol(self, symbol, days=60, until=None):
        return {"items": [{"at": "2021-03-10 10:00:00", "category": "Updates", "text": f"{symbol} news"}], "error": None}


class FakeClient:
    def __init__(self):
        self.calls = []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(model="claude-test", content=[SimpleNamespace(type="tool_use", input={
            "summary": "Market is calm.",
            "recommendations": [{"action": "buy", "ticker": "D", "headline": "h", "rationale": "r", "confidence": "low"}]})])


def make(tmp_path):
    return Trial.create(tmp_path / "replay", name="t", start="2021-03-15", cash=100_000,
                        universe="NIFTYMIDCAP150", top=3, dividends="reinvest", source=market(),
                        universe_obj=FakeUniverse(), screen_fn=top_by_6m, today="2026-10-09")


def test_context_holds_nothing_after_the_clock(tmp_path):
    t = make(tmp_path)
    t.order("D", "buy", qty=10)
    ctx = build_context(t, FakeNews(), lookup="E")
    text = json.dumps(ctx)
    assert all(d <= "2021-03-15" for d in re.findall(r"\d{4}-\d{2}-\d{2}", text))
    assert ctx["date"] == "2021-03-15" and {h["symbol"] for h in ctx["you"]["positions"]} == {"D"}
    assert ctx["lookup"]["symbol"] == "E" and ctx["market"]["nifty_above_200dma"] in (True, False)


class RefusingClient:
    def __init__(self):
        self.messages = self

    def create(self, **kw):
        return SimpleNamespace(model="claude-test", stop_reason="refusal",
                               content=[SimpleNamespace(type="text", text="I can't help with that.")])


def test_ask_raises_when_there_is_no_recorded_view(tmp_path):
    import pytest

    t = make(tmp_path)
    with pytest.raises(ValueError, match="no recorded view"):
        ask(t, RefusingClient(), "claude-x", FakeNews())
    assert t.data["claude"] == [] and t.data["claude_presses"] == 0


def test_ask_uses_one_forced_tool_and_saves_the_answer(tmp_path):
    t = make(tmp_path)
    client = FakeClient()
    entry = ask(t, client, "claude-x", FakeNews())
    kw = client.calls[0]
    assert [x["name"] for x in kw["tools"]] == [TOOL["name"]] and kw["tool_choice"] == {"type": "tool", "name": TOOL["name"]}
    assert "2021-03-15" in kw["system"] and "only" in kw["system"].lower()
    assert entry["date"] == "2021-03-15" and entry["hindsight"] is True and entry["recommendations"][0]["ticker"] == "D"
    assert t.data["claude"][-1] == entry and t.data["claude_presses"] == 1
