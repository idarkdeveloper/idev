"""Always-on local mode: poll deals and corporate announcements during market hours.

Runs the normal check on an interval and, between checks, watches NSE announcements for
the stocks you hold or were recently recommended, notifying on anything new. Meant for a
machine with a fixed IP (the April 2026 SEBI rules) rather than a cron runner.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, time as dtime, timedelta, timezone, tzinfo
from typing import Any, Callable

from .config import Settings
from .groww import GrowwTokenUnavailable, warn_token_block_once
from .risk import check_stops
from .state import STATE_LOCK, State
from .timezones import IST

log = logging.getLogger(__name__)


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


class Watcher:
    def __init__(self, settings: Settings, *, every: int = 60, window: tuple[str, str] = ("08:45", "18:30"),
                 tz: tzinfo = IST, check_fn: Callable[[], Any] | None = None,
                 data: Any | None = None, broker: Any | None = None, notifier: Any | None = None,
                 weekdays_only: bool = True, prices: Any | None = None, auto_exit: bool = False,
                 holidays: Any | None = None, awake: Callable[[bool], Any] | None = keep_awake,
                 news: Any | None = None, broker_factory: Callable[[], Any] | None = None,
                 digest: Any | None = None):
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
        self._digest = digest  # DigestScheduler: the morning and evening emails (own worker thread)
        self._broker = broker
        self._notifier = notifier
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.ticks = 0
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

    # -- one iteration ----------------------------------------------------------
    def interesting_tickers(self) -> list[str]:
        st = State(self.settings.state_dir / "state.json")
        tickers: list[str] = []
        if self._broker is not None:
            try:
                tickers += [p.symbol for p in self._broker.positions()]
            except Exception as e:  # noqa: BLE001
                log.warning("positions unavailable: %s", e)
        tickers += [r["ticker"] for r in st.data.get("recommendations", [])[-10:]
                    if r.get("ticker") and r["ticker"] != "PORTFOLIO"]
        seen: set[str] = set()
        return [t for t in tickers if not (t in seen or seen.add(t))]

    def poll_announcements(self) -> list[dict[str, Any]]:
        if self._data is None or not hasattr(self._data, "announcements"):
            return []
        st = State(self.settings.state_dir / "state.json")
        seen = st.data.setdefault("seen_announcements", {})
        fresh: list[dict[str, Any]] = []
        for t in self.interesting_tickers():
            try:
                rows = self._data.announcements(t, limit=5)
            except Exception as e:  # noqa: BLE001
                log.warning("announcements for %s failed: %s", t, e)
                continue
            for a in rows:
                if a["id"] and a["id"] not in seen:
                    seen[a["id"]] = a["at"]
                    fresh.append(a)
        if fresh:
            st.save()
            if self._notifier is not None:
                body = "\n".join(f"{a['at']} {a['symbol']} [{a['category']}] {a['text']}" for a in fresh)
                self._notifier.send(f"[NEWS] {len(fresh)} new announcement(s)", body)
        return fresh

    def poll_news(self) -> list[dict[str, Any]]:
        """Notify once per headline that is negative with medium/high confidence, for held or recently
        recommended stocks. Fetching is cached and each headline is tagged once, so a tick is cheap."""
        if self._news is None:
            return []
        from .news import is_alert
        if getattr(self._news.tagger, "name", "none") == "none" and not self._warned_no_tagger:
            self._warned_no_tagger = True
            log.warning("news alerts are off: no headline tagger is available "
                        "(start Ollama with `ollama serve` and `ollama pull <model>`, or set NEWS_TAGGER)")
        candidates: list[dict[str, Any]] = []
        for t in self.interesting_tickers():  # slow: network and tagging, with no State held open
            try:
                items = self._news.for_symbol(t)["items"]
            except Exception as e:  # noqa: BLE001
                log.warning("news for %s failed: %s", t, e)
                continue
            candidates += [{**i, "symbol": t} for i in items if is_alert(i)]
        if not candidates:
            return []
        st = State(self.settings.state_dir / "state.json")  # loaded now, so concurrent writes are kept
        seen = st.data.setdefault("seen_news", {})
        fresh: list[dict[str, Any]] = []
        for i in candidates:
            if i["id"] not in seen:
                seen[i["id"]] = datetime.now(self.tz).date().isoformat()  # first-seen day
                fresh.append(i)
        if fresh:
            cutoff = (datetime.now(self.tz) - timedelta(days=30)).date().isoformat()
            for k in [k for k, v in seen.items() if str(v)[:10] < cutoff]:
                del seen[k]  # headlines older than 30 days can't come back
            st.save()
            if self._notifier is not None:
                for i in fresh:
                    self._notifier.send(
                        _plain(f"[NEWS] {i['symbol']}: {i['title']}"),
                        _plain(f"{i['source']}, {i['published']}\n{i['link']}\n"
                               f"Tagged {i['sentiment']} ({i['event']}, {i['confidence']} confidence) by a "
                               "language model; headlines can be wrong or late."))
        return fresh

    def check_trailing_stops(self) -> list[dict[str, Any]]:
        if self._broker is None:
            return []
        try:
            positions = self._broker.positions()
        except GrowwTokenUnavailable as e:
            warn_token_block_once(e, log)  # one warning per cool-down, not one per tick
            return []
        except Exception as e:  # noqa: BLE001
            log.warning("positions unavailable for stop check: %s", e)
            return []
        bars_fn = (lambda sym: self._prices.history(sym, "1y")) if self._prices is not None else (lambda sym: [])
        hits = check_stops(positions, bars_fn)
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

    def sync_live(self) -> dict[str, Any] | None:
        """Live Groww only: re-check open orders and keep GTT stop-losses in line."""
        from .live import is_live_broker, refresh_open_orders, sync_gtt_stops

        if not is_live_broker(self._broker):
            return None
        st = State(self.settings.state_dir / "state.json")
        out: dict[str, Any] = {}
        try:
            out["orders"] = refresh_open_orders(self._broker, st, self._notifier)
        except Exception as e:  # noqa: BLE001
            out["orders_error"] = str(e)
        bars_fn = (lambda sym: self._prices.history(sym, "1y")) if self._prices is not None else None
        out["gtt"] = sync_gtt_stops(self.settings, self._broker, st, bars_fn=bars_fn, notifier=self._notifier)
        st.save()
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
        """Once-a-day public NSE reads: price bands (before 09:00), FII/DII flows and breadth (after 19:00). Each has its
        own once-per-day guard in state and never raises; a quiet tick costs a few small file reads."""
        out: dict[str, Any] = {}
        if self._data is None or not hasattr(self._data, "session"):
            return out
        s = self.settings
        try:
            if getattr(s, "price_band_filter", False) and getattr(s, "market", "in") == "in":
                from .bands import book_for
                n = book_for(s).tick(self._data, now, self.holidays)
                if n:
                    out["price_bands"] = n
            if getattr(s, "flows_breadth", False) and getattr(s, "market", "in") == "in":
                from .breadth import BreadthStore, archive_bars_fn, nifty500_members
                from .flows import FlowStore
                row = FlowStore(s.state_dir).tick(self._data, now, self.holidays)
                if row:
                    out["flows"] = row["date"]
                b = BreadthStore(s.state_dir).tick(self._data, now, lambda: nifty500_members(self._data.session),
                                                   archive_bars_fn(s.state_dir), self.holidays)
                if b:
                    out["breadth"] = b["date"]
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
            trades = fetch_followed(self._data, self.settings.investors, self.settings.watch_source)
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

    def tick(self, force: bool = False) -> dict[str, Any]:
        now = datetime.now(self.tz)
        info: dict[str, Any] = {"at": now.isoformat(timespec="seconds"), "in_window": self.market_window_open(now)}
        if self._digest is not None:  # on its own schedule, whatever the market window; returns at once
            try:
                info["digest"] = self._digest.tick(now)
            except Exception:  # noqa: BLE001 - the emails never stop the watch
                log.exception("daily email check failed")
        info["market_data"] = self.poll_market_data(now)
        if self._awake is not None and info["in_window"] != self._awake_on:
            self._awake(info["in_window"])
            self._awake_on = info["in_window"]
        if not info["in_window"] and not force:
            info["skipped"] = True
            self.last_tick = info
            return info
        alerts_only = self.ensure_broker() is None and self._broker_factory is not None
        if alerts_only:
            info["alerts_only"] = "Groww is unavailable: stop and order checks are paused"
            info["new_deals_unanalysed"] = self.poll_deals_blocked()
        try:
            if self._check_fn is not None and not alerts_only:
                result = self._check_fn()
                info["check"] = result.to_dict() if hasattr(result, "to_dict") else result
        except Exception as e:  # noqa: BLE001
            log.exception("watch check failed")
            info["check_error"] = f"{type(e).__name__}: {e}"
        info["new_announcements"] = self.poll_announcements()
        info["negative_news"] = self.poll_news()
        info["stop_hits"] = self.check_trailing_stops()
        live = self.sync_live()
        if live is not None:
            info["live"] = live
        self.ticks += 1
        self.last_tick = info
        return info

    # -- loop / thread ----------------------------------------------------------
    def run_forever(self) -> None:
        self.started_at = datetime.now(self.tz).isoformat(timespec="seconds")
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                log.exception("watch tick failed")
            self._stop.wait(self.every)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self.run_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
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
