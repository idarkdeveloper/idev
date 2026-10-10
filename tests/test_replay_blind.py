"""Blind-ticker test: masking, pairing, verdict thresholds and the --yes / API key gates. Scripted fake Claude only."""
import json
import re
from types import SimpleNamespace

from trading_agent.replay import blind
from trading_agent.replay.blind import (ALIAS, Case, compare_case, MaskedPrices, build_pair, compare_case, leaks, latest_verdict,
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
    assert leaks(p["masked"], TICKER, NAME, "capital goods") == []
    for word in (TICKER, "Swiftline", "Engineering", "Rajesh", "Kumar", "pledged"):
        assert word.lower() not in masked.lower(), word
    assert not re.search(r"\d{4}-\d{2}-\d{2}", masked)
    assert '"date": "D0"' in masked and "capital goods" in masked and ALIAS in masked
    assert "Today is D0" in masked
    un = prompt_text(p["unmasked"], DAY)
    assert TICKER in un and "Swiftline" in un and DAY in un and "capital goods" in un
    assert leaks(un, TICKER, NAME)                      # the check does catch an unmasked prompt
    assert "Board Meeting" in un and "Rajesh" not in un and "promoter" not in un    # category and date, no text
    assert p["unmasked"]["lookup"]["announcements"].count('"text": ""') == 2


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
    assert len(client.calls) == 4
    for kw in client.calls:
        assert kw["tools"] == [TOOL] and kw["tool_choice"] == {"type": "tool", "name": "record_view"}
    assert [ALIAS in kw["messages"][0]["content"] for kw in client.calls] == [False, False, True, True]
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
    assert rc == 1 and client.calls == [] and "--yes" in text and "8 Claude calls" in text and "$" in text
    assert not (settings.state_dir / "research").exists()


def test_cli_with_yes_runs_saves_and_the_page_can_read_the_verdict(tmp_path):
    settings, uni = _setup(tmp_path)
    lines, client = [], Fake(unmasked=("buy", "high"), masked=("buy", "high"))
    rc = run_cli(_args(yes=True), settings, source=source(), news_client=News(), universe=uni, client=client,
                 out=lines.append, today="2026-10-09")
    assert rc == 0 and len(client.calls) == 8
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


def test_leak_check_ignores_the_system_prompt_and_real_names_that_share_plain_words():
    # "Indian" / "Data" are plain words of the system prompt and parts of real company names
    members = [{"symbol": "INDHOTEL", "name": "The Indian Hotels Company Limited", "industry": "Consumer Services"},
               {"symbol": "DATAPATTNS", "name": "Data Patterns (India) Limited", "industry": "Capital Goods"}]
    src = FakeSource({"^NSEI": path(10_000, 0.0004), "INDHOTEL": path(120, 0.0006), "DATAPATTNS": path(80, 0.0009)})
    for t in ("INDHOTEL", "DATAPATTNS"):
        pr = build_pair(Case("t", DAY, t), source=src, news_client=News(), members=members)
        lab = pr["labels"]
        assert leaks(pr["masked"], t, lab["company"], lab["sector"]) == []
        assert leaks(prompt_text(pr["masked"], "D0") + " the Indian Data", t, lab["company"], "")   # prompt words would trip it


def test_company_words_are_scrubbed_before_the_sector_label_is_set():
    members = [{"symbol": TICKER, "name": "Capital Goods Swiftline Limited", "industry": "Capital Goods"}]
    pr = build_pair(Case("t", DAY, TICKER), source=source(), news_client=News(), members=members)
    assert pr["masked"]["lookup"]["sector"] == "capital goods"          # not turned into COMPANY_A
    assert leaks(pr["masked"], TICKER, "Capital Goods Swiftline Limited", "capital goods") == []


class Noisy(Fake):
    """Unmasked answers alternate buy/hold between calls (pure sampling noise); masked answers are steady."""

    def __init__(self):
        super().__init__()
        self.n = 0

    def create(self, **kw):
        if ALIAS not in kw["messages"][0]["content"]:
            self.n += 1
            self.script["un"] = ("buy", "high") if self.n % 2 else ("hold", "high")
        return super().create(**kw)


def _view(action, conf="high"):
    return stance_of({"recommendations": [{"action": action, "ticker": "X", "confidence": conf}]}, "X")


def test_noise_baseline_is_subtracted_from_the_identity_effect():
    cases = [Case("t", DAY, TICKER)] * 5
    rep = run_blind_test(cases, [pair() for _ in cases], client=Noisy(), model="m", source=source())
    s = rep["summary"]
    # U1 buy, U2 hold: 1 noise pair flips (of 2) = 0.5 per case; U1 buy vs M1 buy: no identity flip
    assert s["identity_flips"] == 0 and s["noise_flips"] == 2.5 and s["net_flips"] == -2.5
    assert s["verdict"] == "no sign of hindsight"
    # identity flips of 2 of 5 that noise explains are not hindsight; the same flips with no noise are
    noisy = [compare_case(Case("t", "d", "X"), _view("buy"), _view("hold"), None, _view("hold"), _view("hold"))] * 2
    quiet = [_res(("buy", "high"), ("buy", "high"))] * 3
    v = verdict(noisy + quiet)
    assert v["identity_flips"] == 2 and v["noise_flips"] == 1.0 and v["net_flips"] == 1.0 and not v["possible_hindsight"]
    assert verdict([_res(("buy", "high"), ("hold", "high"))] * 2 + quiet)["possible_hindsight"]
    assert "noise" in v["thresholds"]


def test_net_confidence_gap_needs_to_beat_noise_by_fifteen_points():
    def case(u1, u2, m1, m2):
        return compare_case(Case("t", "d", "X"), _view("buy", u1), _view("buy", m1), None, _view("buy", u2), _view("buy", m2))
    big = [case("high", "high", "low", "low")] * 5          # identity gap 50, noise 0
    assert verdict(big)["possible_hindsight"] and verdict(big)["net_gap"] == 50
    shaky = [case("high", "low", "low", "high")] * 5        # identity gap 50 but noise gap 50
    v = verdict(shaky)
    assert v["net_gap"] == 0 and not v["possible_hindsight"]


def test_report_is_saved_after_every_case_and_survives_a_crash(tmp_path):
    settings, uni = _setup(tmp_path)

    class Dies(Fake):
        def create(self, **kw):
            if len(self.calls) == 4:           # the first case's four calls are done; the second dies
                raise RuntimeError("API down")
            return super().create(**kw)

    import pytest
    with pytest.raises(RuntimeError):
        run_cli(_args(yes=True), settings, source=source(), news_client=News(), universe=uni, client=Dies(),
                out=lambda *_: None, today="2026-10-09")
    saved = json.loads((settings.state_dir / "research" / "blind_test_2026-10-09.json").read_text())
    assert len(saved["results"]) == 1 and saved["complete"] is False and saved["summary"]["cases"] == 1


def test_cli_skips_cases_that_leak_before_the_estimate_and_never_sends_them(tmp_path, monkeypatch):
    settings, _ = _setup(tmp_path)
    members = MEMBERS + [{"symbol": "LEAKY", "name": "Leakage Holdings Limited", "industry": "Finance"}]
    uni = SimpleNamespace(members_on=lambda d: members)
    src = FakeSource({"^NSEI": path(10_000, 0.0004), TICKER: path(237.5, 0.0011), "LEAKY": path(50, 0.001)})
    real = blind.build_pair

    def bad_pair(case, **kw):
        pr = real(case, **kw)
        if case.ticker == "LEAKY":
            pr["masked"]["lookup"]["note"] = "formerly Leakage"
        return pr

    monkeypatch.setattr(blind, "build_pair", bad_pair)
    lines, client = [], Fake()
    rc = run_cli(_args(yes=True, case=[f"2022-03-15:{TICKER}", "2022-03-15:LEAKY"]), settings, source=src,
                 news_client=News(), universe=uni, client=client, out=lines.append, today="2026-10-09")
    text = "\n".join(lines)
    assert rc == 0 and len(client.calls) == 4                      # only the clean case was paid for
    assert "Skipping 2022-03-15 LEAKY" in text and "Leakage" in text
    assert text.index("Skipping") < text.index("Claude calls")      # reported before the estimate
    rc = run_cli(_args(case=["2022-03-15:LEAKY"]), settings, source=src, news_client=News(), universe=uni,
                 client=Fake(), out=lines.append, today="2026-10-09")
    assert rc == 2 and "nothing can be tested" in lines[-1]


def test_estimate_counts_four_calls_per_case():
    est = blind.estimate_cost("claude-sonnet-5-5", [pair(), pair()], [DAY, DAY])
    assert est["calls"] == 8 and est["usd"] > 0
    one = blind.estimate_cost("claude-sonnet-5-5", [pair()], [DAY])
    assert abs(est["input_tokens"] - 2 * one["input_tokens"]) <= 2
