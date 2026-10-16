"""The bull-vs-bear debate study: four calls per blind case, arguments passed on as untrusted, scoring like the
track record, and the API key / --yes gates. Scripted fake Claude only."""
import json
from types import SimpleNamespace

from trading_agent.replay import debate
from trading_agent.replay.blind import ALIAS, Case
from trading_agent.replay.claude import TOOL

from .replay_fakes import FakeSource, path
from .test_replay_blind import MEMBERS, TICKER, News, _setup, pair


def src(drift=0.0011):
    return FakeSource({"^NSEI": path(10_000, 0.0004), "NIFTYBEES": path(200, 0.0004), TICKER: path(237.5, drift),
                       "OTHER": path(100, 0.0002)})


class Debater:
    """Scripted Claude: arguments for the bull and bear roles; the single call and the judge answer by script."""

    def __init__(self, single=("watch", "low"), judged=("buy", "high")):
        self.calls, self.single, self.judged, self.messages = [], single, judged, self

    def create(self, **kw):
        self.calls.append(kw)
        name = kw["tool_choice"]["name"]
        if name == "record_argument":
            side = "for" if "AGAINST" not in kw["system"] else "against"
            return SimpleNamespace(content=[SimpleNamespace(type="tool_use", input={
                "argument": f"the case {side}", "key_points": ["a", "b"]})])
        action, conf = self.judged if "two arguments" in kw["system"] else self.single
        return SimpleNamespace(model="m", content=[SimpleNamespace(type="tool_use", input={
            "summary": "s", "recommendations": [{"action": action, "ticker": ALIAS, "headline": "h",
                                                 "rationale": "r", "confidence": conf}]})])


def test_four_calls_per_case_and_the_judge_gets_wrapped_arguments():
    c = Debater()
    r = debate.run_debate_test([Case("t", "2022-03-15", TICKER)], [pair()], client=c, model="m", source=src(),
                               cost=0.004)
    assert len(c.calls) == 4
    tools = [k["tool_choice"]["name"] for k in c.calls]
    assert tools == [TOOL["name"], "record_argument", "record_argument", TOOL["name"]]
    judge = json.loads(c.calls[-1]["messages"][0]["content"])
    assert "UNTRUSTED" in judge["argument_for_buying"].upper() and "the case for" in judge["argument_for_buying"]
    assert all(TICKER not in k["messages"][0]["content"] for k in c.calls)          # blind inputs only
    res = r["results"][0]
    assert res["single"]["stance"]["stance"] == "neutral" and not res["single"]["score"]["scored"]
    assert res["debate"]["stance"]["stance"] == "buy" and res["debate"]["score"]["scored"]


def test_scoring_like_the_track_record():
    out = {"excess": 0.05, "risk": 0.1}
    buy = debate.score({"stance": "buy"}, out, 0.004)
    assert buy["right"] and abs(buy["r"] - 0.46) < 1e-9
    sell = debate.score({"stance": "sell"}, out, 0.004)
    assert not sell["right"] and abs(sell["r"] + 0.5) < 1e-9
    assert debate.score({"stance": "neutral"}, out, 0.004) == {"scored": False}
    assert debate.score({"stance": "buy"}, None, 0.004) == {"scored": False}


def test_outcome_uses_the_next_20_bars_against_the_fund():
    o = debate.outcome(src(), TICKER, "2022-03-15")
    assert o["excess"] > 0 and o["risk"] is not None and o["until"] > "2022-03-15"
    assert debate.outcome(src(), "MISSING", "2022-03-15") is None


def test_verdict_needs_both_hit_rate_and_expectancy():
    s = {"scored": 4, "hit_rate": 0.5, "expectancy_r": 0.1, "stances": {}}
    better = {"scored": 4, "hit_rate": 0.75, "expectancy_r": 0.3, "stances": {}}
    mixed = {"scored": 4, "hit_rate": 0.75, "expectancy_r": 0.0, "stances": {}}
    assert debate.verdict(s, better).startswith("the debate beat")
    assert "stays" in debate.verdict(s, mixed)
    assert debate.verdict({"scored": 0}, better).startswith("not enough")


def test_cli_gates_and_a_full_run(tmp_path):
    settings, uni = _setup(tmp_path)
    args = SimpleNamespace(slug="t", cases=2, since="2022-01-01", model=None, yes=False)
    lines, c = [], Debater()
    assert debate.run_cli(args, settings, source=src(), news_client=News(), universe=uni, client=c,
                          out=lines.append, today="2026-10-09") == 1
    assert c.calls == [] and "8 Claude calls" in "\n".join(lines) and "--yes" in lines[-1]
    nokey, _ = _setup(tmp_path / "n", key=None)
    assert debate.run_cli(args, nokey, source=src(), news_client=News(), universe=uni, client=c,
                          out=lines.append) == 2
    args.yes = True
    assert debate.run_cli(args, settings, source=src(), news_client=News(), universe=uni, client=c,
                          out=lines.append, today="2026-10-09") == 0
    assert len(c.calls) == 8
    saved = json.loads((settings.state_dir / "research" / "debate_test_2026-10-09.json").read_text())
    assert saved["debate"]["scored"] == 2 and saved["single"]["scored"] == 0
    assert "Verdict:" in debate.format_report(saved)
    trials = (settings.state_dir / "research" / "trials.jsonl").read_text()
    assert '"study": "debate"' in trials and MEMBERS
