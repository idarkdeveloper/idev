"""Local web dashboard: ``python -m trading_agent ui``.

A small stdlib HTTP server that serves ``ui/index.html`` and a JSON API over the same
objects the CLI uses. Nothing here can place real orders: the order endpoint only
works against the local paper simulator.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .broker import Broker, LocalPaperBroker
from .config import Settings, load_settings
from .investors import classify_client
from .quiver import DisclosedTrade
from .runner import check, make_broker, make_data_source, make_notifier
from .state import State

log = logging.getLogger(__name__)

DEALS_TTL_SECONDS = 600
EDITABLE_ENV_KEYS = {
    "watch_investor": "WATCH_INVESTOR",
    "watch_source": "WATCH_SOURCE",
    "auto_trade": "AUTO_TRADE",
    "notify_email_to": "NOTIFY_EMAIL_TO",
    "notify_webhook_url": "NOTIFY_WEBHOOK_URL",
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
                 dotenv: Path | None = Path(".env")):
        self.settings = settings
        self.dotenv = dotenv
        self._broker = broker
        self._data = data
        self.demo_trades = demo_trades
        self._deals: list[DisclosedTrade] | None = demo_trades
        self._deals_at = time.time() if demo_trades else 0.0
        self._deals_error: str | None = None
        self.jobs: list[Job] = []
        self.lock = threading.Lock()
        self.busy = False

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
        return {
            "now": _now(),
            "settings": {
                "market": s.market, "currency": s.currency, "watch_investor": s.watch_investor,
                "watch_source": s.watch_source, "data_source": s.data_source,
                "broker": getattr(self.broker, "name", s.broker), "mode": mode,
                "auto_trade": s.auto_trade, "claude_model": s.claude_model,
                "notify_email_to": s.notify_email_to or "",
                "notify_webhook_url": s.notify_webhook_url or "",
                "demo": self.demo_trades is not None,
            },
            "connections": {
                "claude": bool(s.anthropic_api_key),
                "groww": s.use_groww,
                "groww_live_orders": s.groww_live_orders,
                "data": s.data_source,
                "prices": "groww" if s.use_groww else "yahoo",
            },
            "account": acct, "positions": positions, "performance": perf,
            "broker_error": broker_error, "deals": deals, "deals_error": self._deals_error,
            "recommendations": recs, "runs": list(reversed(st.data["runs"][-20:])),
            "seen_count": st.seen_count, "busy": self.busy,
            "jobs": [j.to_dict() for j in self.jobs[-5:]],
        }

    # -- actions --------------------------------------------------------------
    def start_check(self, *, force: bool, dry_run: bool) -> Job:
        job = Job(id=len(self.jobs) + 1, kind="dry_run" if dry_run else "check")
        self.jobs.append(job)
        if self.busy:
            job.ok, job.message, job.finished_at = False, "a check is already running", _now()
            return job
        self.busy = True

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

    def paper_order(self, symbol: str, side: str, notional: float) -> dict[str, Any]:
        if not self.paper_only:
            raise PermissionError("orders from the dashboard are allowed only on the paper simulator")
        return self.broker.submit_order(symbol, side, notional=float(notional))

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
                                            whole_shares=old.whole_shares,
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
            if key == "auto_trade":
                value = "true" if value in (True, "true", "1", 1) else "false"
                self.settings.auto_trade = value == "true"
            else:
                value = str(value).strip()
                setattr(self.settings, key, (value or None) if key.startswith("notify") else value)
            applied[env_key] = value
        if "watch_investor" in changes or "watch_source" in changes:
            self._deals, self._deals_at = None, 0.0
        if applied and self.dotenv is not None:
            _write_env(self.dotenv, applied)
        return applied


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
    index_html = (resources.files("trading_agent") / "ui" / "index.html").read_text()

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

        def _body(self) -> dict[str, Any]:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            return json.loads(raw.decode() or "{}")

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                body = index_html.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path == "/api/state":
                self._json(app.snapshot())
            elif path == "/api/jobs":
                self._json([j.to_dict() for j in app.jobs])
            else:
                self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            try:
                body = self._body()
                if path == "/api/check":
                    job = app.start_check(force=bool(body.get("force")), dry_run=bool(body.get("dry_run")))
                    self._json(job.to_dict(), HTTPStatus.ACCEPTED)
                elif path == "/api/order":
                    order = app.paper_order(body["symbol"], body.get("side", "buy"), body["notional"])
                    self._json({"ok": True, "order": order})
                elif path == "/api/dismiss":
                    self._json({"ok": app.dismiss(int(body["index"]))})
                elif path == "/api/settings":
                    self._json({"ok": True, "applied": app.update_settings(body)})
                elif path == "/api/reset":
                    self._json({"ok": True, "removed": app.reset()})
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


def make_server(app: App, host: str = "127.0.0.1", port: int = 8787) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(app))
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
    app = App(settings, **kwargs)
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
