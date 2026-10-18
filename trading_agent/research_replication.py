"""The adopted rules on US, Japanese and UK midcaps, ONCE per market (docs/research/preregistered-replication-2026-10-18.md;
the UK in its addendum, preregistered-replication-2026-10-18b.md).

A replication, not a search: ``FINALIST`` unchanged except that no tax is charged (both sides are compared before
tax), on the S&P MidCap 400 and the TOPIX Mid400, 1 Oct 2016 - 30 Sep 2026, against each market's fund. Every choice
below was fixed in the pre-registration or in its data step, before any strategy run:

* US members: today's S&P MidCap 400 list and its change table (Wikipedia). The history counts as complete, and
  membership is point in time, only if it starts before the lookback year and every month start of the window has
  between 390 and 410 members when rebuilt backward from today's list; otherwise today's members are used for every
  date and the result says so (survivorship).
* Japan members: JPX's TOPIX weight file (``New Index Series Code`` = TOPIX Mid400), today's members only: JPX
  publishes no free change history.
* Benchmarks: IJH (iShares Core S&P Mid-Cap ETF) for the US. No TOPIX Mid400 ETF is listed on JPX (checked 18 Oct
  2026 in the data step), so, as pre-registered, the TOPIX ETF 1306.T for Japan. Adjusted closes (dividends in).
* Costs: 5 bps a side in the US, 10 bps in Japan; the fund pays one buy at the start. No tax either side.
* Liquidity floor: the Indian Rs 1 crore a day at a fixed rate: US$120,000 and 18 million yen.

* UK (addendum): today's FTSE 250 members (Wikipedia; no free change history), ``.L`` prices in pence, against
  the iShares FTSE 250 ETF (MIDD.L). 55 bps on buys (0.5% stamp duty plus 5 bps), 5 bps on sells; the fund pays one
  5 bp buy (no stamp duty on ETFs). Floor GBP 95,000 a day, applied as 9.5 million pence of traded value.

Pass (per market): beat the fund over the window AND a worst fall no more than 10 points deeper than the fund's.
Each market runs once: claimed before the simulation, logged before printing, refused a second time.
"""

from __future__ import annotations

import csv
import io
import math
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from dataclasses import dataclass
from typing import Any

from .research_sim import Market, simulate, summarize, with_rules

PREREGISTRATION = "docs/research/preregistered-replication-2026-10-18.md"
UK_ADDENDUM = "docs/research/preregistered-replication-2026-10-18b.md"
UNIT_JUMP = 50.0                 # a close more than 50x (or under 1/50 of) the day before: a pence/pound mix-up
LOOKBACK_FROM = "2015-10-01"     # a year of history before the first decision
TRADING_FROM = "2016-10-01"
WINDOW_END = "2026-10-01"        # first date NOT used
FALL_MARGIN = 0.10
CAPITAL = 1_000_000.0
COMPLETE_MIN, COMPLETE_MAX = 390, 410
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"}

MARKETS: dict[str, dict[str, Any]] = {
    "us": {"name": "United States", "index": "S&P MidCap 400", "suffix": "", "benchmark": "IJH",
           "benchmark_name": "iShares Core S&P Mid-Cap ETF (IJH)", "one_way_bps": 5.0, "min_turnover": 120_000.0,
           "currency": "USD", "study": "Replication-US", "min_members": 300, "plan": PREREGISTRATION,
           "url": "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies"},
    "jp": {"name": "Japan", "index": "TOPIX Mid400", "suffix": ".T", "benchmark": "1306",
           "benchmark_name": "NEXT FUNDS TOPIX ETF (1306.T; no TOPIX Mid400 ETF is listed)", "one_way_bps": 10.0,
           "min_turnover": 18_000_000.0, "currency": "JPY", "study": "Replication-JP", "min_members": 300,
           "plan": PREREGISTRATION,
           "url": "https://www.jpx.co.jp/automation/english/markets/indices/topix/files/topixweight_e.csv"},
    "uk": {"name": "the United Kingdom", "index": "FTSE 250", "suffix": ".L", "benchmark": "MIDD",
           "benchmark_name": "iShares FTSE 250 ETF (MIDD.L)", "one_way_bps": 5.0, "buy_bps": 55.0, "sell_bps": 5.0,
           "fund_bps": 5.0, "min_turnover": 9_500_000.0, "currency": "GBp", "study": "Replication-UK",
           "min_members": 240, "plan": UK_ADDENDUM, "url": "https://en.wikipedia.org/wiki/FTSE_250_Index"},
}


@dataclass(frozen=True)
class SideCosts:
    """Different charges on buys and sells (UK stamp duty is paid on purchases only)."""

    buy_bps: float
    sell_bps: float

    def charges(self, side: str, notional: float) -> float:
        return notional * (self.buy_bps if side == "buy" else self.sell_bps) / 10_000

    def breakdown(self, side: str, notional: float) -> dict[str, float]:
        c = self.charges(side, notional)
        return {"charges": c, "slippage": 0.0, "total": c}

    def round_trip_bps(self, notional: float, include_slippage: bool = True) -> float:
        return self.buy_bps + self.sell_bps


def cost_model(market: str) -> Any:
    from .costs import FlatCosts
    spec = MARKETS[market]
    if "buy_bps" in spec:
        return SideCosts(spec["buy_bps"], spec["sell_bps"])
    return FlatCosts(one_way_bps=spec["one_way_bps"])


def cost_text(market: str) -> str:
    spec = MARKETS[market]
    if "buy_bps" in spec:
        return f"{spec['buy_bps']:g} bps on buys, {spec['sell_bps']:g} bps on sells"
    return f"{spec['one_way_bps']:g} bps a side"


# --------------------------------------------------------------------------- #
# members
# --------------------------------------------------------------------------- #
class _Tables(HTMLParser):
    """Every <table> with an id, as rows of cell texts; rowspan cells are repeated into the rows below."""

    def __init__(self) -> None:
        super().__init__()
        self.tables: dict[str, list[list[str]]] = {}
        self._id: str | None = None
        self._depth = 0
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._span = 1
        self._pending: dict[int, tuple[str, int]] = {}   # column -> (text, rows left)
        self._cells: list[tuple[str, int]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "table":
            tid = a.get("id")
            if self._id is None and tid:
                self._id, self._depth = tid, 1
                self.tables[tid] = []
                self._pending = {}
            elif self._id is not None:
                self._depth += 1
        elif self._id is not None and self._depth == 1:
            if tag == "tr":
                self._cells = []
            elif tag in ("td", "th"):
                self._cell = []
                try:
                    self._span = int(a.get("rowspan") or 1)
                except ValueError:
                    self._span = 1
            elif tag == "br" and self._cell is not None:
                self._cell.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if self._id is None:
            return
        if tag == "table":
            self._depth -= 1
            if self._depth == 0:
                self._id = None
        elif self._depth == 1 and tag in ("td", "th") and self._cell is not None:
            self._cells.append((re.sub(r"\s+", " ", "".join(self._cell)).strip(), self._span))
            self._cell = None
        elif self._depth == 1 and tag == "tr":
            row: list[str] = []
            cells = list(self._cells)
            col = 0
            while cells or col in self._pending:
                if col in self._pending:
                    text, left = self._pending[col]
                    row.append(text)
                    if left > 1:
                        self._pending[col] = (text, left - 1)
                    else:
                        del self._pending[col]
                else:
                    text, span = cells.pop(0)
                    row.append(text)
                    if span > 1:
                        self._pending[col] = (text, span - 1)
                col += 1
            self.tables[self._id].append(row)

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def us_symbol(ticker: str) -> str:
    """Yahoo's spelling of a US share class: BRK.B -> BRK-B."""
    return re.sub(r"[^A-Z0-9-]", "", ticker.strip().upper().replace(".", "-"))


def _us_date(text: str) -> str | None:
    text = re.sub(r"\[.*?\]", "", text).strip()
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_us(page: str) -> tuple[list[str], list[tuple[str, tuple[str, ...], tuple[str, ...]]]]:
    """Today's S&P MidCap 400 members and the change table (date, added, removed), from the Wikipedia page."""
    p = _Tables()
    p.feed(page)
    rows = p.tables.get("constituents") or []
    current = sorted({us_symbol(r[0]) for r in rows[1:] if r and r[0] and us_symbol(r[0])})
    changes: dict[str, tuple[set[str], set[str]]] = {}
    for r in p.tables.get("changes") or []:
        if len(r) < 5:
            continue
        day = _us_date(r[0])
        if not day:
            continue                                    # the two header rows
        added, removed = us_symbol(r[1]), us_symbol(r[3])
        a, rm = changes.setdefault(day, (set(), set()))
        if added:
            a.add(added)
        if removed:
            rm.add(removed)
    return current, [(d, tuple(sorted(a)), tuple(sorted(r))) for d, (a, r) in sorted(changes.items())]


def parse_jp(text: str) -> list[str]:
    """Today's TOPIX Mid400 members (four-digit or alphanumeric codes) from JPX's weight file."""
    rows = csv.DictReader(io.StringIO(text))
    return sorted({(r.get("Code") or "").strip().upper() for r in rows
                   if (r.get("New Index Series Code") or "").strip() == "TOPIX Mid400" and (r.get("Code") or "").strip()})


def uk_symbol(ticker: str) -> str:
    """Yahoo's spelling of a London ticker (without .L): BT.A -> BT-A."""
    return re.sub(r"[^A-Z0-9-]", "", re.sub(r"\[.*?\]", "", ticker).strip().upper().rstrip(".").replace(".", "-"))


def parse_uk(page: str) -> list[str]:
    """Today's FTSE 250 members from the Wikipedia constituents table (its Ticker column)."""
    p = _Tables()
    p.feed(page)
    rows = p.tables.get("constituents") or []
    if not rows:
        return []
    header = [h.lower() for h in rows[0]]
    col = next((i for i, h in enumerate(header) if h.startswith("ticker") or h.startswith("epic")), None)
    if col is None:
        return []
    return sorted({uk_symbol(r[col]) for r in rows[1:] if len(r) > col and uk_symbol(r[col])})


def drop_bad_prices(bars: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Bars with a zero or negative close (or adjusted close) removed; such a day carries the last good price, as a
    day without trading does. Yahoo's Japanese histories occasionally hold such points."""
    def bad(b: dict[str, Any]) -> bool:
        c, a = b.get("close"), b.get("adj_close")
        return c is None or not float(c) > 0 or (a is not None and not float(a) > 0)
    kept = [b for b in bars if not bad(b)]
    return kept, len(bars) - len(kept)


def unit_jumps(series: dict[str, list[dict[str, Any]]]) -> list[str]:
    """Stocks whose close jumps by more than UNIT_JUMP times in a day (a pence/pound mix-up in the data)."""
    out = []
    for sym, bars in series.items():
        closes = [b["close"] for b in bars if b.get("close")]
        if any(b / a > UNIT_JUMP or a / b > UNIT_JUMP for a, b in zip(closes, closes[1:]) if a > 0 and b > 0):
            out.append(sym)
    return sorted(out)


BENCH_JUMP = 0.40               # a fund moving more than 40% in one day is a data error (an unadjusted split)
LATEST_START = "2017-01-01"     # the first decision must fall within a quarter of TRADING_FROM


def data_problems(m: Market) -> list[str]:
    """Reasons the loaded data cannot carry the pre-registered test. Checked BEFORE the market is claimed, so a data
    fault never spends it. None of these looks at a strategy result."""
    from statistics import median
    out: list[str] = []
    gaps = [(datetime.fromisoformat(b) - datetime.fromisoformat(a)).days for a, b in zip(m.dates, m.dates[1:])]
    if not gaps or median(gaps) > 4:
        out.append(f"the benchmark's prices are not daily ({len(m.dates)} bars, a typical gap of "
                   f"{median(gaps) if gaps else 0:g} days)")
    fund = [v for v in m.fund_adj if v]
    jumps = []
    for i in range(1, len(m.fund_adj)):
        a, b = m.fund_adj[i - 1], m.fund_adj[i]
        if a and b and abs(b / a - 1) > BENCH_JUMP:
            jumps.append(m.dates[i])
    if jumps:
        out.append(f"the benchmark moves more than {BENCH_JUMP:.0%} in a day on {', '.join(jumps[:4])} (a data error)")
    if not fund:
        out.append("the benchmark has no prices")
    from .research_studies import _first_ranked_day
    try:
        first = m.dates[_first_ranked_day(m)]
        if first >= LATEST_START:
            out.append(f"the first day with 20 rankable stocks is {first}, not near {TRADING_FROM}: too few members have "
                       "daily history that far back")
    except ValueError as e:
        out.append(str(e))
    return out


def month_starts(dates: list[str], start: str, end: str) -> list[str]:
    seen, out = set(), []
    for d in dates:
        if start <= d < end and d[:7] not in seen:
            seen.add(d[:7])
            out.append(d)
    return out


def us_membership(current: list[str], changes: list[tuple[str, tuple[str, ...], tuple[str, ...]]],
                  calendar: list[str]) -> tuple[Any, list[str]]:
    """Point-in-time membership when the change history passes the fixed completeness test, else today's list."""
    from .membership import Membership
    m = Membership(frozenset(current), changes, source="Wikipedia S&P 400 change table")
    sizes = {d: len(m.members_on(d)) for d in month_starts(calendar, TRADING_FROM, WINDOW_END)}
    bad = {d: n for d, n in sizes.items() if not COMPLETE_MIN <= n <= COMPLETE_MAX}
    if changes and m.known_since <= LOOKBACK_FROM and sizes and not bad:
        return m, [f"point in time: rebuilt from {len(changes)} change dates since {m.known_since}; every month start "
                   f"has {min(sizes.values())}-{max(sizes.values())} members."]
    why = ("no change history" if not changes else
           f"the history starts {m.known_since}, after {LOOKBACK_FROM}" if m.known_since > LOOKBACK_FROM else
           f"{len(bad)} month start(s) rebuild to outside {COMPLETE_MIN}-{COMPLETE_MAX} members "
           f"(first {min(bad)}: {bad[min(bad)]})")
    return None, [f"today's {len(current)} members for every date ({why}): stocks that left the index are missing, "
                  "so the result is flattered (survivorship)."]


def fetch_members(market: str, session: Any = None) -> tuple[list[str], list[tuple[str, tuple[str, ...], tuple[str, ...]]]]:
    import requests
    s = session or requests.Session()
    resp = s.get(MARKETS[market]["url"], headers=HEADERS, timeout=60)
    resp.raise_for_status()
    if market == "us":
        return parse_us(resp.text)
    if market == "uk":
        return parse_uk(resp.text), []
    return parse_jp(resp.content.decode("utf-8", errors="replace")), []


# --------------------------------------------------------------------------- #
# the market and the run
# --------------------------------------------------------------------------- #
def load_replication_market(market: str, prices: Any, current: list[str],
                            changes: list[tuple[str, tuple[str, ...], tuple[str, ...]]], *,
                            workers: int = 8) -> tuple[Market, list[str]]:
    """Every member (ever, when point in time) on the benchmark's trading days, nothing dated on or after WINDOW_END."""
    spec = MARKETS[market]
    bench, bad_bench = drop_bad_prices([b for b in prices.history(spec["benchmark"], "max")
                                        if LOOKBACK_FROM <= b["date"] < WINDOW_END])
    if not bench or bench[0]["date"] > "2015-10-31":
        raise ValueError(f"the benchmark {spec['benchmark']} has no prices from {LOOKBACK_FROM}")
    calendar = [b["date"] for b in bench]
    membership, notes = us_membership(current, changes, calendar) if market == "us" else (
        None, [f"today's {len(current)} members for every date "
               f"({'JPX' if market == 'jp' else 'Wikipedia'} publishes no free change history): stocks that "
               "left the index are missing, so the result is flattered (survivorship)."])
    universe = sorted(membership.ever_members(LOOKBACK_FROM)) if membership is not None else sorted(current)

    dropped: dict[str, int] = {}

    def load(sym: str) -> tuple[str, list[dict[str, Any]] | None]:
        try:
            bars, bad = drop_bad_prices([b for b in prices.history(sym, "max") if b["date"] < WINDOW_END])
        except Exception:  # noqa: BLE001 - a missing stock is counted, not fatal
            return sym, None
        if bad:
            dropped[sym] = bad
        return sym, bars

    with ThreadPoolExecutor(max_workers=workers) as ex:
        series = {s: b for s, b in ex.map(load, universe) if b and b[0]["date"] < WINDOW_END}
    if bad_bench:
        dropped[spec["benchmark"]] = bad_bench
    if dropped:
        names = sorted(dropped)
        notes.append(f"{sum(dropped.values())} zero or negative price point(s) dropped in {len(names)} series "
                     f"({', '.join(f'{k} {dropped[k]}' for k in names[:12])}{' …' if len(names) > 12 else ''}); "
                     "each such day carries the last good price.")
    members_on = membership.members_on if membership is not None else (lambda d, c=frozenset(current): set(c))
    window_members: set[str] = set()
    for d in month_starts(calendar, TRADING_FROM, WINDOW_END):
        window_members |= members_on(d)
    m = Market(calendar, series, bench, members_on, min_turnover=spec["min_turnover"])
    m.missing = sorted(window_members - set(series))
    m.start_day = TRADING_FROM
    m.point_in_time = membership is not None
    m.unit_jumps = unit_jumps(series) if spec["currency"] == "GBp" else []   # London quotes mix pence and pounds
    return m, notes


def replicate(market: str, m: Market, *, extra_trials: int = 0) -> dict[str, Any]:
    """The adopted rules, unchanged except no tax, once; judged by the pre-registered rule."""
    from .research_studies import FINALIST, _first_ranked_day, _skill
    spec = MARKETS[market]
    start, end = _first_ranked_day(m), len(m.dates) - 1
    if m.dates[start] < TRADING_FROM or m.dates[end] >= WINDOW_END:
        raise ValueError(f"the window must run {TRADING_FROM} to before {WINDOW_END}")
    rules = with_rules(FINALIST, tax=False)
    run = simulate(m, rules, start, end, capital=CAPITAL, cost_model=cost_model(market))
    x = summarize(run, m, start, end)
    buy = spec.get("fund_bps", spec["one_way_bps"]) / 10_000
    fund = None if x["fund_total_return"] is None else (1 + x["fund_total_return"]) * (1 - buy) - 1
    excess = None if fund is None or x["total_return"] is None else x["total_return"] - fund
    checks = {"beats the fund": excess is not None and excess > 0,
              "worst fall within 10 points of the fund's": (x["max_drawdown"] is not None
                                                           and x["fund_max_drawdown"] is not None
                                                           and x["max_drawdown"] >= x["fund_max_drawdown"] - FALL_MARGIN)}
    months = _months_ahead(run, m, start, end)
    return {"market": market, "start": m.dates[start], "end": m.dates[end], "rules": rules.label(),
            "total_return": x["total_return"], "cagr": x["cagr"], "max_drawdown": x["max_drawdown"],
            "turnover": x["turnover"], "fund_total_return": fund, "fund_max_drawdown": x["fund_max_drawdown"],
            "excess": excess, "months_ahead": months, "checks": checks, "passes": all(checks.values()),
            "skill": _skill({"adopted": run}, m, start, end, extra_trials)["adopted"],
            "missing": list(m.missing), "point_in_time": m.point_in_time,
            "members": len(m.raw) + len(m.missing)}


def _months_ahead(run: dict[str, Any], m: Market, start: int, end: int) -> tuple[int, int]:
    from .research_studies import _excess_monthly
    ex = [v for v in _excess_monthly(run, m, start, end) if v is not None]
    return sum(1 for v in ex if v > 0), len(ex)


def format_replication(r: dict[str, Any], notes: list[str]) -> str:
    def pct(v: Any, f: str = "+.1f") -> str:
        return "n/a" if v is None or (isinstance(v, float) and math.isnan(v)) else f"{v * 100:{f}}%"
    spec = MARKETS[r["market"]]
    ahead, n = r["months_ahead"]
    p = (r["skill"] or {}).get("probability")
    lines = [f"REPLICATION, run once: the adopted rules, unchanged (no tax), on the {spec['index']} ({spec['name']}), "
             f"{r['start']} to {r['end']}, {cost_text(r['market'])} ({spec['plan']}).",
             f"  Adopted rules: {pct(r['total_return'])} ({pct(r['cagr'])} a year), worst fall {pct(r['max_drawdown'])}, "
             f"turnover {pct(r['turnover'], '.0f')} a year, ahead of the fund in {ahead} of {n} months.",
             f"  {spec['benchmark_name']}: {pct(r['fund_total_return'])}, worst fall {pct(r['fund_max_drawdown'])}.",
             f"  Against the fund: {pct(r['excess'])}. Skill odds {'n/a' if p is None else f'{p:.2f}'} (information "
             "only)."]
    failed = [k for k, ok in r["checks"].items() if not ok]
    if failed:
        lines.append(f"  Misses: {'; '.join(failed)}.")
    k = len(r["missing"])
    lines.append(f"  Members without prices: {k} of {r['members']}"
                 + (f" ({', '.join(r['missing'][:12])}{' …' if k > 12 else ''})" if k else "") + ".")
    lines.extend(f"  Membership: {n_}" for n_ in notes)
    lines.append(f"  Verdict: the adopted rules {'PASSED' if r['passes'] else 'did NOT pass'} in {spec['name']}. "
                 "What each outcome means was fixed in the pre-registration; no Indian rule changes either way. "
                 "This market is now spent.")
    return "\n".join(lines)


def prices_for(market: str, state_dir: Path) -> Any:
    from .prices import YahooPrices
    return YahooPrices(suffix=MARKETS[market]["suffix"], cache_dir=Path(state_dir) / "replication" / market,
                       cache_ttl=7 * 86400)
