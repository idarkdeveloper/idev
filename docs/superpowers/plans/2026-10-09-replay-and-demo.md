# Replay and Demo Pages Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Replay page (a practice portfolio started on a past date, stepped forward in time, racing the agent's rules and the Nifty, with nothing from after the replay date visible) and a Demo page (the bundled sample-data dashboard on its own tab), next to today's Live dashboard.

**Architecture:** A new package `trading_agent/replay/`. It holds:
- a `ReplayClock` and clocked wrappers over the existing price and news sources,
- a `Trial` that owns three `LocalPaperBroker` accounts (you, agent, Nifty fund),
- a day-by-day step engine reusing the forward test's rebalance rules,
- a scorecard,
- a Claude "view of this day",
- a small router the stdlib HTTP server calls.

The Live page's inline CSS and shared JavaScript helpers move into `ui/nocturne.css` and `ui/common.js` so the new `ui/replay.html` + `ui/replay.js` can reuse them. Demo is a second `App` instance with its own state directory, served under `/demo`.

**Tech Stack:** Python 3.14 stdlib + `requests` + `anthropic` (already used), pytest with the existing `FakeSession`, vanilla JS/SVG (no new libraries).

**Spec:** `docs/superpowers/specs/2026-10-09-replay-and-demo-design.md`

## Global Constraints

- No future data: nothing dated after the replay clock reaches a computation or the browser until End trial (spec "Non-negotiables" 1).
- Replay and Demo never construct a Groww broker; Claude gets no order tool there (Non-negotiables 2).
- No credentials in code, tests or logs; tests use fakes only, no network (Non-negotiables 3).
- Earliest start date: **4 Jan 2021** (`EARLIEST_START = "2021-01-04"`).
- Defaults: practice money **₹1,00,000**, universe **NIFTYMIDCAP150**, agent N **10**, dividends **reinvest**.
- Agent rules: the forward-test rules, i.e. top N of the screen, equal weight, rebalance on the first trading day of each month, trim above **125%** of target, sell at a **3×ATR** trailing stop.
- Same Indian delivery charges for all three portfolios: `cost_model_for("in")`, whole shares.
- Same Nocturne design, fonts and components; no new libraries.
- Run tests with `.venv/Scripts/python.exe -m pytest -q tests` (Windows; the repo has no pyproject).
- Every commit message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

**Deviation from the spec, decided while planning (tell the user):**
- The agent ranks by **momentum only**: the forward-test rules the spec names are momentum-only, and the only quality/value source the screen accepts today is Yahoo's *current* snapshot, which would leak. So no results-filings wrapper is built in this plan.
- NSE news is fetched as each stock's full announcement history, then sliced at the clock: NSE's date-range query times out, but the per-symbol query returns history back to 2004.
- A delisted holding is valued at its last price and flagged "suspended"; it is not auto-closed, because Yahoo gives no delisting price.

## Review Focus

1. **A start date on a weekend or holiday:** the trial opens with the previous trading day's close and the first step begins the next trading day. Pinned in Task 4 (`test_create_on_a_sunday_uses_fridays_close`).
2. **A step that would go past today** (e.g. `+1 year` from a date nine months ago): the clock stops at today's date and the button reports how far it got. Pinned in Task 5 (`test_step_is_capped_at_today`).
3. **Ordering a stock that wasn't listed yet on the replay date:** a clear refusal naming the listing date, and the trial is unchanged. Pinned in Task 4 (`test_order_before_listing_is_refused_with_the_listing_date`).
4. **Pressing a step twice quickly, or stepping while another dashboard job runs:** the second press is refused by the single job slot and the trial is unchanged. Pinned in Task 8 (`test_second_step_while_busy_is_refused`).
5. **Opening a trial after a server restart** (nothing in memory): the snapshot loads from disk alone, with no network needed for what is already stored. Pinned in Task 8 (`test_snapshot_after_restart_needs_no_network`).

---

## File map

| File | Responsibility |
|---|---|
| `trading_agent/replay/__init__.py` | package marker |
| `trading_agent/replay/clock.py` | `ReplayClock`, `FutureDataError`, `ClockedPrices`, `add_months`, `EARLIEST_START` |
| `trading_agent/replay/news.py` | `ClockedNews` over `NSEClient.announcement_history` |
| `trading_agent/replay/trial.py` | `Trial` (create/load/save/transaction/order/rebalance/point), `ReplayUniverse`, `BENCHMARKS`, `list_trials`, `default_screen` |
| `trading_agent/replay/engine.py` | `step()`: the day-by-day walk |
| `trading_agent/replay/scorecard.py` | `end_trial()`, `scorecard()`, `what_happened_next()` |
| `trading_agent/replay/claude.py` | `build_context()`, `ask()` |
| `trading_agent/replay/web.py` | `ReplayApp` (snapshot, jobs, lookup) and `route()` |
| `trading_agent/prices.py` | + `YahooPrices.dividends()` |
| `trading_agent/nse.py` | + `NSEClient.announcement_history()` |
| `trading_agent/broker.py` | + `LocalPaperBroker(now_fn=…)`, `.credit()`, `.credits()` |
| `trading_agent/forward.py` | extract `rebalance_to()` from `ForwardTest.rebalance` |
| `trading_agent/ui.py` | static files, `App.run_background`, `App.demo`, `/demo` and `/replay` routing |
| `trading_agent/ui/nocturne.css` | the Live page's CSS, moved out, plus tabs and banners |
| `trading_agent/ui/common.js` | shared JS helpers (`window.TA`) |
| `trading_agent/ui/index.html` | uses the two files above; tabs; demo banner |
| `trading_agent/ui/replay.html`, `trading_agent/ui/replay.js` | the Replay page |
| `tests/replay_fakes.py` | fake price source, universe and screen for replay tests |
| `tests/test_replay_*.py` | tests per task |
| `README.md` | Replay and Demo sections |

---

### Task 1: Replay clock and clocked prices

**Files:**
- Create: `trading_agent/replay/__init__.py`, `trading_agent/replay/clock.py`, `tests/replay_fakes.py`, `tests/test_replay_clock.py`
- Modify: `trading_agent/prices.py` (add `dividends`)

**Interfaces:**
- Produces:
  - `EARLIEST_START: str`
  - `class FutureDataError(LookupError)`
  - `ReplayClock(today: str)` with `.today -> str`, `.advance_to(day: str) -> None` and `.check(day: str) -> None`
  - `add_months(day: str, n: int) -> str`
  - `ClockedPrices(source, clock, *, field="adj_close")` with:
    - `.history(symbol, range_="2y") -> list[bar]`
    - `.latest_price(symbol) -> float` (also `__call__`)
    - `.price_on(symbol, day) -> float`
    - `.last_trade_date(symbol) -> str | None`
    - `.first_trade_date(symbol) -> str | None`
    - `.dividends(symbol) -> list[{"date","amount"}]`
    - `.calendar(symbol, after, until) -> list[str]`
    - `.clock` (rebindable)
  - `YahooPrices.dividends(symbol, range_="10y") -> list[{"date": str, "amount": float}]`

- [ ] **Step 1: Write the test fakes**

`tests/replay_fakes.py`:

```python
"""Deterministic fake market for replay tests: weekday calendar, smooth price paths."""
from __future__ import annotations

from datetime import date, timedelta


def weekdays(start: str = "2019-01-01", end: str = "2026-10-09") -> list[str]:
    d, stop, out = date.fromisoformat(start), date.fromisoformat(end), []
    while d <= stop:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


DAYS = weekdays()


def path(base: float, daily: float, start: str | None = None, end: str | None = None,
         volume: float = 5e6) -> list[dict]:
    """Bars with close = base * (1 + daily) ** i over the weekday calendar."""
    out = []
    for i, d in enumerate(DAYS):
        if (start and d < start) or (end and d > end):
            continue
        c = round(base * (1 + daily) ** i, 4)
        out.append({"date": d, "close": c, "adj_close": c, "volume": volume})
    return out


class FakeSource:
    """Stands in for YahooPrices: history() returns every bar it has; records calls."""

    def __init__(self, bars: dict[str, list[dict]], divs: dict[str, list[dict]] | None = None):
        self.bars = {k.upper(): v for k, v in bars.items()}
        self.divs = {k.upper(): v for k, v in (divs or {}).items()}
        self.calls: list[tuple[str, str]] = []

    def history(self, symbol: str, range_: str = "2y") -> list[dict]:
        self.calls.append(("history", symbol.upper()))
        if symbol.upper() not in self.bars:
            raise LookupError(f"Yahoo returned no history for {symbol}")
        return [dict(b) for b in self.bars[symbol.upper()]]

    def dividends(self, symbol: str, range_: str = "10y") -> list[dict]:
        return [dict(d) for d in self.divs.get(symbol.upper(), [])]


def market() -> FakeSource:
    """A..E grow at different rates; NEWCO lists in June 2022; GONE stops trading in March 2022."""
    return FakeSource({
        "^NSEI": path(10_000, 0.0004),
        "MID150BEES": path(100, 0.0005),
        "NIFTYBEES": path(150, 0.0004),
        "A": path(100, 0.0012), "B": path(200, 0.0009), "C": path(300, 0.0006),
        "D": path(400, 0.0003), "E": path(500, -0.0002),
        "NEWCO": path(50, 0.001, start="2022-06-01"),
        "GONE": path(80, 0.0005, end="2022-03-31"),
    })


class FakeUniverse:
    def __init__(self, symbols=("A", "B", "C", "D", "E")):
        self.current = [{"symbol": s, "name": f"{s} Limited", "industry": ""} for s in symbols]
        self.membership = None

    def members_on(self, day: str) -> list[dict]:
        return [dict(m) for m in self.current]


def top_by_6m(members, prices, top):
    """A tiny screen: rank by 6-month return, using only what the clocked prices show."""
    rows = []
    for m in members:
        bars = prices.history(m["symbol"], "1y")
        if len(bars) > 126:
            rows.append({"symbol": m["symbol"], "name": m.get("name", ""),
                         "ret_6m": bars[-1]["adj_close"] / bars[-127]["adj_close"] - 1})
    rows.sort(key=lambda r: -r["ret_6m"])
    return rows[:top]
```

- [ ] **Step 2: Write the failing tests**

`tests/test_replay_clock.py`:

```python
import pytest

from trading_agent.replay.clock import (EARLIEST_START, ClockedPrices, FutureDataError, ReplayClock,
                                        add_months)
from .replay_fakes import FakeSource, market, path


def test_clock_only_moves_forward():
    c = ReplayClock("2021-03-01")
    c.advance_to("2021-03-05")
    assert c.today == "2021-03-05"
    with pytest.raises(ValueError):
        c.advance_to("2021-03-04")
    c.check("2021-03-05")
    with pytest.raises(FutureDataError):
        c.check("2021-03-06")


def test_history_and_price_stop_at_the_clock():
    p = ClockedPrices(market(), ReplayClock("2021-03-03"))
    bars = p.history("A", "10y")
    assert bars[-1]["date"] == "2021-03-03" and all(b["date"] <= "2021-03-03" for b in bars)
    assert p.latest_price("A") == bars[-1]["adj_close"]
    assert len(p.history("A", "1y")) == 252
    with pytest.raises(FutureDataError):
        p.price_on("A", "2021-03-04")
    assert p.price_on("A", "2021-03-01") == p.history("A", "10y")[-3]["adj_close"]


def test_weekend_clock_uses_fridays_close():
    p = ClockedPrices(market(), ReplayClock("2021-03-07"))  # a Sunday
    assert p.history("A")[-1]["date"] == "2021-03-05"


def test_not_listed_yet_and_delisted():
    p = ClockedPrices(market(), ReplayClock("2021-03-03"))
    with pytest.raises(LookupError, match="2022-06-01"):
        p.latest_price("NEWCO")
    p.clock.advance_to("2023-01-02")
    assert p.last_trade_date("GONE") == "2022-03-31"
    assert p.first_trade_date("NEWCO") == "2022-06-01"


def test_field_close_vs_adj_close():
    bars = path(100, 0.0)
    for b in bars:
        if b["date"] < "2021-06-15":
            b["adj_close"] = 90.0
    src = FakeSource({"X": bars}, {"X": [{"date": "2021-06-15", "amount": 10.0}, {"date": "2022-06-15", "amount": 10.0}]})
    clock = ReplayClock("2021-06-01")
    assert ClockedPrices(src, clock).latest_price("X") == 90.0
    assert ClockedPrices(src, clock, field="close").latest_price("X") == 100.0
    clock.advance_to("2021-07-01")
    assert ClockedPrices(src, clock).dividends("X") == [{"date": "2021-06-15", "amount": 10.0}]


def test_calendar_and_source_called_once_per_symbol():
    src = market()
    p = ClockedPrices(src, ReplayClock("2021-03-01"))
    assert p.calendar("^NSEI", "2021-03-01", "2021-03-08") == ["2021-03-02", "2021-03-03", "2021-03-04",
                                                              "2021-03-05", "2021-03-08"]
    p.history("A"); p.history("A", "1y"); p.latest_price("A")
    assert src.calls.count(("history", "A")) == 1


def test_add_months_clamps_to_month_end():
    assert add_months("2021-01-31", 1) == "2021-02-28"
    assert add_months("2021-11-15", 3) == "2022-02-15"
    assert EARLIEST_START == "2021-01-04"
```

- [ ] **Step 3: Run the tests to see them fail**

Run: `.venv/Scripts/python.exe -m pytest -q tests/test_replay_clock.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'trading_agent.replay'`

- [ ] **Step 4: Implement the clock and clocked prices**

`trading_agent/replay/__init__.py`:

```python
"""Replay: a practice portfolio started on a past date, with nothing from after that date."""
```

`trading_agent/replay/clock.py`:

```python
"""The replay clock, and price data that cannot see past it.

Every number the Replay page shows is computed from these wrappers. They fetch a long
history once per symbol and slice it at the clock, so the screen, the signal lab, the
momentum stats and the stops (which all call ``prices.history``) work unchanged.
"""

from __future__ import annotations

import bisect
import calendar as _cal
import threading
from datetime import date
from typing import Any

EARLIEST_START = "2021-01-04"  # point-in-time index membership starts in 2021
_KEEP = {"1y": 252, "2y": 504, "5y": 1260}  # YahooPrices returns about this many bars


class FutureDataError(LookupError):
    """Asked for data dated after the replay clock."""


def _iso(day: Any) -> str:
    return date.fromisoformat(str(day)[:10]).isoformat()


def add_months(day: str, n: int) -> str:
    d = date.fromisoformat(day)
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    return date(y, m, min(d.day, _cal.monthrange(y, m)[1])).isoformat()


class ReplayClock:
    def __init__(self, today: str):
        self._today = _iso(today)

    @property
    def today(self) -> str:
        return self._today

    def advance_to(self, day: str) -> None:
        day = _iso(day)
        if day < self._today:
            raise ValueError(f"the replay clock only moves forward ({day} is before {self._today})")
        self._today = day

    def check(self, day: str) -> None:
        if _iso(day) > self._today:
            raise FutureDataError(f"{_iso(day)} is after the replay date {self._today}")


class ClockedPrices:
    """``field``: "adj_close" values with dividends reinvested, "close" with dividends paid as cash."""

    RANGE = "10y"

    def __init__(self, source: Any, clock: ReplayClock, *, field: str = "adj_close"):
        if field not in ("adj_close", "close"):
            raise ValueError("field must be adj_close or close")
        self.source, self.clock, self.field = source, clock, field
        self._bars: dict[str, list[dict[str, Any]]] = {}
        self._dates: dict[str, list[str]] = {}
        self._divs: dict[str, list[dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def _all(self, symbol: str) -> tuple[list[dict[str, Any]], list[str]]:
        s = symbol.upper()
        with self._lock:
            if s in self._bars:
                return self._bars[s], self._dates[s]
        bars = self.source.history(s, self.RANGE)
        with self._lock:
            self._bars[s], self._dates[s] = bars, [b["date"] for b in bars]
            return self._bars[s], self._dates[s]

    def history(self, symbol: str, range_: str = "2y") -> list[dict[str, Any]]:
        bars, dates = self._all(symbol)
        out = bars[:bisect.bisect_right(dates, self.clock.today)]
        keep = _KEEP.get(range_)
        return out[-keep:] if keep else out

    def latest_price(self, symbol: str) -> float:
        bars = self.history(symbol, self.RANGE)
        if not bars:
            first = self.first_trade_date(symbol)
            when = f"not listed until {first}" if first else "no price history"
            raise LookupError(f"{symbol.upper()} has no price on {self.clock.today} ({when})")
        return float(bars[-1][self.field])

    __call__ = latest_price

    def price_on(self, symbol: str, day: str) -> float:
        self.clock.check(day)
        bars, dates = self._all(symbol)
        i = bisect.bisect_right(dates, _iso(day))
        if not i:
            raise LookupError(f"{symbol.upper()} has no price on or before {day}")
        return float(bars[i - 1][self.field])

    def last_trade_date(self, symbol: str) -> str | None:
        bars = self.history(symbol, self.RANGE)
        return bars[-1]["date"] if bars else None

    def first_trade_date(self, symbol: str) -> str | None:
        bars, _ = self._all(symbol)
        return bars[0]["date"] if bars else None

    def dividends(self, symbol: str) -> list[dict[str, Any]]:
        s = symbol.upper()
        if s not in self._divs:
            try:
                self._divs[s] = sorted(self.source.dividends(s, self.RANGE), key=lambda d: d["date"])
            except Exception:  # noqa: BLE001 - no dividend data means none credited
                self._divs[s] = []
        return [d for d in self._divs[s] if d["date"] <= self.clock.today]

    def calendar(self, symbol: str, after: str, until: str) -> list[str]:
        """Trading days in (after, until] by ``symbol``'s bars. Only the step engine uses this:
        it walks into the future one day at a time, moving the clock as it goes."""
        _, dates = self._all(symbol)
        return [d for d in dates if after < d <= until]
```

Add to `trading_agent/prices.py`, inside `class YahooPrices`, after `history`:

```python
    def dividends(self, symbol: str, range_: str = "10y") -> list[dict[str, Any]]:
        """Dividends per share by ex-date, oldest first: [{date, amount}]. Cached on disk."""
        ysym = self.yahoo_symbol(symbol)
        cache = self.cache_dir / f"yahoo_div_{ysym.replace('^', 'IDX_')}_{range_}.json" if self.cache_dir else None
        if cache and cache.exists() and time.time() - cache.stat().st_mtime < self.cache_ttl:
            return json.loads(cache.read_text())
        resp = self.session.get(YAHOO_URL.format(symbol=ysym), headers=HEADERS,
                                params={"range": range_, "interval": "1d", "events": "div"}, timeout=self.timeout)
        resp.raise_for_status()
        try:
            events = (resp.json()["chart"]["result"][0].get("events") or {}).get("dividends") or {}
        except (KeyError, IndexError, TypeError) as e:
            raise LookupError(f"Yahoo returned no dividend data for {ysym}") from e
        out = sorted(({"date": datetime.fromtimestamp(int(v["date"]), tz=timezone.utc).strftime("%Y-%m-%d"),
                       "amount": float(v["amount"])} for v in events.values()), key=lambda d: d["date"])
        if cache:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(out))
        return out
```

Add a test to `tests/test_replay_clock.py`:

```python
def test_yahoo_dividends_parsed_from_chart_events(tmp_path):
    from trading_agent.prices import YahooPrices
    from .conftest import FakeSession

    payload = {"chart": {"result": [{"events": {"dividends": {
        "1": {"amount": 3.6, "date": 1687405500}, "0": {"amount": 5.1, "date": 1655264700}}}}]}}
    p = YahooPrices(session=FakeSession({("GET", "TATASTEEL.NS"): payload}), cache_dir=tmp_path)
    assert p.dividends("TATASTEEL") == [{"date": "2022-06-15", "amount": 5.1}, {"date": "2023-06-22", "amount": 3.6}]
```

- [ ] **Step 5: Run the tests to see them pass**

Run: `.venv/Scripts/python.exe -m pytest -q tests/test_replay_clock.py`
Expected: 8 passed

- [ ] **Step 6: Commit**

```bash
git add trading_agent/replay/__init__.py trading_agent/replay/clock.py trading_agent/prices.py tests/replay_fakes.py tests/test_replay_clock.py
git commit -m "Replay: a clock that only moves forward, and prices that can't see past it

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Clocked NSE announcements

**Files:**
- Create: `trading_agent/replay/news.py`, `tests/test_replay_news.py`
- Modify: `trading_agent/nse.py` (add `announcement_history` after `announcements`)

**Interfaces:**
- Consumes: `ReplayClock`, `FutureDataError` (Task 1); `nse._norm_announcement` (existing, returns `{id, symbol, company, at, category, text, file}` with `at` like `"2026-10-08 20:47:31"`).
- Produces:
  - `NSEClient.announcement_history(symbol) -> list[dict]`: newest first, cached for a day in `cache_dir/nse_ann/<SYMBOL>.json`.
  - `ClockedNews(client, clock)` with `.for_symbol(symbol, days=60, until=None) -> {"items": list, "error": str | None}`.

- [ ] **Step 1: Write the failing tests**

`tests/test_replay_news.py`:

```python
import pytest

from trading_agent.nse import NSEClient
from trading_agent.replay.clock import FutureDataError, ReplayClock
from trading_agent.replay.news import ClockedNews
from .conftest import FakeSession

ROWS = [{"symbol": "TATASTEEL", "sort_date": d, "desc": "Updates", "attchmntText": t, "seq_id": str(i)}
        for i, (d, t) in enumerate([("2021-01-10 10:00:00", "old"), ("2021-02-20 09:00:00", "in window"),
                                    ("2021-03-01 18:00:00", "on the day"), ("2021-03-02 08:00:00", "future")])]


def client(tmp_path, payload=ROWS):
    return NSEClient(session=FakeSession({("GET", "corporate-announcements"): payload}), cache_dir=tmp_path)


def test_history_is_cached_per_symbol(tmp_path):
    c = client(tmp_path)
    first = c.announcement_history("tatasteel")
    assert [a["text"] for a in first] == ["future", "on the day", "in window", "old"]
    calls = len(c.session.calls)
    assert c.announcement_history("TATASTEEL") == first and len(c.session.calls) == calls
    assert (tmp_path / "nse_ann" / "TATASTEEL.json").exists()


def test_news_is_sliced_at_the_clock(tmp_path):
    news = ClockedNews(client(tmp_path), ReplayClock("2021-03-01"))
    r = news.for_symbol("TATASTEEL", days=30)
    assert [a["text"] for a in r["items"]] == ["on the day", "in window"] and r["error"] is None
    with pytest.raises(FutureDataError):
        news.for_symbol("TATASTEEL", until="2021-03-02")


def test_blocked_news_is_reported_not_raised(tmp_path):
    news = ClockedNews(client(tmp_path, RuntimeError("HTTP 403")), ReplayClock("2021-03-01"))
    r = news.for_symbol("TATASTEEL")
    assert r["items"] == [] and r["error"] == "news unavailable for this period (HTTP 403)"
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/Scripts/python.exe -m pytest -q tests/test_replay_news.py`
Expected: FAIL with `AttributeError: 'NSEClient' object has no attribute 'announcement_history'` or `ModuleNotFoundError`

- [ ] **Step 3: Implement**

In `trading_agent/nse.py`, store the cache root in `__init__` (after `self.pit_cache = ...`):

```python
        self.cache_dir = Path(cache_dir) if cache_dir else None
```

Then add after `announcements`:

```python
    def announcement_history(self, symbol: str, max_age_s: float = 86400.0) -> list[dict[str, Any]]:
        """Every NSE announcement for one company, newest first (back to 2004 for old listings).

        NSE's date-range query times out, but the per-symbol query returns the whole history,
        so Replay fetches it once a day and slices it at its clock."""
        sym = symbol.upper()
        path = self.cache_dir / "nse_ann" / f"{sym}.json" if self.cache_dir else None
        if path and path.exists() and time.time() - path.stat().st_mtime < max_age_s:
            return json.loads(path.read_text(encoding="utf-8"))
        data = self._get("api/corporate-announcements", params={"index": "equities", "symbol": sym},
                         referer=f"{self.base_url}/companies-listing/corporate-filings-announcements")
        rows = data if isinstance(data, list) else data.get("data", [])
        out = sorted((_norm_announcement(r) for r in rows), key=lambda a: a["at"], reverse=True)
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(out), encoding="utf-8")
        return out
```

(`json`, `time` and `Path` are already imported in `nse.py`; check with `grep -n "^import\|^from" trading_agent/nse.py` and add any that are missing.)

`trading_agent/replay/news.py`:

```python
"""NSE announcements as they stood on the replay date."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from .clock import ReplayClock


class ClockedNews:
    def __init__(self, client: Any, clock: ReplayClock):
        self.client, self.clock = client, clock

    def for_symbol(self, symbol: str, days: int = 60, until: str | None = None) -> dict[str, Any]:
        until = until or self.clock.today
        self.clock.check(until)
        since = (date.fromisoformat(until) - timedelta(days=days)).isoformat()
        try:
            rows = self.client.announcement_history(symbol)
        except Exception as e:  # noqa: BLE001 - NSE throttles; missing news must show, not break a step
            return {"items": [], "error": f"news unavailable for this period ({e})"}
        items = [a for a in rows if since < a["at"][:10] <= until]
        return {"items": items, "error": None}
```

- [ ] **Step 4: Run them to see them pass**

Run: `.venv/Scripts/python.exe -m pytest -q tests/test_replay_news.py`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add trading_agent/nse.py trading_agent/replay/news.py tests/test_replay_news.py
git commit -m "Replay: NSE announcements per company, cached, sliced at the replay date

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Paper broker hooks and a reusable rebalance

**Files:**
- Modify: `trading_agent/broker.py` (`LocalPaperBroker.__init__`, `submit_order`, new `credit`, `credits`), `trading_agent/forward.py` (extract `rebalance_to`)
- Test: `tests/test_replay_broker.py`; the existing `tests/test_forward.py` must still pass unchanged

**Interfaces:**
- Produces:
  - `LocalPaperBroker(..., now_fn: Callable[[], str] | None = None)`: `filled_at` uses `now_fn()`.
  - `LocalPaperBroker.credit(amount: float, note: str, at: str) -> None` and `LocalPaperBroker.credits() -> list[dict]`.
  - `forward.rebalance_to(broker, picks: list[str], *, top: int, price_fn, cost_model) -> list[dict]`: the trades, each `{"symbol","side","qty","price"}` or with `"error"`.

- [ ] **Step 1: Write the failing tests**

`tests/test_replay_broker.py`:

```python
from trading_agent.broker import LocalPaperBroker
from trading_agent.costs import cost_model_for
from trading_agent.forward import rebalance_to


def test_orders_carry_the_injected_time_and_credits_add_cash(tmp_path):
    b = LocalPaperBroker(tmp_path / "b.json", starting_cash=10_000, price_fn=lambda s: 100.0,
                         whole_shares=True, now_fn=lambda: "2021-03-01T15:30:00+05:30")
    o = b.submit_order("A", "buy", qty=10)
    assert o["filled_at"] == "2021-03-01T15:30:00+05:30"
    b.credit(55.5, "dividend A", "2021-06-15")
    assert b.account().cash == 10_000 - 1000 + 55.5
    assert b.credits() == [{"at": "2021-06-15", "amount": 55.5, "note": "dividend A"}]
    again = LocalPaperBroker(tmp_path / "b.json", price_fn=lambda s: 100.0)
    assert again.credits()[0]["amount"] == 55.5  # persisted


def test_rebalance_to_equal_weights(tmp_path):
    prices = {"A": 100.0, "B": 250.0}
    b = LocalPaperBroker(tmp_path / "b.json", starting_cash=100_000, price_fn=lambda s: prices[s],
                         whole_shares=True, cost_model=cost_model_for("in"))
    trades = rebalance_to(b, ["A", "B"], top=2, price_fn=lambda s: prices[s], cost_model=cost_model_for("in"))
    assert {t["symbol"] for t in trades} == {"A", "B"} and all("error" not in t for t in trades)
    held = {p.symbol: p.qty * p.current_price for p in b.positions()}
    assert 47_000 < held["A"] <= 50_000 and 47_000 < held["B"] <= 50_000
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/Scripts/python.exe -m pytest -q tests/test_replay_broker.py`
Expected: FAIL with `TypeError: ... unexpected keyword argument 'now_fn'` / `ImportError: cannot import name 'rebalance_to'`

- [ ] **Step 3: Implement the broker hooks**

In `trading_agent/broker.py`, change the `LocalPaperBroker.__init__` signature and store `now_fn`:

```python
    def __init__(self, path: Path, starting_cash: float = 80_000.0,
                 price_fn: Any | None = None, currency: str = "USD",
                 whole_shares: bool = False, cost_model: Any | None = None,
                 now_fn: Any | None = None):
        self.path = Path(path)
        self.price_fn = price_fn
        self.currency = currency
        self.whole_shares = whole_shares  # Indian equities trade in whole shares
        self.cost_model = cost_model  # object with .charges(side, notional); None = free
        self.now_fn = now_fn or _utc_now  # Replay stamps fills with the replay date
```

(keep the rest of `__init__` as it is). In `submit_order`, replace

```python
            "filled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
```

with

```python
            "filled_at": self.now_fn(),
```

Add after `orders()`:

```python
    def credit(self, amount: float, note: str, at: str) -> None:
        """Add cash that isn't a trade (a dividend paid out in Replay)."""
        self._state["cash"] += float(amount)
        self._state.setdefault("credits", []).append({"at": at, "amount": round(float(amount), 2), "note": note})
        self._save()

    def credits(self) -> list[dict[str, Any]]:
        return list(self._state.get("credits", []))
```

- [ ] **Step 4: Extract `rebalance_to` in `trading_agent/forward.py`**

Add above `class ForwardTest`:

```python
def rebalance_to(broker: Any, picks: list[str], *, top: int, price_fn: Callable[[str], float],
                 cost_model: Any | None) -> list[dict[str, Any]]:
    """Trade ``broker`` to equal weights in ``picks`` (the forward-test rules): sell what
    dropped out, trim a kept name only above 125% of its target, then buy up to the target
    while cash lasts. Returns the trades; a missing price or cash skips one trade."""
    picks = [p.upper() for p in picks][:top]
    held = {p.symbol: p for p in broker.positions()}
    trades: list[dict[str, Any]] = []

    def trade(sym: str, side: str, qty: int) -> None:
        if qty < 1:
            return
        try:
            o = broker.submit_order(sym, side, qty=qty)
            trades.append({"symbol": sym, "side": side, "qty": o["qty"], "price": o["filled_avg_price"]})
        except Exception as e:  # noqa: BLE001
            trades.append({"symbol": sym, "side": side, "qty": qty, "error": str(e)})

    for sym, p in held.items():
        if sym not in picks:
            trade(sym, "sell", int(p.qty))
    target = broker.account().equity / top
    for sym in picks:
        p = held.get(sym)
        if p is not None and p.current_price and p.qty * p.current_price > target * TRIM_ABOVE:
            trade(sym, "sell", int(math.floor((p.qty * p.current_price - target) / p.current_price)))
    cash = broker.account().cash
    for sym in picks:
        p = next((x for x in broker.positions() if x.symbol == sym), None)
        have = p.qty * p.current_price if p and p.current_price else 0.0
        if have >= target * 0.95:
            continue
        try:
            price = float(price_fn(sym))
        except Exception as e:  # noqa: BLE001
            trades.append({"symbol": sym, "side": "buy", "qty": 0, "error": f"no price: {e}"})
            continue
        budget = min(target - have, cash)
        charges = float(cost_model.charges("buy", budget)) if cost_model else 0.0
        qty = int(math.floor((budget - charges) / price))
        if qty >= 1:
            trade(sym, "buy", qty)
            cash = broker.account().cash
    return trades
```

Replace the body of `ForwardTest.rebalance` with:

```python
    def rebalance(self, picks: list[str], *, eligible: int | None = None) -> dict[str, Any]:
        """Trade the paper account to equal weights in ``picks``."""
        top = self.data["top"]
        fees_before = self.broker.performance()["fees_paid"]
        trades = rebalance_to(self.broker, picks, top=top, price_fn=self.price_fn, cost_model=self.cost_model)
        entry = {"date": self.now().date().isoformat(), "month": self._month(),
                 "picks": [p.upper() for p in picks][:top], "eligible": eligible, "trades": trades,
                 "charges": round(self.broker.performance()["fees_paid"] - fees_before, 2)}
        self.data["rebalances"] = (self.data["rebalances"] + [entry])[-60:]
        self.data["last_rebalance"] = self._month()
        return entry
```

- [ ] **Step 5: Run the new and the forward tests**

Run: `.venv/Scripts/python.exe -m pytest -q tests/test_replay_broker.py tests/test_forward.py`
Expected: all pass (the forward tests unchanged)

- [ ] **Step 6: Commit**

```bash
git add trading_agent/broker.py trading_agent/forward.py tests/test_replay_broker.py
git commit -m "Paper broker: injectable fill time and cash credits; forward-test rebalance reusable

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Trials, the three portfolios and your orders

**Files:**
- Create: `trading_agent/replay/trial.py`, `tests/test_replay_trial.py`

**Interfaces:**
- Consumes: Task 1 (`ReplayClock`, `ClockedPrices`, `EARLIEST_START`), Task 3 (`now_fn`, `rebalance_to`); `cost_model_for`; `screen.run_screen`, `screen.load_universe`; `index_history.point_in_time`.
- Produces:
  - `BENCHMARKS: dict[str, str]`
  - `ReplayUniverse(name, state_dir)` with `.current: list[dict]`, `.membership` and `.members_on(day) -> list[dict]`.
  - `default_screen(members, prices, top) -> list[dict]`: rows with at least `"symbol"`.
  - `Trial`:
    - `Trial.create(replay_dir, *, name, start, cash, universe, top, dividends, source, universe_obj, screen_fn=None, today=None) -> Trial`
    - `Trial.load(root, source, universe_obj, screen_fn=None) -> Trial`
    - attributes `.root`, `.data`, `.clock`, `.prices`, `.you`, `.agent`, `.nifty`, `.universe`
    - methods `.save()`, `.transaction()` (context manager), `.order(symbol, side, notional=None, qty=None) -> dict`, `.rebalance_agent() -> dict`, `.refresh_picks() -> None` and `.point() -> dict`
  - `list_trials(replay_dir) -> list[dict]`

`trial.json` keys:
- `name`, `slug`, `start`, `clock`, `universe`, `benchmark`, `cash`, `top`, `dividends`, `auto_stop`, `ended`
- `last_rebalance_month`, `equity` (list of `{date, you, agent, nifty}`), `rebalances`, `stops`
- `picks` (`{"date", "rows"}`), `claude` (list), `claude_presses`

- [ ] **Step 1: Write the failing tests**

`tests/test_replay_trial.py`:

```python
import json

import pytest

from trading_agent.replay.trial import Trial, list_trials
from .replay_fakes import FakeUniverse, market, top_by_6m


def make(tmp_path, **kw):
    args = dict(name="My first", start="2021-03-01", cash=100_000, universe="NIFTYMIDCAP150", top=3,
                dividends="reinvest", source=market(), universe_obj=FakeUniverse(), screen_fn=top_by_6m,
                today="2026-10-09")
    args.update(kw)
    return Trial.create(tmp_path / "replay", **args)


def test_create_buys_the_fund_and_the_agents_picks(tmp_path):
    t = make(tmp_path)
    assert t.data["slug"] == "my-first" and t.data["benchmark"] == "MID150BEES" and t.clock.today == "2021-03-01"
    assert [p.symbol for p in t.nifty.positions()] == ["MID150BEES"]
    assert 0 <= t.nifty.account().cash < 300  # all in, less than one unit left
    assert {p.symbol for p in t.agent.positions()} == {"A", "B", "C"}  # fastest growers
    assert t.you.positions() == [] and t.you.account().cash == 100_000
    assert t.data["equity"][0]["date"] == "2021-03-01" and t.data["last_rebalance_month"] == "2021-03"
    assert [r["symbol"] for r in t.data["picks"]["rows"]] == ["A", "B", "C"]
    assert (tmp_path / "replay" / "my-first" / "trial.json").exists()


@pytest.mark.parametrize("kw, msg", [
    ({"start": "2020-12-31"}, "on or after 2021-01-04"),
    ({"start": "2026-10-09"}, "in the past"),
    ({"universe": "NIFTYIT"}, "universe"),
    ({"dividends": "sometimes"}, "dividends"),
    ({"top": 0}, "between 1 and 30"),
    ({"cash": 5_000}, "at least"),
])
def test_create_validates(tmp_path, kw, msg):
    with pytest.raises(ValueError, match=msg):
        make(tmp_path, **kw)
    assert not (tmp_path / "replay" / "my-first").exists()


def test_duplicate_name_refused(tmp_path):
    make(tmp_path)
    with pytest.raises(ValueError, match="already"):
        make(tmp_path)


def test_create_on_a_sunday_uses_fridays_close(tmp_path):
    t = make(tmp_path, start="2021-03-07")
    assert t.clock.today == "2021-03-07"
    assert t.nifty.orders()[0]["filled_avg_price"] == t.prices.price_on("MID150BEES", "2021-03-05")


def test_you_order_fills_at_the_replay_close_and_is_dated(tmp_path):
    t = make(tmp_path)
    o = t.order("d", "buy", notional=10_000)
    assert o["symbol"] == "D" and o["qty"] >= 1
    assert o["filled_avg_price"] == t.prices.latest_price("D") and o["filled_at"].startswith("2021-03-01")


def test_order_before_listing_is_refused_with_the_listing_date(tmp_path):
    t = make(tmp_path)
    before = (t.root / "you.json").read_text() if (t.root / "you.json").exists() else None
    with pytest.raises(LookupError, match="not listed until 2022-06-01"):
        t.order("NEWCO", "buy", notional=5_000)
    after = (t.root / "you.json").read_text() if (t.root / "you.json").exists() else None
    assert before == after


def test_transaction_rolls_back_files_and_clock(tmp_path):
    t = make(tmp_path)
    saved = (t.root / "trial.json").read_text()
    with pytest.raises(RuntimeError):
        with t.transaction():
            t.clock.advance_to("2021-04-01")
            t.order("D", "buy", notional=10_000)
            t.data["clock"] = "2021-04-01"
            t.save()
            raise RuntimeError("Yahoo failed for X")
    assert (t.root / "trial.json").read_text() == saved and t.clock.today == "2021-03-01"
    assert t.you.positions() == [] and t.data["clock"] == "2021-03-01"


def test_load_and_list(tmp_path):
    t = make(tmp_path)
    again = Trial.load(t.root, market(), FakeUniverse(), screen_fn=top_by_6m)
    assert again.data == json.loads((t.root / "trial.json").read_text())
    rows = list_trials(tmp_path / "replay")
    assert rows[0]["slug"] == "my-first" and rows[0]["you"] == 0.0 and rows[0]["agent"] is not None
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/Scripts/python.exe -m pytest -q tests/test_replay_trial.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'trading_agent.replay.trial'`

- [ ] **Step 3: Implement `trading_agent/replay/trial.py`**

```python
"""A replay trial: three practice portfolios that start on a past date.

Files in ``state/replay/<slug>/``: ``trial.json`` (settings, clock, curves, logs) and one
paper-broker file per portfolio (``you.json``, ``agent.json``, ``nifty.json``). Fills are
stamped with the replay date. Nothing here can reach a real broker.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any, Callable, Iterator

from ..broker import LocalPaperBroker
from ..costs import cost_model_for
from ..forward import rebalance_to
from .clock import EARLIEST_START, ClockedPrices, ReplayClock

BENCHMARKS = {"NIFTYMIDCAP150": "MID150BEES", "NIFTY50": "NIFTYBEES", "NIFTY100": "NIFTYBEES",
              "NIFTY200": "NIFTYBEES", "NIFTY500": "NIFTYBEES", "NIFTYSMALLCAP250": "HDFCSML250"}
PORTFOLIOS = ("you", "agent", "nifty")


class ReplayUniverse:
    """Today's constituent list (names) plus the point-in-time membership history."""

    def __init__(self, name: str, state_dir: Path):
        from ..index_history import point_in_time
        from ..screen import load_universe

        self.name = name.upper()
        self.current = load_universe(self.name)
        self.membership = point_in_time(self.name, [m["symbol"] for m in self.current], Path(state_dir))
        if self.membership is None:
            raise ValueError(f"no membership history for {self.name}; pick another universe")
        self._names = {m["symbol"]: m for m in self.current}

    def members_on(self, day: str) -> list[dict[str, str]]:
        return [{"symbol": s, "name": self._names.get(s, {}).get("name", ""),
                 "industry": self._names.get(s, {}).get("industry", "")}
                for s in sorted(self.membership.members_on(day))]


def default_screen(members: list[dict[str, str]], prices: Any, top: int) -> list[dict[str, Any]]:
    from ..screen import run_screen
    return run_screen(members, prices, top=top, workers=8)["top"]


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")[:40]
    if not s:
        raise ValueError("give the replay a name")
    return s


class Trial:
    FILES = ("trial.json", "you.json", "agent.json", "nifty.json")

    def __init__(self, root: Path, data: dict[str, Any], source: Any, universe_obj: Any,
                 screen_fn: Callable[..., list[dict[str, Any]]] | None = None):
        self.root, self.data, self.source, self.universe = Path(root), data, source, universe_obj
        self.screen_fn = screen_fn or default_screen
        self.cost_model = cost_model_for("in")
        self.clock = ReplayClock(data["clock"])
        self.prices = ClockedPrices(source, self.clock, field="close" if data["dividends"] == "cash" else "adj_close")
        self._open_brokers()

    # -- files -------------------------------------------------------------------
    def _stamp(self) -> str:
        return f"{self.clock.today}T15:30:00+05:30"

    def _open_brokers(self) -> None:
        def mk(name: str) -> LocalPaperBroker:
            return LocalPaperBroker(self.root / f"{name}.json", starting_cash=self.data["cash"],
                                    price_fn=self.prices, currency="INR", whole_shares=True,
                                    cost_model=self.cost_model, now_fn=self._stamp)
        self.you, self.agent, self.nifty = mk("you"), mk("agent"), mk("nifty")

    def broker(self, who: str) -> LocalPaperBroker:
        return {"you": self.you, "agent": self.agent, "nifty": self.nifty}[who]

    def save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.root / "trial.json.tmp"
        tmp.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
        os.replace(tmp, self.root / "trial.json")

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """All or nothing: on any error, put every file, the data and the clock back."""
        saved = {f: (self.root / f).read_bytes() for f in self.FILES if (self.root / f).exists()}
        data, clock = json.loads(json.dumps(self.data)), self.clock.today
        try:
            yield
        except BaseException:
            for f in self.FILES:
                p = self.root / f
                if f in saved:
                    p.write_bytes(saved[f])
                elif p.exists():
                    p.unlink()
            self.data = data
            self.clock = ReplayClock(clock)
            self.prices.clock = self.clock
            self._open_brokers()
            raise

    # -- create / load -------------------------------------------------------------
    @classmethod
    def create(cls, replay_dir: Path, *, name: str, start: str, cash: float, universe: str, top: int,
               dividends: str, source: Any, universe_obj: Any,
               screen_fn: Callable[..., list[dict[str, Any]]] | None = None, today: str | None = None) -> "Trial":
        today = today or date.today().isoformat()
        start = date.fromisoformat(str(start)[:10]).isoformat()
        universe, top, cash = universe.upper(), int(top), float(cash)
        if start < EARLIEST_START:
            raise ValueError(f"start on or after {EARLIEST_START}: index membership history begins in 2021")
        if start >= today:
            raise ValueError("pick a start date in the past")
        if universe not in BENCHMARKS:
            raise ValueError(f"unknown universe {universe}; choose from {', '.join(BENCHMARKS)}")
        if dividends not in ("reinvest", "cash"):
            raise ValueError("dividends must be reinvest or cash")
        if not 1 <= top <= 30:
            raise ValueError("the agent's number of stocks must be between 1 and 30")
        if cash < 10_000:
            raise ValueError("practice money must be at least ₹10,000")
        slug = _slug(name)
        root = Path(replay_dir) / slug
        if root.exists():
            raise ValueError(f"a replay called {slug} already exists")
        data = {"name": name.strip(), "slug": slug, "start": start, "clock": start, "universe": universe,
                "benchmark": BENCHMARKS[universe], "cash": cash, "top": top, "dividends": dividends,
                "auto_stop": False, "ended": None, "last_rebalance_month": None, "equity": [],
                "rebalances": [], "stops": [], "picks": None, "claude": [], "claude_presses": 0}
        root.mkdir(parents=True)
        try:
            t = cls(root, data, source, universe_obj, screen_fn)
            t._buy_benchmark()
            t.rebalance_agent()
            t.data["equity"].append(t.point())
            t.save()
        except BaseException:
            shutil.rmtree(root, ignore_errors=True)
            raise
        return t

    @classmethod
    def load(cls, root: Path, source: Any, universe_obj: Any,
             screen_fn: Callable[..., list[dict[str, Any]]] | None = None) -> "Trial":
        data = json.loads((Path(root) / "trial.json").read_text(encoding="utf-8"))
        return cls(root, data, source, universe_obj, screen_fn)

    # -- trading -------------------------------------------------------------------
    def _buy_benchmark(self) -> None:
        fund, cash = self.data["benchmark"], self.data["cash"]
        try:
            price = self.prices.latest_price(fund)
        except LookupError as e:
            raise ValueError(f"{fund} has no price on {self.clock.today}: pick a later date or "
                             f"another universe ({e})") from e
        qty = int((cash - self.cost_model.charges("buy", cash)) // price)
        while qty > 0 and qty * price + self.cost_model.charges("buy", qty * price) > cash:
            qty -= 1
        if qty < 1:
            raise ValueError(f"₹{cash:,.0f} buys less than one unit of {fund}")
        self.nifty.submit_order(fund, "buy", qty=qty)

    def order(self, symbol: str, side: str, notional: float | None = None, qty: float | None = None) -> dict[str, Any]:
        if self.data["ended"]:
            raise ValueError("this replay has ended; it is read-only")
        if side not in ("buy", "sell"):
            raise ValueError("side must be buy or sell")
        return self.you.submit_order(symbol.strip().upper(), side, notional=notional, qty=qty)

    def rebalance_agent(self) -> dict[str, Any]:
        day, top = self.clock.today, self.data["top"]
        rows = self.screen_fn(self.universe.members_on(day), self.prices, top)
        picks = [r["symbol"] for r in rows][:top]
        trades = rebalance_to(self.agent, picks, top=top, price_fn=self.prices, cost_model=self.cost_model)
        entry = {"date": day, "picks": picks, "trades": trades}
        self.data["rebalances"] = (self.data["rebalances"] + [entry])[-120:]
        self.data["last_rebalance_month"] = day[:7]
        self.data["picks"] = {"date": day, "rows": [_pick_row(r) for r in rows]}
        return entry

    def refresh_picks(self) -> None:
        """The screen as of the clock (shown on the page; the agent trades only on rebalance days)."""
        rows = self.screen_fn(self.universe.members_on(self.clock.today), self.prices, self.data["top"])
        self.data["picks"] = {"date": self.clock.today, "rows": [_pick_row(r) for r in rows]}

    def point(self) -> dict[str, Any]:
        return {"date": self.clock.today, **{w: round(self.broker(w).account().equity, 2) for w in PORTFOLIOS}}


def _pick_row(r: dict[str, Any]) -> dict[str, Any]:
    keep = ("symbol", "name", "ret_12_1", "ret_6m", "above_200dma", "last_close", "verdict")
    return {k: r.get(k) for k in keep if k in r}


def list_trials(replay_dir: Path) -> list[dict[str, Any]]:
    out = []
    for p in sorted(Path(replay_dir).glob("*/trial.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        last = d["equity"][-1] if d.get("equity") else {}
        ret = {w: (last[w] / d["cash"] - 1) if last.get(w) else None for w in PORTFOLIOS}
        out.append({"slug": d["slug"], "name": d["name"], "start": d["start"], "clock": d["clock"],
                    "universe": d["universe"], "ended": d["ended"], **ret})
    out.sort(key=lambda r: r["clock"], reverse=True)
    return out
```

- [ ] **Step 4: Run them to see them pass**

Run: `.venv/Scripts/python.exe -m pytest -q tests/test_replay_trial.py`
Expected: 13 passed

- [ ] **Step 5: Commit**

```bash
git add trading_agent/replay/trial.py tests/test_replay_trial.py
git commit -m "Replay: trials with three practice portfolios that start on a past date

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: The step engine

**Files:**
- Create: `trading_agent/replay/engine.py`, `tests/test_replay_engine.py`

**Interfaces:**
- Consumes: `Trial` (Task 4), `ClockedPrices.calendar/dividends` (Task 1), `LocalPaperBroker.credit` (Task 3), `risk.check_stops`.
- Produces:
  - `step_target(clock: str, by: str, today: str) -> str` (`by` in `week|month|year`, capped at `today`)
  - `step(trial, until: str, *, today: str | None = None, progress=None) -> dict` → `{"from","to","days","rebalances","stops","dividends"}`
  - `CALENDAR = "^NSEI"`

- [ ] **Step 1: Write the failing tests**

`tests/test_replay_engine.py`:

```python
import pytest

from trading_agent.replay.engine import step, step_target
from trading_agent.replay.trial import Trial
from .replay_fakes import FakeSource, FakeUniverse, market, path, top_by_6m


def make(tmp_path, name="t", source=None, **kw):
    args = dict(name=name, start="2021-03-15", cash=100_000, universe="NIFTYMIDCAP150", top=3,
                dividends="reinvest", source=source or market(), universe_obj=FakeUniverse(),
                screen_fn=top_by_6m, today="2026-10-09")
    args.update(kw)
    return Trial.create(tmp_path / "replay", **args)


def test_step_target():
    assert step_target("2021-03-15", "week", "2026-10-09") == "2021-03-22"
    assert step_target("2021-03-15", "month", "2026-10-09") == "2021-04-15"
    assert step_target("2021-03-15", "year", "2026-10-09") == "2022-03-15"
    with pytest.raises(ValueError):
        step_target("2021-03-15", "decade", "2026-10-09")


def test_month_step_records_every_day_and_rebalances_once(tmp_path):
    t = make(tmp_path)
    r = step(t, "2021-04-15", today="2026-10-09")
    assert t.clock.today == "2021-04-15" and t.data["clock"] == "2021-04-15"
    dates = [p["date"] for p in t.data["equity"]]
    assert dates[0] == "2021-03-15" and dates[1] == "2021-03-16" and dates[-1] == "2021-04-15"
    assert r["days"] == len(dates) - 1 and r["rebalances"] == ["2021-04-01"]
    assert [x["date"] for x in t.data["rebalances"]] == ["2021-03-15", "2021-04-01"]
    assert t.data["picks"]["date"] == "2021-04-15"
    assert t.nifty.positions()[0].current_price == t.prices.latest_price("MID150BEES")


def test_one_year_equals_twelve_months(tmp_path):
    a, b = make(tmp_path, "a"), make(tmp_path, "b")
    step(a, "2022-03-15", today="2026-10-09")
    for _ in range(12):
        step(b, step_target(b.clock.today, "month", "2026-10-09"), today="2026-10-09")
    assert a.data["equity"] == b.data["equity"]
    assert [x["date"] for x in a.data["rebalances"]] == [x["date"] for x in b.data["rebalances"]]


def test_agent_stop_sells_on_the_day_of_the_fall(tmp_path):
    bars = path(100, 0.002)
    for b in bars:
        if b["date"] >= "2021-04-06":
            b["close"] = b["adj_close"] = 60.0  # a crash on 6 April
    src = market()
    src.bars["A"] = bars
    t = make(tmp_path, source=src)
    assert "A" in {p.symbol for p in t.agent.positions()}
    step(t, "2021-04-15", today="2026-10-09")
    assert t.data["stops"][0]["date"] == "2021-04-06" and t.data["stops"][0]["symbol"] == "A"
    assert t.data["stops"][0]["who"] == "agent"


def test_your_stops_only_with_auto_sell(tmp_path):
    src = market()
    src.bars["D"] = [dict(b, close=60.0, adj_close=60.0) if b["date"] >= "2021-03-20" else b for b in src.bars["D"]]
    t = make(tmp_path, source=src)
    t.order("D", "buy", qty=10)
    step(t, "2021-03-25", today="2026-10-09")
    assert "D" in {p.symbol for p in t.you.positions()}
    t.data["auto_stop"] = True
    step(t, "2021-03-26", today="2026-10-09")
    assert "D" not in {p.symbol for p in t.you.positions()}


def test_dividends_reinvest_and_cash_agree(tmp_path):
    def src():
        bars = path(100, 0.0)
        for b in bars:
            if b["date"] < "2021-06-15":
                b["adj_close"] = 90.0
            else:
                b["close"] = b["adj_close"] = 90.0
        m = market()
        m.bars["X"] = bars
        m.divs["X"] = [{"date": "2021-06-15", "amount": 10.0}]
        return m
    totals = {}
    for mode in ("reinvest", "cash"):
        t = make(tmp_path, mode, source=src(), dividends=mode)
        t.order("X", "buy", qty=10)
        step(t, "2021-07-01", today="2026-10-09")
        totals[mode] = t.you.account().equity
        if mode == "cash":
            assert t.you.credits() == [{"at": "2021-06-15", "amount": 100.0, "note": "dividend X"}]
    assert abs(totals["reinvest"] - totals["cash"]) < 5  # only charges on a slightly different notional


def test_failed_step_leaves_the_trial_unchanged(tmp_path):
    t = make(tmp_path)
    before = (t.root / "trial.json").read_text()

    def boom(members, prices, top):
        raise LookupError("Yahoo returned no history for B")
    t.screen_fn = boom
    with pytest.raises(LookupError):
        step(t, "2021-04-15", today="2026-10-09")
    assert (t.root / "trial.json").read_text() == before and t.clock.today == "2021-03-15"


def test_step_is_capped_at_today(tmp_path):
    t = make(tmp_path, start="2026-03-02")
    r = step(t, step_target("2026-03-02", "year", "2026-10-09"), today="2026-10-09")
    assert r["to"] == "2026-10-09" and t.clock.today == "2026-10-09"


def test_ended_trial_cannot_step(tmp_path):
    t = make(tmp_path)
    t.data["ended"] = "2021-03-15"
    with pytest.raises(ValueError, match="ended"):
        step(t, "2021-04-15", today="2026-10-09")
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/Scripts/python.exe -m pytest -q tests/test_replay_engine.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'trading_agent.replay.engine'`

- [ ] **Step 3: Implement `trading_agent/replay/engine.py`**

```python
"""One replay step: walk every trading day up to the target date.

Each day, in order: the agent rebalances on the first trading day of a month, stops are
checked (the agent's always, yours when auto-sell is on), dividends are credited in cash
mode, and the three portfolio values are recorded. The whole step is one transaction.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Callable

from ..risk import check_stops
from .clock import add_months

CALENDAR = "^NSEI"


def step_target(clock: str, by: str, today: str) -> str:
    if by == "week":
        target = (date.fromisoformat(clock) + timedelta(days=7)).isoformat()
    elif by == "month":
        target = add_months(clock, 1)
    elif by == "year":
        target = add_months(clock, 12)
    else:
        raise ValueError("step by week, month or year")
    return min(target, today)


def _stops(trial: Any, who: str, day: str) -> list[dict[str, Any]]:
    broker = trial.broker(who)
    out = []
    for h in check_stops(broker.positions(), lambda s: trial.prices.history(s, "1y")):
        try:
            o = broker.submit_order(h["symbol"], "sell", qty=h["qty"])
            out.append({"date": day, "who": who, "symbol": h["symbol"], "qty": o["qty"],
                        "price": o["filled_avg_price"], "stop": h["stop"]})
        except Exception as e:  # noqa: BLE001 - a stop that can't fill is logged, not fatal
            out.append({"date": day, "who": who, "symbol": h["symbol"], "error": str(e)})
    return out


def _dividends(trial: Any, after: str, day: str) -> list[dict[str, Any]]:
    out = []
    for who in ("you", "agent", "nifty"):
        broker = trial.broker(who)
        for p in broker.positions():
            for d in trial.prices.dividends(p.symbol):
                if after < d["date"] <= day:
                    amount = round(d["amount"] * p.qty, 2)
                    broker.credit(amount, f"dividend {p.symbol}", day)
                    out.append({"date": day, "who": who, "symbol": p.symbol, "amount": amount})
    return out


def step(trial: Any, until: str, *, today: str | None = None,
         progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    if trial.data["ended"]:
        raise ValueError("this replay has ended; it is read-only")
    today = today or date.today().isoformat()
    start = trial.clock.today
    until = min(until, today)
    if until <= start:
        raise ValueError(f"the replay is already at {start}")
    days = trial.prices.calendar(CALENDAR, start, until)
    report: dict[str, Any] = {"from": start, "to": until, "days": len(days), "rebalances": [],
                              "stops": [], "dividends": []}
    with trial.transaction():
        prev = start
        for i, day in enumerate(days):
            trial.clock.advance_to(day)
            if day[:7] != trial.data["last_rebalance_month"]:
                if progress:
                    progress(f"rebalancing the agent on {day}")
                trial.rebalance_agent()
                report["rebalances"].append(day)
            stops = _stops(trial, "agent", day)
            if trial.data["auto_stop"]:
                stops += _stops(trial, "you", day)
            trial.data["stops"] = (trial.data["stops"] + stops)[-500:]
            report["stops"] += stops
            if trial.data["dividends"] == "cash":
                report["dividends"] += _dividends(trial, prev, day)
            trial.data["equity"].append(trial.point())
            prev = day
            if progress and i % 20 == 0:
                progress(f"{day}: {i + 1} of {len(days)} trading days")
        trial.clock.advance_to(until)
        trial.data["clock"] = until
        trial.refresh_picks()
        trial.save()
    return report
```

- [ ] **Step 4: Run them to see them pass**

Run: `.venv/Scripts/python.exe -m pytest -q tests/test_replay_engine.py`
Expected: 9 passed. If `test_one_year_equals_twelve_months` fails on float noise, compare with `pytest.approx` per point rather than loosening the engine.

- [ ] **Step 5: Commit**

```bash
git add trading_agent/replay/engine.py tests/test_replay_engine.py
git commit -m "Replay: step a week, month or year, day by day, all or nothing

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: End trial, scorecard and "What happened next"

**Files:**
- Create: `trading_agent/replay/scorecard.py`, `tests/test_replay_scorecard.py`

**Interfaces:**
- Consumes: `Trial` (Task 4), `engine.step` (Task 5).
- Produces:
  - `end_trial(trial) -> dict` (sets `data["ended"]`, stores and returns the scorecard)
  - `scorecard(trial) -> dict`: `{who: {"final","return","cagr","max_drawdown","charges","trades","dividends","best","worst","hit_rate"}, "claude": [...]}`
  - `what_happened_next(trial, source, today) -> {"dates": [...], "you": [...], "agent": [...], "nifty": [...]}`

- [ ] **Step 1: Write the failing tests**

`tests/test_replay_scorecard.py`:

```python
import pytest

from trading_agent.replay.engine import step
from trading_agent.replay.scorecard import end_trial, what_happened_next
from trading_agent.replay.trial import Trial
from .replay_fakes import FakeUniverse, market, top_by_6m


def make(tmp_path):
    return Trial.create(tmp_path / "replay", name="t", start="2021-03-15", cash=100_000,
                        universe="NIFTYMIDCAP150", top=3, dividends="reinvest", source=market(),
                        universe_obj=FakeUniverse(), screen_fn=top_by_6m, today="2026-10-09")


def test_scorecard_per_portfolio(tmp_path):
    t = make(tmp_path)
    t.order("A", "buy", qty=50)
    t.order("E", "buy", qty=20)
    step(t, "2022-03-15", today="2026-10-09")
    s = end_trial(t)
    assert t.data["ended"] == "2022-03-15" and t.data["scorecard"] == s
    you = s["you"]
    assert you["final"] == t.data["equity"][-1]["you"]
    assert you["return"] == pytest.approx(you["final"] / 100_000 - 1)
    assert you["best"]["symbol"] == "A" and you["worst"]["symbol"] == "E"
    assert you["hit_rate"] == 0.5 and you["trades"] == 2 and you["charges"] > 0
    assert s["agent"]["trades"] >= 3 and s["nifty"]["trades"] == 1
    assert s["agent"]["max_drawdown"] <= 0


def test_ended_trial_is_read_only(tmp_path):
    t = make(tmp_path)
    end_trial(t)
    with pytest.raises(ValueError, match="ended"):
        t.order("A", "buy", qty=1)


def test_what_happened_next_only_after_the_end(tmp_path):
    t = make(tmp_path)
    src = market()
    with pytest.raises(ValueError, match="End the replay"):
        what_happened_next(t, src, "2026-10-09")
    t.order("A", "buy", qty=10)
    end_trial(t)
    n = what_happened_next(t, src, "2021-04-15")
    assert n["dates"][0] == "2021-03-15" and n["dates"][-1] == "2021-04-15"
    a0 = [b for b in src.bars["A"] if b["date"] == "2021-04-15"][0]["adj_close"]
    assert n["you"][-1] == pytest.approx(t.you.account().cash + 10 * a0, abs=0.01)
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/Scripts/python.exe -m pytest -q tests/test_replay_scorecard.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'trading_agent.replay.scorecard'`

- [ ] **Step 3: Implement `trading_agent/replay/scorecard.py`**

```python
"""End of a replay: the scorecard, and what each portfolio would have done since."""

from __future__ import annotations

import bisect
from datetime import date
from typing import Any

from .trial import PORTFOLIOS


def _max_drawdown(values: list[float]) -> float:
    peak, worst = values[0] if values else 0.0, 0.0
    for v in values:
        peak = max(peak, v)
        worst = min(worst, v / peak - 1 if peak else 0.0)
    return worst


def _per_symbol(broker: Any) -> dict[str, float]:
    """Profit or loss per stock: sales and current value minus purchases, charges included."""
    pnl: dict[str, float] = {}
    for o in broker.orders():
        flow = o["notional"] - o["fees"] if o["side"] == "sell" else -(o["notional"] + o["fees"])
        pnl[o["symbol"]] = pnl.get(o["symbol"], 0.0) + flow
    for p in broker.positions():
        pnl[p.symbol] = pnl.get(p.symbol, 0.0) + (p.market_value or 0.0)
    return pnl


def _card(trial: Any, who: str) -> dict[str, Any]:
    b, cash0 = trial.broker(who), trial.data["cash"]
    values = [p[who] for p in trial.data["equity"]]
    final = values[-1]
    years = max((date.fromisoformat(trial.clock.today) - date.fromisoformat(trial.data["start"])).days / 365.25, 1 / 365.25)
    pnl = _per_symbol(b)
    ranked = sorted(pnl.items(), key=lambda kv: kv[1])
    return {"final": final, "return": final / cash0 - 1, "cagr": (final / cash0) ** (1 / years) - 1,
            "max_drawdown": _max_drawdown(values), "charges": b.performance()["fees_paid"],
            "trades": len(b.orders()), "dividends": round(sum(c["amount"] for c in b.credits()), 2),
            "best": {"symbol": ranked[-1][0], "pnl": round(ranked[-1][1], 2)} if ranked else None,
            "worst": {"symbol": ranked[0][0], "pnl": round(ranked[0][1], 2)} if ranked else None,
            "hit_rate": sum(1 for v in pnl.values() if v > 0) / len(pnl) if pnl else None}


def _grade_claude(trial: Any) -> list[dict[str, Any]]:
    """Each Claude call against what the stock did by the end date (hindsight possible)."""
    out = []
    for entry in trial.data["claude"]:
        for r in entry.get("recommendations", []):
            g = {"date": entry["date"], "action": r["action"], "ticker": r["ticker"], "return": None}
            try:
                start = trial.prices.price_on(r["ticker"], entry["date"])
                g["return"] = trial.prices.latest_price(r["ticker"]) / start - 1
            except LookupError:
                pass
            out.append(g)
    return out


def scorecard(trial: Any) -> dict[str, Any]:
    return {**{w: _card(trial, w) for w in PORTFOLIOS}, "claude": _grade_claude(trial)}


def end_trial(trial: Any) -> dict[str, Any]:
    if trial.data["ended"]:
        return trial.data["scorecard"]
    s = scorecard(trial)
    trial.data["ended"], trial.data["scorecard"] = trial.clock.today, s
    trial.save()
    return s


def what_happened_next(trial: Any, source: Any, today: str) -> dict[str, list[Any]]:
    """Hold every portfolio unchanged from the end date to today. This is the only place
    Replay reads prices after its clock, and only once the replay has ended."""
    if not trial.data["ended"]:
        raise ValueError("End the replay first: this shows prices after the replay date")
    end, fld = trial.data["ended"], trial.prices.field
    cal = [b["date"] for b in source.history("^NSEI", "10y") if end <= b["date"] <= today]
    series: dict[str, Any] = {}

    def closes(sym: str) -> tuple[list[str], list[float]]:
        if sym not in series:
            bars = source.history(sym, "10y")
            series[sym] = ([b["date"] for b in bars], [b[fld] for b in bars])
        return series[sym]

    out: dict[str, list[Any]] = {"dates": cal}
    for who in PORTFOLIOS:
        b = trial.broker(who)
        cash, held = b.account().cash, [(p.symbol, p.qty, p.current_price) for p in b.positions()]
        vals = []
        for d in cal:
            v = cash
            for sym, qty, last in held:
                try:
                    ds, cs = closes(sym)
                    i = bisect.bisect_right(ds, d)
                    v += qty * (cs[i - 1] if i else (last or 0.0))
                except LookupError:
                    v += qty * (last or 0.0)
            vals.append(round(v, 2))
        out[who] = vals
    return out
```

- [ ] **Step 4: Run them to see them pass**

Run: `.venv/Scripts/python.exe -m pytest -q tests/test_replay_scorecard.py`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add trading_agent/replay/scorecard.py tests/test_replay_scorecard.py
git commit -m "Replay: End trial scorecard and what each portfolio did afterwards

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Ask Claude about this day

**Files:**
- Create: `trading_agent/replay/claude.py`, `tests/test_replay_claude.py`

**Interfaces:**
- Consumes: `Trial` (Task 4), `ClockedNews` (Task 2), `momentum.momentum_stats`, `risk.atr`, `risk.trailing_stop`, `agent.make_client`.
- Produces:
  - `build_context(trial, news, lookup: str | None = None) -> dict` (dates all ≤ clock)
  - `ask(trial, client, model, news, lookup=None) -> dict`: the saved entry `{"date","summary","recommendations","model","hindsight":True}`
  - `TOOL` (the one tool; no order tool)

- [ ] **Step 1: Write the failing tests**

`tests/test_replay_claude.py`:

```python
import json
import re
from types import SimpleNamespace

from trading_agent.replay.claude import TOOL, ask, build_context
from trading_agent.replay.trial import Trial
from .replay_fakes import FakeUniverse, market, top_by_6m


class FakeNews:
    def for_symbol(self, symbol, days=60, until=None):
        return {"items": [{"at": "2021-03-10 10:00:00", "category": "Updates", "text": f"{symbol} news"}], "error": None}


class FakeClient:
    def __init__(self):
        self.calls = []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(model="claude-test", content=[SimpleNamespace(type="tool_use", input={
            "summary": "Market is calm.",
            "recommendations": [{"action": "buy", "ticker": "D", "headline": "h", "rationale": "r", "confidence": "low"}]})])


def make(tmp_path):
    return Trial.create(tmp_path / "replay", name="t", start="2021-03-15", cash=100_000,
                        universe="NIFTYMIDCAP150", top=3, dividends="reinvest", source=market(),
                        universe_obj=FakeUniverse(), screen_fn=top_by_6m, today="2026-10-09")


def test_context_holds_nothing_after_the_clock(tmp_path):
    t = make(tmp_path)
    t.order("D", "buy", qty=10)
    ctx = build_context(t, FakeNews(), lookup="E")
    text = json.dumps(ctx)
    assert all(d <= "2021-03-15" for d in re.findall(r"\d{4}-\d{2}-\d{2}", text))
    assert ctx["date"] == "2021-03-15" and {h["symbol"] for h in ctx["you"]["positions"]} == {"D"}
    assert ctx["lookup"]["symbol"] == "E" and ctx["market"]["nifty_above_200dma"] in (True, False)


def test_ask_uses_one_forced_tool_and_saves_the_answer(tmp_path):
    t = make(tmp_path)
    client = FakeClient()
    entry = ask(t, client, "claude-x", FakeNews())
    kw = client.calls[0]
    assert [x["name"] for x in kw["tools"]] == [TOOL["name"]] and kw["tool_choice"] == {"type": "tool", "name": TOOL["name"]}
    assert "2021-03-15" in kw["system"] and "only" in kw["system"].lower()
    assert entry["date"] == "2021-03-15" and entry["hindsight"] is True and entry["recommendations"][0]["ticker"] == "D"
    assert t.data["claude"][-1] == entry and t.data["claude_presses"] == 1
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/Scripts/python.exe -m pytest -q tests/test_replay_claude.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'trading_agent.replay.claude'`

- [ ] **Step 3: Implement `trading_agent/replay/claude.py`**

```python
"""Claude's view of one replay day, from clocked data only. One forced tool, no orders."""

from __future__ import annotations

import json
from typing import Any

from ..momentum import momentum_stats
from ..risk import atr, trailing_stop

TOOL = {
    "name": "record_view",
    "description": "Record your reading of the market and portfolio on the replay date, with recommendations.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "2-4 sentences on the market and the portfolio that day."},
            "recommendations": {"type": "array", "items": {"type": "object", "properties": {
                "action": {"type": "string", "enum": ["buy", "sell", "hold", "watch"]},
                "ticker": {"type": "string"},
                "headline": {"type": "string"},
                "rationale": {"type": "string", "description": "2-5 sentences citing the data provided."},
                "confidence": {"type": "string", "enum": ["low", "medium", "high"]}},
                "required": ["action", "ticker", "headline", "rationale", "confidence"]}}},
        "required": ["summary", "recommendations"]},
}

SYSTEM = """You are reviewing a practice portfolio in a replay of the Indian stock market. Today is {date}.
Use ONLY the data in the user's message. Do not use anything you know about events, prices or
results after {date}: the point of the replay is to decide as one could have on that day. If the
data is not enough to judge a stock, say so and choose "watch". Recommendations are suggestions;
the person decides. Indian delivery charges are about 0.25% per round trip plus ₹20 per sale."""


def _stock(trial: Any, news: Any, sym: str) -> dict[str, Any]:
    out: dict[str, Any] = {"symbol": sym}
    try:
        bars = trial.prices.history(sym, "2y")
        s = momentum_stats(bars)
        out.update({k: s.get(k) for k in ("last_close", "ret_1m", "ret_6m", "ret_12_1", "above_200dma",
                                         "pct_from_52w_high", "verdict")})
        out["atr_stop"] = round(trailing_stop(max(b["close"] for b in bars[-60:]), atr(bars[-252:])), 2)
    except Exception as e:  # noqa: BLE001
        out["error"] = str(e)
    n = news.for_symbol(sym, days=60)
    out["announcements"] = [{"date": a["at"][:10], "category": a.get("category", ""),
                             "text": (a.get("text") or "")[:300]} for a in n["items"][:5]]
    if n["error"]:
        out["announcements_error"] = n["error"]
    return out


def build_context(trial: Any, news: Any, lookup: str | None = None) -> dict[str, Any]:
    nifty = momentum_stats(trial.prices.history("^NSEI", "2y"))
    ctx: dict[str, Any] = {
        "date": trial.clock.today,
        "market": {"nifty_close": nifty.get("last_close"), "nifty_ret_1m": nifty.get("ret_1m"),
                   "nifty_ret_6m": nifty.get("ret_6m"), "nifty_above_200dma": nifty.get("above_200dma")},
        "you": {"cash": trial.you.account().cash,
                "positions": [{**_stock(trial, news, p.symbol), "qty": p.qty, "avg_cost": p.avg_entry_price}
                              for p in trial.you.positions()]},
        "agent": {"holdings": [p.symbol for p in trial.agent.positions()],
                  "latest_picks": [_stock(trial, news, r["symbol"])
                                   for r in ((trial.data.get("picks") or {}).get("rows") or [])[:10]]},
    }
    if lookup:
        ctx["lookup"] = _stock(trial, news, lookup.upper())
    return ctx


def ask(trial: Any, client: Any, model: str, news: Any, lookup: str | None = None) -> dict[str, Any]:
    ctx = build_context(trial, news, lookup)
    resp = client.messages.create(model=model, max_tokens=4000, system=SYSTEM.format(date=trial.clock.today),
                                  tools=[TOOL], tool_choice={"type": "tool", "name": TOOL["name"]},
                                  messages=[{"role": "user", "content": json.dumps(ctx, default=str)}])
    block = next(b for b in resp.content if getattr(b, "type", None) == "tool_use")
    entry = {"date": trial.clock.today, "summary": block.input.get("summary", ""),
             "recommendations": block.input.get("recommendations", []),
             "model": getattr(resp, "model", model), "hindsight": True}
    trial.data["claude"].append(entry)
    trial.data["claude_presses"] = trial.data.get("claude_presses", 0) + 1
    trial.save()
    return entry
```

- [ ] **Step 4: Run them to see them pass**

Run: `.venv/Scripts/python.exe -m pytest -q tests/test_replay_claude.py`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add trading_agent/replay/claude.py tests/test_replay_claude.py
git commit -m "Replay: ask Claude about a replay day, from clocked data only, no order tool

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Replay server API and the shared job slot

**Files:**
- Create: `trading_agent/replay/web.py`, `tests/test_replay_web.py`
- Modify: `trading_agent/ui.py` (add `App.run_background`, `App.replay`, routes for `/replay`, `/replay/api/*`, `/static/*`)

**Interfaces:**
- Consumes: Tasks 1–7; `App._refused`, `Job`, `App.jobs`, `App.busy`, `App.running`, `App.settings`.
- Produces:
  - `App.run_background(kind: str, fn: Callable[[Job], str]) -> Job`: `fn` returns the success message and may update `job.message` as progress.
  - `ReplayApp(app, *, source=None, universe_factory=None, news_client=None, client_factory=None, today_fn=None)` with:
    - `.list()`, `.create(body) -> Job`, `.snapshot(slug) -> dict`, `.order(slug, body) -> dict`
    - `.step(slug, body) -> Job`, `.set_auto_stop(slug, on) -> dict`, `.end(slug) -> dict`
    - `.lookup(slug, ticker) -> dict`, `.ask(slug, body) -> dict`, `.tool(slug, body) -> Job`, `.tools(slug) -> dict`
    - `.route(method, path, query, body) -> tuple[int, Any]`
  - Snapshot shape, consumed by Task 11's JS:

```text
{"trial": {name, slug, start, clock, universe, benchmark, top, dividends, auto_stop, ended, cash},
 "race": {"dates": [...], "you": [...], "agent": [...], "nifty": [...]},
 "tiles": {"you"|"agent"|"nifty": {"value", "return", "worst_fall"}},
 "you": {"cash", "equity", "positions": [{symbol, qty, avg_entry_price, current_price, market_value,
          unrealized_pl, stop, suspended, last_trade}]},
 "agent": {"holdings": [{symbol, qty, value, weight}], "last_rebalance": {...} | None,
           "next_rebalance": "YYYY-MM"},
 "picks": {"date", "rows": [...], "will_buy": [...], "will_sell": [...]} | None,
 "stops": [last 20], "claude": [...], "claude_presses": int, "claude_ready": bool,
 "scorecard": {...} | None, "next": {...} | None, "universes": [...]}
```

- [ ] **Step 1: Write the failing tests**

`tests/test_replay_web.py`:

```python
import re
import threading
import time

import pytest

from trading_agent.ui import App
from trading_agent.replay.web import ReplayApp
from .replay_fakes import FakeUniverse, market, top_by_6m


class FakeNews:
    def announcement_history(self, symbol):
        return [{"at": "2021-03-10 10:00:00", "category": "Updates", "text": f"{symbol} update", "file": ""}]


def wait(app, job):
    for _ in range(200):
        if job.finished_at:
            return job
        time.sleep(0.02)
    raise AssertionError("job did not finish")


@pytest.fixture
def rapp(settings, tmp_path):
    settings.market = "in"
    app = App(settings, dotenv=None)
    r = ReplayApp(app, source=market(), universe_factory=lambda name: FakeUniverse(), news_client=FakeNews(),
                  client_factory=None, today_fn=lambda: "2026-10-09", screen_fn=top_by_6m)
    app._replay = r
    return app, r


def create(app, r, **kw):
    body = {"name": "Test run", "start": "2021-03-15", "cash": 100000, "universe": "NIFTYMIDCAP150",
            "top": 3, "dividends": "reinvest", **kw}
    job = wait(app, r.create(body))
    assert job.ok, job.message
    return job.result["slug"]


def test_create_list_and_snapshot_has_no_future_dates(rapp):
    app, r = rapp
    slug = create(app, r)
    assert r.list()[0]["slug"] == slug
    snap = r.snapshot(slug)
    assert snap["trial"]["clock"] == "2021-03-15" and snap["race"]["dates"] == ["2021-03-15"]
    assert snap["picks"]["will_buy"] == [] and set(snap["agent"]["next_rebalance"]) <= set("0123456789-")
    import json
    dates = re.findall(r"\d{4}-\d{2}-\d{2}", json.dumps(snap, default=str))
    assert dates and all(d <= "2021-03-15" for d in dates)


def test_order_step_end_via_route(rapp):
    app, r = rapp
    slug = create(app, r)
    st, o = r.route("POST", f"/replay/api/trial/{slug}/order", {}, {"symbol": "D", "side": "buy", "qty": 5})
    assert st == 200 and o["order"]["qty"] == 5
    st, j = r.route("POST", f"/replay/api/trial/{slug}/step", {}, {"by": "month"})
    assert st == 202
    wait(app, app.jobs[-1])
    assert app.jobs[-1].ok, app.jobs[-1].message
    snap = r.snapshot(slug)
    assert snap["trial"]["clock"] == "2021-04-15" and len(snap["race"]["dates"]) > 20
    st, s = r.route("POST", f"/replay/api/trial/{slug}/end", {}, {})
    assert st == 200 and s["scorecard"]["you"]["trades"] == 1 and s["next"]["dates"][0] == "2021-04-15"


def test_lookup_is_as_of_the_clock(rapp):
    app, r = rapp
    slug = create(app, r)
    st, lk = r.route("GET", f"/replay/api/trial/{slug}/lookup", {"ticker": "A"}, None)
    assert st == 200 and lk["ticker"] == "A" and lk["history"][-1]["d"] == "2021-03-15"
    assert lk["announcements"][0]["text"] == "A update" and lk["today"] == "2021-03-15"


def test_second_step_while_busy_is_refused(rapp):
    app, r = rapp
    slug = create(app, r)
    gate = threading.Event()
    blocker = app.run_background("check", lambda job: gate.wait(5) and "done")
    st, j = r.route("POST", f"/replay/api/trial/{slug}/step", {}, {"by": "week"})
    assert st == 202 and j["ok"] is False and "still running" in j["message"]
    gate.set()
    wait(app, blocker)
    assert r.snapshot(slug)["trial"]["clock"] == "2021-03-15"


def test_snapshot_after_restart_needs_no_network(rapp, settings):
    app, r = rapp
    slug = create(app, r)

    class NoNetwork:
        def history(self, *a, **k):
            raise AssertionError("network used")
        dividends = history
    fresh = ReplayApp(App(settings, dotenv=None), source=NoNetwork(), universe_factory=lambda n: FakeUniverse(),
                      news_client=FakeNews(), today_fn=lambda: "2026-10-09", screen_fn=top_by_6m)
    snap = fresh.snapshot(slug)
    assert snap["trial"]["slug"] == slug and snap["you"]["cash"] == 100000


def test_ask_needs_a_key(rapp):
    app, r = rapp
    slug = create(app, r)
    app.settings.anthropic_api_key = None
    st, body = r.route("POST", f"/replay/api/trial/{slug}/ask", {}, {})
    assert st == 403 and "ANTHROPIC_API_KEY" in body["error"]


def test_replay_never_builds_a_groww_broker(rapp, monkeypatch):
    import trading_agent.groww as g
    monkeypatch.setattr(g.GrowwBroker, "__init__", lambda *a, **k: (_ for _ in ()).throw(AssertionError("groww")))
    app, r = rapp
    slug = create(app, r)
    r.route("POST", f"/replay/api/trial/{slug}/order", {}, {"symbol": "D", "side": "buy", "qty": 1})
    r.snapshot(slug)
```

`test_snapshot_after_restart_needs_no_network` relies on the snapshot using stored broker prices (`LocalPaperBroker` keeps the last price it saw), plus `trial.json`. `ClockedPrices` must therefore not be called by `snapshot()` except through `positions()`, whose `latest_price` falls back to the stored price when the source raises. The `stop` column needs bars, so it uses `None` when history is unavailable.

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/Scripts/python.exe -m pytest -q tests/test_replay_web.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'trading_agent.replay.web'`

- [ ] **Step 3: Add `run_background` and the `replay` property to `App`**

In `trading_agent/ui.py`, extend `JOB_LABELS`:

```python
    JOB_LABELS = {"check": "A check", "dry_run": "A dry run", "backtest": "The deal backtest",
                  "screen": "The factor screen", "factor_backtest": "The portfolio backtest",
                  "signal_lab": "The signal lab", "replay_create": "Starting a replay",
                  "replay_step": "A replay step", "replay_tool": "A replay tool"}
```

Add to `App.__init__` (after `self.running = None`):

```python
        self._replay: Any | None = None  # ReplayApp, built on first use
```

Add these methods to `App` (after `_refused`):

```python
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
        if self._replay is None:
            from .replay.web import ReplayApp
            self._replay = ReplayApp(self)
        return self._replay
```

Add `Callable` to the `typing` import in `ui.py` if it is not there.

- [ ] **Step 4: Implement `trading_agent/replay/web.py`**

```python
"""HTTP-facing Replay: list, create, step, order, end, look up, ask Claude, tools."""

from __future__ import annotations

import re
import threading
from datetime import date
from typing import Any, Callable

from ..momentum import momentum_summary, momentum_stats
from ..risk import atr, trailing_stop
from .clock import EARLIEST_START
from .engine import step, step_target
from .news import ClockedNews
from .scorecard import end_trial, what_happened_next
from .trial import BENCHMARKS, PORTFOLIOS, ReplayUniverse, Trial, list_trials

_SLUG = re.compile(r"^[a-z0-9-]{1,40}$")


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
        self._tools: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    # -- helpers -------------------------------------------------------------------
    def _universe(self, name: str) -> Any:
        if name not in self._universes:
            self._universes[name] = self.universe_factory(name)
        return self._universes[name]

    def trial(self, slug: str) -> Trial:
        if not _SLUG.match(slug or ""):
            raise LookupError("no such replay")
        with self._lock:
            t = self._trials.get(slug)
            if t is None:
                root = self.dir / slug
                if not (root / "trial.json").exists():
                    raise LookupError("no such replay")
                t = Trial.load(root, self.source, _LazyUniverse(self, root), screen_fn=self.screen_fn)
                self._trials[slug] = t
            return t

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
        target = step_target(t.clock.today, str(body.get("by") or "month"), self.today_fn())

        def run(job: Any) -> str:
            def progress(msg: str) -> None:
                job.message = msg
            r = step(t, target, today=self.today_fn(), progress=progress)
            job.result = r
            capped = " (stopped at today's date)" if r["to"] < step_target(r["from"], str(body.get("by") or "month"), "9999-12-31") else ""
            return f"Moved to {r['to']}: {r['days']} trading days, {len(r['rebalances'])} rebalance(s), {len(r['stops'])} stop(s){capped}"
        return self.app.run_background("replay_step", run)

    def order(self, slug: str, body: dict[str, Any]) -> dict[str, Any]:
        t = self.trial(slug)
        o = t.order(str(body.get("symbol") or ""), str(body.get("side") or "buy"),
                    notional=float(body["notional"]) if body.get("notional") else None,
                    qty=float(body["qty"]) if body.get("qty") else None)
        if t.data["equity"] and t.data["equity"][-1]["date"] == t.clock.today:
            t.data["equity"][-1] = t.point()  # today's value now includes the trade's charges
        t.save()
        return {"ok": True, "order": o}

    def set_auto_stop(self, slug: str, on: bool) -> dict[str, Any]:
        t = self.trial(slug)
        t.data["auto_stop"] = bool(on)
        t.save()
        return {"ok": True, "auto_stop": t.data["auto_stop"]}

    def end(self, slug: str) -> dict[str, Any]:
        t = self.trial(slug)
        end_trial(t)
        return self.snapshot(slug)

    def lookup(self, slug: str, ticker: str) -> dict[str, Any]:
        t = self.trial(slug)
        sym = ticker.strip().upper()
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
        if not self.settings.anthropic_api_key:
            raise PermissionError("Ask Claude needs ANTHROPIC_API_KEY in .env")
        from .claude import ask
        t = self.trial(slug)
        if t.data["ended"]:
            raise ValueError("this replay has ended; it is read-only")
        if self.client_factory:
            client = self.client_factory()
        else:
            from ..agent import make_client
            client = make_client(self.settings)
        return ask(t, client, self.settings.claude_model, self._news(t), lookup=(body.get("ticker") or None))

    def tool(self, slug: str, body: dict[str, Any]) -> Any:
        t = self.trial(slug)
        kind, years = str(body.get("kind") or ""), int(body.get("years") or 3)
        if kind not in ("signal_lab", "factor_backtest"):
            raise ValueError("kind must be signal_lab or factor_backtest")

        def run(job: Any) -> str:
            from ..costs import cost_model_for
            members, membership = t.universe.current, t.universe.membership
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
            self._tools.setdefault(slug, {})[kind] = {"date": t.clock.today, "years": years, "text": text}
            return f"{kind.replace('_', ' ')} as of {t.clock.today} finished"
        return self.app.run_background("replay_tool", run)

    def tools(self, slug: str) -> dict[str, Any]:
        t = self.trial(slug)
        return {k: v for k, v in self._tools.get(slug, {}).items() if v["date"] == t.clock.today}

    # -- the page's data ------------------------------------------------------------
    def snapshot(self, slug: str) -> dict[str, Any]:
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
            "next": what_happened_next(t, self.source, self.today_fn()) if ended else None,
            "universes": list(BENCHMARKS),
        }

    # -- routing -------------------------------------------------------------------
    def route(self, method: str, path: str, query: dict[str, str], body: dict[str, Any] | None) -> tuple[int, Any]:
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
        except PermissionError as e:
            return 403, {"error": str(e)}
        except (ValueError, LookupError, KeyError) as e:
            return 400, {"error": str(e)}


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
```

- [ ] **Step 5: Route `/replay` and static files in `trading_agent/ui.py`**

Near the top of `make_handler`, load the Replay page next to the index page:

```python
    replay_html = (resources.files("trading_agent") / "ui" / "replay.html").read_text(encoding="utf-8")
```

Add near `FONT_FILES`:

```python
STATIC_FILES = {"/static/nocturne.css": ("nocturne.css", "text/css; charset=utf-8"),
                "/static/common.js": ("common.js", "text/javascript; charset=utf-8"),
                "/static/replay.js": ("replay.js", "text/javascript; charset=utf-8")}
```

Add a helper method on `Handler`:

```python
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
```

At the very start of `do_GET`, after `path = urlparse(self.path).path`:

```python
            if path in STATIC_FILES:
                name, ctype = STATIC_FILES[path]
                self._bytes((resources.files("trading_agent") / "ui" / name).read_bytes(), ctype)
                return
            if self._replay("GET"):
                return
```

At the start of `do_POST`, after `path = ...`:

```python
            if path.startswith("/replay/api/"):
                try:
                    self._replay("POST")
                except json.JSONDecodeError as e:
                    self._json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
                return
```

Create placeholder files so the server starts before Tasks 9–11: an empty `trading_agent/ui/replay.js`, and `trading_agent/ui/replay.html` containing `<!doctype html><title>Replay</title><p>Replay page coming.</p>`. Task 11 replaces them.

- [ ] **Step 6: Run the new tests and the whole suite**

Run: `.venv/Scripts/python.exe -m pytest -q tests/test_replay_web.py` → expected 7 passed.
Then: `.venv/Scripts/python.exe -m pytest -q tests` → all pass.

- [ ] **Step 7: Commit**

```bash
git add trading_agent/replay/web.py trading_agent/ui.py trading_agent/ui/replay.html trading_agent/ui/replay.js tests/test_replay_web.py
git commit -m "Replay: server API sharing the dashboard's single job slot

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: Shared CSS and JS, page tabs

**Files:**
- Create: `trading_agent/ui/nocturne.css`, `trading_agent/ui/common.js`
- Modify: `trading_agent/ui/index.html`
- Test: `tests/test_ui.py` (static files served; index references them)

**Interfaces:**
- Produces `window.TA` with:
  - `$`, `esc`, `setCurrency(fn)`, `currency`, `sym`, `money`, `signed`, `pct`, `when`, `cap`, `toast`, `api`, `tile`
  - `C`, `NS`, `niceTicks`, `shortDate`, `lineChart`, `histogram`, `rupeesShort`, `inr`, `sinr`, `spct`
  - `daysAgo(at, now)`, `shortDay`, `clip`, `lookupTakeaway(r, now)`, `attachSuggest(ids, onPick)`
- `TA.api(path, body)` prefixes `document.body.dataset.api` to paths that start with `/api/`.
- CSS classes `.tabs`, `.mode-banner.replay`, `.mode-banner.demo`.

- [ ] **Step 1: Write the failing test** (append to `tests/test_ui.py`)

```python
def test_static_files_and_tabs_are_served(settings):
    import threading
    import urllib.request
    from trading_agent.ui import App, make_server

    srv = make_server(App(settings, dotenv=None), port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        css = urllib.request.urlopen(base + "/static/nocturne.css").read().decode()
        js = urllib.request.urlopen(base + "/static/common.js").read().decode()
        page = urllib.request.urlopen(base + "/").read().decode()
        assert ".tabs" in css and "window.TA" in js and "attachSuggest" in js
        assert '/static/nocturne.css' in page and '/static/common.js' in page and 'href="/replay"' in page
        assert "<style>" not in page  # the CSS lives in one shared file now
    finally:
        srv.shutdown()
```

- [ ] **Step 2: Run it to see it fail**

Run: `.venv/Scripts/python.exe -m pytest -q tests/test_ui.py::test_static_files_and_tabs_are_served`
Expected: FAIL (404 for `/static/nocturne.css` until the file exists, or an assertion on `.tabs`)

- [ ] **Step 3: Move the CSS and helpers out with a one-off script**

Save as `C:\Users\affaf\AppData\Local\Temp\claude\C--projects-Share-Trade\5344d1be-4a15-543c-9357-aef2bed4e1e0\scratchpad\extract_ui.py` (scratchpad, not committed) and run it with `.venv/Scripts/python.exe <path>` from the repo root:

```python
"""Move index.html's <style> into nocturne.css and its generic helpers into common.js."""
import pathlib
import re

ui = pathlib.Path("trading_agent/ui")
html = (ui / "index.html").read_text(encoding="utf-8")


def cut(text, start, end_before):
    """Remove text[start-marker .. end-marker) and return (removed, rest). Markers must be unique."""
    i, j = text.index(start), text.index(end_before)
    assert text.count(start) == 1 and text.count(end_before) == 1 and i < j, (start, end_before)
    return text[i:j], text[:i] + text[j:]


# 1. CSS
m = re.search(r"<style>\n(.*?)</style>\n", html, re.S)
css = m.group(1)
html = html[:m.start()] + '<link rel="stylesheet" href="/static/nocturne.css">\n' + html[m.end():]

# 2. helper blocks (removed from index.html; common.js below holds their adapted copies)
_, html = cut(html, "  const esc = (s) =>", "\n  function renderRegime(){")
_, html = cut(html, "  // ---- charts: plain SVG", "\n  // ---- My Groww portfolio")
_, html = cut(html, "  const inr = (v, d=0)", "  async function loadMyPortfolio(refresh){")
_, html = cut(html, "  // Company suggestions under the ticker boxes", "\n  // ---- \"What this means\"")
_, html = cut(html, "  const daysAgo = (at)", "  function sizeTakeaway(")

# 3. index.html now takes the helpers from TA
html = html.replace(
    "  const $ = (id) => document.getElementById(id);\n  let S = null, pollTimer = null;\n",
    "  const {$, esc, money, signed, pct, when, cap, toast, api, tile, C, NS, niceTicks, shortDate, lineChart,\n"
    "         histogram, rupeesShort, inr, sinr, spct, daysAgo, shortDay, clip, lookupTakeaway, attachSuggest,\n"
    "         currency, sym} = TA;\n"
    "  let S = null, pollTimer = null;\n"
    "  TA.setCurrency(() => (S && S.settings && S.settings.currency) || \"INR\");\n", 1)
html = html.replace(
    "\n  // ---- \"What this means\"",
    "\n  attachSuggest([\"lk-ticker\", \"sz-ticker\", \"tk-symbol\"], (input, s) => { if(input.id === \"lk-ticker\") lookup(s); else input.focus(); });\n\n  // ---- \"What this means\"", 1)
html = html.replace("<script>\n(function(){", '<script src="/static/common.js"></script>\n<script>\n(function(){', 1)
(ui / "index.html").write_text(html, encoding="utf-8")
(ui / "nocturne.css").write_text(css, encoding="utf-8")
print("ok", len(css), "bytes of CSS moved")
```

Then append to `trading_agent/ui/nocturne.css`:

```css
/* Page tabs (Live | Replay | Demo) and the mode banners */
.tabs { display: inline-flex; gap: 2px; padding: 3px; border: 1px solid var(--color-divider); border-radius: var(--radius-md); }
.tabs a { padding: 6px 12px; border-radius: calc(var(--radius-md) - 2px); color: var(--color-muted); text-decoration: none; font-size: 13px; font-weight: 500; }
.tabs a:hover { color: var(--color-text); }
.tabs a[aria-current="page"] { background: var(--color-neutral-900); color: var(--color-text); box-shadow: inset 0 0 0 1px var(--color-accent); }
:root { --replay: #e0b354; --demo: #56c2b0; }
.mode-banner { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; padding: 10px 16px; border-radius: var(--radius-md); font-weight: 600; }
.mode-banner.replay { color: var(--replay); background: color-mix(in srgb, var(--replay) 12%, transparent); box-shadow: inset 0 0 0 1px var(--replay); }
.mode-banner.demo { color: var(--demo); background: color-mix(in srgb, var(--demo) 12%, transparent); box-shadow: inset 0 0 0 1px var(--demo); }
.mode-banner .sub { font-weight: 400; }
```

If `--color-text` or `--color-muted` don't exist in the moved CSS, use the token names the CSS actually defines (check with `grep -o "\-\-color-[a-z0-9-]*" trading_agent/ui/nocturne.css | sort -u`).

- [ ] **Step 4: Write `trading_agent/ui/common.js`**

```js
// Shared helpers for the Live, Demo and Replay pages (Nocturne design).
window.TA = (function(){
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
  let currencyFn = () => "INR";  // pages set this once their state says otherwise
  const currency = () => currencyFn();
  const sym = () => currency() === "INR" ? "₹" : "$";
  const money = (v, d=0) => v == null ? "n/a" : sym() + Number(v).toLocaleString(currency() === "INR" ? "en-IN" : "en-US", {maximumFractionDigits:d, minimumFractionDigits:d});
  const signed = (v, d=0) => v == null ? "n/a" : (v >= 0 ? "+" : "−") + money(Math.abs(v), d);
  const pct = (v, d=1) => v == null ? "n/a" : (v >= 0 ? "+" : "") + (v*100).toFixed(d) + "%";
  const when = (iso) => { if(!iso) return ""; const dt = new Date(iso); return dt.toLocaleString(undefined,{day:"2-digit",month:"short",hour:"2-digit",minute:"2-digit"}); };
  const cap = (s) => s ? s[0].toUpperCase()+s.slice(1) : "";
  const toast = (m) => { const t=$("toast"); t.textContent=m; t.style.display="block"; clearTimeout(t._t); t._t=setTimeout(()=>t.style.display="none",4500); };
  // The Demo page sets <body data-api="/demo">, so the same page talks to its own sample-data app.
  const BASE = () => (document.body && document.body.dataset.api) || "";
  const api = async (path, body) => {
    const r = await fetch((path.startsWith("/api/") ? BASE() : "") + path, body ? {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)} : {});
    const j = await r.json().catch(()=>({}));
    if(!r.ok) throw new Error(j.error || r.statusText);
    return j;
  };
  const tile = (l, v, s) => `<div class="tile"><div class="label">${l}</div><div class="big">${v}</div><div class="sub">${s}</div></div>`;

  // ---- charts: plain SVG, one y-axis, hairline grid, crosshair tooltip ----
  const C = {s1:"#9184d9", s2:"#e9e9ed", s3:"#9397ab", ctx:"#75798c", grid:"rgba(233,233,237,.08)", base:"#595d6c", pos:"#9184d9", neg:"#75798c", ring:"#232532"};
  const NS = "http://www.w3.org/2000/svg";
  // >>> PASTE niceTicks, shortDate, lineChart, histogram and rupeesShort here, VERBATIM from the block the
  //     script removed (index.html lines 631-703 before the move; `git show HEAD:trading_agent/ui/index.html`).
  const inr = (v, d=0) => v == null ? "n/a" : "₹" + Number(v).toLocaleString("en-IN", {minimumFractionDigits:d, maximumFractionDigits:d});
  const sinr = (v, d=0) => v == null ? "n/a" : (v >= 0 ? "+" : "−") + inr(Math.abs(v), d);
  const spct = (v) => v == null ? "n/a" : (v >= 0 ? "+" : "−") + Math.abs(v * 100).toFixed(2) + "%";

  // ---- "What this means" for a looked-up stock; `now` is the replay date on the Replay page ----
  const daysAgo = (at, now) => { const d = new Date(String(at).replace(" ", "T")); return isNaN(d) ? null : Math.floor(((now ?? Date.now()) - d) / 864e5); };
  const shortDay = (at) => { const d = new Date(String(at).replace(" ", "T")); return isNaN(d) ? String(at).slice(0, 10) : d.toLocaleDateString("en-IN", {day: "numeric", month: "short"}); };
  const clip = (t, n) => t.length <= n ? t : t.slice(0, t.lastIndexOf(" ", n) > n * 0.6 ? t.lastIndexOf(" ", n) : n).replace(/[,;:.]$/, "") + "…";
  // >>> PASTE `function lookupTakeaway(r){ ... }` VERBATIM from the removed block, then make two edits:
  //     the signature becomes `function lookupTakeaway(r, now){`, and every `daysAgo(x)` call inside it
  //     becomes `daysAgo(x, now)` (four calls).

  // ---- company suggestions under ticker boxes: Up/Down move, Enter picks, Esc closes ----
  function attachSuggest(ids, onPick){
    // >>> PASTE the suggest block VERBATIM from the removed block (from `const sug = $("suggest")` to the
    //     `window.addEventListener("scroll", ...)` line), then make two edits:
    //     1. the list of ids `["lk-ticker", "sz-ticker", "tk-symbol"].forEach(` becomes `ids.forEach(`
    //     2. in pickSuggest, replace the two lines
    //          if(input.id === "lk-ticker") lookup(h.symbol);
    //          else input.focus();
    //        with
    //          onPick(input, h.symbol);
  }

  return {$, esc, setCurrency: (fn) => { currencyFn = fn; }, currency, sym, money, signed, pct, when, cap, toast, api, tile,
          C, NS, niceTicks, shortDate, lineChart, histogram, rupeesShort, inr, sinr, spct,
          daysAgo, shortDay, clip, lookupTakeaway, attachSuggest};
})();
```

The three `>>> PASTE` notes are mechanical moves of code already in the repository. Copy them from `git show HEAD:trading_agent/ui/index.html` (the pre-move file) and apply only the edits listed, then delete the `>>>` comment lines. When done, `grep -n ">>>" trading_agent/ui/common.js` must print nothing.

- [ ] **Step 5: Add the tabs and the demo banner to `index.html`**

In the header, right after the closing `</div>` of `<div class="brand">…</div>`, insert:

```html
    <nav class="tabs" aria-label="Pages"><a href="/" data-tab="live">Live</a><a href="/replay" data-tab="replay">Replay</a><a href="/demo" data-tab="demo">Demo</a></nav>
```

As the first child of `<main>`:

```html
  <div class="mode-banner demo" id="demo-banner" hidden>Demo · sample data <span class="sub">Bundled sample deals and prices, its own practice account. Never touches Groww.</span><button type="button" class="small" id="btn-demo-reset" style="margin-left:auto">Reset demo</button></div>
```

In the inline script, right after the `TA.setCurrency(...)` line:

```js
  const MODE = document.body.dataset.mode || "live";
  document.querySelectorAll(".tabs a").forEach(a => { if(a.dataset.tab === MODE) a.setAttribute("aria-current", "page"); });
  if(MODE === "demo"){
    $("demo-banner").hidden = false;
    $("btn-demo-reset").addEventListener("click", async () => {
      if(!confirm("Put the demo back to its starting sample data?")) return;
      try { await api("/api/reset", {}); toast("Demo reset"); await refresh(); } catch(e){ toast(e.message); }
    });
  }
```

`refresh` is a function declaration in the same scope, so it is hoisted; this is safe.

- [ ] **Step 6: Run the test, the whole suite, and a browser check**

Run: `.venv/Scripts/python.exe -m pytest -q tests` → expected all pass.

Then start the demo dashboard in the background:

```
.venv/Scripts/python.exe -m trading_agent ui --demo --no-open --port 8790
```

With the Playwright tools, open `http://127.0.0.1:8790/` and check:
- no console errors,
- the Live tab is marked current,
- the regime strip, tiles, a lookup (type `TATA`, pick from the list) with its "What this means" note, and the paper chart all render as before.

Stop the server afterwards.

- [ ] **Step 7: Commit**

```bash
git add trading_agent/ui/nocturne.css trading_agent/ui/common.js trading_agent/ui/index.html tests/test_ui.py
git commit -m "Dashboard: shared stylesheet and helpers, and Live | Replay | Demo tabs

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 10: The Demo page

**Files:**
- Modify: `trading_agent/ui.py` (`App.demo`, `/demo` routing, `serve(..., demo=True)` opens `/demo`)
- Test: `tests/test_ui.py`

**Interfaces:**
- Consumes: `cli._demo_inputs(settings)` (existing), Task 9's `data-api` / `data-mode` attributes.
- Produces: `App.demo -> App` (sample data, `state_dir/demo`, Groww credentials stripped, `dotenv=None`); routes `/demo`, `/demo/api/*`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_ui.py`)

```python
def test_demo_is_isolated_from_live_and_groww(settings, monkeypatch, tmp_path):
    import trading_agent.groww as g
    from trading_agent.ui import App
    monkeypatch.setattr(g.GrowwBroker, "__init__", lambda *a, **k: (_ for _ in ()).throw(AssertionError("groww")))
    settings.market, settings.broker, settings.groww_access_token = "in", "groww", "tok"
    settings.watch_investor = "Ashish Kacholia"
    live = App(settings, dotenv=None)
    demo = live.demo
    assert demo.settings.state_dir == settings.state_dir / "demo" and demo.dotenv is None
    assert demo.settings.groww_access_token is None and not demo.settings.use_groww
    snap = demo.snapshot()
    assert snap["settings"]["demo"] is True
    assert not (settings.state_dir / "paper_broker.json").exists()  # live paper account untouched


def test_demo_routes_share_the_page_with_a_prefix(settings):
    import threading
    import urllib.request
    from trading_agent.ui import App, make_server

    settings.market = "in"
    srv = make_server(App(settings, dotenv=None), port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        page = urllib.request.urlopen(base + "/demo").read().decode()
        assert 'data-api="/demo"' in page and 'data-mode="demo"' in page
        import json
        st = json.loads(urllib.request.urlopen(base + "/demo/api/state").read())
        assert st["settings"]["demo"] is True
    finally:
        srv.shutdown()
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/Scripts/python.exe -m pytest -q tests/test_ui.py -k demo`
Expected: FAIL with `AttributeError: 'App' object has no attribute 'demo'`

- [ ] **Step 3: Implement**

In `App.__init__` add `self._demo: "App | None" = None`. Add the property:

```python
    @property
    def demo(self) -> "App":
        """The Demo page's app: bundled sample deals and prices, its own state, no Groww, no .env writes."""
        if self.demo_trades is not None:
            return self  # started with --demo: this app is the demo
        if self._demo is None:
            import dataclasses
            from .cli import _demo_inputs
            s = dataclasses.replace(self.settings, state_dir=self.settings.state_dir / "demo", broker="local",
                                    groww_access_token=None, groww_api_key=None, groww_api_secret=None,
                                    groww_totp_secret=None, groww_live_orders=False)
            trades, broker = _demo_inputs(s)
            self._demo = App(s, broker=broker, demo_trades=trades, dotenv=None, context=self.context)
        return self._demo
```

(If `broker` is not a `Settings` field name, use the field that `use_groww` reads; check with `grep -n "def use_groww" -A4 trading_agent/config.py`.)

In `make_handler`, add a demo copy of the page:

```python
    demo_html = index_html.replace("<body>", '<body data-api="/demo" data-mode="demo">', 1)
```

At the start of `do_GET` (after the static-file and replay checks):

```python
            target = app
            if path == "/demo" or path.startswith("/demo/"):
                target, path = app.demo, (path[5:] or "/")
                if path in ("/", "/index.html"):
                    self._bytes(demo_html.encode(), "text/html; charset=utf-8")
                    return
```

Then rename the existing `do_GET` body (everything after this) into a method `def _get(self, app: App, path: str) -> None:` and call `self._get(target, path)`. Because the parameter is called `app`, the body needs no edits. Do the same for `do_POST`: compute `target, path` the same way, then move the existing `try: ... except ...` block into `def _post(self, app: App, path: str) -> None:` and call `self._post(target, path)`.

In `serve()`, open the demo tab when started with `--demo`:

```python
    url = f"http://{host}:{server.server_address[1]}/" + ("demo" if demo else "")
```

and keep `App(settings, **kwargs)` as it is (with `--demo` the app itself is the demo, so `/demo` and `/` both show sample data).

- [ ] **Step 4: Run the tests and the suite**

Run: `.venv/Scripts/python.exe -m pytest -q tests` → expected all pass.

- [ ] **Step 5: Commit**

```bash
git add trading_agent/ui.py tests/test_ui.py
git commit -m "Dashboard: a Demo tab with sample data and its own practice account

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 11: The Replay page

**Files:**
- Replace: `trading_agent/ui/replay.html`, `trading_agent/ui/replay.js`
- Test: `tests/test_replay_web.py` (page served), plus a browser check

**Interfaces:**
- Consumes: the Task 8 routes and snapshot shape; Task 9's `TA` helpers and CSS.

- [ ] **Step 1: Write the failing test** (append to `tests/test_replay_web.py`)

```python
def test_replay_page_is_served_with_the_shared_assets(settings):
    import threading
    import urllib.request
    from trading_agent.ui import App, make_server

    srv = make_server(App(settings, dotenv=None), port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        page = urllib.request.urlopen(base + "/replay").read().decode()
        assert "/static/common.js" in page and "/static/replay.js" in page and 'id="race-chart"' in page
        js = urllib.request.urlopen(base + "/static/replay.js").read().decode()
        assert "/replay/api/trials" in js
    finally:
        srv.shutdown()
```

- [ ] **Step 2: Run it to see it fail**

Run: `.venv/Scripts/python.exe -m pytest -q tests/test_replay_web.py::test_replay_page_is_served_with_the_shared_assets`
Expected: FAIL on `id="race-chart"` (placeholder page)

- [ ] **Step 3: Write `trading_agent/ui/replay.html`**

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Replay · Trading Agent</title>
<meta name="description" content="Start a practice portfolio on a past date, step forward in time and race the agent's rules and the Nifty.">
<link rel="preload" href="/fonts/inter-latin.woff2" as="font" type="font/woff2" crossorigin>
<link rel="stylesheet" href="/static/nocturne.css">
</head>
<body data-mode="replay">
<header>
  <div class="wrap row">
    <div class="brand">
      <div class="logo"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="3 17 9 11 13 15 21 7"></polyline><polyline points="15 7 21 7 21 13"></polyline></svg></div>
      <div><div class="brand-name">Trading Agent</div><div class="sub">Replay: practise on a past date</div></div>
    </div>
    <nav class="tabs" aria-label="Pages"><a href="/" data-tab="live">Live</a><a href="/replay" data-tab="replay" aria-current="page">Replay</a><a href="/demo" data-tab="demo">Demo</a></nav>
  </div>
</header>
<main>
  <div class="mode-banner replay" id="banner" hidden></div>

  <section id="home" style="display:flex;flex-direction:column;gap:32px">
    <div class="card">
      <div class="cardhead"><h2>Your replays</h2><span class="sub">practice money only; nothing here touches Groww</span></div>
      <div class="scroll"><table><thead><tr><th>Name</th><th>Started</th><th>Replay date</th><th class="num">You</th><th class="num">Agent</th><th class="num">Nifty</th><th><span class="sr-only">Open</span></th></tr></thead><tbody id="trials"></tbody></table></div>
    </div>
    <form class="card" id="new-form">
      <div class="cardhead"><h2>New replay</h2></div>
      <div class="cardbody">
        <div class="grid2">
          <div class="field"><label for="n-name">Name</label><input id="n-name" name="name" required maxlength="40" placeholder="e.g. March 2021 run"></div>
          <div class="field"><label for="n-start">Start date</label><input id="n-start" name="start" type="date" required></div>
          <div class="field"><label for="n-cash">Practice money (₹)</label><input id="n-cash" name="cash" type="number" min="10000" step="1000" value="100000"></div>
          <div class="field"><label for="n-universe">Agent picks from</label><select id="n-universe" name="universe"></select></div>
          <div class="field"><label for="n-top">Agent holds (stocks)</label><input id="n-top" name="top" type="number" min="1" max="30" value="10"></div>
          <div class="field"><label for="n-div">Dividends</label><select id="n-div" name="dividends"><option value="reinvest">Reinvest (prices include dividends)</option><option value="cash">Take as cash</option></select></div>
        </div>
        <div class="row"><button type="submit" class="primary" id="n-submit">Start replay</button><span class="sub" id="n-status"></span></div>
        <div class="sub">On the start date, all three get the same money: <b>you</b> pick stocks, <b>the agent</b> buys the momentum screen's top picks and rebalances monthly, and <b>the Nifty</b> line buys the index fund and holds. Nothing after the replay date is shown until you end the replay. Earliest start: <span id="n-earliest"></span>, where the record of index members begins.</div>
      </div>
    </form>
  </section>

  <section id="trial" hidden style="display:flex;flex-direction:column;gap:32px">
    <div class="card">
      <div class="cardbody row" style="gap:8px">
        <button type="button" class="small" id="b-home">← All replays</button>
        <button type="button" class="primary small" data-step="week">+1 week</button>
        <button type="button" class="primary small" data-step="month">+1 month</button>
        <button type="button" class="primary small" data-step="year">+1 year</button>
        <button type="button" class="small" id="b-end">End replay</button>
        <span class="sub" id="step-status" style="margin-left:auto"></span>
      </div>
    </div>
    <section class="tiles" id="race-tiles"></section>
    <div class="card">
      <div class="cardhead"><h2>The race</h2><span class="sub" id="race-sub"></span></div>
      <div class="cardbody"><div class="chart" id="race-chart"></div></div>
    </div>
    <div id="scorecard"></div>
    <section class="cols">
      <div class="main">
        <div class="card">
          <div class="cardhead"><h2>Your portfolio</h2><span class="sub" id="yp-sub"></span></div>
          <div class="cardbody" style="padding-top:0;border-top:none;border-bottom:1px solid var(--line)"><div class="stats" id="yp-stats"></div></div>
          <div class="scroll"><table><thead><tr><th>Stock</th><th class="num">Qty</th><th class="num">Buy price</th><th class="num">Price</th><th class="num">Invested</th><th class="num">Value</th><th class="num">Profit / loss</th><th class="num">P&amp;L %</th><th class="num">Trailing stop</th><th><span class="sr-only">Sell</span></th></tr></thead><tbody id="yp-rows"></tbody><tfoot id="yp-foot"></tfoot></table></div>
          <form class="cardbody" id="ro-form" style="border-top:1px solid var(--line)">
            <h3>Order on <span id="ro-date"></span></h3>
            <div class="row">
              <input id="ro-symbol" autocomplete="off" placeholder="Ticker or company name" style="flex:1 1 160px;text-transform:uppercase" required>
              <div class="seg" role="group" aria-label="Side"><button type="button" class="on" data-side="buy">Buy</button><button type="button" data-side="sell">Sell</button></div>
              <input id="ro-amount" type="number" min="0" step="any" placeholder="Amount" style="flex:1 1 110px">
              <span class="sub">or</span>
              <input id="ro-qty" type="number" min="0" step="1" placeholder="Shares" style="flex:1 1 90px">
              <button type="submit" class="primary small">Place</button>
            </div>
            <label class="row sub" style="gap:6px"><input type="checkbox" id="ro-autostop"> Auto-sell at my trailing stops (checked every trading day of a step)</label>
            <div class="sub">Fills at the replay day's closing price, whole shares, Indian delivery charges deducted.</div>
          </form>
        </div>
        <div class="card">
          <div class="cardhead"><h2>Agent's picks as of today</h2><span class="sub" id="pk-sub"></span></div>
          <div class="cardbody" id="picks"></div>
        </div>
        <div class="card">
          <div class="cardhead"><h2>Tools as of today</h2><span class="sub">using only data up to the replay date</span></div>
          <div class="cardbody">
            <div class="row"><label class="sub" for="tl-years">Years of history</label><input id="tl-years" type="number" min="1" max="5" value="3" style="width:70px">
              <button type="button" class="small" data-tool="signal_lab">Run signal lab</button>
              <button type="button" class="small" data-tool="factor_backtest">Run factor backtest</button></div>
            <div id="tools"></div>
          </div>
        </div>
      </div>
      <aside class="side">
        <div class="card">
          <div class="cardhead"><h2>Look up a stock</h2></div>
          <form class="cardbody" id="rl-form"><div class="row"><input id="rl-ticker" autocomplete="off" placeholder="Ticker or company name" style="flex:1;text-transform:uppercase"><button type="submit" class="small">Check</button></div>
            <div id="rl-result" class="sub">Momentum, price and NSE announcements as they stood on the replay date.</div></form>
        </div>
        <div class="card">
          <div class="cardhead"><h2>Ask Claude about this day</h2></div>
          <div class="cardbody">
            <div class="row"><button type="button" class="primary small" id="ask-btn">Ask Claude</button><span class="sub" id="ask-note"></span></div>
            <div class="callout sub">Claude was trained on data after most replay dates, so an answer here may use hindsight. Its replay record is shown for interest only and is never a reason to trust it with real money.</div>
            <div id="ask-result"></div>
          </div>
        </div>
      </aside>
    </section>
  </section>
</main>
<div class="suggest" id="suggest" role="listbox" aria-label="Matching companies"></div>
<div class="toast" id="toast"></div>
<script src="/static/common.js"></script>
<script src="/static/replay.js"></script>
</body>
</html>
```

- [ ] **Step 4: Write `trading_agent/ui/replay.js`**

```js
(function(){
  const {$, esc, pct, toast, api, tile, C, lineChart, rupeesShort, inr, sinr, spct, lookupTakeaway, attachSuggest, shortDate, money} = TA;
  let T = null, slug = null, side = "buy";
  const WHO = {you: "You", agent: "Agent (rules)", nifty: "Nifty"};
  const tone = v => v == null ? "" : v > 0 ? "pl-profit" : v < 0 ? "pl-loss" : "";
  const fmtDay = d => new Date(d + "T00:00:00").toLocaleDateString("en-IN", {weekday: "short", day: "numeric", month: "short", year: "numeric"});
  const fall = v => v != null && v < 0 ? "worst fall " + spct(v) : "no fall yet";

  async function waitJob(id, statusEl){
    for(;;){
      const j = await api(`/replay/api/job/${id}`);
      if(statusEl) statusEl.textContent = j.message || "Working…";
      if(j.finished_at) return j;
      await new Promise(r => setTimeout(r, 1000));
    }
  }

  // ---- home ----
  async function loadHome(){
    const meta = await api("/replay/api/meta");
    $("n-start").min = meta.earliest; $("n-start").max = meta.today; $("n-earliest").textContent = fmtDay(meta.earliest);
    $("n-universe").innerHTML = meta.universes.map(u => `<option value="${esc(u)}" ${u === "NIFTYMIDCAP150" ? "selected" : ""}>${esc(u)}</option>`).join("");
    const rows = await api("/replay/api/trials");
    $("trials").innerHTML = rows.length ? rows.map(t => `<tr>
        <td><a href="#${esc(t.slug)}" data-open="${esc(t.slug)}" style="font-weight:600;text-decoration:none">${esc(t.name)}</a>${t.ended ? ' <span class="pill">ended</span>' : ""}<div class="sub" style="font-size:12px">${esc(t.universe)}</div></td>
        <td>${esc(shortDate(t.start))}</td><td>${esc(shortDate(t.clock))}</td>
        <td class="num ${tone(t.you)}">${t.you == null ? "n/a" : spct(t.you)}</td><td class="num ${tone(t.agent)}">${t.agent == null ? "n/a" : spct(t.agent)}</td><td class="num ${tone(t.nifty)}">${t.nifty == null ? "n/a" : spct(t.nifty)}</td>
        <td><button type="button" class="small" data-open="${esc(t.slug)}">Open</button></td></tr>`).join("")
      : `<tr><td colspan="7" class="empty">No replays yet. Start one below.</td></tr>`;
  }
  function show(which){
    $("home").hidden = which !== "home"; $("trial").hidden = which !== "trial"; $("banner").hidden = which !== "trial";
  }
  $("new-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const body = Object.fromEntries(new FormData(ev.target).entries());
    $("n-submit").disabled = true; $("n-status").textContent = "Starting…";
    try {
      const j = await api("/replay/api/trials", body);
      if(j.ok === false){ toast(j.message); return; }
      const done = await waitJob(j.id, $("n-status"));
      if(!done.ok){ toast(done.message); return; }
      location.hash = done.result.slug;
    } catch(e){ toast(e.message); }
    finally { $("n-submit").disabled = false; }
  });

  // ---- one replay ----
  async function open(s){
    slug = s; show("trial");
    try { T = await api(`/replay/api/trial/${encodeURIComponent(s)}`); render(); loadTools(); }
    catch(e){ toast(e.message); location.hash = ""; }
  }
  function render(){
    const t = T.trial, ended = !!t.ended;
    $("banner").innerHTML = `Replay · ${esc(fmtDay(t.clock))}${ended ? " · ended" : ""} <span class="sub">${esc(t.name)} · ${esc(t.universe)} vs ${esc(t.benchmark)} · started ${esc(fmtDay(t.start))} · dividends ${t.dividends === "cash" ? "taken as cash" : "reinvested"}</span>`;
    document.querySelectorAll("[data-step], #b-end, #ask-btn, #ro-form button, #ro-form input").forEach(b => b.disabled = ended);
    $("step-status").textContent = `${T.race.dates.length - 1} trading days since the start`;
    $("ro-date").textContent = fmtDay(t.clock); $("ro-autostop").checked = !!t.auto_stop;
    $("race-tiles").innerHTML = ["you", "agent", "nifty"].map(w => tile(WHO[w] + (w === "nifty" ? ` (${esc(t.benchmark)})` : ""),
      inr(T.tiles[w].value), `<span class="${tone(T.tiles[w].return)}">${spct(T.tiles[w].return)}</span> · ${fall(T.tiles[w].worst_fall)}`)).join("");
    $("race-sub").textContent = `${inr(t.cash)} each on ${shortDate(t.start)}`;
    lineChart($("race-chart"), {x: T.race.dates, height: 240, left: 64, legend: true, endLabels: false, yFmt: rupeesShort,
      label: "Replay race: you, the agent and the Nifty fund", empty: "The race chart starts after the first step.",
      refs: [{y: t.cash, label: "Start"}],
      series: [{name: "You", color: C.s1, values: T.race.you}, {name: "Agent", color: C.s2, values: T.race.agent},
               {name: t.benchmark, color: C.s3, values: T.race.nifty}]});
    renderYou(); renderPicks(); renderClaude(); renderScore();
  }
  function renderYou(){
    const ps = T.you.positions;
    const inv = ps.reduce((a, p) => a + p.qty * p.avg_entry_price, 0), val = ps.reduce((a, p) => a + (p.market_value || 0), 0);
    const pl = val - inv, plp = inv ? pl / inv : null;
    const stat = (k, v, sub, cls) => `<div class="mkt"><span class="k">${k}</span><span class="v ${cls || ""}">${v}</span><span class="sub" style="font-size:12px">${sub}</span></div>`;
    $("yp-stats").innerHTML = stat("Invested", inr(inv), ps.length ? `in ${ps.length} stock${ps.length === 1 ? "" : "s"}` : "nothing bought yet")
      + stat("Value now", inr(val), "at the replay day's close") + stat("Profit / loss", sinr(pl), plp == null ? "on open positions" : spct(plp) + " on open positions", tone(pl))
      + stat("Account total", inr(T.you.equity), `cash ${inr(T.you.cash)}`, tone(T.tiles.you.return));
    $("yp-sub").textContent = `as of ${shortDate(T.trial.clock)}`;
    $("yp-rows").innerHTML = ps.length ? ps.map(p => { const i = p.qty * p.avg_entry_price, pc = i ? p.unrealized_pl / i : null; return `<tr>
        <td><a href="#" data-lookup="${esc(p.symbol)}" style="font-weight:600;text-decoration:none">${esc(p.symbol)}</a>${p.suspended ? ` <span class="pill warn" style="font-size:10px;padding:1px 6px" title="last traded ${esc(p.last_trade)}">suspended</span>` : ""}</td>
        <td class="num">${p.qty}</td><td class="num">${inr(p.avg_entry_price, 2)}</td><td class="num">${inr(p.current_price, 2)}</td>
        <td class="num">${inr(i)}</td><td class="num">${inr(p.market_value)}</td><td class="num ${tone(p.unrealized_pl)}">${sinr(p.unrealized_pl)}</td>
        <td class="num ${tone(pc)}">${pc == null ? "n/a" : spct(pc)}</td>
        <td class="num ${p.stop != null && p.current_price <= p.stop ? "pl-loss" : ""}">${p.stop != null ? inr(p.stop, 2) : "n/a"}</td>
        <td>${T.trial.ended ? "" : `<button type="button" class="small" data-sell="${esc(p.symbol)}" data-qty="${p.qty}">Sell</button>`}</td></tr>`; }).join("")
      : `<tr><td colspan="10" class="empty">No stocks yet. Look one up, copy one of the agent's picks, or type a ticker below.</td></tr>`;
    $("yp-foot").innerHTML = ps.length ? `<tr><td>Total</td><td></td><td></td><td></td><td class="num">${inr(inv)}</td><td class="num">${inr(val)}</td><td class="num ${tone(pl)}">${sinr(pl)}</td><td class="num ${tone(plp)}">${plp == null ? "n/a" : spct(plp)}</td><td></td><td></td></tr>` : "";
  }
  function renderPicks(){
    const p = T.picks, a = T.agent;
    if(!p){ $("picks").innerHTML = `<div class="sub">No picks yet.</div>`; return; }
    $("pk-sub").textContent = `top ${T.trial.top} by momentum on ${shortDate(p.date)} · next rebalance ${a.next_rebalance}`;
    $("picks").innerHTML = `<div class="scroll"><table><thead><tr><th>Stock</th><th class="num">12-1 mom.</th><th class="num">6 m</th><th>Agent</th><th><span class="sr-only">Copy</span></th></tr></thead><tbody>
      ${p.rows.slice(0, T.trial.top).map(r => `<tr><td><a href="#" data-lookup="${esc(r.symbol)}" style="font-weight:600;text-decoration:none">${esc(r.symbol)}</a>${r.name ? `<div class="sub" style="font-size:12px">${esc(r.name)}</div>` : ""}</td>
        <td class="num">${pct(r.ret_12_1)}</td><td class="num">${pct(r.ret_6m)}</td>
        <td>${p.will_buy.includes(r.symbol) ? '<span class="pill ok">buys next rebalance</span>' : '<span class="pill">holds</span>'}</td>
        <td>${T.trial.ended ? "" : `<button type="button" class="small" data-copy="${esc(r.symbol)}">Copy</button>`}</td></tr>`).join("")}</tbody></table></div>
      ${p.will_sell.length ? `<div class="sub">At the next rebalance the agent sells: ${p.will_sell.map(esc).join(", ")}.</div>` : ""}
      <div class="sub">Agent holds ${a.holdings.length} stock${a.holdings.length === 1 ? "" : "s"}${a.holdings.length ? ": " + a.holdings.map(h => esc(h.symbol)).join(", ") : ""}.</div>`;
  }
  function renderClaude(){
    $("ask-note").textContent = T.claude_ready ? `${T.claude_presses} ask${T.claude_presses === 1 ? "" : "s"} in this replay` : "Needs ANTHROPIC_API_KEY in .env";
    $("ask-btn").disabled = !T.claude_ready || !!T.trial.ended;
    $("ask-result").innerHTML = T.claude.slice().reverse().map(c => `<div class="ann"><div class="meta">${esc(fmtDay(c.date))} · may include hindsight</div><div>${esc(c.summary)}</div>
      ${(c.recommendations || []).map(r => `<div class="row" style="gap:6px;margin-top:4px"><span class="act ${esc(r.action)}">${esc(r.action.toUpperCase())}</span><b>${esc(r.ticker)}</b><span class="pill">${esc(r.confidence)}</span></div><div class="sub">${esc(r.headline)}. ${esc(r.rationale)}</div>`).join("")}</div>`).join("");
  }
  function renderScore(){
    const s = T.scorecard, n = T.next;
    if(!s){ $("scorecard").innerHTML = ""; return; }
    const col = w => { const x = s[w]; return `<div class="mkt"><span class="k">${WHO[w]}</span><span class="v ${tone(x.return)}">${spct(x.return)}</span>
      <span class="sub" style="font-size:12px">${inr(x.final)} · ${spct(x.cagr)} a year · ${fall(x.max_drawdown)}<br>${x.trades} trade${x.trades === 1 ? "" : "s"} · charges ${inr(x.charges)}${x.dividends ? ` · dividends ${inr(x.dividends)}` : ""}
      ${x.best ? `<br>best ${esc(x.best.symbol)} ${sinr(x.best.pnl)} · worst ${esc(x.worst.symbol)} ${sinr(x.worst.pnl)}` : ""}${x.hit_rate != null && w !== "nifty" ? `<br>${Math.round(x.hit_rate * 100)}% of stocks made money` : ""}</span></div>`; };
    const graded = (s.claude || []).filter(g => g.return != null && (g.action === "buy" || g.action === "sell"));
    $("scorecard").innerHTML = `<div class="card"><div class="cardhead"><h2>Scorecard</h2><span class="sub">${esc(shortDate(T.trial.start))} to ${esc(shortDate(T.trial.ended))}${T.trial.dividends === "cash" ? " · dividends credited before tax" : ""}</span></div>
      <div class="cardbody"><div class="stats">${["you", "agent", "nifty"].map(col).join("")}</div>
      ${graded.length ? `<div class="sub">Claude's ${graded.length} buy/sell call${graded.length === 1 ? "" : "s"}: ${graded.filter(g => (g.action === "buy") === (g.return > 0)).length} went the way it said, by the end date (may include hindsight).</div>` : ""}
      <h3 style="margin-top:8px">What happened next</h3><div class="chart" id="next-chart"></div>
      <div class="sub">Each portfolio held unchanged from the end of the replay to today.</div></div></div>`;
    lineChart($("next-chart"), {x: n.dates, height: 200, left: 64, legend: true, endLabels: false, yFmt: rupeesShort, label: "After the replay",
      series: [{name: "You", color: C.s1, values: n.you}, {name: "Agent", color: C.s2, values: n.agent}, {name: T.trial.benchmark, color: C.s3, values: n.nifty}]});
  }

  // ---- actions ----
  document.querySelectorAll("[data-step]").forEach(b => b.addEventListener("click", async () => {
    document.querySelectorAll("[data-step]").forEach(x => x.disabled = true);
    try {
      const j = await api(`/replay/api/trial/${slug}/step`, {by: b.dataset.step});
      if(j.ok === false){ toast(j.message); return; }
      const done = await waitJob(j.id, $("step-status"));
      toast(done.message);
    } catch(e){ toast(e.message); }
    await open(slug);
  }));
  $("b-end").addEventListener("click", async () => {
    if(!confirm("End this replay? It becomes read-only and the scorecard and what happened next are shown.")) return;
    try { T = await api(`/replay/api/trial/${slug}/end`, {}); render(); } catch(e){ toast(e.message); }
  });
  $("b-home").addEventListener("click", () => { location.hash = ""; });
  document.querySelectorAll("#ro-form [data-side]").forEach(b => b.addEventListener("click", () => {
    side = b.dataset.side; document.querySelectorAll("#ro-form [data-side]").forEach(x => x.classList.toggle("on", x === b));
  }));
  $("ro-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const body = {symbol: $("ro-symbol").value.trim().toUpperCase(), side};
    if($("ro-qty").value) body.qty = Number($("ro-qty").value); else if($("ro-amount").value) body.notional = Number($("ro-amount").value);
    else { toast("Enter an amount or a number of shares"); return; }
    try { const j = await api(`/replay/api/trial/${slug}/order`, body); toast(`${side === "buy" ? "Bought" : "Sold"} ${j.order.qty} ${j.order.symbol} @ ${inr(j.order.filled_avg_price, 2)}`); $("ro-amount").value = $("ro-qty").value = ""; await open(slug); }
    catch(e){ toast(e.message); }
  });
  $("ro-autostop").addEventListener("change", async (ev) => {
    try { await api(`/replay/api/trial/${slug}/auto-stop`, {on: ev.target.checked}); } catch(e){ toast(e.message); }
  });
  $("ask-btn").addEventListener("click", async () => {
    $("ask-btn").disabled = true; $("ask-note").textContent = "Asking Claude…";
    try { await api(`/replay/api/trial/${slug}/ask`, {ticker: $("rl-ticker").value.trim().toUpperCase() || null}); await open(slug); }
    catch(e){ toast(e.message); renderClaude(); }
  });
  async function lookup(t){
    $("rl-ticker").value = t; $("rl-result").innerHTML = `<span class="sub">Looking up ${esc(t)} as of ${esc(fmtDay(T.trial.clock))}…</span>`;
    try {
      const r = await api(`/replay/api/trial/${slug}/lookup?ticker=${encodeURIComponent(t)}`);
      const now = new Date(r.today + "T23:59:59").getTime(), note = lookupTakeaway(r, now);
      const v = (r.momentum || {}).verdict || "n/a";
      $("rl-result").innerHTML = `<div class="row" style="gap:8px"><span style="font-size:17px;font-weight:700">${esc(r.ticker)}</span>${r.price != null ? `<span class="mono">${inr(r.price, 2)}</span>` : ""}<span class="pill ${v === "strong" ? "ok" : v === "weak" ? "bad" : ""}">${esc(v)} momentum</span></div>
        <div class="sub">${esc(r.momentum_summary || "")}</div>
        ${note ? `<div class="callout" style="margin-top:6px"><b style="color:var(--color-accent)">What this means.</b> ${note}</div>` : ""}
        <div class="chart" id="rl-chart" style="margin-top:6px"></div>
        <h3 style="margin-top:6px">NSE announcements, last 60 days</h3>${r.announcements.length ? r.announcements.map(a => `<div class="ann"><div class="meta">${esc(a.at)} · ${esc(a.category)}</div><div>${a.file ? `<a href="${esc(a.file)}" target="_blank" rel="noopener">${esc(a.text || a.category)}</a>` : esc(a.text || a.category)}</div></div>`).join("") : `<div class="sub">${esc(r.announcements_error || "none in the 60 days before the replay date")}</div>`}`;
      const h = r.history || [];
      lineChart($("rl-chart"), {x: h.map(p => p.d), height: 170, left: 52, endLabels: false, legend: true, label: `${r.ticker} price, year before the replay date`,
        yFmt: v => "₹" + Math.round(v).toLocaleString("en-IN"), empty: "No price history before this date.",
        refs: r.position ? [{y: r.position.avg_entry_price, label: "Your cost"}, {y: r.position.stop, label: "Stop", color: C.neg}] : [],
        series: [{name: "Price", color: C.s1, values: h.map(p => p.c)}, {name: "200-day average", color: C.ctx, width: 1.5, values: h.map(p => p.ma200)}]});
    } catch(e){ $("rl-result").innerHTML = `<span class="sub">${esc(e.message)}</span>`; }
  }
  $("rl-form").addEventListener("submit", (ev) => { ev.preventDefault(); const t = $("rl-ticker").value.trim().toUpperCase(); if(t) lookup(t); });
  attachSuggest(["rl-ticker", "ro-symbol"], (input, s) => { if(input.id === "rl-ticker") lookup(s); else $("ro-amount").focus(); });
  document.querySelectorAll("[data-tool]").forEach(b => b.addEventListener("click", async () => {
    try {
      const j = await api(`/replay/api/trial/${slug}/tool`, {kind: b.dataset.tool, years: Number($("tl-years").value) || 3});
      if(j.ok === false){ toast(j.message); return; }
      $("tools").innerHTML = `<div class="sub">Running… this loads up to five years of prices before the replay date.</div>`;
      const done = await waitJob(j.id, null); toast(done.message); loadTools();
    } catch(e){ toast(e.message); }
  }));
  async function loadTools(){
    const r = await api(`/replay/api/trial/${slug}/tools`);
    const ks = Object.keys(r);
    $("tools").innerHTML = ks.length ? ks.map(k => `<h3 style="margin-top:8px">${k === "signal_lab" ? "Signal lab" : "Factor backtest"} as of ${esc(shortDate(r[k].date))}, ${r[k].years} years</h3><pre class="mono" style="white-space:pre-wrap;font-size:12.5px">${esc(r[k].text)}</pre>`).join("") : `<div class="sub">Run a tool to see how the strategies looked with the data available on the replay date.</div>`;
  }
  document.addEventListener("click", async (ev) => {
    const o = ev.target.closest("[data-open]"); if(o){ ev.preventDefault(); location.hash = o.dataset.open; return; }
    const l = ev.target.closest("a[data-lookup]"); if(l){ ev.preventDefault(); lookup(l.dataset.lookup); $("rl-form").scrollIntoView({behavior: "smooth", block: "center"}); return; }
    const c = ev.target.closest("[data-copy]"); if(c){ $("ro-symbol").value = c.dataset.copy; document.querySelector('#ro-form [data-side="buy"]').click(); $("ro-amount").focus(); return; }
    const s = ev.target.closest("[data-sell]");
    if(s){
      if(!confirm(`Sell all ${s.dataset.qty} ${s.dataset.sell} at the replay day's close?`)) return;
      try { await api(`/replay/api/trial/${slug}/order`, {symbol: s.dataset.sell, side: "sell", qty: Number(s.dataset.qty)}); await open(slug); } catch(e){ toast(e.message); }
    }
  });
  let rsT; window.addEventListener("resize", () => { clearTimeout(rsT); rsT = setTimeout(() => { if(T && !$("trial").hidden) render(); }, 200); });

  function route(){ const h = decodeURIComponent(location.hash.slice(1)); if(h) open(h); else { show("home"); loadHome().catch(e => toast(e.message)); } }
  window.addEventListener("hashchange", route);
  route();
})();
```

- [ ] **Step 5: Run the test, the suite, and a full browser walk-through**

Run: `.venv/Scripts/python.exe -m pytest -q tests` → expected all pass.

Browser check (real public data, no credentials needed; the replay state goes to the dev checkout's `state/replay/`):
1. Start `.venv/Scripts/python.exe -m trading_agent ui --no-open --port 8791` in the background.
2. With the Playwright tools, open `http://127.0.0.1:8791/replay`.
3. Create a replay: name "Walkthrough", start `2023-03-01`, ₹1,00,000, NIFTYMIDCAP150, 10 stocks, reinvest. The first create downloads ~170 price histories; allow a few minutes.
4. Check the banner, the three tiles and the race chart. Buy 5 shares of the first pick with Copy.
5. Press `+1 month`. Check the clock moved, the chart has about 21 more points and the agent rebalanced.
6. Look up a stock. Check its chart ends on the replay date and the news is from the 60 days before it.
7. End the replay. Check the scorecard and the "What happened next" chart.
8. Confirm no console errors. Then delete `state/replay/walkthrough/` and stop the server.

- [ ] **Step 6: Commit**

```bash
git add trading_agent/ui/replay.html trading_agent/ui/replay.js tests/test_replay_web.py
git commit -m "Replay page: start on a past date, step forward, race the agent and the Nifty

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 12: README and the final check

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add a "Replay and Demo" section to `README.md`** (after the dashboard section)

```markdown
## Replay and Demo

The dashboard has three tabs: **Live** (today), **Replay** and **Demo**.

**Replay** (`/replay`) starts a practice portfolio on a past date, from 4 January 2021 (where the record of
index members begins). You, the agent's rules and the index fund each get the same practice money:

- **You** buy and sell at the replay day's closing price, in whole shares, with Indian delivery charges.
- **The agent** buys the momentum screen's top N from the index members of that day and rebalances on the first
  trading day of each month (the forward-test rules), selling at a 3×ATR trailing stop.
- **The Nifty** line buys the index fund (MID150BEES for the Midcap 150, NIFTYBEES for Nifty 50/100/200/500) and holds.

Step forward a week, a month or a year; every trading day in between is simulated. Prices, the screen, the signal
lab, the factor backtest and NSE announcements only ever see data up to the replay date; asking for anything later
raises an error in the code, and tests check this. **End replay** shows the scorecard and what each portfolio did
from then to today. Dividends are reinvested or taken as cash, chosen when you start.

**Ask Claude about this day** sends Claude only the data available that day and charges your Anthropic account per
press. Claude's training data runs past most replay dates, so its replay answers may use hindsight; they are
labelled so and are never a reason to trust it with real money.

Replays are saved in `state/replay/<name>/`. Nothing on the Replay or Demo pages can place a real order.

**Demo** (`/demo`, or `python -m trading_agent ui --demo`) is the dashboard on bundled sample deals and prices,
with its own practice account in `state/demo/`. It never contacts Groww; **Reset demo** starts it over.
```

- [ ] **Step 2: Full suite and push**

Run: `.venv/Scripts/python.exe -m pytest -q tests` → all pass.

```bash
git add README.md
git commit -m "README: Replay and Demo pages

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git push origin master
git -C idev pull --ff-only origin master
```

---

## Self-review notes

- **Spec coverage:**

  | Spec section | Task(s) |
  |---|---|
  | §1 clock, data wrappers, earliest date, approximations | 1, 2, 4 |
  | §2 trial inputs, portfolios, dividends, step, End trial, what happened next | 4, 5, 6 |
  | §3 pages: tabs, banners, home, trial layout, Demo | 9, 10, 11 |
  | §4 Ask Claude | 7, with UI in 11 |
  | §5 errors (unlisted, suspended, renames via membership, Yahoo failure, NSE blocked, no membership) | 1, 2, 4, 5 |
  | §6 tests | in every task |
  | §7 build order | followed |

  Deviations are listed at the top (momentum-only agent; per-symbol news; suspended holdings not auto-closed).
- **Types:**
  - `screen_fn(members, prices, top) -> list[dict]` is the same in Tasks 4, 5, 8 and the fakes.
  - `step(trial, until, *, today, progress)` is the same in Tasks 5 and 8.
  - `run_background(kind, fn(job))` is the same in Task 8's App and ReplayApp.
  - The `TA` names exported in Task 9 match those destructured in Task 11.
