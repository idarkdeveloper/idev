"""Local web dashboard: ``python -m trading_agent ui``.

A small stdlib HTTP server that serves ``ui/index.html`` and a JSON API over the same
objects the CLI uses. The order endpoints only work against the local paper
simulator. Live Groww orders (placed by the agent when GROWW_LIVE_ORDERS=true and
AUTO_TRADE=true) are shown read-only in the order history, with their status, and
each holding shows its GTT stop-loss status.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from .broker import Broker, LocalPaperBroker
from .config import Settings, load_settings
from .investors import classify_client
from .quiver import DisclosedTrade
from .momentum import MomentumScreen, momentum_summary
from .runner import check, free_prices, make_broker, make_data_source, make_notifier
from .state import State
from .watch import Watcher

log = logging.getLogger(__name__)

DEALS_TTL_SECONDS = 600
# The only static files besides the page: Inter, served locally so the page needs no network.
FONT_FILES = {"/fonts/inter-latin.woff2", "/fonts/inter-latin-ext.woff2"}
STATIC_FILES = {"/static/nocturne.css": ("nocturne.css", "text/css; charset=utf-8"),
                "/static/common.js": ("common.js", "text/javascript; charset=utf-8"),
                "/static/replay.js": ("replay.js", "text/javascript; charset=utf-8")}
EDITABLE_ENV_KEYS = {
    "watch_investor": "WATCH_INVESTOR",
    "watch_source": "WATCH_SOURCE",
    "auto_trade": "AUTO_TRADE",
    "notify_email_to": "NOTIFY_EMAIL_TO",
    "notify_webhook_url": "NOTIFY_WEBHOOK_URL",
    "market": "MARKET",
    "paper_starting_cash": "PAPER_STARTING_CASH",
    # Only has an effect when GROWW_LIVE_ORDERS=true, which the dashboard can never set.
    "groww_gtt_stops": "GROWW_GTT_STOPS",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Job:
    id: int
    kind: str
    started_at: str = field(default_factory=_now)
    finished_at: str | None = None
    ok: bool | None = None
    message: str = ""
    result: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class App:
    """Everything the HTTP handler needs, shared across requests."""

    def __init__(self, settings: Settings, *, broker: Broker | None = None,
                 data: Any | None = None, demo_trades: list[DisclosedTrade] | None = None,
                 dotenv: Path | None = Path(".env"), context: Any | None = None,
                 prices: Any | None = None):
        self.settings = settings
        self.dotenv = dotenv
        self._broker = broker
        self._data = data
        self.context = context  # GlobalContext or None
        self.prices = prices or free_prices(settings)
        self.momentum = MomentumScreen(self.prices)
        self.watcher: Watcher | None = None
        self.last_backtest: dict[str, Any] | None = None
        self.last_screen: dict[str, Any] | None = None
        self.last_factor_bt: dict[str, Any] | None = None
        self.last_signal_lab: dict[str, Any] | None = None
        self._my_portfolio: dict[str, Any] | None = None
        self._names: Any | None = None  # CompanyNames, built on first use
        self._my_portfolio_at = 0.0
        self.demo_trades = demo_trades
        self._deals: list[DisclosedTrade] | None = demo_trades
        self._deals_at = time.time() if demo_trades else 0.0
        self._deals_error: str | None = None
        self.jobs: list[Job] = []
        self.lock = threading.Lock()
        self.busy = False
        self.running: Job | None = None  # the job holding the one slot
        self._replay: Any | None = None  # ReplayApp, built on first use
        self._demo: "App | None" = None
        self._lazy_lock = threading.Lock()

    @property
    def demo(self) -> "App":
        """The Demo page's app: bundled sample deals and prices, its own state, no Groww, no .env writes."""
        if self.demo_trades is not None:
            return self  # started with --demo: this app is the demo
        with self._lazy_lock:
            if self._demo is None:
                import dataclasses
                from .cli import _demo_inputs
                s = dataclasses.replace(self.settings, state_dir=self.settings.state_dir / "demo", broker="local",
                                        groww_access_token=None, groww_api_key=None, groww_api_secret=None,
                                        groww_totp_secret=None, groww_live_orders=False,
                                        resend_api_key=None, notify_email_to=None, notify_webhook_url=None)
                trades, broker = _demo_inputs(s)
                self._demo = App(s, broker=broker, demo_trades=trades, dotenv=None, context=self.context)
            return self._demo

    # -- lazy singletons ------------------------------------------------------
    @property
    def broker(self) -> Broker:
        if self._broker is None:
            self._broker = make_broker(self.settings)
        return self._broker

    @property
    def data(self) -> Any | None:
        if self._data is None and self.demo_trades is None:
            self._data = make_data_source(self.settings)
        return self._data

    @property
    def paper_only(self) -> bool:
        return isinstance(self.broker, LocalPaperBroker)

    JOB_LABELS = {"check": "A check", "dry_run": "A dry run", "backtest": "The deal backtest",
                  "screen": "The factor screen", "factor_backtest": "The portfolio backtest",
                  "signal_lab": "The signal lab", "replay_create": "Starting a replay",
                  "replay_step": "A replay step", "replay_tool": "A replay tool"}

    def _refused(self, job: Job) -> Job:
        """One long job at a time. A refused attempt is answered but not recorded, so it
        can't later be reported as the result of the job that was actually running."""
        r = self.running
        what = self.JOB_LABELS.get(r.kind, "Another job") if r else "Another job"
        since = f" (started {r.started_at[11:16]} UTC)" if r and r.started_at else ""
        job.ok, job.finished_at = False, _now()
        job.message = f"{what} is still running{since}; try again when it finishes."
        return job

    def run_background(self, kind: str, fn: Callable[[Job], str]) -> Job:
        """Run ``fn(job)`` in the one job slot; it returns the success message."""
        job = Job(id=len(self.jobs) + 1, kind=kind)
        if self.busy:
            return self._refused(job)
        self.jobs.append(job)
        self.busy, self.running = True, job

        def run() -> None:
            try:
                job.message = fn(job) or "done"
                job.ok = True
            except Exception as e:  # noqa: BLE001
                log.exception("%s failed", kind)
                job.ok, job.message = False, f"{type(e).__name__}: {e}" if not isinstance(e, (ValueError, LookupError)) else str(e)
            finally:
                job.finished_at = _now()
                self.busy = False

        threading.Thread(target=run, daemon=True).start()
        return job

    @property
    def replay(self) -> Any:
        with self._lazy_lock:
            if self._replay is None:
                from .replay.web import ReplayApp
                self._replay = ReplayApp(self)
            return self._replay

    # -- deals ----------------------------------------------------------------
    def deals(self, refresh: bool = False) -> list[DisclosedTrade]:
        fresh = self._deals is not None and time.time() - self._deals_at < DEALS_TTL_SECONDS
        if self.demo_trades is not None or (fresh and not refresh):
            return self._deals or []
        try:
            self._deals = self.data.trades_for_investor(self.settings.watch_investor,
                                                        self.settings.watch_source)
            self._deals_at = time.time()
            self._deals_error = None
        except Exception as e:  # noqa: BLE001
            self._deals_error = str(e)
            log.warning("deal fetch failed: %s", e)
            self._deals = self._deals or []
        return self._deals

    # -- snapshot for the page -----------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        st = State(self.settings.state_dir / "state.json")
        s = self.settings
        deals = []
        for t in self.deals():
            d = t.to_dict()
            d.pop("raw", None)
            d["status"] = "new" if t.key not in st.data["seen"] else "analysed"
            d["client_type"] = classify_client(t.investor, t.source)
            deals.append(d)
        try:
            acct = self.broker.account().to_dict()
            positions = [p.to_dict() for p in self.broker.positions()]
            broker_error = None
        except Exception as e:  # noqa: BLE001
            acct, positions, broker_error = None, [], str(e)
        perf = self.broker.performance() if isinstance(self.broker, LocalPaperBroker) else None
        since = self.broker.created_at if isinstance(self.broker, LocalPaperBroker) else None
        gtt = st.data.get("gtt_stops", {})
        for pos in positions:  # trailing-stop level and GTT status for the table
            g = gtt.get(pos["symbol"].upper())
            pos["gtt"] = ({k: g.get(k) for k in ("status", "trigger", "limit", "qty", "smart_order_id", "last_error")}
                          if g else None)
            try:
                from .risk import atr, trailing_stop
                bars = self.prices.history(pos["symbol"], "1y") if not self.demo_trades else []
                a = atr(bars) if bars else None
                high = pos.get("high_water") or pos.get("current_price") or pos["avg_entry_price"]
                pos["stop"] = round(trailing_stop(high, a), 2)
            except Exception:  # noqa: BLE001
                pos["stop"] = None
        recs = []
        for i, r in enumerate(st.data["recommendations"]):
            recs.append({**r, "index": i})
        recs.reverse()
        if s.groww_live_orders and s.use_groww:
            mode = "live"
        elif s.auto_trade:
            mode = "paper"
        else:
            mode = "recommend"
        regime = None
        if self.context is not None:
            try:
                r = self.context.fetch()
                regime = {k: r[k] for k in ("regime", "score", "signals", "guidance", "summary", "markets")}
            except Exception as e:  # noqa: BLE001
                regime = {"error": str(e)}
        return {
            "now": _now(),
            "regime": regime,
            "watch": ({**self.watcher.status(), "auto_exit": self.watcher.auto_exit} if self.watcher
                      else {"on": False, "every": 60, "auto_exit": False}),
            "orders": self.orders(),
            "forward": self.forward_summary(),
            "backtest": self.last_backtest,
            "screen": self.last_screen,
            "factor_backtest": self.last_factor_bt,
            "signal_lab": self.last_signal_lab,
            "equity_history": st.equity_history(since)[-1000:],
            "equity_stats": st.equity_stats(since),
            "costs": _cost_table(self.settings.market),
            "settings": {
                "market": s.market, "currency": s.currency, "watch_investor": s.watch_investor,
                "watch_source": s.watch_source, "data_source": s.data_source,
                "broker": getattr(self.broker, "name", s.broker), "mode": mode,
                "auto_trade": s.auto_trade, "claude_model": s.claude_model,
                "notify_email_to": s.notify_email_to or "",
                "notify_webhook_url": s.notify_webhook_url or "",
                "paper_starting_cash": s.paper_starting_cash,
                "groww_gtt_stops": s.groww_gtt_stops,
                "max_slippage_pct": s.max_slippage_pct,
                "demo": self.demo_trades is not None,
            },
            "connections": {
                "claude": bool(s.anthropic_api_key),
                "groww": s.use_groww,
                "groww_credentials": s.has_groww_credentials,
                "groww_live_orders": s.groww_live_orders,
                "groww_gtt_active": s.groww_gtt_stops and s.groww_live_orders and s.use_groww,
                "data": s.data_source,
                "prices": "groww" if s.use_groww else "yahoo",
            },
            "account": acct, "positions": positions, "performance": perf,
            "broker_error": broker_error, "deals": deals, "deals_error": self._deals_error,
            "recommendations": recs, "runs": list(reversed(st.data["runs"][-20:])),
            "seen_count": st.seen_count, "busy": self.busy,
            "running": ({"kind": self.running.kind, "label": self.JOB_LABELS.get(self.running.kind, self.running.kind),
                         "started_at": self.running.started_at} if self.busy and self.running else None),
            "jobs": [j.to_dict() for j in self.jobs[-5:]],
        }

    # -- actions --------------------------------------------------------------
    def start_check(self, *, force: bool, dry_run: bool) -> Job:
        job = Job(id=len(self.jobs) + 1, kind="dry_run" if dry_run else "check")
        if self.busy:
            return self._refused(job)
        self.jobs.append(job)
        self.busy, self.running = True, job

        def run() -> None:
            try:
                with self.lock:
                    trades = self.deals(refresh=True)
                    result = check(self.settings, force=force, dry_run=dry_run, trades=trades,
                                   broker=self.broker, data=self.data,
                                   notifier=make_notifier(self.settings))
                job.result = result.to_dict()
                job.ok = not result.refusal
                if result.skipped and not result.new_trades:
                    job.message = "nothing new since the last check"
                elif result.skipped:
                    job.message = f"{len(result.new_trades)} new deal(s); Claude not called (dry run)"
                else:
                    job.message = (f"{len(result.new_trades)} new deal(s), "
                                   f"{len(result.recommendations)} recommendation(s), "
                                   f"{len(result.orders)} order(s)")
            except Exception as e:  # noqa: BLE001
                log.exception("check failed")
                job.ok, job.message = False, f"{type(e).__name__}: {e}"
            finally:
                job.finished_at = _now()
                self.busy = False

        threading.Thread(target=run, daemon=True).start()
        return job

    @property
    def names(self) -> Any | None:
        """Company-name list for search; none in demo mode (no network needed there)."""
        if self.demo_trades is not None:
            return None
        if self._names is None:
            from .instruments import CompanyNames
            self._names = CompanyNames(self.settings.state_dir / "cache")
        return self._names

    def search(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        n = self.names
        return n.search(query, limit) if n is not None else []

    def resolve(self, text: str) -> tuple[str, str | None]:
        """A ticker, or a company name a person typed, to (ticker, company name)."""
        n = self.names
        if n is None:
            return text.strip().upper(), None
        try:
            return n.resolve(text)
        except Exception:  # noqa: BLE001 - name list unavailable: treat the text as a ticker
            return text.strip().upper(), None

    def lookup(self, ticker: str) -> dict[str, Any]:
        typed = ticker
        ticker, company = self.resolve(ticker)
        stats = self.momentum.stats(ticker)
        out: dict[str, Any] = {"ticker": ticker.upper(), "name": company, "momentum": stats,
                               "matched_from": typed if typed.strip().upper() != ticker.upper() else None,
                               "momentum_summary": stats.get("error") or momentum_summary(stats),
                               "announcements": [], "announcements_error": None}
        data = self.data
        if data is not None and hasattr(data, "announcements"):
            try:
                out["announcements"] = data.announcements(ticker, limit=8)
            except Exception as e:  # noqa: BLE001
                out["announcements_error"] = str(e)
        try:
            out["price"] = self.broker.latest_price(ticker)
        except Exception:  # noqa: BLE001
            out["price"] = None
        out["history"] = []
        try:
            bars = self.prices.history(ticker, "2y")
            closes = [b["close"] for b in bars]
            pts = []
            for i in range(max(0, len(bars) - 252), len(bars)):
                ma = sum(closes[i - 199:i + 1]) / 200 if i >= 199 else None
                pts.append({"d": bars[i]["date"], "c": round(closes[i], 2), "ma200": round(ma, 2) if ma else None})
            out["history"] = pts
        except Exception as e:  # noqa: BLE001
            out["history_error"] = str(e)
        pos = next((p for p in self.broker.positions() if p.symbol == ticker.upper()), None) \
            if self.paper_only else None
        if pos is not None:
            from .risk import atr, trailing_stop
            try:
                a = atr(self.prices.history(ticker, "1y"))
            except Exception:  # noqa: BLE001
                a = None
            high = pos.high_water or pos.current_price or pos.avg_entry_price
            out["position"] = {"qty": pos.qty, "avg_entry_price": pos.avg_entry_price,
                               "stop": round(trailing_stop(high, a), 2)}
        return out

    def start_backtest(self, investor: str, days: int, horizons: tuple[int, ...], cost_bps: float) -> Job:
        from .backtest import run_backtest

        job = Job(id=len(self.jobs) + 1, kind="backtest")
        if self.busy:
            return self._refused(job)
        self.jobs.append(job)
        self.busy, self.running = True, job

        def run() -> None:
            try:
                if self.demo_trades is not None:
                    import dataclasses
                    import datetime as dt
                    from .cli import _DemoHistory
                    back = (dt.date.today() - dt.timedelta(days=100)).isoformat()
                    deals = [dataclasses.replace(d, transaction_date=back, report_date=back)
                             for d in self.demo_trades]
                    prices: Any = _DemoHistory(getattr(self.broker, "price_fn", None) or (lambda s: 100.0))
                else:
                    data = self.data
                    deals = data.trades_for_investor(investor, self.settings.watch_source, days=days) \
                        if self.settings.data_source == "nse" else data.trades_for_investor(investor, self.settings.watch_source)
                    prices = self.prices
                result = run_backtest(investor, deals, prices, horizons=horizons, cost_bps=cost_bps)
                self.last_backtest = {"at": _now(), "days": days, **result.to_dict()}
                job.result = self.last_backtest["summary"]
                job.ok = True
                job.message = f"{len(deals)} deals replayed for {investor}"
            except Exception as e:  # noqa: BLE001
                log.exception("backtest failed")
                job.ok, job.message = False, f"{type(e).__name__}: {e}"
            finally:
                job.finished_at = _now()
                self.busy = False

        threading.Thread(target=run, daemon=True).start()
        return job

    def start_screen(self, universe: str, top: int, quality: bool = False, value: bool = False) -> Job:
        from .screen import load_universe, run_screen

        job = Job(id=len(self.jobs) + 1, kind="screen")
        if self.busy:
            return self._refused(job)
        self.jobs.append(job)
        self.busy, self.running = True, job

        def run() -> None:
            try:
                members = load_universe(universe)
                funds = None
                if quality or value:
                    from .fundamentals import YahooFundamentals
                    funds = YahooFundamentals(self.settings.state_dir / "cache")
                result = run_screen(members, self.prices, top=top, fundamentals=funds,
                                    quality=1.0 if quality else 0.0, value=1.0 if value else 0.0)
                result.pop("all", None)
                self.last_screen = {"at": _now(), "universe": universe.upper(), **result}
                job.ok, job.message = True, f"{result['eligible']} eligible of {result['scored']} scored in {universe.upper()}"
            except Exception as e:  # noqa: BLE001
                log.exception("screen failed")
                job.ok, job.message = False, f"{type(e).__name__}: {e}"
            finally:
                job.finished_at = _now()
                self.busy = False

        threading.Thread(target=run, daemon=True).start()
        return job

    def set_watch(self, on: bool, every: int | None = None, auto_exit: bool | None = None) -> dict[str, Any]:
        if on:
            want_exit = self.settings.auto_trade if auto_exit is None else bool(auto_exit)
            if (self.watcher is None or (every and every != self.watcher.every)
                    or want_exit != self.watcher.auto_exit):
                if self.watcher:
                    self.watcher.stop()
                self.watcher = Watcher(
                    self.settings, every=every or 60, data=self.data, broker=self.broker,
                    notifier=make_notifier(self.settings), prices=self.prices,
                    auto_exit=want_exit,
                    # No Claude key: compare deals only, instead of failing every tick.
                    check_fn=lambda: check(self.settings, trades=self.deals(refresh=True), broker=self.broker,
                                           data=self.data, notifier=make_notifier(self.settings),
                                           momentum=self.momentum, context=self.context,
                                           dry_run=not self.settings.anthropic_api_key),
                )
            self.watcher.start()
        elif self.watcher:
            self.watcher.stop()
        if self.watcher:
            st = self.watcher.status()
            st["auto_exit"] = self.watcher.auto_exit
            return st
        return {"on": False, "every": every or 60, "auto_exit": bool(auto_exit)}

    def paper_order(self, symbol: str, side: str, notional: float | None = None,
                    qty: float | None = None) -> dict[str, Any]:
        if not self.paper_only:
            raise PermissionError("orders from the dashboard are allowed only on the paper simulator")
        symbol = str(symbol).strip().upper()
        if not symbol:
            raise ValueError("symbol required")
        if side not in ("buy", "sell"):
            raise ValueError("side must be buy or sell")
        if qty not in (None, "", 0, "0"):
            order = self.broker.submit_order(symbol, side, qty=float(qty))
        elif notional in (None, "", 0, "0"):
            raise ValueError("enter an amount or a quantity")
        else:
            order = self.broker.submit_order(symbol, side, notional=float(notional))
        self._record_equity()
        return order

    def _record_equity(self) -> None:
        from .runner import record_equity
        st = State(self.settings.state_dir / "state.json")
        record_equity(st, self.broker)
        st.save()

    def close_position(self, symbol: str) -> dict[str, Any]:
        symbol = symbol.upper()
        pos = next((p for p in self.broker.positions() if p.symbol == symbol), None)
        if pos is None:
            raise LookupError(f"no open position in {symbol}")
        return self.paper_order(symbol, "sell", qty=pos.qty)

    def dismiss(self, index: int) -> bool:
        st = State(self.settings.state_dir / "state.json")
        ok = st.dismiss_recommendation(index)
        if ok:
            st.save()
        return ok

    def reset(self) -> list[str]:
        paths = [self.settings.state_dir / "state.json", self.settings.state_dir / "paper_broker.json"]
        if isinstance(self._broker, LocalPaperBroker):
            paths.append(self._broker.path)
        removed = []
        for p in paths:
            if p.exists() and p.name not in removed:
                p.unlink()
                removed.append(p.name)
        if isinstance(self._broker, LocalPaperBroker):
            old = self._broker
            self._broker = LocalPaperBroker(old.path, price_fn=old.price_fn, currency=old.currency,
                                            whole_shares=old.whole_shares, cost_model=old.cost_model,
                                            starting_cash=self.settings.paper_starting_cash)
        elif self.demo_trades is None:
            self._broker = None
        return removed

    def update_settings(self, changes: dict[str, Any]) -> dict[str, Any]:
        applied: dict[str, str] = {}
        for key, env_key in EDITABLE_ENV_KEYS.items():
            if key not in changes:
                continue
            value = changes[key]
            if key in ("auto_trade", "groww_gtt_stops"):
                value = "true" if value in (True, "true", "1", 1, "on") else "false"
                setattr(self.settings, key, value == "true")
            elif key == "market":
                value = str(value).strip().lower()
                if value not in ("in", "us"):
                    raise ValueError("market must be 'in' or 'us'")
                if value == self.settings.market:
                    continue
                self._switch_market(value)
            elif key == "paper_starting_cash":
                cash = float(value)
                if cash <= 0:
                    raise ValueError("starting cash must be positive")
                self.settings.paper_starting_cash = cash
                value = f"{cash:g}"
            else:
                value = str(value).strip()
                setattr(self.settings, key, (value or None) if key.startswith("notify") else value)
            applied[env_key] = value
        if "watch_investor" in changes or "watch_source" in changes:
            self._deals, self._deals_at = None, 0.0
        if applied and self.dotenv is not None:
            _write_env(self.dotenv, applied)
        return applied

    def _switch_market(self, market: str) -> None:
        """Rebuild everything that depends on the market: data source, broker, prices."""
        s = self.settings
        if self.watcher:
            self.watcher.stop()
            self.watcher = None
        s.market = market
        s.data_source = "nse" if market == "in" else "quiver"
        s.watch_source = "deals" if market == "in" else "congress"
        if market == "us" and s.broker == "groww":
            s.broker = "local"
        self.prices = free_prices(s)
        self.momentum = MomentumScreen(self.prices)
        if self.demo_trades is None:
            self._broker = None
            self._data = None
        self._deals, self._deals_at = None, 0.0
        self.last_screen = None
        self.last_backtest = None

    # -- calculators and tools ------------------------------------------------
    def cost_quote(self, amount: float) -> dict[str, Any]:
        from .costs import cost_model_for
        if amount <= 0:
            raise ValueError("amount must be positive")
        m = cost_model_for(self.settings.market)
        if not hasattr(m, "round_trip"):
            bps = m.round_trip_bps(amount)
            return {"amount": amount, "model": "flat", "total_bps": bps, "total": amount * bps / 10_000}
        rt = m.round_trip(amount)
        return {"amount": amount, "model": "india_delivery", "buy": rt["buy"], "sell": rt["sell"],
                "charges": rt["charges"], "charges_bps": rt["charges_bps"], "total": rt["total"],
                "total_bps": rt["total_bps"], "slippage_bps_one_way": m.slippage_bps}

    def size_quote(self, ticker: str, equity: float | None = None, risk_pct: float = 1.0,
                   max_pct: float = 10.0) -> dict[str, Any]:
        from .risk import atr, position_size
        ticker, company = self.resolve(ticker)
        eq = equity or self.broker.account().equity
        price = self.broker.latest_price(ticker)
        try:
            a = atr(self.prices.history(ticker, "1y")) if not self.demo_trades else None
        except Exception:  # noqa: BLE001
            a = None
        r = position_size(eq, price, a, risk_pct=risk_pct / 100, max_pct=max_pct / 100,
                          whole_shares=self.settings.market == "in")
        r.update(ticker=ticker.upper(), equity=eq, name=company)
        if r["notional"]:
            try:
                r["round_trip_cost"] = self.cost_quote(r["notional"])
            except Exception:  # noqa: BLE001
                pass
        return r

    def scorecard(self) -> dict[str, Any]:
        from .costs import cost_model_for
        from .scorecard import score_recommendations
        st = State(self.settings.state_dir / "state.json")
        recs = st.data.get("recommendations", [])
        bench = "^NSEI" if self.settings.market == "in" else "^GSPC"
        cm = cost_model_for(self.settings.market)
        return score_recommendations(recs, self.prices, benchmark=bench,
                                     cost_model=cm if hasattr(cm, "round_trip") else None)

    def start_factor_backtest(self, universe: str, top: int, years: int) -> Job:
        from .costs import cost_model_for
        from .factor_backtest import INDEX_FUNDS, run_factor_backtest
        from .index_history import point_in_time
        from .screen import load_universe

        job = Job(id=len(self.jobs) + 1, kind="factor_backtest")
        if self.busy:
            return self._refused(job)
        self.jobs.append(job)
        if self.settings.market != "in":
            job.ok, job.message, job.finished_at = False, "the factor backtest uses NSE indices; switch market to India", _now()
            return job
        self.busy, self.running = True, job

        def run() -> None:
            try:
                members = load_universe(universe)
                r = run_factor_backtest(members, self.prices, top=top, years=years,
                                        cost_model=cost_model_for("in"),
                                        capital=self.settings.paper_starting_cash,
                                        membership=point_in_time(universe, [m["symbol"] for m in members],
                                                                 self.settings.state_dir),
                                        index_fund=INDEX_FUNDS.get(universe.upper()))
                self.last_factor_bt = {"at": _now(), "universe": universe.upper(), **r}
                s_ = r["stats"]
                job.ok = True
                vs = (f"index fund {r['index_fund_symbol']} {s_['index_fund']['total_return']*100:+.1f}%"
                      if "index_fund" in s_ else f"NIFTY 50 {s_['benchmark']['total_return']*100:+.1f}%")
                job.message = (f"{universe.upper()} top {top}: strategy {s_['strategy']['total_return']*100:+.1f}% vs "
                               f"{vs} over {r['months']} months")
            except Exception as e:  # noqa: BLE001
                log.exception("factor backtest failed")
                job.ok, job.message = False, f"{type(e).__name__}: {e}"
            finally:
                job.finished_at = _now()
                self.busy = False

        threading.Thread(target=run, daemon=True).start()
        return job

    def start_signal_lab(self, universe: str, years: int, horizons: list[int]) -> Job:
        from .costs import cost_model_for
        from .index_history import point_in_time
        from .screen import load_universe
        from .signal_lab import run_signal_lab

        job = Job(id=len(self.jobs) + 1, kind="signal_lab")
        if self.busy:
            return self._refused(job)
        self.jobs.append(job)
        if self.settings.market != "in":
            job.ok, job.message, job.finished_at = False, "the signal lab uses NSE indices; switch market to India", _now()
            return job
        self.busy, self.running = True, job

        def run() -> None:
            try:
                members = load_universe(universe)
                r = run_signal_lab(members, self.prices, horizons=horizons, years=years,
                                   membership=point_in_time(universe, [m["symbol"] for m in members],
                                                            self.settings.state_dir),
                                   cost_model=cost_model_for("in"))
                self.last_signal_lab = {"at": _now(), "universe": universe.upper(), "years": years, **r}
                job.ok, job.message = True, r["summary"]
            except Exception as e:  # noqa: BLE001
                log.exception("signal lab failed")
                job.ok, job.message = False, f"{type(e).__name__}: {e}"
            finally:
                job.finished_at = _now()
                self.busy = False

        threading.Thread(target=run, daemon=True).start()
        return job

    def my_portfolio(self, refresh: bool = False) -> dict[str, Any]:
        """Your real Groww holdings with buy price, current price and profit or loss.

        Read-only (the client is built with live orders off). Cached for a minute,
        since the page polls; refresh=True fetches again.
        """
        if self._my_portfolio is not None and not refresh and time.time() - self._my_portfolio_at < 60:
            return self._my_portfolio
        s = self.settings
        if not s.has_groww_credentials:
            return {"linked": False}
        from .groww import GrowwBroker
        from .runner import resolve_groww_token
        from .instruments import CompanyNames, nse_then_bse
        from .prices import YahooPrices
        bse = YahooPrices(suffix=".BO", cache_dir=s.state_dir / "cache")
        try:
            g = GrowwBroker(resolve_groww_token(s), live_orders=False, exchange=s.groww_exchange,
                            price_fallback=nse_then_bse(self.prices, bse))
            positions = g.positions()
        except SystemExit as e:
            return {"linked": True, "error": str(e)}
        except Exception as e:  # noqa: BLE001
            return {"linked": True, "error": f"{type(e).__name__}: {e}"}
        try:
            names = CompanyNames(s.state_dir / "cache").lookup([p.symbol for p in positions])
        except Exception:  # noqa: BLE001 - names are optional
            names = {}
        rows = []
        for p in positions:
            invested = p.qty * p.avg_entry_price
            value = p.qty * p.current_price if p.current_price is not None else None
            info = names.get(p.symbol.upper()) or {}
            rows.append({"symbol": p.symbol, "name": info.get("name"), "exchange": info.get("exchange"),
                         "kind": info.get("kind") or "equity", "maturity": info.get("maturity"),
                         "qty": p.qty, "sellable_qty": p.free_qty,
                         "avg_price": p.avg_entry_price, "price": p.current_price,
                         "invested": round(invested, 2), "value": round(value, 2) if value is not None else None,
                         "pl": round(value - invested, 2) if value is not None else None,
                         "pl_pct": (p.current_price / p.avg_entry_price - 1)
                         if p.current_price is not None and p.avg_entry_price else None})
        rows.sort(key=lambda r: -(r["value"] if r["value"] is not None else r["invested"]))
        priced = [r for r in rows if r["value"] is not None]
        inv_priced = sum(r["invested"] for r in priced)
        value = sum(r["value"] for r in priced)
        out = {"linked": True, "at": _now(), "holdings": rows,
               "invested": round(sum(r["invested"] for r in rows), 2),
               "value": round(value, 2), "pl": round(value - inv_priced, 2),
               "pl_pct": (value / inv_priced - 1) if inv_priced else None,
               "unpriced": [r["symbol"] for r in rows if r["value"] is None]}
        self._my_portfolio, self._my_portfolio_at = out, time.time()
        return out

    def groww_test(self) -> dict[str, Any]:
        """Check Groww credentials end to end without ever returning the token."""
        from .groww import GrowwBroker
        from .runner import resolve_groww_token
        s = self.settings
        if not s.has_groww_credentials:
            return {"ok": False, "message": "No Groww credentials in .env (GROWW_ACCESS_TOKEN, or GROWW_API_KEY "
                                            "with GROWW_API_SECRET or GROWW_TOTP_SECRET)."}
        try:
            token = resolve_groww_token(s)
            g = GrowwBroker(token, live_orders=False, exchange=s.groww_exchange, price_fallback=self.prices)
            holdings = g.holdings()
            acct = g.account()
        except SystemExit as e:
            return {"ok": False, "message": str(e)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": f"{type(e).__name__}: {e}"}
        return {"ok": True, "message": f"Connected: {len(holdings)} holdings, cash {acct.cash:,.2f} {acct.currency}",
                "holdings": len(holdings), "cash": acct.cash, "equity": acct.equity}

    def forward_summary(self) -> dict[str, Any] | None:
        """The forward test's standing from stored prices (no network: the page polls)."""
        from .forward import ForwardTest
        d = self.settings.state_dir / "forward"
        files = sorted(f for f in d.glob("*.json") if not f.stem.endswith("_broker")) if d.exists() else []
        if not files:
            return None
        try:
            s = ForwardTest(self.settings.state_dir, universe=files[0].stem.upper(), price_fn=None).summary()
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)}
        s["history"] = s["history"][-1000:]
        return s

    def orders(self, limit: int = 50) -> list[dict[str, Any]]:
        """Paper fills plus live Groww orders (from state.json), newest first."""
        out: list[dict[str, Any]] = []
        b = self.broker
        if isinstance(b, LocalPaperBroker):
            # newest first already, so the (stable) sort below keeps same-second fills in order
            out += [{**o, "at": o.get("filled_at")} for o in reversed(b.orders())]
        st = State(self.settings.state_dir / "state.json")
        for o in reversed(st.data.get("live_orders", [])):
            out.append({**o, "live": True, "at": o.get("placed_at"),
                        "filled_avg_price": o.get("average_fill_price") or o.get("limit_price"),
                        "notional": round((o.get("average_fill_price") or o.get("limit_price") or 0)
                                          * float(o.get("filled_quantity") or o.get("qty") or 0), 2)})
        def when(o: dict[str, Any]) -> datetime:
            try:
                dt = datetime.fromisoformat(str(o.get("at")))
                return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            except ValueError:
                return datetime.min.replace(tzinfo=timezone.utc)
        out.sort(key=when, reverse=True)
        return out[:limit]


def _cost_table(market: str) -> dict[str, Any]:
    from .costs import cost_model_for
    m = cost_model_for(market)
    if not hasattr(m, "round_trip"):
        return {"model": "flat", "round_trip_bps": m.round_trip_bps(0)}
    return {"model": "india_delivery", "examples": {str(n): round(m.round_trip_bps(n), 1) for n in (10_000, 25_000, 100_000)},
            "slippage_bps_one_way": m.slippage_bps}


def _write_env(path: Path, values: dict[str, str]) -> None:
    """Upsert KEY=value lines; keeps comments and other keys as they are."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text().splitlines() if path.exists() else []
    done: set[str] = set()
    out = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if key in values:
            out.append(f"{key}={values[key]}")
            done.add(key)
        else:
            out.append(line)
    for key, value in values.items():
        if key not in done:
            out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n")


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def make_handler(app: App) -> type[BaseHTTPRequestHandler]:
    index_html = (resources.files("trading_agent") / "ui" / "index.html").read_text(encoding="utf-8")
    demo_html = index_html.replace("<body>", '<body data-api="/demo" data-mode="demo">', 1)
    replay_html = (resources.files("trading_agent") / "ui" / "replay.html").read_text(encoding="utf-8")

    class Handler(BaseHTTPRequestHandler):
        server_version = "trading-agent-ui/1"

        def log_message(self, fmt: str, *args: Any) -> None:  # quieter default log
            log.debug("%s - " + fmt, self.address_string(), *args)

        # helpers
        def _json(self, payload: Any, status: int = 200) -> None:
            body = json.dumps(payload, default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _bytes(self, body: bytes, ctype: str, cache: str = "no-cache") -> None:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.end_headers()
            self.wfile.write(body)

        def _replay(self, method: str) -> bool:
            from urllib.parse import parse_qs
            u = urlparse(self.path)
            if u.path in ("/replay", "/replay/"):
                self._bytes(replay_html.encode(), "text/html; charset=utf-8")
                return True
            if not u.path.startswith("/replay/api/"):
                return False
            body = self._body() if method == "POST" else None
            status, payload = app.replay.route(method, u.path, {k: v[0] for k, v in parse_qs(u.query).items()}, body)
            self._json(payload, status)
            return True

        def _body(self) -> dict[str, Any]:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            return json.loads(raw.decode() or "{}")

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path in STATIC_FILES:
                name, ctype = STATIC_FILES[path]
                try:
                    data = (resources.files("trading_agent") / "ui" / name).read_bytes()
                except FileNotFoundError:  # not shipped yet
                    self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                    return
                self._bytes(data, ctype)
                return
            if self._replay("GET"):
                return
            target = app
            if path == "/demo" or path.startswith("/demo/"):
                target, path = app.demo, (path[5:] or "/")
                if path in ("/", "/index.html"):
                    self._bytes(demo_html.encode(), "text/html; charset=utf-8")
                    return
            self._get(target, path)

        def _get(self, app: App, path: str) -> None:
            if path in ("/", "/index.html"):
                body = index_html.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path in FONT_FILES:
                body = (resources.files("trading_agent") / "ui" / "fonts" / path.rsplit("/", 1)[-1]).read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "font/woff2")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "public, max-age=604800")
                self.end_headers()
                self.wfile.write(body)
            elif path == "/favicon.ico":
                self.send_response(HTTPStatus.NO_CONTENT)
                self.end_headers()
            elif path == "/api/state":
                self._json(app.snapshot())
            elif path == "/api/my-portfolio":
                from urllib.parse import parse_qs
                q = parse_qs(urlparse(self.path).query)
                self._json(app.my_portfolio(refresh=(q.get("refresh") or ["0"])[0] in ("1", "true")))
            elif path == "/api/jobs":
                self._json([j.to_dict() for j in app.jobs])
            elif path == "/api/regime":
                from urllib.parse import parse_qs
                force = (parse_qs(urlparse(self.path).query).get("refresh") or ["0"])[0] in ("1", "true")
                self._json(app.context.fetch(force=force) if app.context else {"error": "not configured"})
            elif path == "/api/search":
                from urllib.parse import parse_qs
                q = (parse_qs(urlparse(self.path).query).get("q") or [""])[0]
                self._json(app.search(q))
            elif path == "/api/lookup":
                from urllib.parse import parse_qs
                ticker = (parse_qs(urlparse(self.path).query).get("ticker") or [""])[0].strip()
                if not ticker:
                    self._json({"error": "ticker required"}, HTTPStatus.BAD_REQUEST)
                else:
                    self._json(app.lookup(ticker))
            elif path == "/api/backtest":
                self._json(app.last_backtest or {})
            elif path == "/api/screen":
                self._json(app.last_screen or {})
            elif path == "/api/scorecard":
                self._json(app.scorecard())
            elif path == "/api/factor-backtest":
                self._json(app.last_factor_bt or {})
            elif path == "/api/signal-lab":
                self._json(app.last_signal_lab or {})
            elif path in ("/api/costs", "/api/size", "/api/orders"):
                from urllib.parse import parse_qs
                q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
                try:
                    if path == "/api/costs":
                        self._json(app.cost_quote(float(q.get("amount") or 0)))
                    elif path == "/api/size":
                        t = (q.get("ticker") or "").strip()
                        if not t:
                            raise ValueError("ticker required")
                        self._json(app.size_quote(t, float(q["equity"]) if q.get("equity") else None,
                                                  float(q.get("risk_pct") or 1.0), float(q.get("max_pct") or 10.0)))
                    else:
                        self._json(app.orders(int(q.get("limit") or 50)))
                except (ValueError, LookupError) as e:
                    self._json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
            else:
                self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path.startswith("/replay/api/"):
                try:
                    self._replay("POST")
                except (json.JSONDecodeError, ValueError) as e:  # bad JSON or Content-Length
                    self._json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
                return
            target = app
            if path == "/demo" or path.startswith("/demo/"):
                target, path = app.demo, (path[5:] or "/")
            self._post(target, path)

        def _post(self, app: App, path: str) -> None:
            try:
                body = self._body()
                if path == "/api/check":
                    job = app.start_check(force=bool(body.get("force")), dry_run=bool(body.get("dry_run")))
                    self._json(job.to_dict(), HTTPStatus.ACCEPTED)
                elif path == "/api/order":
                    order = app.paper_order(body.get("symbol", ""), body.get("side", "buy"),
                                            body.get("notional"), body.get("qty"))
                    self._json({"ok": True, "order": order})
                elif path == "/api/close":
                    self._json({"ok": True, "order": app.close_position(str(body.get("symbol", "")))})
                elif path == "/api/groww-test":
                    self._json(app.groww_test())
                elif path == "/api/factor-backtest":
                    job = app.start_factor_backtest(str(body.get("universe") or "NIFTY200"),
                                                    int(body.get("top", 20)), int(body.get("years", 4)))
                    self._json(job.to_dict(), HTTPStatus.ACCEPTED)
                elif path == "/api/signal-lab":
                    hs = [int(h) for h in str(body.get("horizons") or "5,20,60").split(",") if h.strip()]
                    if not hs or any(h < 1 or h > 250 for h in hs):
                        raise ValueError("horizons must be trading days between 1 and 250")
                    job = app.start_signal_lab(str(body.get("universe") or "NIFTY50"),
                                               int(body.get("years", 5)), hs)
                    self._json(job.to_dict(), HTTPStatus.ACCEPTED)
                elif path == "/api/dismiss":
                    self._json({"ok": app.dismiss(int(body["index"]))})
                elif path == "/api/settings":
                    self._json({"ok": True, "applied": app.update_settings(body)})
                elif path == "/api/reset":
                    self._json({"ok": True, "removed": app.reset()})
                elif path == "/api/backtest":
                    horizons = tuple(int(h) for h in str(body.get("horizons", "5,20,60")).split(",") if h.strip())
                    job = app.start_backtest(str(body.get("investor") or app.settings.watch_investor),
                                             int(body.get("days", 365)), horizons or (5, 20, 60),
                                             float(body.get("cost_bps", 50)))
                    self._json(job.to_dict(), HTTPStatus.ACCEPTED)
                elif path == "/api/screen":
                    job = app.start_screen(str(body.get("universe") or "NIFTY200"), int(body.get("top", 20)),
                                           quality=body.get("quality") in (True, "on", "true", 1),
                                           value=body.get("value") in (True, "on", "true", 1))
                    self._json(job.to_dict(), HTTPStatus.ACCEPTED)
                elif path == "/api/watch":
                    self._json(app.set_watch(bool(body.get("on")), int(body["every"]) if body.get("every") else None,
                                             body.get("auto_exit")))
                else:
                    self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            except PermissionError as e:
                self._json({"error": str(e)}, HTTPStatus.FORBIDDEN)
            except (KeyError, ValueError, LookupError, json.JSONDecodeError) as e:
                self._json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
            except Exception as e:  # noqa: BLE001
                log.exception("request failed")
                self._json({"error": f"{type(e).__name__}: {e}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    return Handler


class _Server(ThreadingHTTPServer):
    def handle_error(self, request: Any, client_address: Any) -> None:
        # The browser gave up on a request (page reloaded or closed mid-reply): nothing is
        # wrong, so don't print a traceback. Anything else is still reported.
        if isinstance(sys.exc_info()[1], (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
            log.debug("client %s closed the connection early", client_address)
            return
        super().handle_error(request, client_address)


def make_server(app: App, host: str = "127.0.0.1", port: int = 8787) -> ThreadingHTTPServer:
    server = _Server((host, port), make_handler(app))
    server.daemon_threads = True
    return server


def serve(settings: Settings | None = None, *, host: str = "127.0.0.1", port: int = 8787,
          open_browser: bool = True, demo: bool = False) -> None:
    settings = settings or load_settings()
    kwargs: dict[str, Any] = {}
    if demo:
        from .cli import _demo_inputs

        trades, broker = _demo_inputs(settings)
        kwargs.update(demo_trades=trades, broker=broker)
    from .regime import GlobalContext
    from .prices import YahooPrices

    kwargs["context"] = GlobalContext(YahooPrices(suffix="", cache_dir=settings.state_dir / "cache", cache_ttl=900))
    app = App(settings, **kwargs)
    server = make_server(app, host, port)
    url = f"http://{host}:{server.server_address[1]}/" + ("demo" if demo else "")
    print(f"Trading Agent dashboard: {url}  (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
