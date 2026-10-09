"""HTTP-facing Replay: list, create, step, order, end, look up, ask Claude, tools."""

from __future__ import annotations

import logging
import re
import threading
from contextlib import contextmanager
from datetime import date
from typing import Any, Callable

from ..momentum import momentum_summary, momentum_stats
from ..risk import atr, trailing_stop
from .clock import EARLIEST_START
from .engine import step, step_target
from .news import ClockedNews
from .scorecard import end_trial, what_happened_next
from ..nse import check_ticker
from .trial import BENCHMARKS, PORTFOLIOS, ReplayUniverse, Trial, list_trials

log = logging.getLogger(__name__)
_SLUG = re.compile(r"^[a-z0-9-]{1,40}$")


class ReplayBusy(Exception):
    """A step or tool job is changing this replay right now."""


class NotFound(Exception):
    """No such replay."""


class ReplayApp:
    def __init__(self, app: Any, *, source: Any | None = None, universe_factory: Callable[[str], Any] | None = None,
                 news_client: Any | None = None, client_factory: Callable[[], Any] | None = None,
                 today_fn: Callable[[], str] | None = None, screen_fn: Callable[..., Any] | None = None):
        self.app, self.settings = app, app.settings
        self.dir = self.settings.state_dir / "replay"
        if source is None:
            from ..prices import YahooPrices
            source = YahooPrices(suffix=".NS", cache_dir=self.settings.state_dir / "cache", cache_ttl=7 * 86400)
        self.source = source
        self._universes: dict[str, Any] = {}
        self.universe_factory = universe_factory or (lambda n: ReplayUniverse(n, self.settings.state_dir))
        if news_client is None:
            from ..nse import NSEClient
            news_client = NSEClient(cache_dir=self.settings.state_dir / "cache")
        self.news_client = news_client
        self.client_factory = client_factory
        self.today_fn = today_fn or (lambda: date.today().isoformat())
        self.screen_fn = screen_fn
        self._trials: dict[str, Trial] = {}
        self._tool_results: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._trial_locks: dict[str, threading.Lock] = {}
        self._holders: dict[str, str] = {}  # who holds each trial's lock, for the 409 message

    # -- helpers -------------------------------------------------------------------
    def _universe(self, name: str) -> Any:
        if name not in self._universes:
            self._universes[name] = self.universe_factory(name)
        return self._universes[name]

    def trial(self, slug: str) -> Trial:
        if not _SLUG.match(slug or ""):
            raise NotFound("no such replay")
        with self._lock:
            t = self._trials.get(slug)
            if t is None:
                root = self.dir / slug
                if not (root / "trial.json").exists():
                    raise NotFound("no such replay")
                t = Trial.load(root, self.source, _LazyUniverse(self, root), screen_fn=self.screen_fn)
                self._trials[slug] = t
            return t

    def _trial_lock(self, slug: str) -> threading.Lock:
        with self._lock:
            return self._trial_locks.setdefault(slug, threading.Lock())

    def _busy(self, slug: str) -> ReplayBusy:
        who = self._holders.get(slug) or "a step"
        return ReplayBusy(f"{who[0].upper()}{who[1:]} is running for this replay; try again when it finishes.")

    @contextmanager
    def _guard(self, slug: str, label: str = "a page load"):
        lk = self._trial_lock(slug)
        if not lk.acquire(blocking=False):
            raise self._busy(slug)
        self._holders[slug] = label
        try:
            yield
        finally:
            lk.release()

    def _background(self, slug: str, kind: str, fn: Callable[[Any], str], label: str = "a step") -> Any:
        """Run a job that holds the replay's lock for its whole run."""
        lk = self._trial_lock(slug)
        if not lk.acquire(blocking=False):
            raise self._busy(slug)
        self._holders[slug] = label

        once = threading.Lock()  # held = release already done

        def release() -> None:
            if once.acquire(blocking=False):
                lk.release()

        def run(job: Any) -> str:
            try:
                return fn(job)
            finally:
                release()
        try:
            job = self.app.run_background(kind, run)
        except BaseException:
            release()
            raise
        if job not in self.app.jobs:  # refused: it was never recorded, so run() never ran
            release()
        return job

    def _news(self, t: Trial) -> ClockedNews:
        return ClockedNews(self.news_client, t.clock)

    # -- actions -------------------------------------------------------------------
    def list(self) -> list[dict[str, Any]]:
        return list_trials(self.dir)

    def create(self, body: dict[str, Any]) -> Any:
        def run(job: Any) -> str:
            universe = str(body.get("universe") or "NIFTYMIDCAP150").upper()
            job.message = f"Loading {universe} membership and prices…"
            t = Trial.create(self.dir, name=str(body.get("name") or ""), start=str(body.get("start") or ""),
                             cash=float(body.get("cash") or 100_000), universe=universe,
                             top=int(body.get("top") or 10), dividends=str(body.get("dividends") or "reinvest"),
                             source=self.source, universe_obj=self._universe(universe),
                             screen_fn=self.screen_fn, today=self.today_fn())
            with self._lock:
                self._trials[t.data["slug"]] = t
            job.result = {"slug": t.data["slug"]}
            return f"Replay {t.data['name']} started on {t.data['start']}"
        return self.app.run_background("replay_create", run)

    def step(self, slug: str, body: dict[str, Any]) -> Any:
        t = self.trial(slug)

        def run(job: Any) -> str:
            target = step_target(t.clock.today, str(body.get("by") or "month"), self.today_fn())
            def progress(msg: str) -> None:
                job.message = msg
            r = step(t, target, today=self.today_fn(), progress=progress)
            job.result = r
            capped = " (stopped at today's date)" if r["to"] < step_target(r["from"], str(body.get("by") or "month"), "9999-12-31") else ""
            return f"Moved to {r['to']}: {r['days']} trading days, {len(r['rebalances'])} rebalance(s), {len(r['stops'])} stop(s){capped}"
        return self._background(slug, "replay_step", run, "a step")

    def order(self, slug: str, body: dict[str, Any]) -> dict[str, Any]:
        with self._guard(slug, "an order"):
            return self._order(slug, body)

    def _order(self, slug: str, body: dict[str, Any]) -> dict[str, Any]:
        t = self.trial(slug)
        o = t.order(str(body.get("symbol") or ""), str(body.get("side") or "buy"),
                    notional=float(body["notional"]) if body.get("notional") else None,
                    qty=float(body["qty"]) if body.get("qty") else None)
        if t.data["equity"] and t.data["equity"][-1]["date"] == t.clock.today:
            t.data["equity"][-1] = t.point()  # today's value now includes the trade's charges
        else:
            t.data["equity"].append(t.point())  # a non-trading day: the curve ends on the account's real value
        t.save()
        return {"ok": True, "order": o}

    def set_auto_stop(self, slug: str, on: bool) -> dict[str, Any]:
        with self._guard(slug, "a setting change"):
            return self._set_auto_stop(slug, on)

    def _set_auto_stop(self, slug: str, on: bool) -> dict[str, Any]:
        t = self.trial(slug)
        if t.data["ended"]:
            raise ValueError("this replay has ended; it is read-only")
        t.data["auto_stop"] = bool(on)
        t.save()
        return {"ok": True, "auto_stop": t.data["auto_stop"]}

    def end(self, slug: str) -> dict[str, Any]:
        with self._guard(slug, "the end-of-replay scorecard"):
            return self._end(slug)

    def _end(self, slug: str) -> dict[str, Any]:
        t = self.trial(slug)
        end_trial(t)
        return self._snapshot(slug)

    def lookup(self, slug: str, ticker: str) -> dict[str, Any]:
        with self._guard(slug, "a look-up"):
            return self._lookup(slug, ticker)

    def _lookup(self, slug: str, ticker: str) -> dict[str, Any]:
        t = self.trial(slug)
        sym = check_ticker(ticker)
        out: dict[str, Any] = {"ticker": sym, "name": None, "today": t.clock.today, "announcements": [],
                               "announcements_error": None, "history": [], "price": None}
        try:
            bars = t.prices.history(sym, "2y")
            stats = momentum_stats(bars)
            out["momentum"], out["momentum_summary"] = stats, stats.get("error") or momentum_summary(stats)
            out["price"] = t.prices.latest_price(sym)
            closes = [b["close"] for b in bars]
            for i in range(max(0, len(bars) - 252), len(bars)):
                ma = sum(closes[i - 199:i + 1]) / 200 if i >= 199 else None
                out["history"].append({"d": bars[i]["date"], "c": round(closes[i], 2), "ma200": round(ma, 2) if ma else None})
        except LookupError as e:
            out["momentum"], out["momentum_summary"] = {"error": str(e)}, str(e)
        n = self._news(t).for_symbol(sym, days=60)
        out["announcements"], out["announcements_error"] = n["items"][:8], n["error"]
        pos = next((p for p in t.you.positions() if p.symbol == sym), None)
        if pos is not None:
            try:
                a = atr(t.prices.history(sym, "1y"))
            except LookupError:
                a = None
            out["position"] = {"qty": pos.qty, "avg_entry_price": pos.avg_entry_price,
                               "stop": round(trailing_stop(pos.high_water or pos.current_price or pos.avg_entry_price, a), 2)}
        return out

    def ask(self, slug: str, body: dict[str, Any]) -> dict[str, Any]:
        with self._guard(slug, "Claude"):
            return self._ask(slug, body)

    def _ask(self, slug: str, body: dict[str, Any]) -> dict[str, Any]:
        if not self.settings.anthropic_api_key:
            raise PermissionError("Ask Claude needs ANTHROPIC_API_KEY in .env")
        from .claude import ask
        t = self.trial(slug)
        if t.data["ended"]:
            raise ValueError("this replay has ended; it is read-only")
        lookup = check_ticker(body["ticker"]) if body.get("ticker") else None
        if self.client_factory:
            client = self.client_factory()
        else:
            from ..agent import make_client
            client = make_client(self.settings)
        return ask(t, client, self.settings.claude_model, self._news(t), lookup=lookup)

    def tool(self, slug: str, body: dict[str, Any]) -> Any:
        t = self.trial(slug)
        kind, years = str(body.get("kind") or ""), int(body.get("years") or 3)
        if kind not in ("signal_lab", "factor_backtest"):
            raise ValueError("kind must be signal_lab or factor_backtest")

        def run(job: Any) -> str:
            from ..costs import cost_model_for
            members, membership = t.universe.members_on(t.clock.today), t.universe.membership
            if kind == "signal_lab":
                from ..signal_lab import format_signal_lab, run_signal_lab
                r = run_signal_lab(members, t.prices, years=years, membership=membership,
                                   cost_model=cost_model_for("in"))
                text = format_signal_lab(r)
            else:
                from ..factor_backtest import format_factor_backtest, run_factor_backtest
                r = run_factor_backtest(members, t.prices, top=t.data["top"], years=years, membership=membership,
                                        cost_model=cost_model_for("in"), capital=t.data["cash"],
                                        index_fund=BENCHMARKS[t.data["universe"]])
                text = format_factor_backtest(r)
            self._tool_results.setdefault(slug, {})[kind] = {"date": t.clock.today, "years": years, "text": text}
            return f"{kind.replace('_', ' ')} as of {t.clock.today} finished"
        return self._background(slug, "replay_tool", run, "a tool")

    def tools(self, slug: str) -> dict[str, Any]:
        with self._guard(slug):
            return self._tools(slug)

    def _tools(self, slug: str) -> dict[str, Any]:
        t = self.trial(slug)
        return {k: v for k, v in self._tool_results.get(slug, {}).items() if v["date"] == t.clock.today}

    # -- the page's data ------------------------------------------------------------
    def snapshot(self, slug: str) -> dict[str, Any]:
        with self._guard(slug):
            return self._snapshot(slug)

    def _snapshot(self, slug: str) -> dict[str, Any]:
        t = self.trial(slug)
        d, eq = t.data, t.data["equity"]
        positions = []
        for p in t.you.positions():
            row = p.to_dict()
            try:
                row["last_trade"] = t.prices.last_trade_date(p.symbol)
                row["stop"] = round(trailing_stop(p.high_water or p.current_price or p.avg_entry_price,
                                                  atr(t.prices.history(p.symbol, "1y"))), 2)
            except Exception:  # noqa: BLE001 - no history (offline): show the position anyway
                row["last_trade"], row["stop"] = None, None
            row["suspended"] = bool(row["last_trade"]) and (date.fromisoformat(t.clock.today) - date.fromisoformat(row["last_trade"])).days > 7
            positions.append(row)
        agent_eq = t.agent.account().equity
        holdings = [{"symbol": p.symbol, "qty": p.qty, "value": round(p.market_value or 0, 2),
                     "weight": (p.market_value or 0) / agent_eq if agent_eq else None} for p in t.agent.positions()]
        held = {h["symbol"] for h in holdings}
        picks = d.get("picks")
        if picks:
            names = [r["symbol"] for r in picks["rows"]][:d["top"]]
            picks = {**picks, "will_buy": [s for s in names if s not in held], "will_sell": sorted(held - set(names))}
        y, m = int(t.clock.today[:4]), int(t.clock.today[5:7])
        tiles = {}
        for w in PORTFOLIOS:
            vals = [p[w] for p in eq]
            peak, worst = vals[0], 0.0
            for v in vals:
                peak = max(peak, v)
                worst = min(worst, v / peak - 1)
            tiles[w] = {"value": vals[-1], "return": vals[-1] / d["cash"] - 1, "worst_fall": worst}
        ended = d["ended"]
        return {
            "trial": {k: d[k] for k in ("name", "slug", "start", "clock", "universe", "benchmark", "top",
                                        "dividends", "auto_stop", "ended", "cash")},
            "race": {"dates": [p["date"] for p in eq], **{w: [p[w] for p in eq] for w in PORTFOLIOS}},
            "tiles": tiles,
            "you": {"cash": t.you.account().cash, "equity": t.you.account().equity, "positions": positions},
            "agent": {"holdings": holdings, "last_rebalance": d["rebalances"][-1] if d["rebalances"] else None,
                      "next_rebalance": f"{y + (m == 12):04d}-{m % 12 + 1:02d}"},
            "picks": picks, "stops": d["stops"][-20:], "claude": d["claude"],
            "claude_presses": d.get("claude_presses", 0), "claude_ready": bool(self.settings.anthropic_api_key),
            "scorecard": d.get("scorecard") if ended else None,
            "next": self._next(t) if ended else None,
            "universes": list(BENCHMARKS),
        }

    def _next(self, t: Trial) -> dict[str, Any]:
        try:
            return what_happened_next(t, self.source, self.today_fn())
        except Exception as e:  # noqa: BLE001 - an offline reopen must still show the stored scorecard
            return {"error": f"What happened next needs price data that could not be loaded: {e}"}

    # -- routing -------------------------------------------------------------------
    def route(self, method: str, path: str, query: dict[str, str], body: dict[str, Any] | None) -> tuple[int, Any]:
        if body is not None and not isinstance(body, dict):
            return 400, {"error": "request body must be a JSON object"}
        body = body or {}
        try:
            if method == "GET" and path == "/replay/api/trials":
                return 200, self.list()
            if method == "GET" and path == "/replay/api/meta":
                return 200, {"earliest": EARLIEST_START, "today": self.today_fn(), "universes": list(BENCHMARKS),
                             "claude_ready": bool(self.settings.anthropic_api_key)}
            if method == "POST" and path == "/replay/api/trials":
                return 202, self.create(body).to_dict()
            m = re.match(r"^/replay/api/job/(\d+)$", path)
            if method == "GET" and m:
                job = next((j for j in self.app.jobs if j.id == int(m.group(1))), None)
                return (200, job.to_dict()) if job else (404, {"error": "no such job"})
            m = re.match(r"^/replay/api/trial/([a-z0-9-]+)(?:/([a-z-]+))?$", path)
            if not m:
                return 404, {"error": "not found"}
            slug, action = m.group(1), m.group(2) or ""
            if method == "GET" and action == "":
                return 200, self.snapshot(slug)
            if method == "GET" and action == "lookup":
                t = (query.get("ticker") or "").strip()
                if not t:
                    raise ValueError("ticker required")
                return 200, self.lookup(slug, t)
            if method == "GET" and action == "tools":
                return 200, self.tools(slug)
            if method == "POST" and action == "order":
                return 200, self.order(slug, body)
            if method == "POST" and action == "step":
                return 202, self.step(slug, body).to_dict()
            if method == "POST" and action == "auto-stop":
                return 200, self.set_auto_stop(slug, body.get("on") in (True, "true", 1, "on"))
            if method == "POST" and action == "end":
                return 200, self.end(slug)
            if method == "POST" and action == "ask":
                return 200, self.ask(slug, body)
            if method == "POST" and action == "tool":
                return 202, self.tool(slug, body).to_dict()
            return 404, {"error": "not found"}
        except NotFound as e:
            return 404, {"error": str(e)}
        except ReplayBusy as e:
            return 409, {"error": str(e)}
        except PermissionError as e:
            return 403, {"error": str(e)}
        except (ValueError, LookupError, KeyError) as e:
            return 400, {"error": str(e)}
        except Exception as e:  # noqa: BLE001
            log.exception("replay route %s %s failed", method, path)
            return 500, {"error": f"{type(e).__name__}: {e}"}


class _LazyUniverse:
    """Membership is needed only when the agent trades or a tool runs, not to show a snapshot,
    so a reopened trial doesn't download index lists just to display."""

    def __init__(self, rapp: ReplayApp, root: Any):
        self.rapp, self.root, self._u = rapp, root, None

    def _get(self) -> Any:
        if self._u is None:
            import json
            name = json.loads((self.root / "trial.json").read_text(encoding="utf-8"))["universe"]
            self._u = self.rapp._universe(name)
        return self._u

    def members_on(self, day: str) -> list[dict[str, str]]:
        return self._get().members_on(day)

    @property
    def current(self) -> list[dict[str, str]]:
        return self._get().current

    @property
    def membership(self) -> Any:
        return self._get().membership
