"""Blind-ticker test: masking, pairing, verdict thresholds and the --yes / API key gates. Scripted fake Claude only."""
import json
import re
from types import SimpleNamespace

from trading_agent.replay import blind
from trading_agent.replay.blind import (ALIAS, Case, MaskedPrices, build_pair, compare_case, leaks, latest_verdict,
                                        pick_cases, prompt_text, run_blind_test, run_cli, stance_of, verdict)
from trading_agent.replay.claude import TOOL
from trading_agent.replay.clock import ClockedPrices, ReplayClock

from .replay_fakes import FakeSource, path

DAY = "2022-03-15"
TICKER = "SWIFTCO"
NAME = "Swiftline Engineering Limited"
MEMBERS = [{"symbol": TICKER, "name": NAME, "industry": "Capital Goods"}, {"symbol": "OTHER", "name": "Other", "industry": ""}]


def source():
    return FakeSource({"^NSEI": path(10_000, 0.0004), TICKER: path(237.5, 0.0011), "OTHER": path(100, 0.0002)})


class News:
    def announcement_history(self, symbol):
        return [{"at": "2022-03-01 10:00:00", "category": "Board Meeting",
                 "text": "Swiftline Engineering Limited: promoter Rajesh Kumar Swift pledged shares of SWIFTCO"},
                {"at": "2022-02-10 09:00:00", "category": "Results", "text": "Swiftline results for Q3"},
                {"at": "2022-06-01 09:00:00", "category": "Dividend", "text": "future announcement"}]


class Fake:
    """Scripted Claude: answers by whether the input names COMPANY_A (masked) or not."""

    def __init__(self, unmasked=("buy", "high"), masked=("buy", "high")):
        self.calls, self.script, self.messages = [], {"un": unmasked, "ma": masked}, self

    def create(self, **kw):
        self.calls.append(kw)
        content = kw["messages"][0]["content"]
        action, conf = self.script["ma" if ALIAS in content else "un"]
        sym = ALIAS if ALIAS in content else TICKER
        return SimpleNamespace(model="m", content=[SimpleNamespace(type="tool_use", input={
            "summary": "s", "recommendations": [{"action": action, "ticker": sym, "headline": "h",
                                                 "rationale": "steady trend and strong momentum", "confidence": conf}]})])


def pair():
    return build_pair(Case("t", DAY, TICKER), source=source(), news_client=News(), members=MEMBERS)


def test_masked_prompt_has_no_identifiers_or_iso_dates():
    p = pair()
    masked = prompt_text(p["masked"], "D0")
    assert leaks(masked, TICKER, NAME) == []
    for word in (TICKER, "Swiftline", "Engineering", "Rajesh", "Kumar", "pledged"):
        assert word.lower() not in masked.lower(), word
    assert not re.search(r"\d{4}-\d{2}-\d{2}", masked)
    assert '"date": "D0"' in masked and "capital goods" in masked and ALIAS in masked
    assert "Today is D0" in masked
    un = prompt_text(p["unmasked"], DAY)
    assert TICKER in un and "Swiftline" in un and DAY in un and "capital goods" in un
    assert leaks(un, TICKER, NAME)                      # the check does catch an unmasked prompt


def test_masked_announcements_keep_category_and_relative_day_only():
    inner = json.loads(re.search(r"\n(\[.*?\])\n", json.dumps(pair()["masked"]["lookup"]["announcements"]).encode()
                                 .decode("unicode_escape"), re.S).group(1))
    assert [a["category"] for a in inner] == ["Board Meeting", "Results"]       # the future one is not shown either
    assert all(a["text"] == "" and re.fullmatch(r"D-\d+", a["date"]) for a in inner)
    assert int(inner[0]["date"][2:]) < int(inner[1]["date"][2:])


def test_prices_indexed_to_100_with_returns_and_ratios_unchanged():
    p = pair()
    un, ma = p["unmasked"]["lookup"], p["masked"]["lookup"]
    for k in ("ret_1m", "ret_6m", "ret_12_1", "above_200dma", "pct_from_52w_high", "verdict"):
        assert un[k] == ma[k], k
    clocked = ClockedPrices(source(), ReplayClock(DAY))
    bars = clocked.history(TICKER, "2y")
    assert abs(ma["last_close"] - 100 * bars[-1]["close"] / bars[0]["close"]) < 1e-6
    assert abs(ma["atr_stop"] / ma["last_close"] - un["atr_stop"] / un["last_close"]) < 1e-4  # the stop is rounded to 2 places
    m = MaskedPrices(clocked, TICKER).history(ALIAS, "2y")
    assert abs(m[0]["close"] - 100) < 1e-9 and m[-1]["date"] == "D0" and m[0]["date"] == f"D-{len(m) - 1}"
    assert [b["volume"] for b in m] == [b["volume"] for b in bars]
    assert abs(p["masked"]["market"]["nifty_ret_6m"] - p["unmasked"]["market"]["nifty_ret_6m"]) < 1e-12
    assert p["masked"]["market"]["nifty_close"] != p["unmasked"]["market"]["nifty_close"]


def test_both_runs_go_through_the_one_record_view_path_with_no_order_tool():
    client = Fake(unmasked=("buy", "high"), masked=("hold", "low"))
    rep = run_blind_test([Case("t", DAY, TICKER)], [pair()], client=client, model="m", source=source())
    assert len(client.calls) == 2
    for kw in client.calls:
        assert kw["tools"] == [TOOL] and kw["tool_choice"] == {"type": "tool", "name": "record_view"}
    assert [ALIAS in kw["messages"][0]["content"] for kw in client.calls] == [False, True]
    r = rep["results"][0]
    assert r["unmasked"]["action"] == "buy" and r["masked"]["action"] == "hold" and r["stance_flip"]
    assert r["confidence_gap"] == 50 and r["outcome"]["return"] > 0 and r["hindsight_signature"] is True
    assert "HINDSIGHT" in rep["labels"]["outcome"]


def test_a_masked_input_that_leaks_is_never_sent():
    p = pair()
    p["masked"]["lookup"]["company"] = NAME
    client = Fake()
    import pytest
    with pytest.raises(ValueError, match="still contains"):
        run_blind_test([Case("t", DAY, TICKER)], [p], client=client, model="m", source=source())
    assert client.calls == []


def _res(un, ma, direction=1):
    out = {"direction": direction, "return": 0.1 * direction, "horizon_bars": 60, "until": "x"} if direction is not None else None
    return compare_case(Case("t", "d", "X"), stance_of({"recommendations": [{"action": un[0], "ticker": "X", "confidence": un[1]}]}, "X"),
                        stance_of({"recommendations": [{"action": ma[0], "ticker": "X", "confidence": ma[1]}]}, "X"), out)


def test_verdict_thresholds():
    same = [_res(("buy", "high"), ("buy", "high")) for _ in range(5)]
    assert verdict(same)["verdict"] == "no sign of hindsight" and verdict(same)["stance_agreement"] == 1.0
    one_flip = same[:4] + [_res(("buy", "high"), ("sell", "high"))]
    assert verdict(one_flip)["verdict"] == "no sign of hindsight"            # 1 of 5 is below 2 of 5
    two_flips = same[:3] + [_res(("buy", "high"), ("hold", "high"))] * 2
    v = verdict(two_flips)
    assert v["verdict"] == "possible hindsight: 2 of 5 cases" and v["stance_flips"] == 2
    assert "at least 2 of 5" in v["thresholds"] and "15" in v["thresholds"]
    gap = [_res(("buy", "medium"), ("buy", "low"))] * 3 + same[:2]            # 25-point gap in 3 of 5: mean 15
    assert verdict(gap)["mean_abs_confidence_gap"] == 15.0 and verdict(gap)["possible_hindsight"]
    small = [_res(("buy", "medium"), ("buy", "low"))] * 2 + same[:3]          # mean 10
    assert not verdict(small)["possible_hindsight"]
    assert verdict([])["cases"] == 0


def test_hindsight_signature_follows_the_direction_the_stock_went():
    up = _res(("buy", "high"), ("buy", "low"), direction=1)
    down_more_bullish = _res(("buy", "high"), ("buy", "low"), direction=-1)
    down_more_bearish = _res(("sell", "high"), ("hold", "low"), direction=-1)
    unscored = _res(("buy", "high"), ("buy", "low"), direction=None)
    assert up["hindsight_signature"] is True and down_more_bullish["hindsight_signature"] is False
    assert down_more_bearish["hindsight_signature"] is True and unscored["hindsight_signature"] is None
    v = verdict([up, down_more_bullish, down_more_bearish, unscored])
    assert v["hindsight_signature_cases"] == 2 and v["scored_cases"] == 3


def test_stance_reading_handles_missing_and_unnamed_recommendations():
    assert stance_of({"recommendations": []}, "X")["stance"] == "none"
    assert stance_of({"recommendations": [{"action": "watch", "ticker": "COMPANY_A", "confidence": "low"}]}, "X")["stance"] == "neutral"
    assert stance_of({"recommendations": [{"action": "buy", "ticker": "Z", "confidence": "high"}] * 2}, "X")["stance"] == "none"


def test_pick_cases_is_deterministic_and_spread_over_tickers():
    data = {"slug": "t", "rebalances": [{"date": f"2022-{m:02d}-01", "picks": ["A", "B", "C"]} for m in range(1, 7)]}
    a = pick_cases(data, 3, today="2026-01-01")
    assert a == pick_cases(data, 3, today="2026-01-01") and len({c.ticker for c in a}) == 3
    assert len(pick_cases(data, 5, today="2026-01-01")) == 5
    assert pick_cases({"slug": "t", "rebalances": []}, 5) == []


def _setup(tmp_path, key="k"):
    state = tmp_path / "state"
    (state / "replay" / "t").mkdir(parents=True)
    (state / "replay" / "t" / "trial.json").write_text(json.dumps({
        "slug": "t", "name": "T", "universe": "NIFTY200", "dividends": "reinvest", "cash": 100000,
        "rebalances": [{"date": "2022-03-15", "picks": [TICKER]}, {"date": "2022-02-15", "picks": [TICKER]}]}))
    settings = SimpleNamespace(state_dir=state, anthropic_api_key=key, claude_model="claude-sonnet-5-5")
    uni = SimpleNamespace(members_on=lambda d: MEMBERS)
    return settings, uni


def _args(**kw):
    return SimpleNamespace(**{"slug": "t", "cases": 2, "case": None, "model": None, "yes": False, **kw})


def test_cli_refuses_without_an_api_key(tmp_path):
    settings, uni = _setup(tmp_path, key=None)
    lines, client = [], Fake()
    assert run_cli(_args(yes=True), settings, source=source(), news_client=News(), universe=uni, client=client,
                   out=lines.append) == 2
    assert client.calls == [] and "ANTHROPIC_API_KEY" in lines[0]
    assert not (settings.state_dir / "research").exists()


def test_cli_shows_the_estimate_and_needs_yes(tmp_path):
    settings, uni = _setup(tmp_path)
    lines, client = [], Fake()
    rc = run_cli(_args(), settings, source=source(), news_client=News(), universe=uni, client=client,
                 out=lines.append, today="2026-10-09")
    text = "\n".join(lines)
    assert rc == 1 and client.calls == [] and "--yes" in text and "4 Claude calls" in text and "$" in text
    assert not (settings.state_dir / "research").exists()


def test_cli_with_yes_runs_saves_and_the_page_can_read_the_verdict(tmp_path):
    settings, uni = _setup(tmp_path)
    lines, client = [], Fake(unmasked=("buy", "high"), masked=("buy", "high"))
    rc = run_cli(_args(yes=True), settings, source=source(), news_client=News(), universe=uni, client=client,
                 out=lines.append, today="2026-10-09")
    assert rc == 0 and len(client.calls) == 4
    saved = settings.state_dir / "research" / "blind_test_2026-10-09.json"
    rep = json.loads(saved.read_text())
    assert rep["summary"]["verdict"] == "no sign of hindsight" and len(rep["results"]) == 2
    v = latest_verdict(settings.state_dir)
    assert v["verdict"] == "no sign of hindsight" and v["cases"] == 2 and v["file"] == saved.name
    run_cli(_args(yes=True, case=["2022-02-15:" + TICKER]), settings, source=source(), news_client=News(), universe=uni,
            client=Fake(masked=("sell", "high")), out=lines.append, today="2026-10-09")
    assert (settings.state_dir / "research" / "blind_test_2026-10-09_2.json").exists()
    assert latest_verdict(settings.state_dir)["verdict"].startswith("possible hindsight")
    assert latest_verdict(tmp_path / "nowhere") is None


def test_cli_rejects_a_bad_case(tmp_path):
    settings, uni = _setup(tmp_path)
    lines = []
    assert run_cli(_args(yes=True, case=["nonsense"]), settings, source=source(), news_client=News(), universe=uni,
                   client=Fake(), out=lines.append) == 2
    assert blind.DEFAULT_CASES == 5
