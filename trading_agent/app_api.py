"""Read-only HTTP API for the phone app: GET /v1/health, /v1/portfolio, /v1/history and nothing else.

It reads two files the watch service writes (the feed and the daily value history) and the device-secret file; it
writes nothing, reads no .env and imports no Groww, broker, config, runner or ui code. Every method but GET is
refused with 405 without its body being read."""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .app_auth import Authenticator
from .app_feed import load_feed, load_history

log = logging.getLogger(__name__)

DEFAULT_EVERY_S = 300   # the feed interval when an older feed file does not say; stale is two intervals old
RANGES = ("1m", "3m", "1y", "all")
REQUEST_TIMEOUT_S = 15   # a client that sends nothing cannot hold a thread (this runs before authentication)
_ERRORS = {400: "bad request", 405: "method not allowed", 414: "request too long", 431: "headers too large"}


def _parse_aware(text: Any) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(text))
    except ValueError:
        return None
    return dt if dt.tzinfo else None


def _fresh_feed(state_dir: Path, now: datetime | None = None) -> tuple[dict[str, Any], int] | None:
    """The feed with age_s / stale / market_closed worked out now, or None when there is no usable feed (no data
    yet). Closed: the writer said so, or the day's Groww window (``window_end``) is over; old figures are then normal."""
    feed = load_feed(state_dir)
    try:
        as_of = datetime.fromisoformat(feed["as_of"]) if feed else None
    except (KeyError, TypeError, ValueError):
        as_of = None
    value = feed.get("value") if feed else None
    # the writer's empty "no data yet" feed (source "none", no value) is not data: 503, as with no file
    if (feed is None or as_of is None or as_of.tzinfo is None or feed.get("source") not in ("groww", "saved")
            or isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)):
        return None
    now = now or datetime.now(timezone.utc)
    age = max(0, int((now - as_of).total_seconds()))
    end = _parse_aware(feed.get("window_end"))
    closed = feed.get("market_closed") is True or (end is not None and now > end)
    every = feed.get("every_s")
    every = every if isinstance(every, int) and not isinstance(every, bool) and every > 0 else DEFAULT_EVERY_S
    body = {k: v for k, v in feed.items() if k not in ("window_end", "every_s")}   # not in the phone contract
    return {**body, "age_s": age, "stale": age > 2 * every and not closed, "market_closed": closed}, age


def make_handler(state_dir: Path, auth: Authenticator) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "app-api"
        sys_version = ""
        device: str | None = None
        timeout = REQUEST_TIMEOUT_S

        def _send(self, status: int, body: Any) -> None:
            data = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")   # an unread body must not be taken for the next request
            self.end_headers()
            self.wfile.write(data)

        def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
            # every error (bad request line, unknown method, oversized header) is JSON, never an HTML page
            code = 405 if code == 501 else code   # a method we have no handler for is just "not GET"
            self.close_connection = True
            self._send(code, {"error": _ERRORS.get(code, "error")})

        def _secret(self) -> str | None:
            scheme, _, token = (self.headers.get("Authorization") or "").partition(" ")
            return token.strip() if scheme.lower() == "bearer" else None

        def do_GET(self) -> None:
            name, status = auth.check(self._secret(), self.client_address[0])
            if status != 200:   # decide on the status, never on the name
                self._send(status, {"error": "unauthorised" if status == 401 else "too many requests"})
                return
            self.device = name
            url = urlsplit(self.path)
            if url.path == "/v1/health":
                fresh = _fresh_feed(state_dir)
                self._send(200, {"ok": True, "feed_age_s": fresh[1] if fresh else None})
            elif url.path == "/v1/portfolio":
                fresh = _fresh_feed(state_dir)
                self._send(200, fresh[0]) if fresh else self._send(503, {"error": "no data yet"})
            elif url.path == "/v1/history":
                rng = (parse_qs(url.query).get("range") or ["1m"])[0]
                self._send(200, {"points": load_history(state_dir, rng if rng in RANGES else "1m")})
            else:
                self._send(404, {"error": "not found"})

        def _refuse(self) -> None:   # the body is deliberately never read
            self._send(405, {"error": "method not allowed"})

        do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _refuse

        def log_request(self, code: Any = "-", size: Any = "-") -> None:
            path = getattr(self, "path", "").partition("?")[0].partition("#")[0]   # a query string is never logged
            log.info("%s %s %s %s", self.command or "-", path[:200], code, self.device or "-")

        def log_message(self, fmt: str, *args: Any) -> None:   # protocol errors can echo request text: say nothing
            pass

    return Handler


def make_server(state_dir: Path, host: str, port: int, authenticator: Authenticator | None = None) -> ThreadingHTTPServer:
    state_dir = Path(state_dir)
    server = ThreadingHTTPServer((host, port), make_handler(state_dir, authenticator or Authenticator(state_dir)))
    server.daemon_threads = True
    return server


def serve(state_dir: Path, host: str, port: int) -> None:
    server = make_server(state_dir, host, port)
    log.info("app-api listening on %s:%s (read-only)", host, server.server_address[1])
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def add_arguments(p: argparse.ArgumentParser) -> None:
    """The options, shared with ``python -m trading_agent app-api`` (cli) so there is one copy."""
    p.add_argument("--state-dir", default="state", help="the state folder (default: state)")
    p.add_argument("--host", default=os.environ.get("APP_API_HOST") or "127.0.0.1")
    p.add_argument("--port", type=int, help="default: env APP_API_PORT else 8788")


def run(args: argparse.Namespace) -> int:
    try:
        port = args.port if args.port is not None else int(os.environ.get("APP_API_PORT") or 8788)
    except ValueError:
        print("APP_API_PORT must be a whole number", file=sys.stderr)
        return 1
    serve(Path(args.state_dir), args.host, port)
    return 0


def main(argv: list[str] | None = None) -> int:
    """The service's entry point (``python -m trading_agent.app_api``): it never imports cli, config or Groww code."""
    p = argparse.ArgumentParser(prog="python -m trading_agent.app_api", description="the read-only phone app API")
    add_arguments(p)
    return run(p.parse_args(argv))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    raise SystemExit(main())
