"""A plain-English summary of a replay so far: who is ahead, what each side holds, and why.

Rule-based, fixed templates. Built only from what is known at the replay clock: the equity curve,
the brokers' orders and positions (priced by the clocked source) and the stop log. It never reads
a price after the clock, so it is safe to show before the replay ends.
"""

from __future__ import annotations

from typing import Any

from .scorecard import _max_drawdown, _per_symbol

MAX_NAMES = 8


def _inr(v: float) -> str:
    """Rupees with Indian digit grouping, no sign: 2110500 -> ₹21,10,500."""
    s = f"{abs(round(v)):d}"
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts: list[str] = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts + [tail])
    return "₹" + s


def _pct(v: float, signed: bool = True) -> str:
    return f"{v * 100:+.1f}%" if signed else f"{abs(v) * 100:.1f}%"


def _names(symbols: list[str]) -> str:
    shown = symbols[:MAX_NAMES]
    more = len(symbols) - len(shown)
    return ", ".join(shown) + (f" +{more} more" if more > 0 else "")


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _result(name: str, ret: float, gain: float, verb_made: str = "made") -> str:
    if round(ret, 3) == 0:
        return f"{name} broke even ({_pct(ret)})"
    if ret > 0:
        return f"{name} {verb_made} {_pct(ret)} ({_inr(gain)})"
    return f"{name} lost {_pct(ret, False)} ({_inr(gain)})"


def _holding(p: Any) -> dict[str, Any]:
    value = p.market_value or 0.0
    cost = p.qty * p.avg_entry_price
    pl = p.unrealized_pl if p.unrealized_pl is not None else value - cost
    return {"symbol": p.symbol, "qty": p.qty, "value": round(value, 2), "pl": round(pl, 2),
            "pl_pct": (pl / cost) if cost else None}


def _best_worst(hs: list[dict[str, Any]]) -> str:
    one = lambda h: f"{h['symbol']} ({'+' if h['pl'] >= 0 else '-'}{_inr(h['pl'])}, {_pct(h['pl_pct'] or 0)})"  # noqa: E731
    ranked = sorted(hs, key=lambda h: h["pl"])
    if len(ranked) == 1:
        return f"only {one(ranked[0])}"
    return f"best {one(ranked[-1])}, worst {one(ranked[0])}"


def build_summary(trial: Any) -> dict[str, Any] | None:
    """The summary dict for the page, or None before the replay has taken a step."""
    eq = trial.data["equity"]
    if len(eq) < 2:
        return None
    cash0, ended = trial.data["cash"], bool(trial.data["ended"])
    fund = trial.data["benchmark"]
    label = {"you": "You", "agent": "the agent's rules", "nifty": fund}
    ret, gain, fall = {}, {}, {}
    for w in label:
        vals = [p[w] for p in eq]
        ret[w], gain[w], fall[w] = vals[-1] / cash0 - 1, vals[-1] - cash0, _max_drawdown(vals)

    # -- who won ---------------------------------------------------------------------------
    brokers = {w: trial.broker(w) for w in label}
    orders = {w: brokers[w].orders() for w in label}
    lines: list[dict[str, str]] = []
    r1 = lambda w: round(ret[w], 3)  # noqa: E731 - compare at the precision shown (0.1%)
    if not orders["you"]:
        verdict = ("You did not trade in this replay." if ended
                   else "You have not placed a trade yet, so your money is all cash.")
    else:
        beat = [label[w] for w in ("agent", "nifty") if r1("you") > r1(w)]
        tied = [label[w] for w in ("agent", "nifty") if r1("you") == r1(w)]
        behind = [label[w] for w in ("agent", "nifty") if r1("you") < r1(w)]
        if len(beat) == 2:
            verdict = "You beat both."
        elif len(behind) == 2:
            verdict = "You trailed both."
        elif len(tied) == 2:
            verdict = "You matched both."
        else:
            bits = []
            if beat:
                bits.append("beat " + " and ".join(beat))
            if tied:
                bits.append("matched " + " and ".join(tied))
            if behind:
                bits.append("trailed " + " and ".join(behind))
            verdict = "You " + " and ".join(bits) + "."
    lines.append({"label": "Who is ahead",
                  "text": f"{_result('You', ret['you'], gain['you'])}, {_result(label['agent'], ret['agent'], gain['agent'])}, "
                          f"{_result(label['nifty'], ret['nifty'], gain['nifty'])}. {verdict}"})

    # -- risk ------------------------------------------------------------------------------
    falls = {w: round(fall[w], 3) for w in label}
    if all(v == 0 for v in falls.values()):
        risk = "None of the three has fallen from its high yet."
    else:
        worst = min(falls.values())
        bumpy = [label[w] for w in label if falls[w] == worst]
        tail = (f"{bumpy[0][0].upper()}{bumpy[0][1:]} had the bumpiest ride." if len(bumpy) == 1
                else "The ride was about equally bumpy.")
        risk = (f"Worst fall from a high: you {_pct(fall['you'], False) if fall['you'] else 'none'}, "
                f"the agent's rules {_pct(fall['agent'], False) if fall['agent'] else 'none'}, "
                f"{fund} {_pct(fall['nifty'], False) if fall['nifty'] else 'none'}. {tail}")
    lines.append({"label": "Risk", "text": risk})

    # -- costs -----------------------------------------------------------------------------
    fees = {w: brokers[w].performance()["fees_paid"] for w in ("you", "agent")}

    def charge_note(w: str) -> str:
        c, g = fees[w], gain[w]
        if not c:
            return f"{_inr(0)} in charges"
        if g > 0:
            return (f"{_inr(c)} in charges (larger than the gain)" if c > g
                    else f"{_inr(c)} in charges ({c / g * 100:.0f}% of the gain)")
        return f"{_inr(c)} in charges, which added to the loss" if g < 0 else f"{_inr(c)} in charges"
    lines.append({"label": "Costs",
                  "text": f"You made {_plural(len(orders['you']), 'trade')} with {charge_note('you')}; the agent made "
                          f"{_plural(len(orders['agent']), 'trade')} with {charge_note('agent')}."})

    # -- holdings compared -----------------------------------------------------------------
    mine = [_holding(p) for p in brokers["you"].positions()]
    theirs = [_holding(p) for p in brokers["agent"].positions()]
    m_set, a_set = [h["symbol"] for h in mine], [h["symbol"] for h in theirs]
    both = sorted(set(m_set) & set(a_set))
    only_me, only_agent = sorted(set(m_set) - set(a_set)), sorted(set(a_set) - set(m_set))
    if not mine and not theirs:
        held = "Neither you nor the agent holds any stock right now (all cash)."
    else:
        parts = []
        if both:
            parts.append(f"in both: {_names(both)}")
        parts.append(f"only yours: {_names(only_me)}" if only_me else "only yours: none")
        parts.append(f"only the agent's: {_names(only_agent)}" if only_agent else "only the agent's: none")
        held = f"You hold {_plural(len(mine), 'stock')} and the agent holds {_plural(len(theirs), 'stock')}; " + "; ".join(parts) + "."
        for who, hs in (("Yours", mine), ("The agent's", theirs)):
            held += f" {who}: {_best_worst(hs)}." if hs else f" {who}: all cash."
    lines.append({"label": "Holdings compared", "text": held})

    def hit(w: str) -> str | None:
        pnl = _per_symbol(brokers[w])
        return f"{sum(1 for v in pnl.values() if v > 0)} of {len(pnl)} ({sum(1 for v in pnl.values() if v > 0) / len(pnl) * 100:.0f}%)" if pnl else None
    h_me, h_ag = hit("you"), hit("agent")
    if h_me or h_ag:
        if not orders["you"]:
            txt = (f"You have not traded yet; the agent: {h_ag} made money." if h_ag
                   else "You have not traded yet, and the agent holds nothing.")
        else:
            txt = (f"Of the stocks traded so far, {h_me or 'none'} made money for you and "
                   f"{h_ag or 'none'} for the agent (sold stocks count, charges included).")
        lines.append({"label": "Stocks that made money", "text": txt})

    # -- why they differ (facts only) ------------------------------------------------------
    why = []
    d = trial.data
    exact = d.get("counts_exact") is True and "rebalance_count" in d
    n_reb = d["rebalance_count"] if "rebalance_count" in d else max(len(d["rebalances"]) - 1, 0)
    if n_reb:
        why.append(f"the agent rebalanced {'' if exact else 'at least '}{_plural(n_reb, 'time')} since the start "
                   f"({_plural(len(orders['agent']), 'trade')} in all)")
    if "agent_stop_count" in d:
        n_stop, stop_exact = d["agent_stop_count"], exact
    else:
        n_stop, stop_exact = sum(1 for x in d.get("stops", []) if x.get("who") == "agent" and "error" not in x), False
    if n_stop:
        why.append(f"its trailing stop sold {'' if stop_exact else 'at least '}{_plural(n_stop, 'position')}")
    eq_you, eq_agent = eq[-1]["you"], eq[-1]["agent"]
    if mine and eq_you:
        top = max(mine, key=lambda h: h["value"])
        why.append(f"you hold {_plural(len(mine), 'stock')}, the largest ({top['symbol']}) being {top['value'] / eq_you * 100:.0f}% of your account")
    elif orders["you"]:
        why.append("you hold no stocks now")
    cash = {w: brokers[w].account().cash for w in ("you", "agent")}
    why.append(f"cash is {cash['you'] / eq_you * 100:.0f}% of your account and {cash['agent'] / eq_agent * 100:.0f}% of the agent's"
               if eq_you and eq_agent else "")
    why = [w for w in why if w]
    if why:
        lines.append({"label": "What differed", "text": "; ".join(why)[0].upper() + "; ".join(why)[1:] + "."})

    # -- bottom line -----------------------------------------------------------------------
    if not orders["you"]:
        bottom = (f"You did not trade in this replay; the agent's rules {'made' if ret['agent'] >= 0 else 'lost'} {_pct(ret['agent'], False)}."
                  if ended else "Place a trade of your own and this will compare your picks with the agent's.")
    elif r1("you") <= 0 and r1("agent") <= 0:
        bottom = "Neither your picks nor the agent's rules are in profit over this period; one period is not proof of how either would do over time."
    elif r1("you") > r1("agent") and r1("you") >= r1("nifty"):
        bottom = "Your picks did better over this period; one period is not proof of skill."
    elif r1("agent") > r1("you"):
        bottom = ("The agent's rules did better than your picks over this period; one period is not proof either way."
                  if r1("agent") >= r1("nifty") else
                  f"The {fund} fund did better than both of you; one period is not proof either way.")
    elif r1("nifty") > r1("you"):
        bottom = f"The {fund} fund did better than your picks over this period; one period is not proof either way."
    else:
        bottom = "Your picks and the agent's rules finished level over this period; one period is not proof either way."

    # -- holdings table ---------------------------------------------------------------------
    by_me, by_ag = {h["symbol"]: h for h in mine}, {h["symbol"]: h for h in theirs}
    table = [{"symbol": s, "you": by_me.get(s), "agent": by_ag.get(s)} for s in
             sorted(set(by_me) | set(by_ag), key=lambda s: -((by_me.get(s) or {}).get("value", 0) + (by_ag.get(s) or {}).get("value", 0)))]
    return {"title": "Final summary" if ended else "Summary so far", "as_of": trial.clock.today, "ended": ended,
            "lines": lines, "bottom": bottom, "table": table}
