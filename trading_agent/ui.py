"""Local web dashboard: ``python -m trading_agent ui``.

A small stdlib HTTP server that serves ``ui/index.html`` and a JSON API over the same
objects the CLI uses. The order endpoints only work against the local paper
simulator. Live Groww orders (placed by the agent when GROWW_LIVE_ORDERS=true and
AUTO_TRADE=true) are shown read-only in the order history, with their status, and
each holding shows its GTT stop-loss status.
"""

from __future__ import annotations

import dataclasses
import json
import math
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

from . import taxes
from .broker import AlreadyCopied, Broker, LocalPaperBroker
from .config import (DIGEST_WRITERS, Settings, load_settings, parse_digest_time, parse_allowed_hosts, parse_heartbeat_url, parse_investors,
                     parse_forward_start, parse_telegram_chat, parse_telegram_token)
from .investors import classify_client
from .quiver import DisclosedTrade, fetch_followed, filter_by_investors, followed_names
from .momentum import MomentumScreen, momentum_summary
from .groww import GrowwTokenUnavailable, warn_token_block_once
from .runner import (check, free_prices, make_broker, equity_key, make_data_source, make_notifier,
                     make_practice_broker, read_groww_portfolio, cached_notifier)
from . import safety
from .state import STATE_LOCK, State
from .stops import FILLS_KEY, PracticeStopChecker
from .digest_schedule import build_digest, make_scheduler
from .watch import Watcher

log = logging.getLogger(__name__)

DEALS_TTL_SECONDS = 600
# The only static files besides the page: Inter, served locally so the page needs no network.
FONT_FILES = {"/fonts/inter-latin.woff2", "/fonts/inter-latin-ext.woff2"}
STATIC_FILES = {"/static/nocturne.css": ("nocturne.css", "text/css; charset=utf-8"),
                "/static/common.js": ("common.js", "text/javascript; charset=utf-8"),
                "/static/replay.js": ("replay.js", "text/javascript; charset=utf-8")}
EDITABLE_ENV_KEYS = {
    "watch_investor": "WATCH_INVESTOR",  # one name (older form); watch_investors below is the list
    "watch_investors": "INVESTORS",
    "watch_source": "WATCH_SOURCE",
    "bse_deals": "BSE_DEALS",  # also read BSE bulk/block deals beside NSE's
    "flows_breadth": "FLOWS_BREADTH",  # FII/DII flows and market breadth lines in the morning email
    "price_band_filter": "PRICE_BAND_FILTER",  # skip 2% and 5% price-band stocks for buys
    "auto_trade": "AUTO_TRADE",
    "notify_email_to": "NOTIFY_EMAIL_TO",
    "notify_webhook_url": "NOTIFY_WEBHOOK_URL",
    "telegram_bot_token": "TELEGRAM_BOT_TOKEN",
    "telegram_chat_id": "TELEGRAM_CHAT_ID",
    "telegram_alerts": "TELEGRAM_ALERTS",
    "heartbeat_url": "HEARTBEAT_URL",
    "allowed_hosts": "TA_ALLOWED_HOSTS",   # not on the Settings form; validated like the rest
    "forward_start": "FORWARD_START",
    "market": "MARKET",
    "paper_starting_cash": "PAPER_STARTING_CASH",
    # The daily emails (digest.py), sent by the watch service
    "digest_morning_on": "DIGEST_MORNING_ON",
    "digest_evening_on": "DIGEST_EVENING_ON",
    "digest_morning": "DIGEST_MORNING",
    "digest_evening": "DIGEST_EVENING",
    "digest_writer": "DIGEST_WRITER",
    "digest_bulletin": "DIGEST_BULLETIN",
    "digest_charts": "DIGEST_CHARTS",
    # Only has an effect when GROWW_LIVE_ORDERS=true, which the dashboard can never set.
    "groww_gtt_stops": "GROWW_GTT_STOPS",
    "groww_ddpi_confirmed": "GROWW_DDPI_CONFIRMED",
    "groww_sell_t1": "GROWW_SELL_T1",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _inr(v: float, d: int = 0) -> str:
    """Rupees with Indian digit grouping (12,34,567)."""
    neg, v = v < 0, abs(round(v, d))
    whole, _, frac = f"{v:.{d}f}".partition(".")
    head, tail = whole[:-3], whole[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    out = ",".join(parts + [tail]) if parts else tail
    return ("−" if neg else "") + "₹" + out + ("." + frac if d else "")


class Busy(Exception):
    """Another request is already doing this (answered 409)."""


class NeedsConfirmation(Exception):
    """The request would repeat something already done; the page must ask again with the confirmation flag (HTTP 409)."""

    def __init__(self, message: str, copied_on: str | None = None):
        super().__init__(message)
        self.copied_on = copied_on


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
                 prices: Any | None = None, practice: LocalPaperBroker | None = None):
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
        self._news: Any | None = None  # NewsService, built on first use
        self._my_portfolio_at = 0.0
        self.demo_trades = demo_trades
        self._deals: list[DisclosedTrade] | None = demo_trades
        self._deals_at = time.time() if demo_trades else 0.0
        self._deals_error: str | None = None
        self._bar_seen: str | None = None  # newest price-bar date a lookup used (shown in the freshness chip)
        self.jobs: list[Job] = []
        self.lock = threading.Lock()
        self._slot_lock = threading.Lock()  # guards busy/running only; never held across a job
        self.busy = False
        self.running: Job | None = None  # the job holding the one slot
        self._replay: Any | None = None  # ReplayApp, built on first use
        self._holidays: Any | None = None
        self._demo: "App | None" = None
        self._settings_version = 0  # bumped when settings change, so the Demo child is rebuilt only then
        self._demo_ver = -1
        self._practice: LocalPaperBroker | None = practice  # the practice account the Demo page uses
        self._parent: "App | None" = None  # set on the Demo page's app: it shares this app's data sources
        self._stops: PracticeStopChecker | None = None  # the practice stop checker (see ensure_stop_checker)
        self._preview_lock = threading.Lock()  # one email preview at a time
        self._lazy_lock = threading.RLock()  # one lock builds both the broker and the practice account
        self.clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)  # tests replace it (financial year)

    @property
    def demo(self) -> "App":
        """The Demo page's app: the same real data sources, state and recommendations as this one, but with the
        practice account, never a Groww order, never the real .env, never a notification. On the offline sample
        dashboard (``ui --demo``) the app is its own Demo page."""
        if self.demo_trades is not None or self._parent is not None:
            return self
        with self._lazy_lock:
            if self._demo is None or self._demo_ver != self._settings_version:
                settings = dataclasses.replace(self.settings, groww_live_orders=False, resend_api_key=None,
                                               notify_email_to=None, notify_webhook_url=None,
                                               telegram_bot_token=None, telegram_chat_id=None, heartbeat_url=None)
                child = App(settings, dotenv=None, context=self.context, prices=self.prices)
                child.momentum, child._parent = self.momentum, self
                self._demo, self._demo_ver = child, self._settings_version
            return self._demo

    @property
    def page(self) -> str:
        return "demo" if self.demo_trades is not None or self._parent is not None else "live"

    def _now_dt(self) -> datetime:
        return (self._parent or self).clock()

    def _demo_only(self, what: str) -> None:
        """The practice-account tools exist only on the Demo page (and the offline sample, which is its own Demo page)."""
        if self.demo_trades is None and self._parent is None:
            raise PermissionError(f"{what}: this is done on the Demo page")

    def _on_live_page(self, what: str) -> None:
        if self._parent is not None:
            raise PermissionError(f"{what}: do this on the Live page")

    @property
    def practice_broker(self) -> LocalPaperBroker:
        """The practice (paper) account on state/paper_broker.json. When this app's own broker already is that
        account (paper mode) it is the same object, so there is one writer."""
        if self._parent is not None:
            return self._parent.practice_broker
        if self.demo_trades is not None:
            return self.broker  # type: ignore[return-value]
        with self._lazy_lock:
            if self._practice is None:
                try:
                    b = self.broker  # paper mode: the same object, so there is one writer
                except Exception:  # noqa: BLE001 - e.g. Groww unreachable: the practice account still works
                    b = None
                self._practice = b if isinstance(b, LocalPaperBroker) else make_practice_broker(
                    self.settings, price_fn=self.prices)
            return self._practice

    # -- lazy singletons ------------------------------------------------------
    @property
    def broker(self) -> Broker:
        if self._parent is not None:
            return self._parent.practice_broker
        if self._broker is None:
            with self._lazy_lock:
                if self._broker is None:
                    b = make_broker(self.settings)
                    if isinstance(b, LocalPaperBroker) and self._practice is not None:
                        b = self._practice  # the practice account is already open: keep one writer
                    elif isinstance(b, LocalPaperBroker):
                        self._practice = b
                    self._broker = b
        return self._broker

    @property
    def data(self) -> Any | None:
        if self._parent is not None:
            return self._parent.data
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
        with self._slot_lock:  # the check and the claim of the slot are one step
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
    def holidays(self) -> Any | None:
        """NSE trading holidays (India only; not fetched in demo mode)."""
        if self._parent is not None:
            return self._parent.holidays
        if self.settings.market != "in" or self.demo_trades is not None:
            return None
        if self._holidays is None:
            from .holidays import NSEHolidays
            self._holidays = NSEHolidays(cache_dir=self.settings.state_dir / "cache")
        return self._holidays

    @property
    def replay(self) -> Any:
        with self._lazy_lock:
            if self._replay is None:
                from .replay.web import ReplayApp
                self._replay = ReplayApp(self)
            return self._replay

    # -- deals ----------------------------------------------------------------
    def deals(self, refresh: bool = False) -> list[DisclosedTrade]:
        if self._parent is not None:  # the same deals, fetched and cached once
            out = self._parent.deals(refresh)
            self._deals_error = self._parent._deals_error
            return out
        fresh = self._deals is not None and time.time() - self._deals_at < DEALS_TTL_SECONDS
        if self.demo_trades is not None or (fresh and not refresh):
            return self._deals or []
        try:
            self._deals = fetch_followed(self.data, self.settings.investors, self.settings.watch_source)
            self._deals_at = time.time()
            self._deals_error = None
        except Exception as e:  # noqa: BLE001
            self._deals_error = str(e)
            log.warning("deal fetch failed: %s", e)
            self._deals = self._deals or []
        return self._deals

    # -- snapshot for the page -----------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        # Read "busy" before the state file: a reply saying a job has finished then always carries that job's results.
        busy = self.busy
        st = State(self.settings.state_dir / "state.json")
        s = self.settings
        deals = []
        for t in self.deals():
            d = t.to_dict()
            d.pop("raw", None)
            d["status"] = "new" if t.key not in st.data["seen"] else "analysed"
            d["client_type"] = classify_client(t.investor, t.source)
            d["who"] = followed_names(t.investor, s.investors)  # which followed investor(s) this deal belongs to
            deals.append(d)
        try:
            broker = self.broker  # resolved once: in live mode this can fail (e.g. Groww refusing a token)
            acct = broker.account().to_dict()
            positions = [p.to_dict() for p in broker.positions()]
            broker_error = None
        except Exception as e:  # noqa: BLE001
            broker = self._broker  # whatever exists already, else None
            acct, positions, broker_error = None, [], str(e)
        perf = broker.performance() if isinstance(broker, LocalPaperBroker) else None
        since = broker.created_at if isinstance(broker, LocalPaperBroker) else None
        # the practice account and a real account keep separate curves; no broker at all: the real one's
        ekey = equity_key(broker) if broker is not None else "equity_history"
        practice_tax = copy_info = None
        if self.page == "demo" and isinstance(broker, LocalPaperBroker):
            if s.market == "in":
                practice_tax = taxes.fy_summary(broker.orders(), self._now_dt())
            cs = broker.copy_status()
            copy_info = {"held": len(cs["held"]), "copied_on": self._ist_day(cs["last_copy_at"]) if cs["held"] else None}
        gtt = st.data.get("gtt_stops", {})
        for pos in positions:  # trailing-stop level and GTT status for the table
            g = gtt.get(pos["symbol"].upper())
            pos["gtt"] = ({k: g.get(k) for k in ("status", "trigger", "limit", "qty", "smart_order_id", "last_error")}
                          if g else None)
            try:
                from .risk import position_stop
                bars = []
                if pos.get("stop_type") == "trailing" and not self.demo_trades:
                    bars = self.prices.history(pos["symbol"], "1y")
                st_ = position_stop(pos, bars)
                pos["stop"] = round(st_["level"], 2) if st_["level"] is not None else None
                pos["stop_type"], pos["stop_label"], pos["stop_value"] = st_["type"], st_["label"], st_["value"]
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
            "page": self.page,
            "regime": regime,
            "watch": ({**self.watcher.status(), "auto_exit": self.watcher.auto_exit} if self.watcher
                      else {"on": False, "every": 60, "auto_exit": False}),
            "orders": self.orders(),
            "forward": self.forward_summary(),
            "backtest": self.last_backtest,
            "screen": self.last_screen,
            "factor_backtest": self.last_factor_bt,
            "signal_lab": self.last_signal_lab,
            "stop_fills": st.data.get(FILLS_KEY, [])[-20:] if self.page == "demo" else [],
            "practice_tax": practice_tax,
            "copy_info": copy_info,
            "equity_history": st.equity_history(since, ekey)[-1000:],
            "equity_stats": st.equity_stats(since, ekey),
            "costs": _cost_table(self.settings.market),
            "settings": {
                "market": s.market, "currency": s.currency, "watch_investor": s.watch_investor,
                "investors": s.investors,
                "watch_source": s.watch_source, "data_source": s.data_source,
                "broker": getattr(broker, "name", s.broker), "mode": mode,
                "auto_trade": s.auto_trade, "claude_model": s.claude_model,
                "notify_email_to": s.notify_email_to or "",
                "notify_webhook_url": s.notify_webhook_url or "",
                # secrets are never sent back to the page: only whether they are set
                "telegram_token_set": bool(s.telegram_bot_token), "telegram_chat_id": s.telegram_chat_id or "",
                "telegram_alerts": s.telegram_alerts, "heartbeat_set": bool(s.heartbeat_url),
                "paper_starting_cash": s.paper_starting_cash,
                "groww_gtt_stops": s.groww_gtt_stops, "bse_deals": s.bse_deals,
                "flows_breadth": s.flows_breadth, "price_band_filter": s.price_band_filter,
                "groww_ddpi_confirmed": s.groww_ddpi_confirmed, "groww_sell_t1": s.groww_sell_t1,
                "digest_morning_on": s.digest_morning_on, "digest_evening_on": s.digest_evening_on,
                "digest_morning": s.digest_morning, "digest_evening": s.digest_evening,
                "digest_enabled": s.digest_enabled, "digest_writer": s.digest_writer,
                "digest_bulletin": s.digest_bulletin, "digest_charts": s.digest_charts,
                "digest_channel": bool(s.resend_api_key and s.notify_email_to) or bool(s.notify_webhook_url),
                "max_slippage_pct": s.max_slippage_pct,
                "demo": self.demo_trades is not None,
            },
            "market_day": self.holidays.today() if self.holidays is not None else None,
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
            "bands": self._bands_for([p["symbol"] for p in positions]),
            "broker_error": broker_error, "deals": deals, "deals_error": self._deals_error,
            "recommendations": recs, "runs": list(reversed(st.data["runs"][-20:])),
            "seen_count": st.seen_count, "busy": busy,
            "running": ({"kind": self.running.kind, "label": self.JOB_LABELS.get(self.running.kind, self.running.kind),
                         "started_at": self.running.started_at} if self.busy and self.running else None),
            "jobs": [j.to_dict() for j in self.jobs[-5:]],
            "protection": self.protection(positions, gtt),
            "live_orders": self._live_orders_on(),   # the one effective flag: the strip and the protection column both use it
        }

    # -- safety and freshness ---------------------------------------------------
    def _live_orders_on(self) -> bool:
        s = self.settings
        return bool(s.groww_live_orders and s.use_groww and self._parent is None and self.demo_trades is None)

    def protection(self, positions: list[dict[str, Any]], gtt: dict[str, Any]) -> dict[str, Any] | None:
        """Which stop protects each real holding (Live page only; the practice page has its own stops)."""
        if self.page != "live":
            return None
        now = self._now_dt()
        watch = safety.watch_info(self.settings.state_dir, now, safety.market_open(now, self._safe_holidays()))
        return safety.protection_map(positions if self._live_orders_on() else [], gtt,
                                     live_orders=self._live_orders_on(), watch=watch)

    def _safe_holidays(self) -> Any | None:
        try:
            return self.holidays
        except Exception:  # noqa: BLE001
            return None

    def note_bar(self, lookup: dict[str, Any] | None) -> None:
        """Remember the newest price-bar date a stock lookup returned."""
        try:
            d = str(((lookup or {}).get("history") or [])[-1]["d"])[:10]
        except (IndexError, KeyError, TypeError):
            return
        root = self._parent or self
        if not root._bar_seen or d > root._bar_seen:
            root._bar_seen = d

    def freshness(self) -> dict[str, Any]:
        root = self._parent or self
        return safety.freshness(self.settings.state_dir, self._now_dt(), holidays=self._safe_holidays(),
                                live_orders=self._live_orders_on(), bar_at=root._bar_seen,
                                deals_at=root._deals_at or None)

    # -- actions --------------------------------------------------------------
    def start_check(self, *, force: bool, dry_run: bool) -> Job:
        self._on_live_page("Checks run the real agent")
        with self._slot_lock:
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
        if self._parent is not None:
            return self._parent.names
        if self.demo_trades is not None:
            return None
        if self._names is None:
            from .instruments import CompanyNames
            self._names = CompanyNames(self.settings.state_dir / "cache")
        return self._names

    @property
    def news(self) -> Any | None:
        """Headlines + tags for the look-up; none in demo mode (no network needed there)."""
        if self._parent is not None:
            return self._parent.news
        if self.demo_trades is not None:
            return None
        if self._news is None:
            from .news import NewsService
            self._news = NewsService(self.settings)
        return self._news

    def news_for_ticker(self, text: str) -> dict[str, Any]:
        """Headlines for the look-up, answered at once: tags already known are shown, the rest are tagged on a
        background thread. Empty in demo mode."""
        svc = self.news
        if svc is None:
            return {"items": [], "errors": [], "tagger": "none", "demo": True}
        ticker, company = self.resolve(text)
        try:
            return {**svc.for_symbol(ticker, company, background=True), "ticker": ticker.upper()}
        except Exception as e:  # noqa: BLE001 - headlines are a nicety
            return {"items": [], "errors": [f"{type(e).__name__}: {e}"], "tagger": "none", "ticker": ticker.upper()}

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

    def _bands_for(self, symbols: list[str]) -> dict[str, str]:
        """symbol -> price band label ("5%", "10%") for the pills; empty when the filter is off or no list is stored."""
        from .bands import book_for
        book = book_for(self.settings)
        out = {}
        for sym in symbols:
            label = book.rule(sym)["label"]
            if label and label != "no band":
                out[sym] = label
        return out

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
            from .risk import position_stop
            try:
                bars = self.prices.history(ticker, "1y") if (pos.stop_type or "trailing") == "trailing" else []
            except Exception:  # noqa: BLE001
                bars = []
            st_ = position_stop(pos, bars)
            out["position"] = {"qty": pos.qty, "avg_entry_price": pos.avg_entry_price,
                               "stop": round(st_["level"], 2) if st_["level"] is not None else None,
                               "stop_type": st_["type"], "stop_label": st_["label"]}
        from .bands import book_for
        rule = book_for(self.settings).rule(ticker)
        out["band"], out["band_note"], out["band_skip"] = (rule["label"] if isinstance(rule["band"], int) else None), rule["reason"], rule["skip"]
        return out

    def backtest_names(self, investor: Any) -> list[str]:
        """Who a backtest covers: empty / "All followed" is every followed investor; otherwise the given name(s),
        which may be a followed name or free text (and several, comma separated or as a list)."""
        if isinstance(investor, str) and investor.strip().lower() in ("", "all", "all followed", "__all__"):
            return self.settings.investors
        return parse_investors(investor)

    def start_backtest(self, investor: Any, days: int, horizons: tuple[int, ...], cost_bps: float) -> Job:
        from .backtest import run_backtest, run_backtest_followed

        names = self.backtest_names(investor)
        label = names[0] if len(names) == 1 else ", ".join(names)
        bt_label = "All followed" if names == self.settings.investors else ", ".join(names)
        job = Job(id=len(self.jobs) + 1, kind="backtest")
        with self._slot_lock:  # the check and the claim of the slot are one step
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
                             for d in filter_by_investors(self.demo_trades, names)]
                    prices: Any = _DemoHistory(getattr(self.broker, "price_fn", None) or (lambda s: 100.0))
                else:
                    data = self.data
                    deals = fetch_followed(data, names, self.settings.watch_source,
                                           days=days if self.settings.data_source == "nse" else None)
                    prices = self.prices
                if len(names) == 1:
                    result = run_backtest(names[0], deals, prices, horizons=horizons, cost_bps=cost_bps)
                else:  # pooled result plus one row per investor
                    result = run_backtest_followed(names, deals, prices, label=bt_label,
                                                   horizons=horizons, cost_bps=cost_bps)
                self.last_backtest = {"at": _now(), "days": days, **result.to_dict()}
                job.result = self.last_backtest["summary"]
                job.ok = True
                job.message = f"{len(deals)} deals replayed for {label}"
            except Exception as e:  # noqa: BLE001
                log.exception("backtest failed")
                job.ok, job.message = False, f"{type(e).__name__}: {e}"
            finally:
                job.finished_at = _now()
                self.busy = False

        threading.Thread(target=run, daemon=True).start()
        return job

    def start_screen(self, universe: str, top: int, quality: bool = False, value: bool = False) -> Job:
        from .bands import book_for
        from .screen import load_universe, run_screen

        job = Job(id=len(self.jobs) + 1, kind="screen")
        with self._slot_lock:  # the check and the claim of the slot are one step
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
                                    quality=1.0 if quality else 0.0, value=1.0 if value else 0.0,
                                    bands=book_for(self.settings))
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
        self._on_live_page("The watch controls the real agent")
        if on:
            want_exit = self.settings.auto_trade if auto_exit is None else bool(auto_exit)
            if (self.watcher is None or (every and every != self.watcher.every)
                    or want_exit != self.watcher.auto_exit):
                if self.watcher:
                    self.watcher.stop()
                try:
                    wbroker = self.broker
                except GrowwTokenUnavailable as e:  # live mode, Groww refusing a token: watch alerts-only for now
                    warn_token_block_once(e, log)
                    wbroker = None
                notifier = make_notifier(self.settings)
                self.watcher = Watcher(
                    self.settings, every=every or 60, data=self.data, broker=wbroker,
                    broker_factory=lambda: self.broker,
                    notifier=notifier, prices=self.prices,
                    auto_exit=want_exit, holidays=self.holidays,
                    digest=make_scheduler(self.settings, cached_notifier(self.settings), data=self.data, prices=self.prices,
                                          news=self.news, holidays=self.holidays,
                                          practice=wbroker if isinstance(wbroker, LocalPaperBroker) else None,
                                          groww=lambda: self.my_portfolio(), context=self.context),
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
                    qty: float | None = None, stop_type: str | None = None,
                    stop_value: float | None = None) -> dict[str, Any]:
        if self.demo_trades is None and self._parent is None:
            raise PermissionError("practice orders are placed on the Demo page")
        if not self.paper_only:
            raise PermissionError("orders from the dashboard are allowed only on the paper simulator")
        symbol = str(symbol).strip().upper()
        if not symbol:
            raise ValueError("symbol required")
        if side not in ("buy", "sell"):
            raise ValueError("side must be buy or sell")
        stop = None
        if side == "buy" and stop_type not in (None, ""):  # none given: a held position keeps its stop, a new one trails
            from .risk import normalize_stop
            stop = normalize_stop(stop_type, stop_value, self.broker.latest_price(symbol))
        if side == "buy":
            from .bands import book_for
            blocked = book_for(self.settings).refuse_buy(symbol)
            if blocked:
                raise ValueError(blocked)
            self._check_topup_percent_stop(symbol, stop, qty, notional)
        if qty not in (None, "", 0, "0"):
            order = self.broker.submit_order(symbol, side, qty=float(qty), stop=stop)
        elif notional in (None, "", 0, "0"):
            raise ValueError("enter an amount or a quantity")
        else:
            order = self.broker.submit_order(symbol, side, notional=float(notional), stop=stop)
        self._record_equity()
        return order

    def _check_topup_percent_stop(self, symbol: str, stop: dict[str, Any] | None, qty: Any, notional: Any) -> None:
        """A buy that adds to a position with a percent stop moves its average buy price, and so the stop level. Refuse
        it when the new level would be at or above today's price, since the position would be sold at once."""
        from .risk import AT_ONCE
        pos = next((p for p in self.broker.positions() if p.symbol == symbol), None)
        if pos is None:
            return
        eff_type, eff_value = (stop["type"], stop.get("value")) if stop is not None else (pos.stop_type, pos.stop_value)
        if eff_type != "percent" or not eff_value:
            return
        price = self.broker.latest_price(symbol)
        if not price or price <= 0:
            raise ValueError(f"no usable price for {symbol} right now, so the stop cannot be checked; try again shortly")
        try:
            add = float(qty) if qty not in (None, "", 0, "0") else float(notional) / price
        except (TypeError, ValueError):
            return  # the order itself reports a bad amount
        if getattr(self.broker, "whole_shares", False):
            add = float(math.floor(add))
        if add <= 0:
            return
        avg = (pos.qty * pos.avg_entry_price + add * price) / (pos.qty + add)
        if avg * (1 - float(eff_value) / 100) >= price:
            raise ValueError(f"after this buy your average price would be {avg:,.2f}, which puts the "
                             f"{float(eff_value):g}% stop at or above today's price, so it would sell at once. "
                             "Choose a smaller stop percentage or another stop type")

    def set_stop(self, symbol: str, stop_type: str, stop_value: Any = None) -> dict[str, Any]:
        """Change the practice stop-loss of an open practice position (Demo page only)."""
        if self.demo_trades is None and self._parent is None:
            raise PermissionError("practice stops are set on the Demo page; Live uses the real GTT at Groww")
        if not self.paper_only:
            raise PermissionError("stops from the dashboard are allowed only on the paper simulator")
        from .risk import normalize_stop, position_stop, AT_ONCE
        symbol = str(symbol).strip().upper()
        broker = self.broker
        pos = next((p for p in broker.positions() if p.symbol == symbol), None)
        if pos is None:
            raise LookupError(f"no open position in {symbol}")
        price = pos.current_price if pos.current_price is not None else broker.latest_price(symbol)
        stop = normalize_stop(stop_type, stop_value, price)
        if stop["type"] == "percent" and price <= position_stop(
                {**pos.to_dict(), "stop_type": "percent", "stop_value": stop["value"]})["level"]:
            raise ValueError(AT_ONCE)
        broker.set_stop(symbol, stop)
        return stop

    def ensure_stop_checker(self) -> PracticeStopChecker | None:
        """Start (once) the practice stop checker: one daemon thread for the practice account, India only. It runs
        while the dashboard process runs, with or without a browser tab open."""
        root = self._parent or self
        with root._lazy_lock:
            if root._stops is None and root.settings.market == "in":
                root._stops = PracticeStopChecker(
                    lambda: root.practice_broker, root.settings.state_dir / "state.json",
                    bars_fn=lambda sym: root.prices.history(sym, "1y") if not root.demo_trades else [],
                    holidays=root.holidays, after_fill=lambda: root.demo._record_equity(),
                    enabled_fn=lambda: root.settings.market == "in")
            if root._stops is not None:
                root._stops.start()
            return root._stops

    def _record_equity(self) -> None:
        """Read the account first (slow, may be a network call), then load, merge and save state.json in one
        short step under the state lock, so a write made meanwhile (a new seen deal) is not overwritten."""
        broker = self.broker
        try:
            acct, n = broker.account(), len(broker.positions())
        except Exception as e:  # noqa: BLE001
            log.debug("equity point skipped: %s", e)
            return
        with STATE_LOCK:
            st = State(self.settings.state_dir / "state.json")
            st.record_equity(acct.equity, acct.cash, n, since=getattr(broker, "created_at", None),
                             key=equity_key(broker))
            st.save()

    def close_position(self, symbol: str) -> dict[str, Any]:
        symbol = symbol.upper()
        pos = next((p for p in self.broker.positions() if p.symbol == symbol), None)
        if pos is None:
            raise LookupError(f"no open position in {symbol}")
        return self.paper_order(symbol, "sell", qty=pos.qty)

    def dismiss(self, index: int) -> bool:
        with STATE_LOCK:
            st = State(self.settings.state_dir / "state.json")
            ok = st.dismiss_recommendation(index)
            if ok:
                st.save()
        return ok

    def reset(self) -> list[str]:
        """Live: forget the seen deals and recommendations (state.json); the practice account is not touched.
        Demo: start the practice account over, nothing else. Offline sample: both, in its own folder."""
        if self._parent is not None:
            return self._parent.reset_practice()
        removed = []
        paths = [self.settings.state_dir / "state.json"]
        with STATE_LOCK:
            keep = None
            if self.demo_trades is None and paths[0].exists():  # Live: the practice account's curve and fills are not Live's to forget
                old = State(paths[0]).data
                keep = {k: old[k] for k in ("practice_equity", FILLS_KEY) if old.get(k)}
            for p in paths:
                if p.exists() and p.name not in removed:
                    p.unlink()
                    removed.append(p.name)
            if keep:
                fresh = State(paths[0])
                fresh.data.update(keep)
                fresh.save()
        if self.demo_trades is not None:  # the broker is the only thing that deletes its file (under its own locks)
            pb = self.practice_broker
            if pb.reset(self.settings.paper_starting_cash) and pb.path.name not in removed:
                removed.append(pb.path.name)
        return removed

    def reset_practice(self) -> list[str]:
        """Start the practice account over (PAPER_STARTING_CASH), in place so every holder of it sees the fresh one."""
        b = self.practice_broker
        return [b.path.name] if b.reset(self.settings.paper_starting_cash) else []

    # -- copy the real Groww portfolio into the practice account, sell what-ifs, tax ------------------
    # Nothing here writes to Groww: the portfolio is only READ (my_portfolio), and every sale is on the practice account.
    @staticmethod
    def _ist_day(at: Any) -> str | None:
        if not at:
            return None
        return taxes.ist_date(at).strftime("%d %b %Y").lstrip("0")

    def _copyable(self) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        """The Groww holdings that can be copied (read-only) and those skipped, with the reason. Raises ValueError
        with the reason when the holdings can't be read at all."""
        m = self.my_portfolio(refresh=True)
        if not m.get("linked"):
            raise ValueError("Groww isn't linked, so there is nothing to copy; add your Groww keys to .env")
        if m.get("error"):
            raise ValueError(f"Groww didn't answer: {m['error']}")
        if m.get("source") == "saved":  # last known holdings with delayed prices: may be stale, never copied
            raise ValueError(f"Groww didn't answer ({m.get('reason')}); the saved holdings are not copied "
                             "because they may be out of date. Try again when Groww is back.")
        usable: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        for h in m.get("holdings") or []:
            price, avg, qty = h.get("price"), h.get("avg_price"), h.get("qty")
            if price is None or not price > 0:
                skipped.append({"symbol": h["symbol"], "reason": "no market price"})
            elif not avg or not avg > 0 or not qty or not qty > 0:
                skipped.append({"symbol": h["symbol"], "reason": "no buy price or quantity"})
            else:
                usable.append({"symbol": h["symbol"], "qty": qty, "avg_price": avg, "price": price})
        if not usable:
            raise ValueError("none of your Groww holdings has a market price, so nothing can be copied"
                             if skipped else "there are no holdings in your Groww account")
        return usable, skipped

    @staticmethod
    def _copy_message(res: dict[str, Any], skipped: list[dict[str, str]], usable: list[dict[str, Any]]) -> str:
        parts = [f"Copied {len(res['added']) + len(res['merged'])} holding{'s' if len(res['added']) + len(res['merged']) != 1 else ''} "
                 f"into the practice account at your real buy prices (cost {_inr(res['cost'])})."]
        if res["merged"]:
            parts.append("Already in the practice account, so merged (quantity added, average price weighted): "
                         + ", ".join(res["merged"]) + ".")
        if skipped:
            parts.append("Skipped: " + "; ".join(f"{k['symbol']} — {k['reason']}" for k in skipped) + ".")
        parts.append("Copied holdings have no stop-loss; set one per stock with Edit stop.")
        parts.append("Practice cash is unchanged and Groww was not touched.")
        return " ".join(parts)

    def copy_groww(self, confirm_again: bool = False, held_over_year: Any = None) -> dict[str, Any]:
        """Copy the Groww holdings into the practice account (Demo only). Cash is not reduced; the copy is recorded
        so the practice profit since start does not count it. A second copy while copied positions are still held
        needs ``confirm_again``."""
        self._demo_only("Copying your Groww portfolio")
        broker = self.practice_broker
        status = broker.copy_status()
        if status["held"] and not confirm_again:
            day = self._ist_day(status["last_copy_at"])
            raise NeedsConfirmation(f"Already copied on {day}; copy again adds the holdings again?", day)
        usable, skipped = self._copyable()
        flags = {str(k).upper(): bool(v) for k, v in (held_over_year or {}).items()} if isinstance(held_over_year, dict) else {}
        for h in usable:
            h["held_over_year"] = flags.get(h["symbol"].upper(), False)
        try:
            res = broker.copy_in(usable, only_if_not_copied=not confirm_again)
        except AlreadyCopied as e:  # another request copied between the check above and the lock
            day = self._ist_day(e.last_copy_at)
            raise NeedsConfirmation(f"Already copied on {day}; copy again adds the holdings again?", day) from None
        self._record_equity()
        return {"ok": True, **res, "skipped": skipped, "message": self._copy_message(res, skipped, usable)}

    def reset_to_groww(self) -> dict[str, Any]:
        """Practice account back to the starting cash plus a fresh copy of the Groww holdings (Demo only). If the
        holdings can't be read nothing is changed."""
        self._demo_only("Resetting to your Groww portfolio")
        usable, skipped = self._copyable()  # raises before anything is touched
        broker = self.practice_broker
        out: dict[str, Any] = {}
        broker.reset(self.settings.paper_starting_cash, then=lambda: out.update(broker.copy_in(usable)))  # one transaction
        res = out
        self._record_equity()
        msg = (f"Practice account reset to {_inr(self.settings.paper_starting_cash)} cash plus a fresh copy of your Groww "
               f"holdings. " + self._copy_message(res, skipped, usable))
        return {"ok": True, **res, "skipped": skipped, "message": msg}

    def sell_preview(self, symbol: Any, qty: Any = None, held_over_year: Any = None,
                     source: Any = None) -> dict[str, Any]:
        """What selling would mean, without trading: sale value, charges, proceeds, realised profit or loss and the
        estimated capital-gains tax. A symbol held in the practice account is priced from that position; otherwise
        from the Groww holding (read-only). ``source="groww"`` prices the Groww holding (its quantity and average) even
        when the symbol is also held in practice. ``held_over_year`` is remembered in the practice position when it is
        given (not None); None leaves the saved setting alone."""
        self._demo_only("The sell what-if")
        if self.settings.market != "in":
            raise ValueError("the sale and tax estimate cover Indian shares; switch the market to India")
        sym = str(symbol or "").strip().upper()
        if not sym:
            raise ValueError("symbol required")
        broker = self.practice_broker
        pos = broker.position(sym)
        flag = None if held_over_year is None else bool(held_over_year)
        if pos is not None and flag is not None and flag != pos.held_over_year:
            broker.set_held_over_year(sym, flag)
        saved_flag = pos.held_over_year if pos is not None else False
        want_groww = str(source or "").lower() == "groww"
        if pos is not None and not want_groww:
            basis, held, avg, price = "practice", float(pos.qty), float(pos.avg_entry_price), pos.current_price
            state = {"held_over_year": saved_flag if flag is None else flag, "source": pos.source,
                     "opened_at": pos.opened_at}
        else:
            m = self.my_portfolio()
            row = next((h for h in (m.get("holdings") or []) if str(h["symbol"]).upper() == sym), None) \
                if m.get("linked") and not m.get("error") else None
            if row is None:
                raise LookupError(f"{sym} is not in your practice account or your Groww portfolio" if pos is None
                                  else f"{sym} is not in your Groww portfolio")
            basis, held, avg, price = "groww", float(row["qty"]), float(row["avg_price"]), row.get("price")
            state = {"held_over_year": saved_flag if flag is None else flag, "source": "groww", "opened_at": None}
        if price is None or not price > 0:
            raise ValueError(f"{sym} has no market price right now, so a sale can't be priced")
        if qty in (None, ""):
            q = held
        else:
            try:
                q = float(qty)
            except (TypeError, ValueError):
                raise ValueError("enter the number of shares to sell") from None
        if not q > 0:
            raise ValueError("enter how many shares to sell")
        if getattr(broker, "whole_shares", False) and q != math.floor(q):
            raise ValueError("whole shares only")
        if q > held + 1e-9:
            raise ValueError(f"You hold {held:g} {sym}; you can't sell {q:g}.")
        now = self._now_dt()
        long_term = taxes.is_long_term(state, now.isoformat())
        from .costs import cost_model_for
        model = broker.cost_model or cost_model_for("in")
        value = q * price
        charges = float(model.charges("sell", value))
        proceeds = value - charges
        cost = q * avg
        realised = round(proceeds - cost, 2)  # rounded once, exactly as the sale records it, so preview and sale agree
        est = taxes.estimate_sale(realised, long_term, broker.orders(), now.isoformat())
        if basis == "practice" and state["source"] != "groww" and state["opened_at"]:
            hold_note = (f"Holding period counted from your first buy on {self._ist_day(state['opened_at'])} "
                         "(the position's first buy, not lot by lot).")
        else:
            hold_note = ("Groww gives no buy date, so this counts as short term unless you tick 'Held more than a year' "
                         "(short term is the conservative choice).")
        avg_note = ("Average price is what you paid per share; buy charges were paid from cash and are not in it, "
                    "so they don't reduce this profit." if basis == "practice" and state["source"] != "groww"
                    else "Average price is your Groww buy average; the charges you paid when buying are not included.")
        avg_note += " The estimate may be slightly low: STT is not deductible from the gain, and a top-up is dated from your first buy."
        practice_qty = float(pos.qty) if pos is not None else None
        practice_note = None
        if basis == "groww" and pos is not None:
            practice_note = (f"Priced on your Groww holding. You hold {practice_qty:g} in practice"
                             + (f", so Sell in practice sells at most {practice_qty:g}." if q > practice_qty
                                else "; Sell in practice sells from that position at its own average price."))
        if long_term:
            left = est["exemption_left_before"]
            tax_text = (f"Long term: 12.5% of the gain above the {_inr(taxes.LT_EXEMPTION)} yearly exemption "
                        f"({_inr(left)} of it left before this sale in {est['fy']}), plus 4% cess.")
        else:
            tax_text = "Short term: 20% of the gain, plus 4% cess."
        if realised < 0:
            tax_text = "A loss, so no tax on this sale. " + taxes.LOSS_NOTE[0].upper() + taxes.LOSS_NOTE[1:] + "."
            if est["tax_saved"]:
                tax_text += f" It would also cut the tax on earlier gains this year by about {_inr(est['tax_saved'])}."
        return {"symbol": sym, "basis": basis, "in_practice": pos is not None, "held": held, "qty": q, "price": price,
                "avg_price": avg, "long_term": long_term, "held_over_year": bool(state["held_over_year"]),
                "sale_value": round(value, 2), "charges": round(charges, 2), "proceeds": round(proceeds, 2),
                "cost": round(cost, 2), "realised_pl": realised,
                "practice_qty": practice_qty, "practice_note": practice_note,
                "charges_breakdown": ({k: round(v, 2) for k, v in model.breakdown("sell", value).items()}
                                      if hasattr(model, "breakdown") else None),
                "tax": est, "tax_text": tax_text, "holding_note": hold_note, "cost_note": avg_note,
                "disclaimer": taxes.DISCLAIMER, "fy": est["fy"]}

    def practice_sell(self, symbol: Any, qty: Any = None, held_over_year: Any = None) -> dict[str, Any]:
        """Sell a practice position at the latest price (charges as usual) and record the realised profit or loss
        and the tax estimate with the order. Only a symbol held in the practice account can be sold; a quantity above
        what the practice account holds is capped at that quantity."""
        self._demo_only("Selling in practice")
        sym = str(symbol or "").strip().upper()
        pos = self.practice_broker.position(sym) if sym else None
        if pos is None:
            raise LookupError(f"{sym or 'that stock'} is not in the practice account; copy your portfolio first")
        capped = False
        if qty not in (None, ""):
            try:
                want = float(qty)
            except (TypeError, ValueError):
                raise ValueError("enter the number of shares to sell") from None
            if want > pos.qty:
                qty, capped = pos.qty, True
        pv = self.sell_preview(sym, qty, held_over_year, source="practice")
        broker = self.practice_broker

        def tax_fields(order: dict[str, Any], orders: list[dict[str, Any]]) -> dict[str, Any]:
            est = taxes.estimate_sale(order["realised_pl"], order["long_term"], orders, order["filled_at"])
            return {"tax_estimate": est["estimate"], "tax_saved": est["tax_saved"], "tax_fy": est["fy"],
                    "sold_from": "practice"}

        order = broker.submit_order(pv["symbol"], "sell", qty=pv["qty"], extra_fn=tax_fields)
        self._record_equity()
        proceeds = order["notional"] - order["fees"]
        msg = (f"Practice sale: {_inr(proceeds, 2)} proceeds, P&L {_inr(order['realised_pl'], 2)}, "
               f"estimated tax {_inr(order['tax_estimate'], 2)}")
        if capped:
            msg += f" (capped at the {pos.qty:g} you hold in practice)"
        return {"ok": True, "order": order, "proceeds": round(proceeds, 2), "message": msg}

    def update_settings(self, changes: dict[str, Any]) -> dict[str, Any]:
        self._on_live_page("Settings change the real agent")
        applied: dict[str, str] = {}
        ops: list[Callable[[], None]] = []   # memory changes, run only after every value checked and .env written
        is_demo = self.dotenv is None and self.demo_trades is not None
        st = self.settings
        new_market = st.market
        if isinstance(changes.get("market"), str) and changes["market"].strip().lower() in ("in", "us"):
            new_market = changes["market"].strip().lower()
        for key, env_key in EDITABLE_ENV_KEYS.items():
            if key not in changes:
                continue
            if is_demo and (key.startswith("notify") or key.startswith("telegram") or key == "heartbeat_url"):
                continue  # the Demo page never sends notifications
            value = changes[key]
            _check_type(key, value)
            if key in ("watch_investors", "watch_investor"):   # the page sends one comma list; line breaks are never a separator here
                for item in (value if isinstance(value, (list, tuple)) else [value]):
                    _check_env_value(env_key, item)
            if key == "digest_writer":
                value = str(value).strip().lower()
                if value not in DIGEST_WRITERS:
                    raise ValueError("the summary writer must be one of: " + ", ".join(DIGEST_WRITERS))
                ops.append(lambda v=value: setattr(st, "digest_writer", v))
            elif key in ("digest_morning", "digest_evening"):
                value = parse_digest_time(key.split("_")[1], value)   # ValueError (a 400) when not HH:MM or out of range
                ops.append(lambda k=key, v=value: setattr(st, k, v))
            elif key == "forward_start":
                value = parse_forward_start(value) or ""
                ops.append(lambda v=value: setattr(st, "forward_start", v or None))
            elif key == "allowed_hosts":
                value = ",".join(parse_allowed_hosts(value))
                ops.append(lambda v=value: setattr(st, "allowed_hosts", parse_allowed_hosts(v)))
            elif key in ("telegram_bot_token", "telegram_chat_id", "heartbeat_url"):
                # validated here; the error never repeats the value (a token or ping URL is a secret)
                parse = {"telegram_bot_token": parse_telegram_token, "telegram_chat_id": parse_telegram_chat,
                         "heartbeat_url": parse_heartbeat_url}[key]
                value = parse(value) or ""
                ops.append(lambda k=key, v=value: setattr(st, k, v or None))
            elif key in ("auto_trade", "groww_gtt_stops", "groww_ddpi_confirmed", "groww_sell_t1", "bse_deals", "flows_breadth", "price_band_filter", "digest_morning_on", "digest_evening_on", "digest_bulletin", "digest_charts",
                         "telegram_alerts"):
                value = "true" if _bool_setting(key, value) else "false"
                ops.append(lambda k=key, v=value == "true": setattr(st, k, v))
            elif key == "market":
                value = str(value).strip().lower()
                if value not in ("in", "us"):
                    raise ValueError("market must be 'in' or 'us'")
                if value == st.market:
                    continue
                ops.append(lambda v=value: self._switch_market(v))
            elif key == "paper_starting_cash":
                try:
                    cash = float(value)
                except (TypeError, ValueError):
                    raise ValueError("starting cash must be a number") from None
                if not math.isfinite(cash) or cash <= 0:
                    raise ValueError("starting cash must be a positive number")
                ops.append(lambda c=cash: setattr(st, "paper_starting_cash", c))
                value = f"{cash:g}"
            elif key == "watch_investors":
                names = parse_investors(value)  # a list, or text with one name per line or commas
                ops.append(lambda n=names: setattr(st, "investors", n))
                value = ",".join(names)
            elif key == "watch_investor":
                names = parse_investors(value)
                if st.watch_investors or len(names) > 1:
                    # INVESTORS is set (or several names came), so the list replaces it, or the old list would win
                    ops.append(lambda n=names: setattr(st, "investors", n))
                    applied["INVESTORS"] = ",".join(names)
                else:
                    ops.append(lambda n=names: setattr(st, "watch_investor", n[0]))
                value = names[0]
            else:
                value = "" if value is None else str(value).strip()
                if key == "watch_source" and value.lower() not in WATCH_SOURCES[new_market]:
                    raise ValueError(f"watch_source must be one of {', '.join(sorted(WATCH_SOURCES[new_market]))}")
                if key == "watch_source":
                    value = value.lower()
                ops.append(lambda k=key, v=value: setattr(st, k, (v or None) if k.startswith("notify") else v))
            applied[env_key] = value
        for k, v in applied.items():
            _check_env_value(k, v)   # nothing is written or changed if any value is unsafe
        if applied and self.dotenv is not None:
            _write_env(self.dotenv, applied)
        for op in ops:
            op()
        if "bse_deals" in changes and self._data is not None and hasattr(self._data, "bse"):
            from .bse import make_bse_client
            self._data.bse = make_bse_client(self.settings)  # None when switched off: no more BSE calls
        if ("watch_investor" in changes or "watch_investors" in changes or "watch_source" in changes
                or "bse_deals" in changes):
            self._deals, self._deals_at = None, 0.0
        if applied:
            self._settings_version += 1
        return applied

    def _switch_market(self, market: str) -> None:
        """Rebuild everything that depends on the market: data source, broker, prices."""
        s = self.settings
        if self.watcher:
            self.watcher.stop()
            self.watcher = None
        self._settings_version += 1
        s.market = market
        s.data_source = "nse" if market == "in" else "quiver"
        s.watch_source = "deals" if market == "in" else "congress"
        if market == "us" and s.broker == "groww":
            s.broker = "local"
        self.prices = free_prices(s)
        self.momentum = MomentumScreen(self.prices)
        if self.demo_trades is None:
            self._broker = self._practice = None
            self._data = None
        self._deals, self._deals_at = None, 0.0
        self.last_screen = None
        self.last_backtest = None

    # -- calculators and tools ------------------------------------------------
    def cost_quote(self, amount: float) -> dict[str, Any]:
        from .costs import cost_quote_for
        return cost_quote_for(self.settings.market, amount)

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
        memberships = None
        if self.settings.market == "in" and recs:   # nothing to score: no index lists to download
            from .scorecard import build_memberships
            memberships = build_memberships(self.settings.state_dir, use_cache=True)
        return score_recommendations(recs, self.prices, benchmark=bench,
                                     cost_model=cm if hasattr(cm, "round_trip") else None,
                                     memberships=memberships)

    def start_factor_backtest(self, universe: str, top: int, years: int) -> Job:
        from .costs import cost_model_for
        from .factor_backtest import INDEX_FUNDS, run_factor_backtest, validate_factor_backtest
        from .index_history import point_in_time
        from .screen import load_universe

        job = Job(id=len(self.jobs) + 1, kind="factor_backtest")
        with self._slot_lock:  # the check and the claim of the slot are one step
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
                membership = point_in_time(universe, [m["symbol"] for m in members], self.settings.state_dir)

                def run_top(n: int) -> dict[str, Any]:
                    return run_factor_backtest(members, self.prices, top=n, years=years,
                                               cost_model=cost_model_for("in"),
                                               capital=self.settings.paper_starting_cash,
                                               membership=membership, index_fund=INDEX_FUNDS.get(universe.upper()))

                r = run_top(top)
                try:  # the other portfolio sizes reuse the cached price histories
                    r["validation"] = validate_factor_backtest(
                        run_top, top, base=r, universe=universe, progress=lambda m: setattr(job, "message", m))
                except Exception:  # noqa: BLE001 - the backtest itself still stands
                    log.exception("factor backtest validation failed")
                    r["validation"] = None
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
        with self._slot_lock:  # the check and the claim of the slot are one step
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
        if self._parent is not None:  # the same real holdings, read once
            return self._parent.my_portfolio(refresh)
        if self._my_portfolio is not None and not refresh and time.time() - self._my_portfolio_at < 60:
            return self._my_portfolio
        out = read_groww_portfolio(self.settings, self.prices, _now())
        if not out.get("at"):  # not linked, or an error: not cached
            return out
        try:
            out["bands"] = self._bands_for([str(h.get("symbol")) for h in out.get("holdings") or []])
        except Exception:  # noqa: BLE001 - the pill is a nicety
            out["bands"] = {}
        self._my_portfolio, self._my_portfolio_at = out, time.time()
        return out

    def digest_preview(self, kind: str, with_summary: bool = False) -> dict[str, Any]:
        """The morning or evening email as it would be sent now, for the Settings dialog. Works on the Live and
        Demo pages (read-only; nothing is sent). The written summary is only made on request, since it can use
        Ollama or your Claude credit. On the offline sample dashboard the sections that need real data say so."""
        from .digest import DigestContext, make_context
        if kind not in ("morning", "evening"):
            raise ValueError("kind must be morning or evening")
        root = self._parent or self
        if not root._preview_lock.acquire(blocking=False):
            raise Busy("A preview is already being built; try again when it finishes.")
        try:
            if root.demo_trades is not None:   # the offline sample: no network, no real holdings
                ctx = DigestContext(settings=root.settings, practice=root.broker,
                                    state_path=root.settings.state_dir / "state.json")
            else:
                try:
                    practice = root.practice_broker
                except Exception:  # noqa: BLE001 - e.g. Groww unreachable: the digest says the practice account is unavailable
                    practice = None
                ctx = make_context(root.settings, data=root.data, prices=root.prices, news=root.news,
                                   holidays=root.holidays, context=root.context, groww=lambda: root.my_portfolio(),
                                   practice=practice)
            configured = root.settings.digest_writer
            # Preview shows the free rules summary; the AI writer (Ollama, then Claude) only when asked for
            writer = ("claude" if configured in ("rules", "none") else configured) if with_summary else "rules"
            email = build_digest(kind, ctx, writer=writer)
        finally:
            root._preview_lock.release()
        from .digest_render import inline_data_urls
        out = {k: email[k] for k in ("subject", "text", "html", "writer")}
        out["html"] = inline_data_urls(out["html"], email.get("images"))   # the preview iframe shows the charts as data: URLs
        return out

    def groww_test(self) -> dict[str, Any]:
        """Check Groww credentials end to end without ever returning the token."""
        from .groww import GrowwBroker
        from .runner import resolve_groww_token
        self._on_live_page("The Groww connection test")
        s = self.settings
        if not s.has_groww_credentials:
            return {"ok": False, "message": "No Groww credentials in .env (GROWW_ACCESS_TOKEN, or GROWW_API_KEY "
                                            "with GROWW_API_SECRET or GROWW_TOTP_SECRET)."}
        try:
            token = resolve_groww_token(s)
            g = GrowwBroker(token, live_orders=False, exchange=s.groww_exchange, price_fallback=self.prices)
            holdings = g.holdings()
            acct = g.account()
        except (SystemExit, GrowwTokenUnavailable) as e:
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
        """Live page: real Groww orders (state.json). Demo page: practice fills. Offline sample: both. Newest first."""
        out: list[dict[str, Any]] = []
        if self.page == "demo":
            try:
                b = self.broker
            except GrowwTokenUnavailable:  # e.g. Groww refusing a token: show the other orders
                b = None
            if isinstance(b, LocalPaperBroker):
                # newest first already, so the (stable) sort below keeps same-second fills in order
                out += [{**o, "at": o.get("filled_at")} for o in reversed(b.orders())]
        if self._parent is None:  # the Demo page never shows real orders; the offline sample app shows both
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


WATCH_SOURCES = {"in": {"deals", "bulk", "block", "insider"}, "us": {"congress", "insider"}}


_TRUE_WORDS, _FALSE_WORDS = {"true", "1", "yes", "on"}, {"false", "0", "no", "off"}


def _bool_setting(key: str, value: Any) -> bool:
    """A switch from JSON: true/false, 1/0, or the words true, false, 1, 0, yes, no, on, off. Anything else is a 400
    (a typo must not silently switch something off)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    word = value.strip().lower() if isinstance(value, str) else None
    if word in _TRUE_WORDS:
        return True
    if word in _FALSE_WORDS:
        return False
    raise ValueError(f"{key} must be true or false")


def _check_type(key: str, value: Any) -> None:
    """Settings arrive as JSON: refuse a list, dict, number or bool where text is expected, with a plain message."""
    if key in ("watch_investors", "watch_investor"):
        ok = isinstance(value, str) or (isinstance(value, (list, tuple)) and all(isinstance(i, str) for i in value))
        if not ok:
            raise ValueError(f"{key} must be text or a list of names")
    elif key in ("auto_trade", "groww_gtt_stops", "groww_ddpi_confirmed", "groww_sell_t1", "bse_deals", "flows_breadth", "price_band_filter", "digest_morning_on", "digest_evening_on", "digest_bulletin", "digest_charts",
                 "telegram_alerts"):
        _bool_setting(key, value)
    elif key in ("telegram_bot_token", "telegram_chat_id", "heartbeat_url", "forward_start", "allowed_hosts"):
        if value is not None and not isinstance(value, str):
            raise ValueError(f"{key} must be text (or empty to clear it)")
    elif key in ("digest_morning", "digest_evening", "digest_writer"):
        if not isinstance(value, str):
            raise ValueError(f"{key} must be a time as text, HH:MM")
    elif key == "paper_starting_cash":
        if isinstance(value, (bool, list, tuple, dict)):
            raise ValueError("starting cash must be a number")
    elif key.startswith("notify"):
        if value is not None and not isinstance(value, str):
            raise ValueError(f"{key} must be text (or null to clear it)")
    elif not isinstance(value, str):
        raise ValueError(f"{key} must be text")


def _check_env_value(key: str, value: Any) -> None:
    """A .env value must stay on one line: a line break (or NUL) would let a form field add another setting."""
    text = str(value)
    if chr(0) in text or text != "".join(text.splitlines()):
        raise ValueError(f"{key}: the value must be on one line (no line breaks or control characters)")


def _write_env(path: Path, values: dict[str, str]) -> None:
    """Upsert KEY=value lines; keeps comments and other keys as they are. Refuses unsafe values before writing."""
    for k, v in values.items():
        _check_env_value(k, v)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
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
    path.write_text(chr(10).join(out) + chr(10), encoding="utf-8")


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def make_handler(app: App) -> type[BaseHTTPRequestHandler]:
    index_html = (resources.files("trading_agent") / "ui" / "index.html").read_text(encoding="utf-8")
    sample = ' data-sample="1"' if app.demo_trades is not None else ""  # the offline sample dashboard
    demo_html = index_html.replace("<body>", f'<body data-api="/demo" data-mode="demo"{sample}>', 1)
    if sample:  # the sample app is the whole dashboard, at /
        index_html = index_html.replace("<body>", f'<body data-mode="demo"{sample}>', 1)
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
            refused = self._request_refusal("GET")
            if refused:
                self._refuse(refused)
                return
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
            elif path == "/api/freshness":
                self._json(app.freshness())
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
                    found = app.lookup(ticker)
                    app.note_bar(found)
                    self._json(found)
            elif path == "/api/news":
                from urllib.parse import parse_qs
                ticker = (parse_qs(urlparse(self.path).query).get("ticker") or [""])[0].strip()
                if not ticker:
                    self._json({"error": "ticker required"}, HTTPStatus.BAD_REQUEST)
                else:
                    self._json(app.news_for_ticker(ticker))
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

        _LOCAL_NAMES = ("127.0.0.1", "localhost", "[::1]")

        @staticmethod
        def _split_host(host: str) -> tuple[str, str | None]:
            host = host.strip().lower()
            if host.startswith("["):   # [::1]:8787
                end = host.find("]")
                rest = host[end + 1:]
                return host[:end + 1], rest[1:] if rest.startswith(":") else None
            name, _, port = host.partition(":")
            return name, port or None

        def _host_allowed(self, host: str, *, forwarded: bool = False) -> bool:
            """localhost / 127.0.0.1 / [::1] on any port, and the TA_ALLOWED_HOSTS names.

            Any port for the local names: an SSH tunnel (laptop 8788 -> server 8787) keeps the laptop's port in
            Host. DNS rebinding needs a foreign name in Host, so a local name on another port is not a risk."""
            name, port = self._split_host(host)
            if not name:
                return False
            if name in self._LOCAL_NAMES and not forwarded:
                return port is None or (port.isdigit() and 0 < int(port) < 65536)
            for a in app.settings.allowed_hosts:
                an, ap = self._split_host(a)
                if name == an and (ap is None or ap == port):
                    return True
            return False

        def _request_refusal(self, method: str) -> tuple[int, str] | None:
            """Checked on every request before any handler: the Host must be ours (421 otherwise, which stops DNS
            rebinding); /api GETs are refused cross-site; a POST must be JSON and same-origin (a browser sends a
            text/plain cross-site POST without a preflight, so the content type and the Origin are both checked)."""
            host = self.headers.get("Host") or ""
            if not self._host_allowed(host):
                return 421, "unknown Host; add it to TA_ALLOWED_HOSTS to reach the dashboard under that name"
            site = self.headers.get("Sec-Fetch-Site")
            if method == "GET":
                if urlparse(self.path).path.startswith(("/api/", "/demo/api/", "/replay/api/")) and site in ("cross-site", "same-site"):
                    return 403, "cross-site request refused"
                return None
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                return 415, "POST bodies must be Content-Type: application/json"
            origin = self.headers.get("Origin")
            if origin is not None:
                own = {f"http://{host.strip().lower()}"}
                if self.client_address[0] in ("127.0.0.1", "::1"):   # tailscale serve proxies from localhost
                    fh = (self.headers.get("X-Forwarded-Host") or "").split(",")[0].strip()
                    fp = (self.headers.get("X-Forwarded-Proto") or "https").split(",")[0].strip().lower()
                    if fh and fp in ("http", "https") and self._host_allowed(fh, forwarded=True):
                        own.add(f"{fp}://{fh.lower()}")
                if origin.strip().lower() not in own:
                    return 403, "cross-origin request refused"
            elif site not in (None, "same-origin", "none"):
                return 403, "cross-site request refused"
            return None

        def _refuse(self, refused: tuple[int, str]) -> None:
            try:   # drain the unread body so the client gets the answer instead of a reset connection
                self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 1_000_000))
            except (ValueError, OSError):
                pass
            self._json({"error": refused[1]}, refused[0])

        def do_POST(self) -> None:  # noqa: N802
            refused = self._request_refusal("POST")
            if refused:
                self._refuse(refused)
                return
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
                                            body.get("notional"), body.get("qty"),
                                            body.get("stop_type"), body.get("stop_value"))
                    self._json({"ok": True, "order": order})
                elif path == "/api/stop":
                    self._json({"ok": True, "stop": app.set_stop(str(body.get("symbol", "")), body.get("type"),
                                                                  body.get("value"))})
                elif path == "/api/practice/copy-groww":
                    self._json(app.copy_groww(bool(body.get("confirm_again")), body.get("held_over_year")))
                elif path == "/api/practice/sell-preview":
                    self._json(app.sell_preview(body.get("symbol"), body.get("qty"), body.get("held_over_year"),
                                                body.get("source")))
                elif path == "/api/practice/sell":
                    self._json(app.practice_sell(body.get("symbol"), body.get("qty"), body.get("held_over_year")))
                elif path == "/api/practice/reset-to-groww":
                    self._json(app.reset_to_groww())
                elif path == "/api/close":
                    self._json({"ok": True, "order": app.close_position(str(body.get("symbol", "")))})
                elif path == "/api/digest/preview":
                    self._json(app.digest_preview(str(body.get("kind") or ""), bool(body.get("summary"))))
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
                    job = app.start_backtest(body.get("investor") or "",
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
            except Busy as e:
                self._json({"error": str(e)}, HTTPStatus.CONFLICT)
            except NeedsConfirmation as e:
                self._json({"error": str(e), "needs_confirm": True, "copied_on": e.copied_on}, HTTPStatus.CONFLICT)
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


def sample_app(settings: Settings, context: Any | None) -> App:
    """The offline sample dashboard (``ui --demo``): bundled sample deals and prices, its own state and practice
    account in ``state_dir/demo-sample``, no Groww, no .env writes, no notifications."""
    from .cli import _demo_inputs
    s = dataclasses.replace(settings, state_dir=settings.state_dir / "demo-sample", broker="local",
                            groww_access_token=None, groww_api_key=None, groww_api_secret=None,
                            groww_totp_secret=None, groww_live_orders=False,
                            resend_api_key=None, notify_email_to=None, notify_webhook_url=None,
                            telegram_bot_token=None, telegram_chat_id=None, heartbeat_url=None)
    trades, broker = _demo_inputs(s)
    return App(s, broker=broker, demo_trades=trades, dotenv=None, context=context)


def serve(settings: Settings | None = None, *, host: str = "127.0.0.1", port: int = 8787,
          open_browser: bool = True, demo: bool = False) -> None:
    settings = settings or load_settings()
    from .notify import install_log_redaction
    install_log_redaction()
    from .regime import GlobalContext
    from .prices import YahooPrices

    from .price_archive import archive_for
    context = GlobalContext(YahooPrices(suffix="", cache_dir=settings.state_dir / "cache", cache_ttl=900,
                                        archive=archive_for(settings)))
    # --demo is the offline sample dashboard, isolated in state_dir/demo-sample. Without it the app is the
    # real one, and its /demo page is the same page with the practice account.
    app = sample_app(settings, context) if demo else App(settings, context=context)
    app.ensure_stop_checker()  # practice stop-losses are checked while the dashboard runs, tab open or not
    server = make_server(app, host, port)
    url = f"http://{host}:{server.server_address[1]}/"
    print(f"Trading Agent dashboard: {url}  (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
