"""Telegram commands, step 1: read commands and PRACTICE orders, from one chat only.

The watch service long-polls the bot (``getUpdates``, outbound only) on its own thread and answers only
TELEGRAM_CHAT_ID; every other chat is ignored and logged without its text. The update offset is saved before an
update is handled, so a restart never replays a command (an order is at most once).

Commands: /help /status /lookup /size /portfolio /brief /stops, and /buy /sell in the PRACTICE account only. An order
is never placed by the command itself: it shows an inline Confirm / Cancel; the callback must come from the same chat
and carry the one-time nonce of a confirmation still under 2 minutes old, and a second tap does nothing. The order then
goes through the dashboard's own practice path (App.demo.paper_order): Indian filters (a flagged buy is held back;
there is no override here), price bands, the collar time rule, the day brake and HALT all apply.

/lookup answers with four buttons: Size (1%), Stops and Chart answer for that company, Practice Buy only shows the
/buy line to send (it never places anything). /chart sends a picture of the last 3 months (daily candles, 20/50-day
EMAs, your cost and stop). The daily brief carries Portfolio / Stops / Status buttons that run those read commands. /halt is the emergency kill switch: it asks first, and only a tap on ENGAGE HALT
within 60 seconds (a one-time nonce, from the same chat and person) engages it, through the dashboard's own kill switch
(state/HALT, the watch stops, an alert). /halt is never rate-limited. Releasing HALT is not possible from Telegram.

There is no path to a live order: every call goes to the Demo app, whose settings force GROWW_LIVE_ORDERS off and whose
broker must be the practice account (checked again before each order), whatever GROWW_LIVE_ORDERS says. Read commands
follow the dashboard's Groww market-hours rules and never force a Groww login. Replies are HTML-escaped and kept under
Telegram's 4096 characters; 20 commands a minute at most. The bot token is never logged.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import secrets
import socket
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import requests

from .notify import add_log_secret, redact
from .state import atomic_write
from .timezones import IST

log = logging.getLogger(__name__)

STATE_FILE = "telegram_commands.json"
LEASE_FILE = "telegram_commands.lock"
LEASE_S = 120.0            # a poller that has not renewed its lease this long is gone; another may take over
CONFIRM_TTL_S = 120.0      # an order confirmation expires after 2 minutes
HALT_TTL_S = 60.0          # a /halt confirmation expires after 60 seconds
RATE_PER_MIN = 20
POLL_TIMEOUT_S = 25
LIMIT = 4000               # Telegram allows 4096 characters per message
BACKOFF_S = (5, 15, 60)

HELP = ("<b>Commands</b> (practice account only; nothing here places a live order). "
        "Use the company's name or its NSE code: <i>tata steel</i> or <i>TATASTEEL</i>.\n"
        "/lookup NAME: price, momentum, 200-day average, stop, news and what it means\n"
        "/chart NAME: 3 months of daily candles with the 20/50-day EMAs, your cost and stop\n"
        "/size NAME [risk% max%]: shares, cost, stop and charges (practice account)\n"
        "/portfolio: the practice account and your Groww holdings\n"
        "/stops: practice stops and the GTT stop-losses at Groww\n"
        "/brief: the morning brief now\n"
        "/status: live orders, kill switch, day brake\n"
        "/halt: emergency kill switch (stops the watch and all trading, after you confirm)\n"
        "/buy NAME QTY or /buy NAME ₹AMOUNT: a PRACTICE buy, after you confirm\n"
        "/sell NAME QTY: a PRACTICE sell, after you confirm")

# shown by Telegram when you type "/" (setMyCommands, sent when the poller starts)
COMMANDS = [{"command": "lookup", "description": "Look up a company: price, trend, stop, news (name or code)"},
            {"command": "chart", "description": "Price chart: 3 months daily, EMAs, cost and stop (name or code)"},
            {"command": "size", "description": "Position size: NAME [risk% max%]"},
            {"command": "buy", "description": "PRACTICE buy: NAME QTY or NAME ₹AMOUNT (you confirm)"},
            {"command": "sell", "description": "PRACTICE sell: NAME QTY (you confirm)"},
            {"command": "portfolio", "description": "Practice account and Groww holdings"},
            {"command": "stops", "description": "Practice stops and GTT stop-losses"},
            {"command": "brief", "description": "The morning brief now"},
            {"command": "status", "description": "Live orders, kill switch, day brake"},
            {"command": "halt", "description": "Emergency kill switch: stop watch and trading"},
            {"command": "help", "description": "What the bot can do"}]
NAMED = ("lookup", "chart", "size", "buy", "sell")   # commands that take a company name or code
GO = {"portfolio", "stops", "status"}   # the read commands the brief's buttons may run (go:<command>)
CHART_RANGE = "3M"
MAX_CHOICES = 5
TICKER_RE = re.compile(r"[A-Z0-9][A-Z0-9&_-]{0,19}(?:\.BO)?")   # an NSE code (M&M, BAJAJ-AUTO) or a BSE-only XYZ.BO
BSE_RE = re.compile(r"[A-Z0-9][A-Z0-9&_-]{0,19}\.BO")


def bse_only(ticker: str) -> bool:
    """A BSE-only stock (no NSE listing), tracked as <code>.BO: information only, never bought."""
    return bool(BSE_RE.fullmatch(str(ticker or "").upper()))


def lookup_buttons(ticker: str) -> dict[str, Any] | None:
    """The /lookup reply's buttons; None for a code that cannot ride in callback data."""
    if not TICKER_RE.fullmatch(ticker or ""):
        return None
    if bse_only(ticker):   # not tradable here: no sizing and no buy, only what is information
        return {"inline_keyboard": [[{"text": "🛡️ Stops", "callback_data": f"st:{ticker}"},
                                     {"text": "📈 Chart", "callback_data": f"ch:{ticker}"}]]}
    return {"inline_keyboard": [[{"text": "📏 Size (1%)", "callback_data": f"sz:{ticker}:1"},
                                 {"text": "🛡️ Stops", "callback_data": f"st:{ticker}"}],
                                [{"text": "📈 Chart", "callback_data": f"ch:{ticker}"},
                                 {"text": "🟢 Practice Buy", "callback_data": f"by:{ticker}"}]]}


def e(v: Any) -> str:
    """HTML-escape anything for a Telegram HTML message."""
    return html.escape(str(v), quote=False)


def fit(text: str, limit: int = LIMIT) -> str:
    """Cut a reply to Telegram's limit at a line end (never inside a tag or an entity)."""
    if len(text) <= limit:
        return text
    cut = text.rfind("\n", 0, limit - 20)
    return (text[:cut] if cut > 0 else text[:limit - 20].rsplit(" ", 1)[0]) + "\n…"


def inr(v: Any, d: int = 0) -> str:
    from .takeaway import _money
    return _money(v, d)


def pct(v: Any, d: int = 1) -> str:
    return "n/a" if v is None else f"{'+' if v >= 0 else ''}{v * 100:.{d}f}%"


def short_name(name: str | None) -> str:
    return re.sub(r"\s+(Limited|Ltd\.?)$", "", str(name or "").strip(), flags=re.I)


def label(symbol: str, name: str | None) -> str:
    """'Tata Steel (TATASTEEL)', escaped; just the code when the name is unknown."""
    n = short_name(name)
    return f"{e(n)} ({e(symbol)})" if n and n.upper() != symbol.upper() else e(symbol)


class TelegramError(RuntimeError):
    pass


class TelegramAPI:
    """The few Bot API methods the commands need. Errors are raised with the token removed."""

    def __init__(self, token: str, session: requests.Session | None = None):
        self.token = token
        self.session = session or requests.Session()
        add_log_secret(token)

    def call(self, method: str, payload: dict[str, Any], timeout: float = 10.0) -> Any:
        try:
            r = self.session.post(f"https://api.telegram.org/bot{self.token}/{method}", json=payload, timeout=timeout + 10)
            data = r.json()
        except Exception as ex:  # noqa: BLE001
            raise TelegramError(redact(f"{method}: {type(ex).__name__}: {ex}", self.token)) from None
        if not isinstance(data, dict) or not data.get("ok"):
            desc = data.get("description") if isinstance(data, dict) else data
            raise TelegramError(redact(f"{method}: {desc}", self.token))
        return data.get("result")

    def upload(self, method: str, payload: dict[str, Any], files: dict[str, tuple[str, bytes, str]],
               timeout: float = 30.0) -> Any:
        """A multipart call (sendPhoto with the picture's bytes). Same errors as ``call``."""
        try:
            r = self.session.post(f"https://api.telegram.org/bot{self.token}/{method}",
                                  data={k: v if isinstance(v, str) else json.dumps(v) for k, v in payload.items()},
                                  files=files, timeout=timeout)
            data = r.json()
        except Exception as ex:  # noqa: BLE001
            raise TelegramError(redact(f"{method}: {type(ex).__name__}: {ex}", self.token)) from None
        if not isinstance(data, dict) or not data.get("ok"):
            desc = data.get("description") if isinstance(data, dict) else data
            raise TelegramError(redact(f"{method}: {desc}", self.token))
        return data.get("result")


# --------------------------------------------------------------------------- #
# the poller lease: one poller per state directory, across processes
# --------------------------------------------------------------------------- #
def take_lease(state_dir: Path, owner: str, now: float | None = None) -> bool:
    """Take or renew the poller lease (state/telegram_commands.lock). Two pollers would steal each other's updates
    (Telegram answers 409), so only the holder polls; a lease not renewed for LEASE_S is free."""
    path = Path(state_dir) / LEASE_FILE
    now = time.time() if now is None else now
    try:
        cur = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cur = None
    if isinstance(cur, dict) and cur.get("owner") != owner and now - float(cur.get("at") or 0) < LEASE_S:
        return False
    atomic_write(path, json.dumps({"owner": owner, "at": now}))
    return True


# --------------------------------------------------------------------------- #
# the bot
# --------------------------------------------------------------------------- #
class CommandBot:
    """``app`` is the dashboard's App (the Live one); every command uses its Demo child (``app.demo``)."""

    def __init__(self, settings: Any, app: Any, api: TelegramAPI, *, clock: Callable[[], float] = time.time):
        self.settings = settings
        self.app = app
        self.api = api
        self.clock = clock
        self.chat = str(settings.telegram_chat_id or "").strip()
        self.path = Path(settings.state_dir) / STATE_FILE
        self.pending: dict[str, dict[str, Any]] = {}   # order confirmations, by nonce
        self.picks: dict[str, dict[str, Any]] = {}     # "Did you mean…" choices, by nonce
        self.halts: dict[str, dict[str, Any]] = {}     # /halt confirmations, by nonce
        self._rate: deque[float] = deque()
        self._rate_warned = 0.0

    # -- state ---------------------------------------------------------------------------------------
    def load(self) -> dict[str, Any]:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return d if isinstance(d, dict) else {}

    def _save(self, **changes: Any) -> None:
        d = self.load()
        d.update(changes)
        atomic_write(self.path, json.dumps(d))

    @property
    def practice(self) -> Any:
        return self.app.demo

    # -- polling ---------------------------------------------------------------------------------------
    def poll_once(self, timeout: int = POLL_TIMEOUT_S) -> int:
        """One getUpdates round; handles what came. Returns the number of updates. Raises TelegramError."""
        offset = int(self.load().get("offset") or 0)
        updates = self.api.call("getUpdates", {"offset": offset, "timeout": timeout,
                                               "allowed_updates": ["message", "callback_query"]}, timeout=timeout)
        n = 0
        for u in updates or []:
            uid = int(u.get("update_id") or 0)
            # saved first: a crash while handling never replays this update (an order is placed at most once)
            self._save(offset=max(offset, uid + 1))
            offset = max(offset, uid + 1)
            n += 1
            try:
                self.handle(u)
            except Exception:  # noqa: BLE001 - one bad update never stops the poller
                log.exception("telegram command failed")
        self._save(last_poll_at=datetime.now(IST).isoformat(timespec="seconds"), last_error=None,
                   handled=int(self.load().get("handled") or 0) + n)
        return n

    def handle(self, u: dict[str, Any]) -> None:
        if "callback_query" in u:
            self.on_callback(u["callback_query"])
            return
        msg = u.get("message") or {}
        chat = str((msg.get("chat") or {}).get("id") or "")
        sender = str((msg.get("from") or {}).get("id") or "")
        if not chat or chat != self.chat or sender != self.chat:   # a private chat: the sender is the chat
            log.info("telegram: ignored an update from another chat or sender (%s)", chat or "unknown")
            return
        text = str(msg.get("text") or "").strip()
        if not text.startswith("/"):
            return
        parts = text.split()
        cmd = parts[0][1:].split("@", 1)[0].lower()
        args = parts[1:]
        if cmd == "halt":   # the emergency stop is never rate-limited
            self._guard(cmd, lambda: self.cmd_halt(args))
            return
        if not self._allow():
            if self.clock() - self._rate_warned > 60:
                self._rate_warned = self.clock()
                self.reply("Too many commands: wait a minute.")
            return
        plain = {"help": self.cmd_help, "start": self.cmd_help, "status": self.cmd_status,
                 "portfolio": self.cmd_portfolio, "brief": self.cmd_brief, "stops": self.cmd_stops}
        if cmd not in plain and cmd not in NAMED:
            self.reply(f"Unknown command /{e(cmd)}. /help lists them.")
            return
        self._guard(cmd, lambda: plain[cmd](args) if cmd in plain else self._named(cmd, args))

    def _guard(self, cmd: str, fn: Callable[[], Any]) -> None:
        try:
            fn()
        except Exception as ex:  # noqa: BLE001 - every command answers, even when it fails
            log.warning("telegram /%s failed: %s", cmd, redact(ex, self.api.token))
            self.reply(f"/{e(cmd)} failed: {e(type(ex).__name__)}: {e(redact(ex, self.api.token))[:300]}")

    # -- company names -----------------------------------------------------------------------------------
    USAGE = {"lookup": "/lookup NAME (a company name or NSE code)",
             "chart": "/chart NAME (a company name or NSE code)",
             "size": "/size NAME [risk% max%], e.g. /size tata steel 1 10",
             "buy": "/buy NAME QTY or /buy NAME ₹AMOUNT (practice account only)",
             "sell": "/sell NAME QTY (practice account only)"}

    def _split(self, cmd: str, args: list[str]) -> tuple[str, list[str]] | str:
        """(the company words, the numbers after them), or the usage text. Names of any length: the numbers are
        read from the end (/size: up to two; /buy and /sell: the last word)."""
        args = list(args)
        if cmd in ("lookup", "chart"):
            rest: list[str] = []
        elif cmd == "size":
            rest = []
            while args and len(rest) < 2 and re.fullmatch(r"\d+(?:\.\d+)?%?", args[-1]):
                rest.insert(0, args.pop())
        else:
            if len(args) < 2:
                return self.USAGE[cmd]
            rest = [args.pop()]
        if not args:
            return self.USAGE[cmd]
        return " ".join(args), rest

    def _names(self) -> Any | None:
        try:
            return getattr(self.practice, "names", None)
        except Exception:  # noqa: BLE001
            return None

    def _table(self) -> dict[str, str]:
        n = self._names()
        try:
            return n._nse_names() if n is not None else {}
        except Exception:  # noqa: BLE001
            return {}

    def name_of(self, symbol: str) -> str | None:
        sym = symbol.upper()
        name = self._table().get(sym)
        if name or not bse_only(sym):
            return name
        n = self._names()   # a BSE-only code is not in NSE's list: the Groww instrument list may know it
        try:
            got = n.lookup([sym.removesuffix(".BO")]) if n is not None else {}
            return (got.get(sym.removesuffix(".BO")) or {}).get("name")
        except Exception:  # noqa: BLE001
            return None

    def _named(self, cmd: str, args: list[str]) -> None:
        got = self._split(cmd, args)
        if isinstance(got, str):
            self.reply(f"Usage: {e(got)}")
            return
        text, rest = got
        if cmd in ("buy", "sell") and not re.fullmatch(r"(?:₹|rs\.?|inr)?\d+(?:\.\d+)?", rest[-1].replace(",", ""), re.I):
            self.reply(f"Usage: {e(self.USAGE[cmd])}")   # a bad quantity is caught before any company is chosen
            return
        target = self._pick(cmd, text, rest)
        if target is not None:
            self._run(cmd, rest, target)

    def _pick(self, cmd: str, text: str, rest: list[str]) -> tuple[str, str | None] | None:
        """The one company ``text`` means, or None after asking which (several match) or suggesting names (none)."""
        table = self._table()
        names = self._names()
        raw = text.strip()
        if bse_only(raw):   # a BSE-only code: not in NSE's list, taken as typed
            return raw.upper(), self.name_of(raw)
        if not table or names is None:   # no name list (offline): the dashboard's own resolve
            return self.practice.resolve(raw)
        if raw.upper() in table:
            return raw.upper(), table[raw.upper()]
        hits = names.search(raw, limit=8)
        if hits:
            top = hits[0]["score"]
            if top >= 60 and sum(1 for h in hits if h["score"] == top) == 1:
                return hits[0]["symbol"], hits[0]["name"]
            self._offer(cmd, rest, [(h["symbol"], h["name"]) for h in hits[:MAX_CHOICES]],
                        f"“{e(raw)}” matches several companies. Which one?")
            return None
        close = self._closest(raw, table)
        if close:
            self._offer(cmd, rest, close, f"No company is called “{e(raw)}”. Did you mean:")
        else:
            self.reply(f"No NSE company matches “{e(raw)}”. Try the company's name or its NSE code.")
        return None

    @staticmethod
    def _closest(raw: str, table: dict[str, str]) -> list[tuple[str, str]]:
        import difflib

        from .instruments import CompanyNames
        q = CompanyNames._norm(raw)
        by_name: dict[str, str] = {}
        for sym, name in table.items():
            by_name.setdefault(CompanyNames._norm(name), sym)
        out: list[tuple[str, str]] = []
        for n in difflib.get_close_matches(q, list(by_name), n=MAX_CHOICES, cutoff=0.6):
            out.append((by_name[n], table[by_name[n]]))
        for sym in difflib.get_close_matches(raw.upper().replace(" ", ""), list(table), n=MAX_CHOICES, cutoff=0.75):
            if all(sym != o[0] for o in out):
                out.append((sym, table[sym]))
        return out[:MAX_CHOICES]

    def _offer(self, cmd: str, rest: list[str], choices: list[tuple[str, str]], text: str) -> None:
        nonce = secrets.token_urlsafe(9)
        self.picks = {k: v for k, v in self.picks.items() if self.clock() - v["at"] < CONFIRM_TTL_S}
        self.picks[nonce] = {"chat": self.chat, "cmd": cmd, "rest": rest, "choices": choices, "at": self.clock()}
        rows = [[{"text": f"{short_name(n)} ({sym})"[:60], "callback_data": f"pk:{nonce}:{i}"}]
                for i, (sym, n) in enumerate(choices)]
        self.reply(f"{text}\n(for /{e(cmd)}{(' ' + e(' '.join(rest))) if rest else ''})", {"inline_keyboard": rows})

    def _run(self, cmd: str, rest: list[str], target: tuple[str, str | None]) -> None:
        if cmd == "lookup":
            self.cmd_lookup(target)
        elif cmd == "chart":
            self.cmd_chart(target)
        elif cmd == "size":
            self.cmd_size(target, rest)
        else:
            self.cmd_order(cmd, target, rest)

    def _allow(self) -> bool:
        now = self.clock()
        while self._rate and now - self._rate[0] >= 60:
            self._rate.popleft()
        if len(self._rate) >= RATE_PER_MIN:
            return False
        self._rate.append(now)
        return True

    def reply(self, text: str, markup: dict[str, Any] | None = None) -> Any:
        payload: dict[str, Any] = {"chat_id": self.chat, "text": fit(text), "parse_mode": "HTML",
                                   "disable_web_page_preview": True}
        if markup:
            payload["reply_markup"] = markup
        return self.api.call("sendMessage", payload)

    # -- read commands ---------------------------------------------------------------------------------
    def cmd_help(self, args: list[str]) -> None:
        self.reply(HELP)

    def cmd_status(self, args: list[str]) -> None:
        from . import day_brake, killswitch
        s = self.settings
        live = bool(s.groww_live_orders and s.use_groww)
        lines = [f"<b>Status</b> · {datetime.now(IST):%d %b %H:%M} IST",
                 ("LIVE · real Groww account · live orders ON (the agent/watch can trade)" if live
                  else "LIVE · your real Groww account · live orders OFF"),
                 "Telegram commands: practice account only (never live orders).",
                 e(killswitch.reason(s.state_dir) or "Kill switch: not halted."),
                 e(day_brake.reason(s.state_dir) or "Day brake: off.")]
        self.reply("\n".join(lines))

    def cmd_lookup(self, target: tuple[str, str | None]) -> None:
        from .takeaway import lookup_takeaway
        app = self.practice
        r = app.lookup(target[0])
        r["name"] = r.get("name") or target[1]
        try:
            r["news"] = app.news_for_ticker(r["ticker"])
        except Exception:  # noqa: BLE001
            r["news"] = {"items": []}
        m = r.get("momentum") or {}
        hist = r.get("history") or []
        ma200 = next((h.get("ma200") for h in reversed(hist) if h.get("ma200")), None)
        lines = [f"<b>{label(r['ticker'], r.get('name'))}</b>",
                 f"Price {inr(r.get('price'), 2)}" if r.get("price") else "Price unavailable"]
        if m.get("error"):
            lines.append(f"Momentum unavailable: {e(m['error'])}")
        else:
            lines.append(f"Momentum: 1m {pct(m.get('ret_1m'))} · 3m {pct(m.get('ret_3m'))} · 6m {pct(m.get('ret_6m'))}")
        if ma200:
            lines.append(f"200-day average {inr(ma200, 2)} ({'above' if m.get('above_200dma') else 'below'} it)")
        pos = r.get("position")
        if pos and pos.get("stop"):
            lines.append(f"Your practice stop: {inr(pos['stop'], 2)} ({e(pos.get('stop_label') or 'trailing')})")
        else:
            try:
                q = app.size_quote(r["ticker"])
                if q.get("stop"):
                    lines.append(f"Suggested stop: {inr(q['stop'], 2)} ({e(q.get('basis') or 'trailing')})")
            except Exception:  # noqa: BLE001
                pass
        if r.get("filter_flags"):
            lines.append("Filters: " + e("; ".join(r["filter_flags"])))
        items = (r.get("news") or {}).get("items") or []
        if items:
            lines.append("<b>Recent news</b>")
            lines += [f"• {e(str(n.get('title') or '')[:140])} ({e(n.get('source') or '')})" for n in items[:3]]
        note = lookup_takeaway(r, None, None)
        if note:
            lines.append("<b>What this means</b>\n" + e(note))
        self.reply("\n".join(lines), lookup_buttons(str(r["ticker"])))

    def cmd_chart(self, target: tuple[str, str | None]) -> None:
        """3 months of daily candles as a picture, from the dashboard's own candles (the lookup's price source)."""
        from .charts import stock_daily_png
        ticker, name = target
        data = self.practice.candles(ticker, CHART_RANGE)
        who = label(ticker, name)
        png = stock_daily_png(f"{short_name(name) + ' (' + ticker + ')' if name else ticker} · 3 months, daily", data)
        if png is None:
            self.reply(f"{who}: no chart ({e(data.get('error') or 'no price history')}).")
            return
        bars = data["bars"]
        first, last = float(bars[0]["close"]), float(bars[-1]["close"])
        caption = (f"📈 <b>{who}</b> · close {inr(last, 2)} ({pct(last / first - 1 if first else None)} over 3 months)"
                   + (f"\nYour cost {inr(data['position']['cost'], 2)} · stop {inr(data['position']['stop'], 2)}"
                      if (data.get("position") or {}).get("cost") and (data.get("position") or {}).get("stop") else ""))
        self.api.upload("sendPhoto", {"chat_id": self.chat, "caption": caption[:1000], "parse_mode": "HTML"},
                        {"photo": (f"{ticker}.png", png, "image/png")})

    def cmd_size(self, target: tuple[str, str | None], rest: list[str]) -> None:
        nums = [a.rstrip("%") for a in rest[:2]]
        risk = float(nums[0]) if nums else 1.0
        cap = float(nums[1]) if len(nums) > 1 else 10.0
        if not (0 < risk <= 5 and 0 < cap <= 100):
            self.reply("Risk must be above 0 and at most 5%; the cap above 0 and at most 100%.")
            return
        ticker, name = target
        q = self.practice.size_quote(ticker, risk_pct=risk, max_pct=cap)
        if not q.get("qty"):
            self.reply(f"{label(ticker, name)}: no position size ({e(q.get('reason') or 'too small for one share')}).")
            return
        rt = q.get("round_trip_cost") or {}
        lines = [f"<b>{label(ticker, name or q.get('name'))}</b> · practice account {inr(q.get('equity'))}",
                 f"{q['qty']} shares × {inr(q['price'], 2)} = {inr(q['notional'])} (risk {risk:g}%, cap {cap:g}%)",
                 f"Stop {inr(q['stop'], 2)} ({e(q.get('basis') or '')})"]
        if rt:
            lines.append(f"Round-trip charges about {inr(rt.get('charges', rt.get('total')), 2)}")
        self.reply("\n".join(lines))

    def _groww_portfolio(self) -> dict[str, Any]:
        """The dashboard's read: Groww's market-hours rules, never forced (no login is forced from Telegram)."""
        try:
            return self.app.my_portfolio()
        except Exception as ex:  # noqa: BLE001
            return {"error": f"{type(ex).__name__}: {ex}"}

    def cmd_portfolio(self, args: list[str]) -> None:
        pb = self.practice.practice_broker
        acct = pb.account()
        lines = [f"<b>Practice account</b> · cash {inr(acct.cash)} · value {inr(acct.equity)}"]
        for p in sorted(pb.positions(), key=lambda p: p.symbol)[:25]:
            px = p.current_price
            pl = (px / p.avg_entry_price - 1) if px and p.avg_entry_price else None
            lines.append(f"• {label(p.symbol, self.name_of(p.symbol))} {p.qty:g} @ {inr(p.avg_entry_price, 2)} → "
                         f"{inr(px, 2) if px else 'n/a'} ({pct(pl)})")
        g = self._groww_portfolio()
        if g.get("linked") is False:
            lines.append("Groww: not linked.")
        elif g.get("holdings"):
            when = g.get("reason") if g.get("source") == "saved" else "live"
            lines.append(f"<b>Groww</b> ({e(when)}) · value {inr(g.get('value'))} · P/L {inr(g.get('pl'))} ({pct(g.get('pl_pct'))})")
            for h in sorted(g["holdings"], key=lambda h: -(h.get("value") or 0))[:15]:
                sym = str(h.get("symbol") or "")
                lines.append(f"• {label(sym, h.get('name') or self.name_of(sym))} {h.get('qty')} @ {inr(h.get('avg_price'), 2)} → "
                             f"{inr(h.get('price'), 2) if h.get('price') else 'n/a'}")
        elif g.get("error"):
            lines.append(f"Groww: unavailable ({e(str(g['error'])[:200])}).")
        self.reply("\n".join(lines))

    def _stop_lines(self, only: str | None = None) -> tuple[list[str], list[str]]:
        """(practice stop lines, GTT stop-loss lines), for every position or just ``only``."""
        from .risk import position_stop
        from .state import State
        pb = self.practice.practice_broker
        practice: list[str] = []
        for p in sorted((p for p in pb.positions() if only is None or p.symbol == only), key=lambda p: p.symbol)[:25]:
            bars: list[dict[str, Any]] = []
            if (p.stop_type or "trailing") == "trailing":
                try:
                    bars = self.app.prices.history(p.symbol, "1y")
                except Exception:  # noqa: BLE001
                    bars = []
            st = position_stop(p, bars)
            who = label(p.symbol, self.name_of(p.symbol))
            if st["level"] is None:
                practice.append(f"• {who}: no stop-loss")
            elif p.current_price:
                practice.append(f"• {who}: {e(st['label'])} stop {inr(st['level'], 2)}, "
                                f"{(1 - st['level'] / p.current_price) * 100:.1f}% below {inr(p.current_price, 2)}")
            else:
                practice.append(f"• {who}: {e(st['label'])} stop {inr(st['level'], 2)}")
        gtt = State(Path(self.settings.state_dir) / "state.json").data.get("gtt_stops") or {}
        rows = [(sym, r) for sym, r in sorted(gtt.items()) if only is None or sym == only][:25]
        groww = [f"• {label(sym, self.name_of(sym))} qty {e(r.get('qty'))} trigger {inr(r.get('trigger'), 2)} ({e(r.get('status') or '')})"
                 for sym, r in rows]
        return practice, groww

    def cmd_stops(self, args: list[str]) -> None:
        practice, groww = self._stop_lines()
        lines = ["<b>Practice stops</b>", *(practice or ["No practice positions."])]
        if groww:
            lines += ["<b>GTT stop-losses at Groww</b>", *groww]
        self.reply("\n".join(lines))

    def stops_for(self, ticker: str) -> None:
        """The 🛡️ Stops button: this company's practice stop and GTT stop-loss, or the stop the sizing would use."""
        name = self.name_of(ticker)
        practice, groww = self._stop_lines(ticker)
        lines = [f"🛡️ <b>Stops · {label(ticker, name)}</b>"]
        lines += [*practice, *(["<b>GTT stop-loss at Groww</b>", *groww] if groww else [])]
        if not practice and not groww:
            lines.append("Not held in the practice account and no GTT stop-loss at Groww.")
            try:
                q = self.practice.size_quote(ticker)
                if q.get("stop"):
                    lines.append(f"Suggested stop: {inr(q['stop'], 2)} ({e(q.get('basis') or 'trailing')})")
            except Exception:  # noqa: BLE001
                pass
        self.reply("\n".join(lines))

    def cmd_brief(self, args: list[str]) -> None:
        from .telegram_brief import telegram_brief
        self.reply("Building the morning brief…")
        email = self.app.digest_email("morning", groww=self._groww_portfolio)
        b = telegram_brief(email, getattr(self.settings, "allowed_hosts", None), commands=True)
        self.reply(b["html"], b.get("button"))

    # -- the emergency stop ----------------------------------------------------------------------------
    def cmd_halt(self, args: list[str]) -> None:
        """Ask before engaging the kill switch: ENGAGE HALT / Cancel, a one-time nonce good for 60 seconds."""
        from . import killswitch
        now = self.clock()
        self.halts = {k: v for k, v in self.halts.items() if now - v["at"] < HALT_TTL_S}
        nonce = secrets.token_urlsafe(9)
        self.halts[nonce] = {"chat": self.chat, "at": now}
        already = killswitch.reason(self.settings.state_dir)
        text = ("⛔ <b>Emergency stop</b>\n"
                + (f"{e(already)}\nEngaging again re-runs the stop steps.\n" if already else "")
                + "This engages the kill switch: it writes state/HALT, stops the watch, blocks every order path "
                "(live and practice) and sends an alert. Release it from the dashboard or the CLI; Telegram cannot.\n"
                "Confirm within 60 seconds.")
        self.reply(text, {"inline_keyboard": [[{"text": "⛔ ENGAGE HALT", "callback_data": f"halt_confirm:{nonce}"},
                                               {"text": "Cancel", "callback_data": f"no:{nonce}"}]]})

    def _halt_tap(self, cid: Any, msg: dict[str, Any], verb: str, nonce: str) -> None:
        h = self.halts.pop(nonce, None)   # one-time: a second tap finds nothing
        if h is None or h.get("chat") != self.chat:
            self._answer(cid, "Nothing to confirm (already done, cancelled or unknown).")
            return
        self._clear_buttons(msg)
        if verb != "halt_confirm":
            self._answer(cid, "Cancelled.")
            self.reply("Cancelled: the kill switch was not engaged.")
            return
        if self.clock() - h["at"] >= HALT_TTL_S:
            self._answer(cid, "Expired: not engaged.")
            self.reply("Expired: the kill switch was not engaged. Send /halt again.")
            return
        self._answer(cid, "Engaging the kill switch…")
        try:
            r = self.app.kill_switch("HALT", by="telegram")
        except Exception as ex:  # noqa: BLE001 - say so: the user must know the stop did not happen
            log.warning("telegram /halt failed: %s", redact(ex, self.api.token))
            self.reply(f"⚠️ The kill switch was NOT engaged: {e(type(ex).__name__)}: {e(redact(ex, self.api.token))[:300]}\n"
                       "Use the dashboard or the CLI (kill-switch).")
            return
        self.reply(f"⛔ <b>HALT engaged</b>\n{e(r.get('summary') or '')}".rstrip())

    # -- the /lookup buttons -----------------------------------------------------------------------------
    def _lookup_tap(self, cid: Any, verb: str, rest: str) -> None:
        """sz:<TICKER>:<RISK>, st:<TICKER>, ch:<TICKER>, by:<TICKER>. Read-only (no order), so no nonce; rate-limited like a command."""
        ticker, _, risk = rest.partition(":")
        if not TICKER_RE.fullmatch(ticker) or (verb == "sz") != bool(risk) or (risk and not re.fullmatch(r"\d+(?:\.\d+)?", risk)):
            self._answer(cid, "Unknown button.")
            return
        if not self._allow():
            self._answer(cid, "Too many commands: wait a minute.")
            return
        self._answer(cid, {"sz": f"Size at {risk}% risk", "st": "Stops", "ch": "Drawing the chart…", "by": "Practice buy"}[verb])
        target = (ticker, self.name_of(ticker))
        if verb == "sz":
            self._guard("size", lambda: self.cmd_size(target, [risk]))
        elif verb == "st":
            self._guard("stops", lambda: self.stops_for(ticker))
        elif verb == "ch":
            self._guard("chart", lambda: self.cmd_chart(target))
        else:   # never places anything: it shows the command to send, which then asks to confirm as usual
            self.reply(f"🟢 Practice buy of {label(*target)}: send\n<code>/buy {e(ticker)} QTY</code>\n"
                       f"or <code>/buy {e(ticker)} ₹AMOUNT</code>\n(you confirm before anything is placed)")

    def _go_tap(self, cid: Any, cmd: str) -> None:
        """The daily brief's buttons: go:portfolio, go:stops, go:status. Read commands only; rate-limited."""
        if cmd not in GO:
            self._answer(cid, "Unknown button.")
            return
        if not self._allow():
            self._answer(cid, "Too many commands: wait a minute.")
            return
        self._answer(cid, f"/{cmd}")
        run = {"portfolio": self.cmd_portfolio, "stops": self.cmd_stops, "status": self.cmd_status}[cmd]
        self._guard(cmd, lambda: run([]))

    # -- practice orders -------------------------------------------------------------------------------
    def cmd_order(self, side: str, target: tuple[str, str | None], rest: list[str]) -> None:
        from . import killswitch
        halted = killswitch.reason(self.settings.state_dir, f"place a practice {side}")
        if halted:
            self.reply(e(halted))
            return
        raw = rest[-1].replace(",", "") if rest else ""
        amount = qty = None
        m = re.fullmatch(r"(?:₹|rs\.?|inr)?(\d+(?:\.\d+)?)", raw, re.I)
        if m is None:
            self.reply(f"Usage: {e(self.USAGE[side])}")
            return
        if raw[0].isdigit():
            qty = float(m.group(1))
            if not qty.is_integer() or qty < 1:
                self.reply("The quantity must be a whole number of shares.")
                return
        elif side == "buy":
            amount = float(m.group(1))
        else:
            self.reply("Sell a number of shares: /sell NAME QTY.")
            return
        ticker, name = target
        who = label(ticker, name)
        if side == "buy" and bse_only(ticker):   # said before any confirmation, not after the tap
            from .broker import BSE_ONLY_MESSAGE
            self.reply(f"{who}: {e(BSE_ONLY_MESSAGE)}.")
            return
        try:
            price = float(self.practice.practice_broker.latest_price(ticker))
        except Exception as ex:  # noqa: BLE001
            self.reply(f"{who}: no price ({e(type(ex).__name__)}). Nothing placed.")
            return
        est_qty = qty if qty is not None else float(int((amount or 0) // price))
        nonce = secrets.token_urlsafe(9)
        self.pending = {k: v for k, v in self.pending.items() if self.clock() - v["at"] < CONFIRM_TTL_S}
        self.pending[nonce] = {"chat": self.chat, "side": side, "symbol": ticker, "name": name, "qty": qty,
                               "amount": amount, "at": self.clock()}
        text = (f"<b>PRACTICE {side.upper()}</b> {int(est_qty)} × {who} at about {inr(price, 2)} = {inr(est_qty * price)} "
                f"plus charges.\nCompany: <b>{who}</b>. Is that the one you meant?\n"
                "Practice money only. Confirm within 2 minutes.")
        self.reply(text, {"inline_keyboard": [[{"text": "Confirm", "callback_data": f"ok:{nonce}"},
                                               {"text": "Cancel", "callback_data": f"no:{nonce}"}]]})

    def on_callback(self, cq: dict[str, Any]) -> None:
        msg = cq.get("message") or {}
        chat = str((msg.get("chat") or {}).get("id") or "")
        sender = str((cq.get("from") or {}).get("id") or "")
        cid = cq.get("id")
        if not chat or chat != self.chat or sender != self.chat:   # only you can confirm, never another group member
            log.info("telegram: ignored a button press from another chat or sender (%s)", chat or "unknown")
            return
        data = str(cq.get("data") or "")
        verb, _, nonce = data.partition(":")
        if verb == "pk":
            self._picked(cid, msg, nonce)
            return
        if verb in ("sz", "st", "ch", "by"):
            self._lookup_tap(cid, verb, nonce)
            return
        if verb == "go":
            self._go_tap(cid, nonce)
            return
        if verb == "halt_confirm" or (verb == "no" and nonce in self.halts):
            self._halt_tap(cid, msg, verb, nonce)
            return
        p = self.pending.pop(nonce, None)   # one-time: a second tap finds nothing
        if p is None or p.get("chat") != chat:
            self._answer(cid, "Nothing to confirm (already done, cancelled or unknown).")
            return
        self._clear_buttons(msg)
        if self.clock() - p["at"] >= CONFIRM_TTL_S:
            self._answer(cid, "Expired: nothing placed.")
            self.reply(f"Expired: the practice {e(p['side'])} of {label(p['symbol'], p.get('name'))} was not placed.")
            return
        if verb != "ok":
            self._answer(cid, "Cancelled.")
            self.reply(f"Cancelled: practice {e(p['side'])} of {label(p['symbol'], p.get('name'))}.")
            return
        self._answer(cid, "Placing the practice order…")
        self.reply(self.place(p))

    def _picked(self, cid: Any, msg: dict[str, Any], data: str) -> None:
        """A "Did you mean…" button: run the same command for the chosen company (one-time, 2 minutes)."""
        nonce, _, idx = data.partition(":")
        p = self.picks.pop(nonce, None)
        if p is None or p.get("chat") != self.chat:
            self._answer(cid, "Nothing to choose (already chosen or unknown).")
            return
        self._clear_buttons(msg)
        if self.clock() - p["at"] >= CONFIRM_TTL_S:
            self._answer(cid, "Expired: send the command again.")
            return
        try:
            sym, name = p["choices"][int(idx)]
        except (ValueError, IndexError):
            self._answer(cid, "Unknown choice.")
            return
        self._answer(cid, f"{short_name(name)} ({sym})"[:60])
        self._guard(p["cmd"], lambda: self._run(p["cmd"], p["rest"], (sym, name)))

    def place(self, p: dict[str, Any]) -> str:
        """The confirmed order, through the dashboard's practice path. Returns the reply text."""
        from .broker import LocalPaperBroker
        from .ui import Busy, FilterWarning
        app = self.practice
        if not isinstance(getattr(app, "broker", None), LocalPaperBroker) or getattr(app.settings, "groww_live_orders", False):
            return "Refused: Telegram places practice orders only, and the practice account is not available."
        try:
            o = app.paper_order(p["symbol"], p["side"], notional=p.get("amount"), qty=p.get("qty"))
        except FilterWarning as ex:
            return f"Held back by the Indian-market filters (no override from Telegram): {e(ex)}"
        except (PermissionError, ValueError, LookupError, Busy) as ex:
            return f"Refused: {e(ex)}"
        return (f"<b>Practice {e(o['side']).upper()}</b> {o['qty']:g} × {label(o['symbol'], p.get('name'))} at "
                f"{inr(o['filled_avg_price'], 2)} = {inr(o['notional'])}, charges {inr(o.get('fees'), 2)}.")

    def _answer(self, cid: Any, text: str) -> None:
        if cid:
            try:
                self.api.call("answerCallbackQuery", {"callback_query_id": cid, "text": text[:190]})
            except TelegramError as ex:
                log.info("telegram: answerCallbackQuery failed: %s", ex)

    def _clear_buttons(self, msg: dict[str, Any]) -> None:
        if msg.get("message_id"):
            try:
                self.api.call("editMessageReplyMarkup", {"chat_id": self.chat, "message_id": msg["message_id"],
                                                          "reply_markup": {"inline_keyboard": []}})
            except TelegramError as ex:
                log.info("telegram: could not remove the buttons: %s", ex)

    # -- the loop ----------------------------------------------------------------------------------------
    def register_commands(self) -> bool:
        """setMyCommands: Telegram then lists the commands with their descriptions when you type "/". Never raises."""
        try:
            self.api.call("setMyCommands", {"commands": COMMANDS})
            return True
        except TelegramError as ex:
            log.info("telegram: could not register the command list: %s", ex)
            return False

    def run(self, stop: threading.Event) -> None:
        owner = f"{socket.gethostname()}:{os.getpid()}:{threading.get_ident()}"
        fails = 0
        warned_lease = False
        registered = False
        while not stop.is_set():
            if not take_lease(self.settings.state_dir, owner):
                if not warned_lease:
                    log.warning("telegram commands: another process is polling the bot; this one waits")
                    warned_lease = True
                stop.wait(30)
                continue
            if not registered:
                registered = self.register_commands()
            try:
                self.poll_once()
                fails = 0
            except Exception as ex:  # noqa: BLE001 - never dies; backs off
                from . import health
                msg = redact(f"{type(ex).__name__}: {ex}", self.api.token)
                self._save(last_error=msg, last_error_at=datetime.now(IST).isoformat(timespec="seconds"))
                health.note_run(self.settings.state_dir, "telegram_commands", ok=False, error=msg)
                stop.wait(BACKOFF_S[min(fails, len(BACKOFF_S) - 1)])
                fails += 1


def private_chat(chat_id: Any) -> bool:
    """A private chat with one person: a positive numeric id (groups and channels are negative, or @names)."""
    return bool(re.fullmatch(r"[1-9]\d*", str(chat_id or "").strip()))


def commands_configured(settings: Any) -> bool:
    """TELEGRAM_COMMANDS=true with a bot token and a PRIVATE chat id: commands are never taken from a group or a
    channel, where other members could run them or confirm an order."""
    return bool(getattr(settings, "telegram_commands", False) and settings.telegram_bot_token
                and private_chat(settings.telegram_chat_id))


def start(settings: Any, app_fn: Callable[[], Any] | None = None) -> threading.Event | None:
    """Start the poller thread when TELEGRAM_COMMANDS=true (and a bot token and chat id are set). Returns the Event
    that stops it, or None when it is off."""
    if not commands_configured(settings):
        if getattr(settings, "telegram_commands", False) and settings.telegram_chat_id and not private_chat(settings.telegram_chat_id):
            log.warning("TELEGRAM_COMMANDS is on but TELEGRAM_CHAT_ID is a group or channel: commands stay off "
                        "(they are only taken from your private chat with the bot)")
        return None

    def make_app() -> Any:
        from .runner import free_prices
        from .ui import App
        return App(settings, dotenv=None, prices=free_prices(settings))
    stop = threading.Event()

    def run() -> None:
        try:
            bot = CommandBot(settings, (app_fn or make_app)(), TelegramAPI(settings.telegram_bot_token))
            bot.run(stop)
        except Exception:  # noqa: BLE001
            log.exception("telegram commands stopped")
    threading.Thread(target=run, daemon=True, name="telegram-commands").start()
    log.info("Telegram commands on: answering chat %s (practice orders only)", settings.telegram_chat_id)
    return stop
