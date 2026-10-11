"""Telegram commands, step 1: read commands and PRACTICE orders, from one chat only.

The watch service long-polls the bot (``getUpdates``, outbound only) on its own thread and answers only
TELEGRAM_CHAT_ID; every other chat is ignored and logged without its text. The update offset is saved before an
update is handled, so a restart never replays a command (an order is at most once).

Commands: /help /status /lookup /size /portfolio /brief /stops, and /buy /sell in the PRACTICE account only. An order
is never placed by the command itself: it shows an inline Confirm / Cancel; the callback must come from the same chat
and carry the one-time nonce of a confirmation still under 2 minutes old, and a second tap does nothing. The order then
goes through the dashboard's own practice path (App.demo.paper_order): Indian filters (a flagged buy is held back;
there is no override here), price bands, the collar time rule, the day brake and HALT all apply.

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
RATE_PER_MIN = 20
POLL_TIMEOUT_S = 25
LIMIT = 4000               # Telegram allows 4096 characters per message
BACKOFF_S = (5, 15, 60)

HELP = ("<b>Commands</b> (practice account only; nothing here places a live order)\n"
        "/lookup SYMBOL: price, momentum, 200-day average, stop, news and what it means\n"
        "/size SYMBOL [risk% max%]: shares, cost, stop and charges (practice account)\n"
        "/portfolio: the practice account and your Groww holdings\n"
        "/stops: practice stops and the GTT stop-losses at Groww\n"
        "/brief: the morning brief now\n"
        "/status: live orders, kill switch, day brake\n"
        "/buy SYMBOL QTY or /buy SYMBOL ₹AMOUNT: a PRACTICE buy, after you confirm\n"
        "/sell SYMBOL QTY: a PRACTICE sell, after you confirm")


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
        self.pending: dict[str, dict[str, Any]] = {}
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
        if not self._allow():
            if self.clock() - self._rate_warned > 60:
                self._rate_warned = self.clock()
                self.reply("Too many commands: wait a minute.")
            return
        parts = text.split()
        cmd = parts[0][1:].split("@", 1)[0].lower()
        args = parts[1:]
        fn = {"help": self.cmd_help, "start": self.cmd_help, "status": self.cmd_status, "lookup": self.cmd_lookup,
              "size": self.cmd_size, "portfolio": self.cmd_portfolio, "brief": self.cmd_brief, "stops": self.cmd_stops,
              "buy": lambda a: self.cmd_order("buy", a), "sell": lambda a: self.cmd_order("sell", a)}.get(cmd)
        if fn is None:
            self.reply(f"Unknown command /{e(cmd)}. /help lists them.")
            return
        try:
            fn(args)
        except Exception as ex:  # noqa: BLE001 - every command answers, even when it fails
            log.warning("telegram /%s failed: %s", cmd, redact(ex, self.api.token))
            self.reply(f"/{e(cmd)} failed: {e(type(ex).__name__)}: {e(redact(ex, self.api.token))[:300]}")

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

    def _resolve(self, args: list[str], usage: str) -> tuple[str, str | None] | None:
        if not args:
            self.reply(f"Usage: {e(usage)}")
            return None
        return self.practice.resolve(" ".join(args))

    def cmd_lookup(self, args: list[str]) -> None:
        from .takeaway import lookup_takeaway
        got = self._resolve(args, "/lookup SYMBOL or company name")
        if got is None:
            return
        app = self.practice
        r = app.lookup(got[0])
        try:
            r["news"] = app.news_for_ticker(r["ticker"])
        except Exception:  # noqa: BLE001
            r["news"] = {"items": []}
        m = r.get("momentum") or {}
        hist = r.get("history") or []
        ma200 = next((h.get("ma200") for h in reversed(hist) if h.get("ma200")), None)
        lines = [f"<b>{e(r['ticker'])}</b>{(' · ' + e(r['name'])) if r.get('name') else ''}",
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
        self.reply("\n".join(lines))

    def cmd_size(self, args: list[str]) -> None:
        if not args:
            self.reply("Usage: /size SYMBOL [risk% max%]")
            return
        nums = [a.rstrip("%") for a in args[1:3]]
        try:
            risk = float(nums[0]) if nums else 1.0
            cap = float(nums[1]) if len(nums) > 1 else 10.0
        except ValueError:
            self.reply("Usage: /size SYMBOL [risk% max%], e.g. /size TCS 1 10")
            return
        if not (0 < risk <= 5 and 0 < cap <= 100):
            self.reply("Risk must be above 0 and at most 5%; the cap above 0 and at most 100%.")
            return
        ticker, _ = self.practice.resolve(args[0])
        q = self.practice.size_quote(ticker, risk_pct=risk, max_pct=cap)
        if not q.get("qty"):
            self.reply(f"{e(ticker)}: no position size ({e(q.get('reason') or 'too small for one share')}).")
            return
        rt = q.get("round_trip_cost") or {}
        lines = [f"<b>{e(q['ticker'])}</b> · practice account {inr(q.get('equity'))}",
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
            lines.append(f"• {e(p.symbol)} {p.qty:g} @ {inr(p.avg_entry_price, 2)} → {inr(px, 2) if px else 'n/a'} ({pct(pl)})")
        g = self._groww_portfolio()
        if g.get("linked") is False:
            lines.append("Groww: not linked.")
        elif g.get("holdings"):
            when = g.get("reason") if g.get("source") == "saved" else "live"
            lines.append(f"<b>Groww</b> ({e(when)}) · value {inr(g.get('value'))} · P/L {inr(g.get('pl'))} ({pct(g.get('pl_pct'))})")
            for h in sorted(g["holdings"], key=lambda h: -(h.get("value") or 0))[:15]:
                lines.append(f"• {e(h.get('symbol'))} {h.get('qty')} @ {inr(h.get('avg_price'), 2)} → {inr(h.get('price'), 2) if h.get('price') else 'n/a'}")
        elif g.get("error"):
            lines.append(f"Groww: unavailable ({e(str(g['error'])[:200])}).")
        self.reply("\n".join(lines))

    def cmd_stops(self, args: list[str]) -> None:
        from .risk import position_stop
        from .state import State
        pb = self.practice.practice_broker
        lines = ["<b>Practice stops</b>"]
        for p in sorted(pb.positions(), key=lambda p: p.symbol)[:25]:
            bars: list[dict[str, Any]] = []
            if (p.stop_type or "trailing") == "trailing":
                try:
                    bars = self.app.prices.history(p.symbol, "1y")
                except Exception:  # noqa: BLE001
                    bars = []
            st = position_stop(p, bars)
            if st["level"] is None:
                lines.append(f"• {e(p.symbol)}: no stop-loss")
            elif p.current_price:
                lines.append(f"• {e(p.symbol)}: {e(st['label'])} stop {inr(st['level'], 2)}, "
                             f"{(1 - st['level'] / p.current_price) * 100:.1f}% below {inr(p.current_price, 2)}")
            else:
                lines.append(f"• {e(p.symbol)}: {e(st['label'])} stop {inr(st['level'], 2)}")
        if len(lines) == 1:
            lines.append("No practice positions.")
        gtt = State(Path(self.settings.state_dir) / "state.json").data.get("gtt_stops") or {}
        if gtt:
            lines.append("<b>GTT stop-losses at Groww</b>")
            lines += [f"• {e(sym)} qty {e(r.get('qty'))} trigger {inr(r.get('trigger'), 2)} ({e(r.get('status') or '')})"
                      for sym, r in sorted(gtt.items())[:25]]
        self.reply("\n".join(lines))

    def cmd_brief(self, args: list[str]) -> None:
        from .telegram_brief import telegram_brief
        self.reply("Building the morning brief…")
        email = self.app.digest_email("morning", groww=self._groww_portfolio)
        b = telegram_brief(email, getattr(self.settings, "allowed_hosts", None))
        self.reply(b["html"], b.get("button"))

    # -- practice orders -------------------------------------------------------------------------------
    def cmd_order(self, side: str, args: list[str]) -> None:
        from . import killswitch
        usage = "/buy SYMBOL QTY or /buy SYMBOL ₹AMOUNT" if side == "buy" else "/sell SYMBOL QTY"
        if len(args) < 2:
            self.reply(f"Usage: {e(usage)} (practice account only)")
            return
        halted = killswitch.reason(self.settings.state_dir, f"place a practice {side}")
        if halted:
            self.reply(e(halted))
            return
        raw = args[-1].replace(",", "")
        amount = qty = None
        m = re.fullmatch(r"(?:₹|rs\.?|inr)?(\d+(?:\.\d+)?)", raw, re.I)
        if m is None:
            self.reply(f"Usage: {e(usage)}")
            return
        if raw[0].isdigit():
            qty = float(m.group(1))
            if not qty.is_integer() or qty < 1:
                self.reply("The quantity must be a whole number of shares.")
                return
        elif side == "buy":
            amount = float(m.group(1))
        else:
            self.reply("Sell a number of shares: /sell SYMBOL QTY.")
            return
        ticker, name = self.practice.resolve(" ".join(args[:-1]))
        try:
            price = float(self.practice.practice_broker.latest_price(ticker))
        except Exception as ex:  # noqa: BLE001
            self.reply(f"{e(ticker)}: no price ({e(type(ex).__name__)}). Nothing placed.")
            return
        est_qty = qty if qty is not None else float(int((amount or 0) // price))
        nonce = secrets.token_urlsafe(9)
        self.pending = {k: v for k, v in self.pending.items() if self.clock() - v["at"] < CONFIRM_TTL_S}
        self.pending[nonce] = {"chat": self.chat, "side": side, "symbol": ticker, "qty": qty, "amount": amount,
                               "at": self.clock()}
        what = f"{int(est_qty)} × {e(ticker)}{(' (' + e(name) + ')') if name else ''}"
        text = (f"<b>PRACTICE {side.upper()}</b> {what} at about {inr(price, 2)} = {inr(est_qty * price)} plus charges.\n"
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
        p = self.pending.pop(nonce, None)   # one-time: a second tap finds nothing
        if p is None or p.get("chat") != chat:
            self._answer(cid, "Nothing to confirm (already done, cancelled or unknown).")
            return
        self._clear_buttons(msg)
        if self.clock() - p["at"] >= CONFIRM_TTL_S:
            self._answer(cid, "Expired: nothing placed.")
            self.reply(f"Expired: the practice {e(p['side'])} of {e(p['symbol'])} was not placed.")
            return
        if verb != "ok":
            self._answer(cid, "Cancelled.")
            self.reply(f"Cancelled: practice {e(p['side'])} of {e(p['symbol'])}.")
            return
        self._answer(cid, "Placing the practice order…")
        self.reply(self.place(p))

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
        return (f"<b>Practice {e(o['side']).upper()}</b> {o['qty']:g} × {e(o['symbol'])} at {inr(o['filled_avg_price'], 2)} = "
                f"{inr(o['notional'])}, charges {inr(o.get('fees'), 2)}.")

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
    def run(self, stop: threading.Event) -> None:
        owner = f"{socket.gethostname()}:{os.getpid()}:{threading.get_ident()}"
        fails = 0
        warned_lease = False
        while not stop.is_set():
            if not take_lease(self.settings.state_dir, owner):
                if not warned_lease:
                    log.warning("telegram commands: another process is polling the bot; this one waits")
                    warned_lease = True
                stop.wait(30)
                continue
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
