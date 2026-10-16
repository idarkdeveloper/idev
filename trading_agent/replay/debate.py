"""Research: a bull-vs-bear debate before Claude's recommendation, against today's single call (from TradingAgents).

For each case (a replay date and a stock the replay's screen picked), the BLIND input of the blind-ticker test is
used (ticker and company become COMPANY_A, dates are relative, prices indexed), so neither arm can lean on what the
model may remember about later events. Then:

* single call: today's Ask-Claude call (``request_view``), once;
* debate: one call argues the strongest honest case FOR buying, one the strongest case AGAINST, and a third call (the
  same forced ``record_view`` tool) reads the data and both arguments and decides. The arguments are model text built
  from untrusted data, so they are passed on wrapped as untrusted.

Both are scored like the track record, with prices after the replay date (used only to grade): over the next
``HORIZON`` trading days a buy is right when it beat NIFTYBEES by more than the round-trip cost, a sell when the stock
then lagged it; hold and watch are not scored. Expectancy in R uses 1R = 2 x ATR(14) at the replay date.

Kept only if the debate beats the single call on both hit rate and expectancy, on cases from months the model has not
been tuned on (by default from the sealed holdout's start); with a handful of cases this is a smoke test. It makes
paid API calls (4 per case), so the CLI shows an estimate and needs --yes.
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ..untrusted import wrap_json
from .blind import ALIAS, Case, prompt_text, stance_of
from .claude import SYSTEM, TOOL, request_view

HORIZON = 20
CALLS_PER_CASE = 4
ARGUMENT_TOOL = {
    "name": "record_argument",
    "description": "Record your argument about the stock.",
    "input_schema": {"type": "object", "properties": {
        "argument": {"type": "string", "description": "4-8 sentences citing only the data provided."},
        "key_points": {"type": "array", "items": {"type": "string"}, "description": "2-5 short points."}},
        "required": ["argument", "key_points"]},
}
ROLE = {
    "bull": ("Your role: argue the strongest HONEST case FOR buying {name} now, using only the data provided. "
             "Do not invent facts; if the case is weak, say what would have to be true."),
    "bear": ("Your role: argue the strongest HONEST case AGAINST buying {name} now (or for selling it), using only "
             "the data provided. Do not invent facts; if the case is weak, say what would have to be true."),
}
JUDGE = ("You also get two arguments written by other analysts from the same data: one for buying, one against. They "
         "may be wrong or one-sided. Weigh them against the data yourself, then record your view.")


def _argue(client: Any, model: str, date_label: str, ctx: dict[str, Any], side: str, name: str) -> dict[str, Any]:
    system = SYSTEM.format(date=date_label) + "\n" + ROLE[side].format(name=name)
    resp = client.messages.create(model=model, max_tokens=1500, system=system, tools=[ARGUMENT_TOOL],
                                  tool_choice={"type": "tool", "name": ARGUMENT_TOOL["name"]},
                                  messages=[{"role": "user", "content": json.dumps(ctx, default=str)}])
    block = next((b for b in resp.content if getattr(b, "type", None) == "tool_use"), None)
    if block is None or not isinstance(block.input, dict):
        raise ValueError(f"Claude returned no {side} argument")
    return {"argument": str(block.input.get("argument", ""))[:3000],
            "key_points": [str(p)[:300] for p in (block.input.get("key_points") or [])][:5]}


def debate_view(client: Any, model: str, date_label: str, ctx: dict[str, Any], name: str = ALIAS
                ) -> tuple[dict[str, Any], dict[str, Any]]:
    """Bull, bear, then the judge's recorded view: (view, {"bull": ..., "bear": ...})."""
    bull = _argue(client, model, date_label, ctx, "bull", name)
    bear = _argue(client, model, date_label, ctx, "bear", name)
    judged = {**ctx, "debate_note": JUDGE,
              "argument_for_buying": wrap_json(bull, "argument_for"),
              "argument_against_buying": wrap_json(bear, "argument_against")}
    resp = client.messages.create(model=model, max_tokens=4000, system=SYSTEM.format(date=date_label) + "\n" + JUDGE,
                                  tools=[TOOL], tool_choice={"type": "tool", "name": TOOL["name"]},
                                  messages=[{"role": "user", "content": json.dumps(judged, default=str)}])
    block = next((b for b in resp.content if getattr(b, "type", None) == "tool_use"), None)
    if block is None or not isinstance(block.input, dict):
        raise ValueError("Claude returned no judged view")
    return block.input, {"bull": bull, "bear": bear}


def outcome(source: Any, ticker: str, day: str, *, benchmark: str = "NIFTYBEES", horizon: int = HORIZON,
            field: str = "adj_close") -> dict[str, Any] | None:
    """HINDSIGHT, for grading only: the stock's and the fund's return over the next ``horizon`` bars, and 1R."""
    import bisect
    from ..scorecard import risk_unit
    try:
        bars, fund = source.history(ticker, "10y"), source.history(benchmark, "10y")
    except Exception:  # noqa: BLE001
        return None
    dates, fdates = [b["date"] for b in bars], [b["date"] for b in fund]
    i, fi = bisect.bisect_right(dates, day) - 1, bisect.bisect_right(fdates, day) - 1
    if i < 0 or fi < 0 or i + horizon >= len(bars) or fi + horizon >= len(fund):
        return None
    ret = bars[i + horizon][field] / bars[i][field] - 1
    fret = fund[fi + horizon][field] / fund[fi][field] - 1
    return {"return": ret, "fund_return": fret, "excess": ret - fret, "risk": risk_unit(bars, bars[i]["date"]),
            "until": bars[i + horizon]["date"]}


def score(stance: dict[str, Any], out: dict[str, Any] | None, cost: float) -> dict[str, Any]:
    """Right / wrong and the result in R, like the track record (hold and watch are not scored)."""
    if out is None or stance["stance"] not in ("buy", "sell"):
        return {"scored": False}
    ex = out["excess"]
    right = (ex - cost > 0) if stance["stance"] == "buy" else (ex < 0)
    r = None
    if out.get("risk"):
        r = ((ex - cost) if stance["stance"] == "buy" else -ex) / out["risk"]
    return {"scored": True, "right": right, "r": r}


def summarize(results: list[dict[str, Any]], arm: str) -> dict[str, Any]:
    sc = [r[arm]["score"] for r in results if r[arm]["score"].get("scored")]
    rs = [s["r"] for s in sc if s.get("r") is not None]
    return {"scored": len(sc), "hit_rate": sum(s["right"] for s in sc) / len(sc) if sc else None,
            "expectancy_r": sum(rs) / len(rs) if rs else None,
            "stances": {k: sum(1 for r in results if r[arm]["stance"]["stance"] == k)
                        for k in ("buy", "sell", "neutral", "none")}}


def verdict(single: dict[str, Any], debate: dict[str, Any]) -> str:
    if not single["scored"] or not debate["scored"]:
        return "not enough scored calls to compare (hold and watch are not scored)"
    better_hits = (debate["hit_rate"] or 0) > (single["hit_rate"] or 0)
    better_r = (debate["expectancy_r"] if debate["expectancy_r"] is not None else -9) > \
        (single["expectancy_r"] if single["expectancy_r"] is not None else -9)
    if better_hits and better_r:
        return "the debate beat the single call on hit rate and expectancy (keep testing on more cases)"
    return "the debate did not beat the single call on both hit rate and expectancy: today's single call stays"


def estimate_cost(model: str, pairs: list[dict[str, Any]]) -> dict[str, Any]:
    """Approximate USD: the masked prompt 4 times per case, plus two arguments added to the judge's input."""
    from ..agent import PRICES_PER_MTOK
    base = sum(math.ceil(len(prompt_text(p["masked"], "D0")) / 3.5) for p in pairs)
    tokens_in = 4 * base + len(pairs) * 1200
    calls = CALLS_PER_CASE * len(pairs)
    tokens_out = len(pairs) * (700 + 2 * 600 + 700)
    price = PRICES_PER_MTOK.get(model)
    usd = None if price is None else (tokens_in * price[0] + tokens_out * price[1]) / 1e6
    return {"calls": calls, "input_tokens": tokens_in, "output_tokens": tokens_out, "usd": usd, "model": model}


def run_debate_test(cases: list[Case], pairs: list[dict[str, Any]], *, client: Any, model: str, source: Any,
                    cost: float, field: str = "adj_close",
                    progress: Callable[[str], None] | None = None,
                    checkpoint: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    results: list[dict[str, Any]] = []

    def report() -> dict[str, Any]:
        s, d = summarize(results, "single"), summarize(results, "debate")
        return {"run_at": datetime.now().isoformat(timespec="seconds"), "model": model, "horizon": HORIZON,
                "single": s, "debate": d, "verdict": verdict(s, d), "results": list(results),
                "labels": {"outcome": "HINDSIGHT: returns after the replay date, used only to grade, never shown"}}

    for case, pair in zip(cases, pairs):
        ticker = pair["labels"]["ticker"]
        if progress:
            progress(f"{case.date} {ticker}: single call, then bull, bear and judge")
        _, single_view = request_view(client, model, "D0", pair["masked"])
        debate, args = debate_view(client, model, "D0", pair["masked"])
        out = outcome(source, ticker, case.date, field=field)
        s1, d1 = stance_of(single_view, ticker), stance_of(debate, ticker)
        results.append({"case": case.to_dict(), "outcome": out, "arguments": args,
                        "single": {"stance": s1, "score": score(s1, out, cost)},
                        "debate": {"stance": d1, "score": score(d1, out, cost)}})
        if checkpoint:
            checkpoint(report())
    return report()


def format_report(r: dict[str, Any]) -> str:
    def line(name: str, x: dict[str, Any]) -> str:
        hit = "n/a" if x["hit_rate"] is None else f"{x['hit_rate'] * 100:.0f}%"
        exp = "n/a" if x["expectancy_r"] is None else f"{x['expectancy_r']:+.2f}R"
        st = ", ".join(f"{k} {v}" for k, v in x["stances"].items() if v)
        return f"  {name:<12} scored {x['scored']:>2}  right {hit:>5}  expectancy {exp:>7}  ({st})"
    return "\n".join([f"Bull-vs-bear debate vs the single call ({len(r['results'])} blind cases, {r['model']}, "
                      f"{r['horizon']} trading days, vs NIFTYBEES after charges):",
                      line("single call", r["single"]), line("debate", r["debate"]), f"  Verdict: {r['verdict']}"])


def run_cli(args: Any, settings: Any, *, source: Any | None = None, news_client: Any | None = None,
            universe: Any | None = None, client: Any | None = None, today: str | None = None,
            out: Callable[[str], None] = print) -> int:
    """`replay debate-test SLUG`: 2 = refused, 1 = estimate shown but --yes not given, 0 = ran."""
    from ..costs import cost_model_for
    from ..scorecard import _ist_today
    from .blind import build_pair, leaks, pick_cases, research_dir
    if not getattr(settings, "anthropic_api_key", None):
        out("Refusing: the debate test makes paid Claude calls and ANTHROPIC_API_KEY is not set.")
        return 2
    trial_file = Path(settings.state_dir) / "replay" / args.slug / "trial.json"
    if not trial_file.exists():
        out(f"No replay called {args.slug} (looked for {trial_file}).")
        return 2
    data = json.loads(trial_file.read_text(encoding="utf-8"))
    today = today or _ist_today()
    pool = pick_cases(data, 10 ** 6, today=today, min_age_days=35)
    since = getattr(args, "since", None) or ""
    unseen = [c for c in pool if c.date >= since]
    cases = (unseen if len(unseen) >= int(args.cases) else pool)[:int(args.cases)]
    if since and len(unseen) < int(args.cases):
        out(f"Only {len(unseen)} case(s) from {since} on in this replay; using older months too (less unseen).")
    if not cases:
        out("This replay has no recorded picks old enough to grade.")
        return 2
    if source is None:
        from ..price_archive import archive_for
        from ..prices import YahooPrices
        source = YahooPrices(suffix=".NS", cache_dir=Path(settings.state_dir) / "cache", cache_ttl=7 * 86400,
                             archive=archive_for(settings))
    if news_client is None:
        from ..nse import NSEClient
        news_client = NSEClient(cache_dir=Path(settings.state_dir) / "cache")
    if universe is None:
        from .trial import ReplayUniverse
        universe = ReplayUniverse(data["universe"], settings.state_dir)
    field = "close" if data.get("dividends") == "cash" else "adj_close"
    model = getattr(args, "model", None) or settings.claude_model
    pairs = [build_pair(c, source=source, news_client=news_client, members=universe.members_on(c.date), field=field,
                        cash=float(data.get("cash") or 100_000)) for c in cases]
    kept = [(c, p) for c, p in zip(cases, pairs)
            if not leaks(p["masked"], p["labels"]["ticker"], p["labels"]["company"], p["labels"]["sector"])]
    if not kept:
        out("No case could be masked cleanly, so nothing can be tested.")
        return 2
    cases, pairs = [c for c, _ in kept], [p for _, p in kept]
    est = estimate_cost(model, pairs)
    shown = "cost unknown for this model" if est["usd"] is None else f"about ${est['usd']:.2f}"
    out(f"Debate test on {data['name']}: {len(cases)} blind cases, {est['calls']} Claude calls with {model}, "
        f"estimated {shown}.")
    for c in cases:
        out(f"  {c.date} {c.ticker}")
    if not getattr(args, "yes", False):
        out("Nothing was sent. This costs real API money: run again with --yes to go ahead.")
        return 1
    if client is None:
        from ..agent import make_client
        client = make_client(settings)
    path = research_dir(settings.state_dir) / f"debate_test_{today}.json"
    cost = cost_model_for("in").round_trip_bps(25_000) / 10_000

    def keep(partial: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({**partial, "slug": data["slug"], "estimate": est}, indent=2, default=str),
                        encoding="utf-8")

    report = run_debate_test(cases, pairs, client=client, model=model, source=source, cost=cost, field=field,
                             progress=out, checkpoint=keep)
    keep(report)
    out(format_report(report))
    out(f"Saved {path}")
    from ..gap_diagnosis import log_trial
    log_trial(settings.state_dir, "debate", {"cases": len(cases), "model": model},
              {"single": report["single"], "debate": report["debate"]})
    return 0
