"""Always-on local mode: poll deals and corporate announcements during market hours.

Runs the normal check on an interval and, between checks, watches NSE announcements for
the stocks you hold or were recently recommended, notifying on anything new. Meant for a
machine with a fixed IP (the April 2026 SEBI rules) rather than a cron runner.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime, time as dtime, timedelta, timezone, tzinfo
from typing import Any, Callable

from . import killswitch
from .config import Settings
from .groww import GrowwTokenUnavailable, warn_token_block_once
from .risk import check_stops
from .state import STATE_LOCK, State
from .timezones import IST

log = logging.getLogger(__name__)
ALIVE_EVERY_S = 30.0   # state/watch_alive.json is rewritten at most this often


def keep_awake(on: bool) -> bool:
    """Ask Windows not to sleep while watch mode is in the market window (a laptop used as the
    trading machine). Returns True when the request was made; does nothing elsewhere."""
    import sys
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))
        return True
    except Exception:  # noqa: BLE001 - a convenience, never a failure
        return False



def _plain(text: str) -> str:
    """Third-party text going to a webhook: break Slack/Discord mentions and control markup."""
    for bad in ("<!", "<@", "<#"):
        text = text.replace(bad, bad[0] + " " + bad[1])
    return re.sub(r"@(everyone|here|channel)", lambda m: "@ " + m.group(1), text)


MARKET_DATA_BUDGET_S = 30.0   # network time the once-a-day NSE reads may spend in one tick


class _CachedCalendar:
    """The holiday calendar as the heartbeat thread may use it: cached days only (``peek_trading_day``), no fetch."""

    def __init__(self, holidays: Any | None):
        self._h = holidays

    def is_trading_day(self, day: Any) -> bool:
        h = self._h
        if h is None:
            return bool(day.weekday() < 5)
        peek = getattr(h, "peek_trading_day", None)
        if peek is not None:
            return bool(peek(day))
        return bool(h.is_trading_day(day))


class Watcher:
    def __init__(self, settings: Settings, *, every: int = 60, window: tuple[str, str] = ("08:45", "18:30"),
                 tz: tzinfo = IST, check_fn: Callable[[], Any] | None = None,
                 data: Any | None = None, broker: Any | None = None, notifier: Any | None = None,
                 weekdays_only: bool = True, prices: Any | None = None, auto_exit: bool = False,
                 holidays: Any | None = None, awake: Callable[[bool], Any] | None = keep_awake,
                 news: Any | None = None, broker_factory: Callable[[], Any] | None = None,
                 digest: Any | None = None, forward: Any | None = None, heartbeat: Any | None = None,
                 integration: Any | None = None, backup: Any | None = None, close_snapshot: Any | None = None,
                 app_feed: Any | None = None):
        self.settings = settings
        self._broker_factory = broker_factory  # builds the broker later when it could not be built at start
        self._blocked_until: datetime | None = None
        self._broker_warned: dict[str, float] = {}
        self._prices = prices  # object with .history(symbol, range) for ATR-based stops
        self.auto_exit = auto_exit  # sell paper positions that hit their trailing stop
        self.every = max(15, int(every))
        self.window = (dtime.fromisoformat(window[0]), dtime.fromisoformat(window[1]))
        self.tz = tz
        self.weekdays_only = weekdays_only
        self.holidays = holidays  # NSEHolidays: no polling on exchange holidays
        self._awake = awake  # keeps a Windows laptop awake in the market window
        self._awake_on: bool | None = None
        self._check_fn = check_fn
        self._data = data
        self._warned_no_tagger = False
        self._news = news  # NewsService: negative headlines for held stocks
        self._story_jobs: list[tuple[dict[str, Any], str]] = []   # news alerts waiting for their article (own thread)
        self._story_lock = threading.Lock()
        self._story_thread: threading.Thread | None = None
        self._digest = digest  # DigestScheduler: the morning and evening emails (own worker thread)
        self._forward = forward  # ForwardScheduler: the paper forward test, once a trading day after the close
        self._backup = backup  # BackupScheduler: copies the state files once a trading day after the close
        self._integration = integration  # IntegrationScheduler: the read-only live-services check at 08:35 IST
        self._app_feed = app_feed  # app_feed.AppFeed: the phone app's feed file (read-only, off unless APP_FEED)
        self._app_feed_thread: threading.Thread | None = None
        self._close_snapshot = close_snapshot  # groww_hours.CloseSnapshot: one saved Groww read at the close (15:35-15:50)
        self._groww_paused = False  # live Groww broker outside the Groww window: no Groww call this tick
        self._heartbeat = heartbeat  # Heartbeat: pings the dead-man URL every minute, at all hours
        self._broker = broker
        self._notifier = notifier
        self._stop = threading.Event()
        self._halt_logged = False
        self._thread: threading.Thread | None = None
        self.ticks = 0
        self._alive_at = -1e9
        self._tick_started: str | None = None
        self._tick_finished: str | None = None
        self._last_error: str | None = None
        self._progress = time.monotonic()   # stamped at the start and end of every loop iteration (the heartbeat reads it)
        self.last_tick: dict[str, Any] | None = None
        self.started_at: str | None = None

    # -- schedule -------------------------------------------------------------
    def market_window_open(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(self.tz)
        if self.weekdays_only and now.weekday() >= 5:
            return False
        if self.holidays is not None and not self.holidays.is_trading_day(now.date()):
            return False
        return self.window[0] <= now.time() <= self.window[1]

    def groww_paused(self, now: datetime | None = None) -> bool:
        """A real (live-orders) Groww broker is not touched outside the Groww window. The practice account needs no
        gate here: its Groww prices are gated where it is built (runner.make_practice_broker)."""
        s = self.settings
        if not (getattr(s, "use_groww", False) and getattr(s, "groww_live_orders", False)):
            return False
        from .groww_hours import window_open
        return not window_open(s, now or datetime.now(self.tz), self.holidays)

    # -- one iteration ----------------------------------------------------------
    def _tickers_by_reason(self) -> tuple[list[str], list[str]]:
        """(held, recently recommended)."""
        st = State(self.settings.state_dir / "state.json")
        held: list[str] = []
        if self._broker is not None and not self._groww_paused:
            try:
                held = [p.symbol for p in self._broker.positions()]
            except Exception as e:  # noqa: BLE001
                log.warning("positions unavailable: %s", e)
        recent = [r["ticker"] for r in st.data.get("recommendations", [])[-10:]
                  if r.get("ticker") and r["ticker"] != "PORTFOLIO"]
        return held, recent

    def real_holdings(self) -> list[str]:
        """Your real Groww shares, from the holdings snapshot the dashboard and the daily emails save (no Groww call
        here). In practice mode the watch's broker is the practice account, so without this a filing by a company
        you really own (a dividend, results) was never polled. Shares only: funds and bonds have no NSE filings. A
        BSE-only holding is read as SYMBOL.BO, whose filings come from BSE."""
        from .runner import load_groww_snapshot
        snap = load_groww_snapshot(self.settings) if getattr(self.settings, "market", "in") == "in" else None
        out: list[str] = []
        for r in (snap or {}).get("holdings", []):
            if (r.get("kind") or "equity") != "equity":
                continue
            sym = str(r["symbol"]).strip().upper()
            if str(r.get("exchange") or "").upper() == "BSE" and not sym.endswith(".BO"):
                sym += ".BO"
            out.append(sym)
        return out

    def interesting_tickers(self) -> list[str]:
        held, recent = self._tickers_by_reason()
        return list(dict.fromkeys(held + self.real_holdings() + recent))  # de-duplicated, first-seen order

    def poll_announcements(self) -> list[dict[str, Any]]:
        if self._data is None or not hasattr(self._data, "announcements"):
            return []
        st = State(self.settings.state_dir / "state.json")
        seen = st.data.setdefault("seen_announcements", {})
        fresh: list[dict[str, Any]] = []
        tickers = self.interesting_tickers()
        # A real holding polled for the first time has its existing filings recorded as seen without an alert, so
        # linking Groww (or buying a share) does not send weeks of old filings at once; from then on each new one alerts.
        polled = set(st.data.get("announcement_tickers", []))
        first_time = {t for t in self.real_holdings() if t not in polled}
        got, errors, changed = 0, [], False
        for t in tickers:
            try:
                rows = self._data.announcements(t, limit=20)   # 20, not 5: results days bring a burst of filings
            except Exception as e:  # noqa: BLE001
                log.warning("announcements for %s failed: %s", t, e)
                errors.append(f"{t}: {type(e).__name__}: {e}")
                continue
            got += len(rows)
            if t not in polled:
                polled.add(t)
                changed = True
            for a in rows:
                if a["id"] and a["id"] not in seen:
                    seen[a["id"]] = a["at"]
                    changed = True
                    if t not in first_time:
                        fresh.append(a)
        if tickers:   # the Health tab's record: a success when at least one stock could be read
            from . import health
            health.note_run(self.settings.state_dir, "announcements", ok=len(errors) < len(tickers), rows=got,
                            error="; ".join(errors[:3]) or None)
        if changed:
            st.data["announcement_tickers"] = sorted(polled)[-1000:]
            st.save()
        if fresh:
            if self._notifier is not None:
                from .agent import send_with_html
                from .alert_cards import announcements_card, announcements_subject
                body = "\n".join(f"{a['at']} {a['symbol']} [{a['category']}] {a['text']}" for a in fresh)
                try:
                    card: str | None = announcements_card(fresh)
                except Exception:  # noqa: BLE001 - the text email still goes
                    log.exception("announcements card failed")
                    card = None
                send_with_html(self._notifier, _plain(announcements_subject(fresh)), _plain(body), card)
        return fresh

    def poll_news(self) -> list[dict[str, Any]]:
        """Notify once per headline that counts as an instant alert (news.is_instant_alert: negative, or positive
        about a company event, with medium/high confidence), for stocks in the practice account, your real Groww
        holdings and recent recommendations. Fetching is cached and each headline is tagged once, so a tick is cheap.
        A real holding's headlines are recorded silently the first time it is looked at, so no backlog arrives."""
        if self._news is None:
            return []
        from .news import is_instant_alert
        if getattr(self._news.tagger, "name", "none") == "none" and not self._warned_no_tagger:
            self._warned_no_tagger = True
            log.warning("news alerts are off: no headline tagger is available "
                        "(start Ollama with `ollama serve` and `ollama pull <model>`, or set NEWS_TAGGER)")
        candidates: list[dict[str, Any]] = []
        held, recent = self._tickers_by_reason()
        real = self.real_holdings()
        tickers = list(dict.fromkeys(held + real + recent))
        got, errors = 0, []
        for t in tickers:  # slow: network and tagging, with no State held open
            try:
                items = self._news.for_symbol(t)["items"]
            except Exception as e:  # noqa: BLE001
                log.warning("news for %s failed: %s", t, e)
                errors.append(f"{t}: {type(e).__name__}: {e}")
                continue
            got += len(items)
            candidates += [{**i, "symbol": t} for i in items if is_instant_alert(i)]
        if tickers:   # the Health tab's record
            from . import health
            health.note_run(self.settings.state_dir, "news", ok=len(errors) < len(tickers), rows=got,
                            error="; ".join(errors[:3]) or None)
        st = State(self.settings.state_dir / "state.json")  # loaded now, so concurrent writes are kept
        looked = set(st.data.get("news_tickers", []))
        first_time = {t for t in real if t not in looked}
        read_ok = first_time - {e.split(":")[0] for e in errors}   # a failed fetch gets its first look next tick
        if read_ok:
            st.data["news_tickers"] = sorted(looked | read_ok)[-1000:]
            st.save()
        if not candidates:
            return []
        seen = st.data.setdefault("seen_news", {})
        fresh: list[dict[str, Any]] = []
        quiet = False
        for i in candidates:
            if i["id"] not in seen:
                seen[i["id"]] = datetime.now(self.tz).date().isoformat()  # first-seen day
                if i["symbol"] in first_time:
                    quiet = True     # a real holding's first look: recorded, not alerted
                else:
                    fresh.append(i)
        if quiet and not fresh:
            st.save()
        if fresh:
            cutoff = (datetime.now(self.tz) - timedelta(days=30)).date().isoformat()
            for k in [k for k, v in seen.items() if str(v)[:10] < cutoff]:
                del seen[k]  # headlines older than 30 days can't come back
            st.save()
            if self._notifier is not None:
                self._queue_news_alerts([(i, "held in your portfolio" if i["symbol"] in held or i["symbol"] in real
                                          else "recently recommended by the agent (watch list)") for i in fresh])
        return fresh

    def _queue_news_alerts(self, jobs: list[tuple[dict[str, Any], str]]) -> None:
        """Each alert as a card. With a NewsService the article is read and summarised first, on the news-alert
        thread: that takes network and model time, which must never hold up the stop checks in this tick."""
        story = getattr(self._news, "story", None)
        if story is None:
            from .news_story import basic_card
            tagger = getattr(getattr(self._news, "tagger", None), "name", None)
            for item, ctx in jobs:
                self._send_news_card(basic_card(item, item["symbol"], None, ctx, tagger))
            return
        with self._story_lock:
            self._story_jobs.extend(jobs)
            if self._story_thread is None or not self._story_thread.is_alive():
                self._story_thread = threading.Thread(target=self._story_worker, daemon=True, name="news-alerts")
                self._story_thread.start()

    def _story_worker(self) -> None:
        from .news_story import basic_card, contradicts_headline
        news = self._news
        assert news is not None
        while True:
            with self._story_lock:
                if not self._story_jobs:
                    return
                item, ctx = self._story_jobs.pop(0)
            try:
                card = news.story(item, item["symbol"], ctx)
            except Exception:  # noqa: BLE001 - the alert still goes out, with the headline alone
                log.exception("news story failed for %s", item.get("symbol"))
                card = basic_card(item, item["symbol"], None, ctx, getattr(news.tagger, "name", None))
            if contradicts_headline(card):   # the article read in full disagrees with the headline tag
                log.info("news alert for %s not sent: headline tagged %s, the article reads %s",
                         item.get("symbol"), card.get("sentiment"), card.get("impact"))
                continue
            try:
                self._send_news_card(card)
            except Exception:  # noqa: BLE001
                log.exception("news alert not sent for %s", item.get("symbol"))

    def _send_news_card(self, card: dict[str, Any]) -> None:
        from .news_story import render_html, render_telegram, render_text, subject
        tg_html, tg_plain, tg_button = render_telegram(card)
        assert self._notifier is not None
        self._notifier.send(_plain(subject(card)), _plain(render_text(card)), html=render_html(card),
                            telegram_html=tg_html, telegram_plain=tg_plain, telegram_button=tg_button)

    def check_trailing_stops(self) -> list[dict[str, Any]]:
        if self._broker is None or self._groww_paused:
            return []
        try:
            positions = self._broker.positions()
        except GrowwTokenUnavailable as e:
            warn_token_block_once(e, log)  # one warning per cool-down, not one per tick
            return []
        except Exception as e:  # noqa: BLE001
            log.warning("positions unavailable for stop check: %s", e)
            return []
        prices = self._prices
        bars_fn = (lambda sym: prices.history(sym, "1y")) if prices is not None else (lambda sym: [])
        hits = check_stops(positions, bars_fn)
        if hits and getattr(self.settings, "market", "in") == "in":   # no stop over a split / bonus ex-date
            try:
                from .corp_actions import book_for as corp_book_for
                book = corp_book_for(self.settings, calendar=self.holidays)
                suspended = State(self.settings.state_dir / "state.json").data.get("gtt_suspended") or {}
                practice = getattr(self._broker, "name", "").startswith("local-paper")
                now = datetime.now(self.tz)
                hits = [h for h in hits if not book.held_back(h["symbol"], now, suspended.get(h["symbol"]),
                                                              account="paper_broker" if practice else None)]
            except Exception as e:  # noqa: BLE001
                log.warning("corporate-action check for stops unavailable: %s", e)
        if not hits:
            return []
        state_path = self.settings.state_dir / "state.json"
        alerted = dict(State(state_path).data.get("stop_alerts", {}))  # a read only: no State is held across the sells
        fresh = []
        for h in hits:
            key = f"{h['symbol']}:{round(h['stop'], 2)}"
            if key in alerted:
                continue
            if self.auto_exit and getattr(self._broker, "name", "").startswith("local-paper"):
                try:
                    # re-checked inside the broker's lock: the dashboard's practice checker may have sold it already
                    order = self._broker.sell_if_stopped(h["symbol"], qty=h["qty"], level=h["level"],
                                                         stop={"type": h["type"], "value": h["stop_value"]},
                                                         extra={"stop_level": h["stop"], "stop_type": h["type"]})
                except Exception as e:  # noqa: BLE001
                    h["order_error"] = str(e)
                    order = None
                if order is not None:
                    h["order"] = order
                elif "order_error" not in h:
                    continue  # already sold by the other seller, or the price came back above the stop
            fresh.append(h)
        if fresh:
            from .stops import record_stop_fill
            with STATE_LOCK:  # reload now and change only our own keys, so a write made while we sold is kept
                st = State(state_path)
                marks = st.data.setdefault("stop_alerts", {})
                for h in fresh:
                    marks[f"{h['symbol']}:{round(h['stop'], 2)}"] = h["price"]
                st.save()
                for h in fresh:
                    if h.get("order"):
                        record_stop_fill(state_path, h["order"],
                                         {"level": h["level"], "type": h["type"], "label": h["label"]})
        if fresh and self._notifier is not None:
            body = "\n".join(f"{h['symbol']}: {h['price']:.2f} at/below {h['label']} stop {h['stop']:.2f} "
                             f"({h['drawdown_from_high']*100:+.1f}% from high)"
                             + (" - paper SOLD" if h.get("order") else "") for h in fresh)
            self._notifier.send(f"[STOP] {len(fresh)} position(s) hit their stop", body)
        return fresh

    def check_day_brake(self, now: datetime) -> dict[str, Any] | None:
        """Live Groww only: the account down DAY_BRAKE_PCT from the previous close -> no buys for the session."""
        from .live import is_live_broker
        if not is_live_broker(self._broker) or self._groww_paused:
            return None
        from . import day_brake
        from .live_alerts import make_alert_fn
        notifier = self._notifier

        def send(subject: str, body: str) -> None:
            if notifier is not None:
                notifier.send(subject, body)
        return day_brake.evaluate(self.settings, self._broker, day_brake.prev_close_fn(self._prices, now.date()), now,
                                  calendar=self.holidays, alert=make_alert_fn(self.settings.state_dir, send))

    def sync_live(self) -> dict[str, Any] | None:
        """Live Groww only: re-check open orders and keep GTT stop-losses in line."""
        from .live import GTT_KEYS, is_live_broker, locked_gtt_sync, refresh_open_orders

        if not is_live_broker(self._broker) or self._groww_paused:
            return None
        st = State(self.settings.state_dir / "state.json")
        out: dict[str, Any] = {}
        try:
            out["orders"] = refresh_open_orders(self._broker, st, self._notifier)
        except Exception as e:  # noqa: BLE001
            out["orders_error"] = str(e)
        st.save(fresh_keys=GTT_KEYS)
        prices = self._prices
        bars_fn = (lambda sym: prices.history(sym, "1y")) if prices is not None else None
        try:
            out["gtt"] = locked_gtt_sync(self.settings, self._broker, bars_fn=bars_fn, notifier=self._notifier)
        except TimeoutError as e:   # another process is syncing: the next tick tries again
            out["gtt"] = [{"action": "error", "error": str(e)}]
        return out

    def ensure_broker(self) -> Any | None:
        """The broker, built now if it could not be at start (Groww refusing a login token). While it cannot
        be built the watch runs alerts-only: deals, announcements and news, with a single warning per block."""
        if self._broker is None and self._broker_factory is not None:
            self._blocked_until = None
            try:
                self._broker = self._broker_factory()
            except GrowwTokenUnavailable as e:
                self._blocked_until = e.until
                warn_token_block_once(e, log)
            except (Exception, SystemExit) as e:  # noqa: BLE001 - whatever stops the broker: keep the alerts running
                key = f"{type(e).__name__}: {e}"
                if time.monotonic() - self._broker_warned.get(key, -3600.0) >= 3600.0:
                    self._broker_warned[key] = time.monotonic()
                    log.warning("broker unavailable, watching alerts only: %s", key)
        return self._broker

    def poll_market_data(self, now: datetime) -> dict[str, Any]:
        """Once-a-day public NSE reads: price bands and the Indian-market filter lists (ASM/GSM, board meetings, pledges)
        from the morning, FII/DII flows and breadth (after 19:00). Each has its
        own once-per-day guard in state and never raises; a quiet tick costs a few small file reads."""
        out: dict[str, Any] = {}
        if self._data is None or not hasattr(self._data, "session"):
            return out
        data = self._data
        s = self.settings
        deadline = time.monotonic() + MARKET_DATA_BUDGET_S   # at most about this much network time per tick
        try:
            if getattr(s, "price_band_filter", False) and getattr(s, "market", "in") == "in":
                from .bands import book_for
                n = book_for(s).tick(self._data, now, self.holidays, timeout=20.0)
                if n:
                    out["price_bands"] = n
            if getattr(s, "flows_breadth", False) and getattr(s, "market", "in") == "in":
                from .breadth import BreadthStore, archive_bars_fn, nifty500_members
                from .flows import FlowStore
                if time.monotonic() < deadline:
                    row = FlowStore(s.state_dir).tick(self._data, now, self.holidays)
                    if row:
                        out["flows"] = row["date"]
                if time.monotonic() < deadline:
                    b = BreadthStore(s.state_dir).tick(self._data, now, lambda: nifty500_members(data.session),
                                                       archive_bars_fn(s.state_dir), self.holidays, deadline=deadline)
                    if b:
                        out["breadth"] = b["date"]
            if getattr(s, "market", "in") == "in" and time.monotonic() < deadline:   # last: the lowest priority read
                from .indian_filters import filters_for   # ASM/GSM, board meetings, pledges: once per trading day
                got = filters_for(s, calendar=self.holidays).tick(self._data, now, deadline=deadline)
                if got:
                    out["indian_filters"] = got
            if getattr(s, "market", "in") == "in" and time.monotonic() < deadline:   # once a year (and monthly refresh)
                from .clearing import ClearingHolidays
                n_days = ClearingHolidays(s.state_dir).tick(self._data, now)
                if n_days is not None:
                    out["clearing_holidays"] = n_days
            if getattr(s, "market", "in") == "in" and time.monotonic() < deadline:   # splits, bonuses: once a trading day
                from .corp_actions import book_for as corp_book_for
                n_rows = corp_book_for(s, calendar=self.holidays).tick(self._data, now)
                if n_rows is not None:
                    out["corp_actions"] = n_rows
            if time.monotonic() >= deadline:
                out["budget_used_up"] = True
        except Exception:  # noqa: BLE001 - market data never stops the watch
            log.exception("market data poll failed")
        return out

    def poll_deals_blocked(self) -> list[dict[str, Any]]:
        """Alerts-only mode: tell the user about new deals without analysing them (that needs the broker).

        Nothing is marked seen, so the full check analyses each one once the broker is back; the keys already
        alerted are remembered (state ``alerted_while_blocked``) so every deal alerts once, not every tick."""
        if self._data is None:
            return []
        from .quiver import fetch_followed
        try:
            from . import health
            trades = health.recorded(self.settings.state_dir, health.deals_source(self.settings),
                                     lambda: fetch_followed(self._data, self.settings.investors, self.settings.watch_source))
        except Exception as e:  # noqa: BLE001
            log.warning("deals unavailable: %s", e)
            return []
        path = self.settings.state_dir / "state.json"
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with STATE_LOCK:  # reload and change only our own key, so a write made meanwhile is kept
            st = State(path)
            seen = st.data["seen"]
            alerted = st.data.setdefault("alerted_while_blocked", {})
            for k in [k for k in alerted if k in seen]:
                del alerted[k]
            fresh = [t for t in st.new_trades(trades) if t.key not in alerted]
            for t in fresh:
                alerted[t.key] = now
            if fresh:
                st.save()
        if fresh and self._notifier is not None:
            from .notify import clean_text
            until = self._blocked_until
            when = (f"until {until.astimezone(IST).strftime('%H:%M IST')}" if until else "until it is back")
            by: dict[str, list[Any]] = {}
            from .deal_events import consolidate_deals, event_text
            for t in consolidate_deals(fresh):   # one alert line per deal event, not one per exchange
                by.setdefault(clean_text(t.investor, 120), []).append(t)
            for who, ts in by.items():
                self._notifier.send(
                    f"[DEAL] {who}: {len(ts)} new deal(s) - analysis paused: Groww unavailable {when}",
                    "\n".join(clean_text(event_text(t) if (t.raw or {}).get("consolidated") else t.summary(), 300) for t in ts)
                    + "\n\nThese are not analysed yet; the full check runs on them when Groww is back.")
        return [t.to_dict() for t in fresh]

    def _app_feed_tick(self, now: datetime) -> None:
        try:
            assert self._app_feed is not None
            self._app_feed.tick(now)
        except Exception:  # noqa: BLE001 - never stops the watch
            log.exception("app feed failed")

    def tick(self, force: bool = False) -> dict[str, Any]:
        now = datetime.now(self.tz)
        halted = killswitch.reason(getattr(self.settings, "state_dir", None), "run the watch")
        if halted:   # kill switch: nothing runs (no emails, no data, no Groww); the loop stays alive and idles
            if not self._halt_logged:
                log.warning("%s", halted)
                self._halt_logged = True
            info_h: dict[str, Any] = {"at": now.isoformat(timespec="seconds"), "halted": halted, "skipped": True}
            self.last_tick = info_h
            return info_h
        if self._halt_logged:
            log.info("HALT released: the watch runs again")
            self._halt_logged = False
        info: dict[str, Any] = {"at": now.isoformat(timespec="seconds"), "in_window": self.market_window_open(now)}
        if self._digest is not None:  # on its own schedule, whatever the market window; returns at once
            try:
                info["digest"] = self._digest.tick(now)
            except Exception:  # noqa: BLE001 - the emails never stop the watch
                log.exception("daily email check failed")
        info["market_data"] = self.poll_market_data(now)
        if getattr(self.settings, "market", "in") == "in":   # from 08:30 on an ex-date: rescale the paper accounts
            try:
                from .corp_actions import normalise_all
                done = normalise_all(self.settings, now)
                if done:
                    info["normalised"] = done
                    if self._notifier is not None:
                        self._notifier.send(f"[CORPORATE ACTION] {len(done)} practice position(s) adjusted",
                                            "\n".join(f"{d['symbol']} ({d['account']}): {d['label']}, qty {d['qty_from']:g} -> "
                                                      f"{d['qty_to']:g}, buy price {d['avg_from']:g} -> {d['avg_to']:g}" for d in done))
            except Exception:  # noqa: BLE001 - never stops the watch
                log.exception("corporate-action normaliser failed")
        if self._forward is not None:  # once a trading day after the close; returns at once (its own worker thread)
            try:
                info["forward"] = self._forward.tick(now)
            except Exception:  # noqa: BLE001 - the paper forward test never stops the watch
                log.exception("forward test check failed")
        if self._backup is not None:  # once a trading day after the close; its own worker thread
            try:
                info["backup"] = self._backup.tick(now)
            except Exception:  # noqa: BLE001 - the backup never stops the watch
                log.exception("backup scheduling failed")
        if self._integration is not None:  # once a trading day from 08:35; returns at once (its own worker thread)
            try:
                info["integration"] = self._integration.tick(now)
            except Exception:  # noqa: BLE001 - the integration check never stops the watch
                log.exception("integration check scheduling failed")
        if self._close_snapshot is not None:  # 15:35-15:50 on trading days: the one saved Groww read of the session
            try:
                info["close_snapshot"] = self._close_snapshot.tick(now)
            except Exception:  # noqa: BLE001 - never stops the watch
                log.exception("close snapshot failed")
        if self._app_feed is not None:  # rebuilds state/app_feed.json while the Groww window is open; its own
            # worker thread, as Groww timeouts in an outage can take a minute (AppFeed.tick never raises)
            if self._app_feed_thread is not None and self._app_feed_thread.is_alive():
                info["app_feed"] = "running"
            else:
                self._app_feed_thread = threading.Thread(target=self._app_feed_tick, args=(now,), daemon=True,
                                                         name="app-feed")
                self._app_feed_thread.start()
                info["app_feed"] = "started"
        self._groww_paused = self.groww_paused(now)
        if self._groww_paused:
            info["groww_paused"] = "outside the Groww window: no Groww calls (stops and order checks resume at the open)"
        if self._awake is not None and info["in_window"] != self._awake_on:
            self._awake(info["in_window"])
            self._awake_on = info["in_window"]
        if not info["in_window"] and not force:
            info["skipped"] = True
            self.last_tick = info
            return info
        alerts_only = (not self._groww_paused and self.ensure_broker() is None
                       and self._broker_factory is not None)
        if alerts_only:
            info["alerts_only"] = "Groww is unavailable: stop and order checks are paused"
            info["new_deals_unanalysed"] = self.poll_deals_blocked()
        if not alerts_only:
            braked = self.check_day_brake(now)
            if braked:
                info["day_brake"] = braked
        try:
            if self._check_fn is not None and not alerts_only and not self._groww_paused:
                result = self._check_fn()
                info["check"] = result.to_dict() if hasattr(result, "to_dict") else result
        except Exception as e:  # noqa: BLE001
            log.exception("watch check failed")
            info["check_error"] = f"{type(e).__name__}: {e}"
        self._progress = time.monotonic()   # a long tick that keeps moving is not a stall: stamp between the sub-steps
        info["new_announcements"] = self.poll_announcements()
        info["negative_news"] = self.poll_news()
        self._progress = time.monotonic()
        info["stop_hits"] = self.check_trailing_stops()
        self._progress = time.monotonic()
        live = self.sync_live()
        self._progress = time.monotonic()
        if live is not None:
            info["live"] = live
        self.ticks += 1
        self.last_tick = info
        return info

    # -- loop / thread ----------------------------------------------------------
    def _write_alive(self, error: str | None = None, *, force: bool = False) -> None:
        """state/watch_alive.json: {"at", "tick_started", "tick_finished", "last_error", "every", "market_window"},
        times as ISO in IST. Written atomically, at most once per 30 s; a failure to write never touches the loop."""
        try:
            mono = time.monotonic()
            if not force and mono - self._alive_at < ALIVE_EVERY_S:
                return
            self._alive_at = mono
            from .state import atomic_write
            now = datetime.now(self.tz)
            atomic_write(self.settings.state_dir / "watch_alive.json", json.dumps({
                "at": now.isoformat(timespec="seconds"), "tick_started": self._tick_started,
                "tick_finished": self._tick_finished, "last_error": (error or "")[:300] or None,
                "every": self.every, "market_window": self.market_window_open(now)}))
        except Exception:  # noqa: BLE001
            pass

    def run_forever(self) -> None:
        self.started_at = datetime.now(self.tz).isoformat(timespec="seconds")
        self._progress = time.monotonic()
        if self._heartbeat is not None:   # its own thread: a long tick cannot silence it, a stalled loop is reported
            try:
                from .heartbeat import stall_after
                from .safety import market_open
                cached = _CachedCalendar(self.holidays)   # the heartbeat thread must never make a network call
                self._heartbeat.start(lambda: self._progress, stall_s=stall_after(self.every), stop=self._stop,
                                      window=lambda: market_open(datetime.now(IST), cached), loop_every=self.every)
            except Exception:  # noqa: BLE001 - the dead-man ping never stops the watch
                log.warning("heartbeat thread did not start")
        while not self._stop.is_set():
            self._progress = time.monotonic()
            self._tick_started = datetime.now(self.tz).isoformat(timespec="seconds")
            error: str | None = None
            raised: str | None = None
            self._write_alive(self._last_error)
            try:
                result = self.tick()
                if isinstance(result, dict) and result.get("check_error"):
                    error = str(result["check_error"])
            except Exception as e:  # noqa: BLE001
                log.exception("watch tick failed")
                error = raised = f"{type(e).__name__}: {e}"
            self._progress = time.monotonic()
            self._tick_finished = datetime.now(self.tz).isoformat(timespec="seconds")
            self._last_error = error
            self._write_alive(error, force=True)
            if self._heartbeat is not None:
                try:
                    if raised is not None:   # only an unexpected exception is a heartbeat fail
                        from .notify import redact
                        self._heartbeat.report_error(redact(raised, getattr(self.settings, "telegram_bot_token", None)))
                    else:   # a routine check_error (Groww or NSE down) is an alerted condition, the loop is alive
                        self._heartbeat.report_clean()
                except Exception:  # noqa: BLE001
                    pass
            self._stop.wait(self.every)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self.run_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._heartbeat is not None:
            try:
                self._heartbeat.stop()
            except Exception:  # noqa: BLE001
                pass
        if self._awake is not None and self._awake_on:
            self._awake(False)
            self._awake_on = False

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._stop.is_set())

    def status(self) -> dict[str, Any]:
        return {"on": self.running, "every": self.every, "ticks": self.ticks,
                "window": [self.window[0].isoformat(timespec="minutes"), self.window[1].isoformat(timespec="minutes")],
                "in_window": self.market_window_open(), "started_at": self.started_at,
                "last_tick": self.last_tick}
