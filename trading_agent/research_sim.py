"""A daily portfolio simulator for the research roadmap (stop rules, rank buffer, drawdown brake, weights, taxes).

The monthly factor backtest (factor_backtest.py) marks the portfolio once a month, which cannot test a stop or a
brake that acts between rebalances. This simulator walks every trading day:

1. each holding is marked to its dividend-adjusted close; spare cash earns a liquid fund's rate (``LIQUID_RATE``,
   a constant: there is no free daily history of a liquid fund's NAV);
2. stops are checked on the close (sold at that close, charges paid); a stopped stock is not bought again before the
   next rebalance;
3. the drawdown brake sets the invested share from the regime series (the universe's index fund) and every holding is
   trimmed or restored proportionally when that share changes;
4. on the first trading day of each month (or year) the ranker's list is turned into a target portfolio: the top
   ``top`` names, or with a rank buffer the holdings still ranked within ``buffer`` plus new names for the empty
   slots only; equal or inverse-volatility weights;
5. with ``tax`` on, every sale books a short- or long-term gain (365 days) and the tax for each Indian financial year
   (April to March) is paid out of cash on its last day, at the rates of the sale date (STCG 15% / LTCG 10% above
   ₹1 lakh until 22 Jul 2024, then 20% / 12.5% above ₹1.25 lakh); a short-term loss is set off against gains first.

Everything a decision uses is known on that day's close: rankings use bars up to the day, ATR and averages likewise.
Rankings are computed once per rebalance day (``Market.ranking``) and shared by every variant, so variants differ only
in their rules.

Simplifications, stated in every report: sales and purchases at the close (with ``Rules.lag`` = 1, at the close of
the session after the decision, as the agent trades); one cost basis and one holding date per position (a top-up keeps
the first buy date); Yahoo's history has no daily high/low, so ATR is close-to-close. ``Rules.floor`` sells a holding
whose value has fallen that far below its cost (charges included), the forward test's disaster floor;
``Rules.cost_mult`` scales every charge, for stress tests.
"""

from __future__ import annotations

import math
import statistics
from bisect import bisect_right
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any, Callable

LIQUID_RATE = 0.065
MIN_BARS = 274          # 12-1 momentum needs 252 + 21 bars
STOP_RULES = ("none", "atr3", "atr5", "two_stage", "grace20", "ema50")
BRAKES = ("none", "ema", "sma100", "ema_breadth", "ema_flows")
TAX_CHANGE = "2024-07-23"
LTCG_START = "2018-04-01"      # long-term gains on listed shares were exempt before this (Finance Act 2018)
GRANDFATHER_DAY = "2018-01-31" # shares bought before 1 Feb 2018: cost is at least their value on this day


@dataclass(frozen=True)
class Rules:
    top: int = 20
    stop: str = "none"              # one of STOP_RULES
    buffer: int | None = None       # keep holdings while ranked this or better (35 in the spec)
    tax_aware: bool = False         # with a buffer: a profitable holding aged 335-364 days waits for day 366
    tax_aware_floor: int = 50       # ... unless its rank falls below this
    brake: str = "none"             # one of BRAKES
    breadth_floor: float = 0.35
    breadth_days: int = 3
    weights: str = "equal"          # equal (1/top a slot) | equal_picks (1/number picked) | inverse_vol
    w_min: float = 0.025
    w_max: float = 0.08
    min_names_for_ivol: int = 13
    rebalance: str = "monthly"      # monthly | bimonthly | quarterly | yearly
    rebalance_month: int = 1        # yearly: the month of the yearly rebalance
    tax: bool = False
    liquid_rate: float = LIQUID_RATE
    floor: float | None = None      # sell when a holding is this fraction below its cost (0.25 in the forward test)
    lag: int = 0                    # sessions between a decision and its trade (1 = the next session's close)
    cost_mult: float = 1.0          # charges times this (a stress test)
    industry_cap: int | None = None # with a buffer: no new buy while this many holdings share its industry

    def label(self) -> str:
        parts = [f"stop={self.stop}"] if self.stop != "none" else []
        if self.floor:
            parts.append(f"floor{self.floor * 100:.0f}%")
        if self.buffer:
            parts.append(f"buffer{self.buffer}" + ("+tax-aware" if self.tax_aware else ""))
        if self.brake != "none":
            parts.append(f"brake={self.brake}")
        if self.weights != "equal":
            parts.append(self.weights)
        if self.rebalance != "monthly":
            parts.append(self.rebalance)
        if self.lag:
            parts.append(f"fills +{self.lag} day" + ("s" if self.lag > 1 else ""))
        if self.cost_mult != 1.0:
            parts.append(f"costs x{self.cost_mult:g}")
        if self.industry_cap:
            parts.append(f"at most {self.industry_cap} per industry")
        return ", ".join(parts) or "baseline"


# --------------------------------------------------------------------------- #
# indicators on aligned series (None-aware)
# --------------------------------------------------------------------------- #
def ema(values: list[float | None], n: int) -> list[float | None]:
    out: list[float | None] = []
    k, cur, seen = 2 / (n + 1), None, 0
    for v in values:
        if v is None:
            out.append(cur if seen >= n else None)
            continue
        cur = v if cur is None else cur + k * (v - cur)
        seen += 1
        out.append(cur if seen >= n else None)
    return out


def sma(values: list[float | None], n: int) -> list[float | None]:
    out: list[float | None] = []
    window: list[float] = []
    total = 0.0
    for v in values:
        if v is not None:
            window.append(v)
            total += v
            if len(window) > n:
                total -= window.pop(0)
        out.append(total / n if len(window) == n else None)
    return out


def atr_close(values: list[float | None], n: int = 14) -> list[float | None]:
    """Close-to-close ATR (Yahoo's daily history has no high/low): mean |close - previous close| over n days."""
    out: list[float | None] = []
    trs: list[float] = []
    prev = None
    for v in values:
        if v is not None and prev is not None:
            trs.append(abs(v - prev))
            if len(trs) > n:
                trs.pop(0)
        if v is not None:
            prev = v
        out.append(sum(trs) / n if len(trs) == n else None)
    return out


def vol60(adj: list[float | None]) -> list[float | None]:
    """Annualised 60-day volatility of log returns, on the aligned series."""
    out: list[float | None] = []
    rets: list[float] = []
    prev = None
    for v in adj:
        if v is not None and prev:
            rets.append(math.log(v / prev))
            if len(rets) > 60:
                rets.pop(0)
        if v is not None:
            prev = v
        if len(rets) == 60:   # population sd with fsum: equal to statistics.pstdev to ~1e-15, far faster
            mu = math.fsum(rets) / 60
            out.append(math.sqrt(math.fsum((x - mu) ** 2 for x in rets) / 60) * math.sqrt(252))
        else:
            out.append(None)
    return out


# --------------------------------------------------------------------------- #
# market data, aligned to one calendar
# --------------------------------------------------------------------------- #
class Market:
    """Every series a simulation reads, aligned to the regime fund's trading days. ``members_on(day)`` is the
    point-in-time index membership (or today's members)."""

    def __init__(self, calendar: list[str], series: dict[str, list[dict[str, Any]]], regime: list[dict[str, Any]],
                 members_on: Callable[[str], set[str]], *, min_turnover: float = 1e7,
                 flows: dict[str, float] | None = None):
        self.dates = calendar
        self.members_on = members_on
        self.min_turnover = min_turnover
        self.raw = series
        self.raw_dates = {s: [b["date"] for b in bars] for s, bars in series.items()}
        self.close: dict[str, list[float | None]] = {}
        self.adj: dict[str, list[float | None]] = {}
        for sym, bars in series.items():
            self.close[sym], self.adj[sym] = self._align(bars)
        self.fund_close, self.fund_adj = self._align(regime)
        self.fund_ema50, self.fund_ema200 = ema(self.fund_close, 50), ema(self.fund_close, 200)
        self.fund_sma100 = sma(self.fund_close, 100)
        self.flows = flows or {}
        self.missing: list[str] = []
        self.point_in_time = False   # membership taken at each date (load_market with a membership history)
        self.unit_jumps: list[str] = []   # stocks whose close jumps >50x in a day (a pence/pound mix-up)
        self.start_day = calendar[0] if calendar else ""   # the test window's first day (load_market sets it)
        self._atr: dict[str, list[float | None]] = {}
        self._ema50: dict[str, list[float | None]] = {}
        self._vol: dict[str, list[float | None]] = {}
        self._breadth: list[float | None] | None = None
        self._rank: dict[int, list[str]] = {}
        self._rank_detail: dict[int, dict[str, int]] = {}
        self._scores: dict[int, dict[str, float]] = {}
        self._stats: dict[int, dict[str, dict[str, Any]]] = {}

    def _align(self, bars: list[dict[str, Any]]) -> tuple[list[float | None], list[float | None]]:
        by = {b["date"]: b for b in bars}
        close: list[float | None] = []
        adj: list[float | None] = []
        first = bars[0]["date"] if bars else None
        last_c = last_a = None
        for d in self.dates:
            b = by.get(d)
            if b is not None:
                last_c, last_a = float(b["close"]), float(b.get("adj_close") or b["close"])
            # a day the stock did not trade (or a gap) carries the last close; before its first bar it has none
            close.append(last_c if first and d >= first else None)
            adj.append(last_a if first and d >= first else None)
        return close, adj

    def atr(self, sym: str) -> list[float | None]:
        if sym not in self._atr:
            self._atr[sym] = atr_close(self.close[sym])
        return self._atr[sym]

    def ema50(self, sym: str) -> list[float | None]:
        if sym not in self._ema50:
            self._ema50[sym] = ema(self.close[sym], 50)
        return self._ema50[sym]

    def vol(self, sym: str) -> list[float | None]:
        if sym not in self._vol:
            self._vol[sym] = vol60(self.adj[sym])
        return self._vol[sym]

    def breadth(self) -> list[float | None]:
        """Share of that day's index members closing above their own 50-day average."""
        if self._breadth is None:
            sm = {s: sma(self.close[s], 50) for s in self.close}
            out: list[float | None] = []
            for t, d in enumerate(self.dates):
                up = n = 0
                for s in self.members_on(d):
                    c, m = (self.close.get(s) or [None] * (t + 1))[t], (sm.get(s) or [None] * (t + 1))[t]
                    if c is not None and m is not None:
                        n += 1
                        up += c > m
                out.append(up / n if n >= 20 else None)
            self._breadth = out
        return self._breadth

    def momentum_ranking(self, t: int) -> list[str]:
        """The screen's own ranking (screen.score_universe) on data up to day t: eligible names, best first."""
        if t not in self._rank:
            from .momentum import momentum_stats
            from .screen import _vol, score_universe
            day = self.dates[t]
            stats = {}
            for sym in self.members_on(day):
                bars = self.raw.get(sym)
                if not bars:
                    continue
                i = bisect_right(self.raw_dates[sym], day)
                if i < MIN_BARS:
                    continue
                window = bars[:i]
                st = momentum_stats(window)
                st["vol_60d"] = _vol(window)
                stats[sym] = st
            ranked = score_universe(stats, min_turnover=self.min_turnover)
            self._rank[t] = [r["symbol"] for r in ranked if r["eligible"]]
            self._scores[t] = {r["symbol"]: r["score"] for r in ranked if r["eligible"]}
            self._stats[t] = stats
            self._rank_detail[t] = {s: i + 1 for i, s in enumerate(self._rank[t])}
        return self._rank[t]

    def momentum_scores(self, t: int) -> dict[str, float]:
        """The screen's composite score of each ELIGIBLE name on day t (trend and liquidity filters passed)."""
        self.momentum_ranking(t)
        return self._scores[t]

    def stats_on(self, t: int) -> dict[str, dict[str, Any]]:
        """momentum_stats of every ranked member on day t (6-month return, ...)."""
        self.momentum_ranking(t)
        return self._stats[t]

    def rank_of(self, t: int, sym: str) -> int | None:
        self.momentum_ranking(t)
        return self._rank_detail[t].get(sym)

    def rebalance_days(self, start: int, end: int, every: str = "monthly", month: int = 1) -> list[int]:
        """Month starts; every second or third month start (``bimonthly`` / ``quarterly``); or with ``every="yearly"``
        the start day plus the first trading day of ``month`` each year."""
        out, seen = [], set()
        for t in range(start, end + 1):
            d = self.dates[t]
            if every == "yearly":
                y, mth = int(d[:4]), int(d[5:7])
                key = str(y if mth >= month else y - 1)
            elif every in ("bimonthly", "quarterly"):   # every 2 / 3 calendar months (Jan, Mar, ... / Jan, Apr, ...)
                k = 2 if every == "bimonthly" else 3
                key = f"{d[:4]}-{(int(d[5:7]) - 1) // k}"
            else:
                key = d[:7]
            if key not in seen:
                seen.add(key)
                out.append(t)
        return out


def load_market(members: list[str], prices: Any, *, regime: str, years: int, until: str | None,
                membership: Any | None = None, workers: int = 8, flows: dict[str, float] | None = None) -> Market:
    """Fetch the regime fund and every symbol ever in the index during the window (plus a lookback year)."""
    since_until = 0
    if until:
        since_until = max(0, math.ceil((date.today() - date.fromisoformat(until[:10])).days / 365.25))
    need = years + 1 + since_until
    rng = "2y" if need <= 2 else "5y" if need <= 5 else "10y" if need <= 10 else "max"
    reg = prices.history(regime, rng)
    calendar = [b["date"] for b in reg if not until or b["date"] < until]
    reg = [b for b in reg if not until or b["date"] < until]
    universe = sorted(membership.ever_members(calendar[0])) if membership is not None and calendar else members

    def load(sym: str) -> tuple[str, list[dict[str, Any]] | None]:
        try:
            bars = prices.history(sym, rng)
            return sym, [b for b in bars if not until or b["date"] < until]
        except Exception:  # noqa: BLE001 - a missing stock is reported, not fatal
            return sym, None

    with ThreadPoolExecutor(max_workers=workers) as ex:
        series = {s: b for s, b in ex.map(load, universe) if b}
    members_on = (lambda d: membership.members_on(d)) if membership is not None else (lambda d: set(members))
    m = Market(calendar, series, reg, members_on, flows=flows)
    m.missing = sorted(set(universe) - set(series))
    m.point_in_time = membership is not None
    if until:   # the test starts ``years`` before the cutoff; earlier bars are only the ranking's lookback
        end = date.fromisoformat(until[:10])
        m.start_day = max(m.start_day, end.replace(year=end.year - years).isoformat())
    return m


# --------------------------------------------------------------------------- #
# the simulation
# --------------------------------------------------------------------------- #
@dataclass
class Position:
    value: float
    cost: float                # rupees paid (cost basis)
    entry_t: int
    entry_px: float
    entry_atr: float | None
    high: float
    bought: str                # date of the first buy (holding period for tax)
    breakeven: bool = False
    grandfathered: float | None = None   # value on 31 Jan 2018 for shares held then (plus later top-ups at cost)


@dataclass
class TaxBook:
    st: float = 0.0
    lt: float = 0.0
    paid: float = 0.0
    by_fy: dict[str, float] = field(default_factory=dict)


def fy_of(day: str) -> str:
    y, m = int(day[:4]), int(day[5:7])
    return f"FY{y + 1 if m >= 4 else y}"


def tax_due(st: float, lt: float, day: str) -> float:
    """Tax for a financial year ending on ``day``: 20% / 12.5% above Rs 1.25 lakh from 23 Jul 2024; 15% / 10% above
    Rs 1 lakh before; and before 1 Apr 2018 long-term gains on listed shares were exempt (short-term 15%)."""
    old = day < TAX_CHANGE
    st_rate, lt_rate, exempt = (0.15, 0.10, 100_000.0) if old else (0.20, 0.125, 125_000.0)
    if day < LTCG_START:
        lt_rate = 0.0
    if st < 0:              # a short-term loss is set off against long-term gains too
        lt, st = lt + st, 0.0
    return st_rate * max(st, 0.0) + lt_rate * max(lt - exempt, 0.0)


def inverse_vol_weights(vols: dict[str, float | None], w_min: float, w_max: float) -> dict[str, float]:
    """Weights proportional to 1/vol, each clipped to [w_min, w_max] of the invested part (water-filling); names
    without a volatility get the average weight. Sums to at most 1."""
    names = list(vols)
    if not names:
        return {}
    inv = {s: 1 / v for s, v in vols.items() if v and v > 0}
    avg = statistics.fmean(inv.values()) if inv else 1.0
    raw = {s: inv.get(s, avg) for s in names}
    w = {s: raw[s] / sum(raw.values()) for s in names}
    fixed: dict[str, float] = {}
    for _ in range(len(names) + 1):
        free = [s for s in names if s not in fixed]
        left = 1.0 - sum(fixed.values())
        tot = sum(raw[s] for s in free)
        w = {**fixed, **{s: left * raw[s] / tot for s in free}} if free and tot else dict(fixed)
        out = [s for s in free if w[s] > w_max or w[s] < w_min]
        if not out:
            break
        for s in out:
            fixed[s] = min(max(w[s], w_min), w_max)
    return w


def capped_picks(ranked: list[str], keep: list[str], slots: int, top: int, cap: int,
                 industries: dict[str, str]) -> list[str]:
    """New buys as the buffer rule picks them (from the top ``top`` names not already held), except that a name whose
    industry already has ``cap`` holdings is skipped and the next name in the ranking takes its slot."""
    counts: dict[str, int] = {}
    for s in keep:
        if industries.get(s):
            counts[industries[s]] = counts.get(industries[s], 0) + 1
    new: list[str] = []
    skipped = 0
    for i, s in enumerate(ranked):
        if len(new) >= slots or i >= top + skipped:
            break
        if s in keep:
            continue
        ind = industries.get(s)
        if ind and counts.get(ind, 0) >= cap:
            skipped += 1
            continue
        new.append(s)
        if ind:
            counts[ind] = counts.get(ind, 0) + 1
    return new


def stop_triggered(m: Market, sym: str, p: Position, t: int, rule: str) -> bool:
    """Does ``rule`` sell position ``p`` at day t's close? Updates the position's high and break-even flag."""
    c = m.close[sym][t]
    a = m.atr(sym)[t]
    if c is None:
        return False
    p.high = max(p.high, c)
    if rule == "none":
        return False
    if rule in ("atr3", "atr5", "grace20"):
        if rule == "grace20" and t - p.entry_t < 20:
            return False
        mult = 5.0 if rule == "atr5" else 3.0
        return a is not None and c < p.high - mult * a
    if rule == "ema50":
        e = m.ema50(sym)[t]
        return e is not None and c < e
    if rule == "two_stage":
        ea = p.entry_atr or a
        if ea is None:
            return False
        risk = 2.5 * ea
        if c >= p.entry_px + 1.5 * risk:
            p.breakeven = True
        level = p.entry_px - risk
        if p.breakeven:
            level = max(p.entry_px, p.high - 3.5 * (a or ea))
        return c < level
    raise ValueError(f"unknown stop rule {rule}")


def simulate(m: Market, rules: Rules, start: int, end: int, *, capital: float = 500_000.0,
             cost_model: Any | None = None, ranker: Callable[[Market, int], list[str]] | None = None,
             industries: dict[str, str] | None = None) -> dict[str, Any]:
    """Run ``rules`` from day ``start`` to day ``end`` (indices into ``m.dates``, inclusive). ``industries`` maps a
    symbol to its industry for ``rules.industry_cap`` (names with no industry are never capped)."""
    rank = ranker or (lambda mk, t: mk.momentum_ranking(t))
    rebal = set(m.rebalance_days(start, end, rules.rebalance, rules.rebalance_month))
    daily_rate = (1 + rules.liquid_rate) ** (1 / 365.0) - 1
    cash = capital
    pos: dict[str, Position] = {}
    curve, exposure_log, trades, events = [], [], [], {"stops": 0, "brake_trades": 0, "tax_waits": 0, "floors": 0}
    costs = traded = 0.0
    traded_cum: list[float] = []   # traded notional so far, at each day's close (turnover by period)
    fills: list[tuple[int, str, float]] = []   # (day, symbol, notional) of every buy and sell (capacity)
    held: list[tuple[int, list[str]]] = []      # (decision day, holdings after that rebalance) (macro exposure)
    tax = TaxBook()
    stopped: set[str] = set()
    exposure = 1.0
    breadth = m.breadth() if rules.brake == "ema_breadth" else None
    low_breadth = 0

    def charge(side: str, notional: float) -> float:
        nonlocal costs, traded
        if notional <= 0:
            return 0.0
        traded += notional
        c = cost_model.breakdown(side, notional)["total"] * rules.cost_mult if cost_model is not None else 0.0
        costs += c
        return c

    def sell(sym: str, t: int, frac: float, reason: str) -> None:
        nonlocal cash
        p = pos[sym]
        proceeds = p.value * frac
        cash += proceeds - charge("sell", proceeds)
        fills.append((t, sym, proceeds))
        basis = p.cost * frac
        if rules.tax:
            held = (date.fromisoformat(m.dates[t]) - date.fromisoformat(p.bought)).days
            if held > 365:
                tax_basis = basis
                if p.grandfathered is not None and m.dates[t] >= LTCG_START:   # cost = max(cost, min(31 Jan 2018 value, sale))
                    tax_basis = max(basis, min(p.grandfathered * frac, proceeds))
                tax.lt += proceeds - tax_basis
            else:
                tax.st += proceeds - basis
        if frac >= 0.999999:
            c = m.close[sym][t]
            trades.append({"symbol": sym, "entry": m.dates[p.entry_t], "exit": m.dates[t], "days": t - p.entry_t,
                           "return": (c / p.entry_px - 1) if c and p.entry_px else None, "reason": reason})
            del pos[sym]
        else:
            p.value -= proceeds
            p.cost -= basis
            if p.grandfathered is not None:
                p.grandfathered *= 1 - frac

    def buy(sym: str, t: int, amount: float) -> None:
        nonlocal cash
        if amount <= 1.0:
            return
        c = charge("buy", amount)
        cash -= amount + c
        fills.append((t, sym, amount))
        px = m.close[sym][t] or 0.0
        if sym in pos:
            p = pos[sym]
            p.value += amount
            p.cost += amount + c
            gf = p.grandfathered
            if gf is not None:   # a later top-up keeps its own cost
                p.grandfathered = gf + amount + c
        else:
            a = m.atr(sym)[t]
            pos[sym] = Position(value=amount, cost=amount + c, entry_t=t, entry_px=px, entry_atr=a, high=px,
                                bought=m.dates[t])

    def stop_hit(sym: str, p: Position, t: int) -> bool:
        return stop_triggered(m, sym, p, t, rules.stop)

    def brake_level(t: int) -> float:
        nonlocal low_breadth
        if rules.brake == "none":
            return 1.0
        c = m.fund_close[t]
        if c is None:
            return exposure
        if rules.brake == "sma100":
            s = m.fund_sma100[t]
            return 1.0 if s is None or c >= s else 0.0
        e50, e200 = m.fund_ema50[t], m.fund_ema200[t]
        level = 1.0 if e50 is None or c >= e50 else (0.5 if e200 is None or c >= e200 else 0.0)
        if rules.brake == "ema_breadth" and breadth is not None:
            b = breadth[t]
            low_breadth = low_breadth + 1 if b is not None and b < rules.breadth_floor else 0
            if low_breadth >= rules.breadth_days:
                level = max(0.0, level - 0.5)
        if rules.brake == "ema_flows":
            net = [m.flows.get(m.dates[k]) for k in range(max(0, t - 4), t + 1)]
            if len(net) == 5 and all(x is not None and x < 0 for x in net):
                level = max(0.0, level - 0.5)
        return level

    def weights_for(names: list[str], t: int) -> dict[str, float]:
        if rules.weights == "inverse_vol" and len(names) >= rules.min_names_for_ivol:
            return inverse_vol_weights({s: m.vol(s)[t] for s in names}, rules.w_min, rules.w_max)
        if rules.weights == "equal_picks" and names:
            return {s: 1 / len(names) for s in names}
        return {s: 1 / rules.top for s in names}

    def below_floor(sym: str, p: Position, d: int, t: int) -> bool:
        """Was the holding at least ``rules.floor`` below its cost on day d's close (d = t when fills are same-day)?"""
        if not rules.floor or p.cost <= 0:
            return False
        a_d, a_t = m.adj[sym][d], m.adj[sym][t]
        if not a_d or not a_t:
            return False
        return p.value * a_d / a_t <= (1 - rules.floor) * p.cost

    last_fy = fy_of(m.dates[start])
    grandfather_done = m.dates[start] > GRANDFATHER_DAY   # windows that start after 31 Jan 2018 never need it
    for t in range(start, end + 1):
        day = m.dates[t]
        d = t - rules.lag   # the decision day: everything decided on d's close is traded at t's close
        # 1. mark to market and accrue interest
        if t > start:
            for sym, p in pos.items():
                a0, a1 = m.adj[sym][t - 1], m.adj[sym][t]
                if a0 and a1:
                    p.value *= a1 / a0
            cash *= (1 + daily_rate) ** ((date.fromisoformat(day) - date.fromisoformat(m.dates[t - 1])).days)
        # shares held on 31 Jan 2018 keep that day's value as their minimum cost for long-term gains
        if rules.tax and not grandfather_done and day >= GRANDFATHER_DAY:
            grandfather_done = True
            for sym, p in pos.items():
                if p.bought <= GRANDFATHER_DAY:
                    a0, a1 = m.adj[sym][t - 1] if t > start else None, m.adj[sym][t]
                    on_day = day == GRANDFATHER_DAY or not (a0 and a1)
                    p.grandfathered = p.value if on_day else p.value * a0 / a1   # type: ignore[operator]
        # tax for the financial year that just ended is paid on its last day
        if rules.tax and fy_of(day) != last_fy:
            due = tax_due(tax.st, tax.lt, m.dates[t - 1])
            cash -= due
            tax.paid += due
            tax.by_fy[last_fy] = due
            tax.st = tax.lt = 0.0
            last_fy = fy_of(day)
        # 2. stops and the floor, decided on day d's close
        if d >= start:
            for sym in [s for s, p in pos.items() if p.entry_t <= d and stop_hit(s, p, d)]:
                sell(sym, t, 1.0, "stop")
                stopped.add(sym)
                events["stops"] += 1
            for sym in [s for s, p in pos.items() if p.entry_t <= d and below_floor(s, p, d, t)]:
                sell(sym, t, 1.0, "floor")
                stopped.add(sym)
                events["floors"] += 1
        # 3. brake
        new_level = brake_level(d) if d >= start else exposure
        if new_level != exposure:
            total = cash + sum(p.value for p in pos.values())
            invested = sum(p.value for p in pos.values())
            target = new_level * total
            if invested > 0 and target < invested:
                frac = 1 - target / invested
                for sym in list(pos):
                    sell(sym, t, frac, "brake")
                events["brake_trades"] += 1
            elif invested > 0 and target > invested:
                room = min(target - invested, cash)
                for sym, p in list(pos.items()):
                    buy(sym, t, room * p.value / invested)
                events["brake_trades"] += 1
        exposure = new_level
        # 4. rebalance
        if d in rebal and d >= start and t < end:
            stopped.clear()
            ranked = rank(m, d)
            rank_pos = {s: i + 1 for i, s in enumerate(ranked)}
            total = cash + sum(p.value for p in pos.values())
            if rules.buffer:
                keep = [s for s in pos if rank_pos.get(s, 10 ** 6) <= rules.buffer]
                if rules.tax_aware:
                    for s in [s for s in pos if s not in keep]:
                        p = pos[s]
                        age = (date.fromisoformat(m.dates[d]) - date.fromisoformat(p.bought)).days
                        if 335 <= age <= 365 and p.value > p.cost and rank_pos.get(s, 10 ** 6) <= rules.tax_aware_floor:
                            keep.append(s)
                            events["tax_waits"] += 1
                for s in [s for s in list(pos) if s not in keep]:
                    sell(s, t, 1.0, "rank")
                slots = rules.top - len(keep)
                if rules.industry_cap and industries is not None:
                    new = capped_picks(ranked, keep, slots, rules.top, rules.industry_cap, industries)
                else:
                    new = [s for s in ranked[:rules.top] if s not in keep][:max(slots, 0)]
                w = weights_for(keep + new, t)
                for s in new:
                    buy(s, t, min(cash, exposure * total * w.get(s, 1 / rules.top)))
            else:
                picks = ranked[:rules.top]
                for s in [s for s in list(pos) if s not in picks]:
                    sell(s, t, 1.0, "rank")
                w = weights_for(picks, t)
                total = cash + sum(p.value for p in pos.values())
                for s in picks:
                    target = exposure * total * w.get(s, 0.0)
                    cur = pos[s].value if s in pos else 0.0
                    if target < cur - 1.0:
                        sell(s, t, (cur - target) / cur, "reweight")
                for s in picks:
                    target = exposure * total * w.get(s, 0.0)
                    cur = pos[s].value if s in pos else 0.0
                    if target > cur + 1.0:
                        buy(s, t, min(cash, target - cur))
        if d in rebal and d >= start and t < end:
            held.append((d, sorted(pos)))
        total = cash + sum(p.value for p in pos.values())
        curve.append(total)
        traded_cum.append(traded)
        exposure_log.append(sum(p.value for p in pos.values()) / total if total else 0.0)
    if rules.tax:   # the open financial year's realised gains
        due = tax_due(tax.st, tax.lt, m.dates[end])
        tax.paid += due
        tax.by_fy[last_fy] = due
        curve[-1] -= due
    open_days = [end - p.entry_t for p in pos.values()]
    return {"rules": rules, "label": rules.label(), "dates": m.dates[start:end + 1], "curve": curve,
            "exposure": exposure_log, "trades": trades, "open_days": open_days, "costs": costs, "traded": traded,
            "traded_cum": traded_cum, "fills": fills, "held": held,
            "tax_paid": tax.paid, "tax_by_fy": tax.by_fy, "events": events, "capital": capital}


def fund_curve(m: Market, start: int, end: int, capital: float = 500_000.0) -> list[float | None]:
    a0 = m.fund_adj[start]
    return [capital * a / a0 if a and a0 else None for a in m.fund_adj[start:end + 1]]


def stats(curve: list[float | None], dates: list[str]) -> dict[str, Any]:
    vals = [v for v in curve if v is not None]
    if len(vals) < 2 or vals[0] <= 0:
        return {"total_return": None, "cagr": None, "max_drawdown": None, "volatility": None}
    rets = [vals[i] / vals[i - 1] - 1 for i in range(1, len(vals)) if vals[i - 1] > 0]
    years = max((date.fromisoformat(dates[-1]) - date.fromisoformat(dates[0])).days, 1) / 365.25
    peak, mdd = vals[0], 0.0
    for v in vals:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    return {"total_return": vals[-1] / vals[0] - 1,
            "cagr": (vals[-1] / vals[0]) ** (1 / years) - 1 if vals[-1] > 0 else -1.0,
            "max_drawdown": mdd, "volatility": statistics.pstdev(rets) * math.sqrt(252) if len(rets) > 1 else None}


def monthly(curve: list[float | None], dates: list[str]) -> list[float | None]:
    """The curve at each month's first trading day plus the last day (for the deflated Sharpe on monthly data)."""
    out, seen = [], set()
    for v, d in zip(curve, dates):
        if d[:7] not in seen:
            seen.add(d[:7])
            out.append(v)
    out.append(curve[-1])
    return out


def summarize(run: dict[str, Any], m: Market, start: int, end: int) -> dict[str, Any]:
    """The numbers every study compares: returns, worst fall, volatility, turnover, holding periods, costs, tax."""
    dates = run["dates"]
    s = stats(run["curve"], dates)
    f = stats(fund_curve(m, start, end, run["capital"]), dates)
    years = max((date.fromisoformat(dates[-1]) - date.fromisoformat(dates[0])).days, 1) / 365.25
    avg = statistics.fmean(run["curve"]) if run["curve"] else 0.0
    held = [tr["days"] for tr in run["trades"]] + run["open_days"]
    return {**s, "fund_total_return": f["total_return"], "fund_max_drawdown": f["max_drawdown"],
            "excess": (s["total_return"] - f["total_return"]) if s["total_return"] is not None
            and f["total_return"] is not None else None,
            "turnover": run["traded"] / avg / years / 2 if avg else None,   # one-way, a year
            "median_hold_days": statistics.median(held) if held else None,
            "completed_trades": len(run["trades"]), "stops": run["events"]["stops"],
            "floors": run["events"].get("floors", 0),
            "brake_trades": run["events"]["brake_trades"], "tax_waits": run["events"]["tax_waits"],
            "costs": run["costs"], "tax_paid": run["tax_paid"],
            "avg_invested": statistics.fmean(run["exposure"]) if run["exposure"] else None}


def with_rules(base: Rules, **kw: Any) -> Rules:
    return replace(base, **kw)
