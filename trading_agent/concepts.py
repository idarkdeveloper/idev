"""Concept of the day: a fixed, human-written library of short explanations for the evening bulletin.

Nothing here is generated or advice: each entry says what a term means, and one line says where this agent uses it
or where you see it in the emails. ``concept_for(day)`` rotates through the library by trading-day number, and on
Fridays gives a longer "concept of the week". Options terms (PCR) are explained as definitions only.
"""

from __future__ import annotations

from datetime import date
from typing import Any

# (title, explanation, "how this agent uses it / where you see it")
CONCEPTS: list[tuple[str, str, str]] = [
    ("Support and resistance",
     "Support is a price area where a stock or index has stopped falling before; resistance is an area where it has "
     "stopped rising before. They are drawn from past turning points, so they describe history. Prices often pass "
     "through them.",
     "The bulletin lists the nearest swing low below and swing high above the close as watch levels."),
    ("EMA (exponential moving average)",
     "An EMA is an average of recent prices that gives more weight to the latest ones, so it reacts faster than a "
     "simple average. A 21-period EMA on 15-minute bars covers roughly the last day of trading.",
     "The 15-minute Nifty chart draws the 21 EMA and the text says what share of the session closed above it."),
    ("ADX (trend strength)",
     "ADX measures how strong a trend is, not its direction. Below 20 is usually read as weak or no trend, 20 to 25 as "
     "a trend developing, 25 to 40 as strong and above 40 as very strong. The +DI and -DI lines show which side is "
     "stronger.",
     "The Nifty block states the daily ADX(14) band and whether +DI or -DI is higher."),
    ("RSI (relative strength index)",
     "RSI compares the size of recent gains with recent losses on a scale of 0 to 100. Readings above 70 or below 30 "
     "are called stretched, but a strong trend can stay there for weeks.",
     "The Nifty block shows the daily RSI(14) next to the ADX."),
    ("ATR and position sizing",
     "ATR (average true range) is the typical daily distance a price moves, including gaps. Sizing a position from ATR "
     "means a volatile stock gets a smaller quantity than a calm one for the same amount of money at risk.",
     "The morning email sizes each idea so that a stop one ATR-based distance away risks about 1% of equity."),
    ("Stop-loss and trailing stop",
     "A stop-loss is a price at which a position is closed to limit a loss. A trailing stop follows the price up by a "
     "set distance and is never lowered, so it locks in part of a gain while leaving room for normal swings.",
     "The practice account closes a position when its stop is hit; the evening email lists any stop sells."),
    ("GTT orders",
     "A GTT (good till triggered) order waits at the broker until the market reaches a trigger price, then places the "
     "order. Unlike a normal order it does not expire at the end of the day.",
     "The agent can mirror its stops as GTT orders on Groww only when live orders are switched on in the settings."),
    ("The 200-day average and market regime",
     "The 200-day average is the mean close of the last 200 sessions. An index above it and rising is often called an "
     "uptrend regime; below it, a downtrend regime. It lags by design, so it changes its mind slowly.",
     "The morning mood and the World markets table use it for the UP, DOWN and mixed trend labels."),
    ("Momentum",
     "Momentum is the tendency of stocks that rose over the past months to keep outperforming for a while, and of "
     "laggards to keep lagging. It is a statistical tendency across many stocks, not a promise for any one of them.",
     "The morning screen ranks the index's stocks by their past 6-month return."),
    ("Drawdown",
     "Drawdown is the fall from a previous peak to a later low, shown as a percentage. A 20% drawdown needs a 25% rise "
     "to get back to the old peak, which is why large drawdowns are hard to recover from.",
     "The scorecard and backtest pages report the maximum drawdown of a strategy."),
    ("Bulk and block deals",
     "A bulk deal is a trade that is at least 0.5% of a company's listed shares in one day. A block deal is a large "
     "single trade of at least Rs 25 crore made in a special window. Exchanges publish both, naming the buyer and "
     "the seller.",
     "The emails list recent deals by the investors you follow."),
    ("Promoter and insider buying",
     "Promoters and insiders must disclose when they trade their own company's shares. Such trades show what insiders "
     "did, but not why: a purchase can be for many reasons and a sale can be for personal ones.",
     "The deals section can follow insider disclosures when that watch source is chosen."),
    ("PCR (put-call ratio), a definition",
     "PCR is the number of put options divided by the number of call options traded or open on an index. Some people "
     "read a high value as caution and a low value as optimism; others read it the opposite way. Options can lose "
     "their whole value quickly, and this agent does not trade them.",
     "This is explained only as a term; the bulletin does not use option data."),
    ("VIX (volatility index)",
     "India VIX is built from index option prices and shows how much movement the market expects over the next 30 "
     "days, as an annual percentage. It tends to rise when markets fall quickly and to be low when they are calm.",
     "The risk gauges show India VIX and US VIX and flag unusually high readings."),
    ("Rupee and FII flows",
     "FIIs (foreign institutional investors) buy and sell Indian shares in dollars. When they sell heavily, the rupee "
     "often weakens too, because they convert rupees back to dollars. Flows are one influence among many.",
     "The risk gauges include USD/INR and flag a new 1-year low for the rupee."),
    ("T+1 settlement",
     "On Indian exchanges a trade settles one working day after the trade day (T+1): shares reach your demat account "
     "and money moves the next day.",
     "That is why today's buys appear in your Groww holdings only from the next day."),
    ("STCG and LTCG tax",
     "Gains on listed shares sold within one year are short-term capital gains (STCG) and are taxed at a higher flat "
     "rate than long-term gains (LTCG) after one year. Rates and exemptions change with the Budget, so check the "
     "current ones.",
     "The taxes page of the app estimates both kinds for your sells."),
    ("Diversification",
     "Diversification means holding assets that do not all move together, so one bad outcome hurts less. Ten stocks in "
     "the same sector are less diversified than five in different sectors.",
     "The portfolio views show how much of the value each holding makes up."),
    ("Rebalancing",
     "Rebalancing is bringing a portfolio back to its target mix after prices moved it, for example selling part of "
     "what grew and adding to what lagged. It keeps risk near the level chosen at the start.",
     "The app shows the weights of your holdings so drift from a mix you chose is visible."),
    ("Slippage and limit orders",
     "Slippage is the gap between the price you expected and the price you got. A market order takes the best price "
     "available now; a limit order only trades at your price or better, and may not trade at all.",
     "The agent refuses an order whose price moved more than the slippage limit in the settings."),
    ("Circuit limits",
     "Exchanges set a daily band, such as 5%, 10% or 20%, beyond which a stock cannot trade that day. When it is hit, "
     "trading in that stock stops for the day or for a pause, so an exit is not always possible at the moment you want.",
     "Small and thin stocks hit circuits more often, which is one reason the screen asks for liquidity."),
    ("Pivot points",
     "Classic pivot points are computed from one session's high, low and close: P = (H + L + C) / 3, R1 = 2P - L, "
     "S1 = 2P - H, R2 = P + (H - L), S2 = P - (H - L). They are widely watched reference levels for the next session.",
     "The bulletin lists P, R1, R2, S1 and S2 from the day's Nifty candle."),
    ("Candlestick basics",
     "A candle shows a period's open, high, low and close. A marubozu has almost no shadows (a body of at least 90% of "
     "the range); a doji has a tiny body; a hammer has a long lower shadow; an engulfing candle's body covers the "
     "previous body.",
     "The bulletin names the pattern of the day's candle and of the two 4-hour candles by fixed rules."),
    ("Gap up and gap down",
     "A gap is when a session opens away from the previous close, with no trading in between. Gaps come from news "
     "or events outside market hours and are sometimes, but not always, followed by a move back to the old level.",
     "The Nifty block states the opening gap in points against the previous close."),
    ("Delivery versus intraday",
     "In delivery trading you take the shares into your demat account and can hold them as long as you like. In "
     "intraday trading the position must be closed the same day, otherwise it is squared off. The costs and tax "
     "treatment differ.",
     "This agent works with delivery holdings, not intraday trades."),
    ("Dividends and the ex-date",
     "A dividend is a share of profit paid per share. Only holders on the record date receive it, and the share "
     "price usually drops by about the dividend on the ex-date, the first day it trades without the dividend.",
     "Dividend dates can appear in company announcements that the emails check."),
    ("Results season",
     "Listed companies report quarterly results within 45 days of the quarter's end (60 days for the year-end quarter). Prices can move sharply on the "
     "day around a result, because it replaces estimates with actual numbers.",
     "The morning email flags a holding that has results due in the next few days."),
    ("Index funds versus stock picking",
     "An index fund holds all the stocks of an index at low cost and gets the index's return. Picking stocks tries to "
     "beat that return, and over long periods many professional pickers have not.",
     "The scorecard compares the agent's results with simply holding the Nifty."),
    ("Risk-off and risk-on",
     "Risk-on describes periods when investors favour shares and other risky assets; risk-off, when they move to "
     "safer ones such as bonds, gold or cash. The labels are summaries of behaviour after the fact.",
     "The morning mood gives a regime label from fixed rules over index trends and volatility."),
]

# Longer entries for Fridays: "Concept of the week" (same shape).
WEEKLY: list[tuple[str, str, str]] = [
    ("Support and resistance, in more detail",
     "Support and resistance are price areas, not exact prices. A level becomes more notable the more often price has "
     "turned there and the more volume traded around it. A swing low is a low with lower prices on neither side for "
     "a few bars; a swing high is the mirror image. When price breaks a resistance area and stays above it, many "
     "traders then treat it as support, and the reverse after a break below support. None of this is a rule of "
     "nature: levels are remembered by people, so they work only as far as people act on them, and price often "
     "moves through them. The bulletin therefore calls them watch levels and picks the nearest one at least 0.3% "
     "away from the close, so that a level the price is already sitting on is not shown.",
     "See the watch levels in the Nifty block, with the date of the swing each one comes from."),
    ("ADX and the DI lines, in more detail",
     "ADX (average directional index) is built from two lines, +DI and -DI, which compare today's highs and lows with "
     "yesterday's. When highs keep rising +DI grows; when lows keep falling -DI grows. ADX is a smoothed measure of "
     "the gap between the two, so it rises in any strong trend, up or down, and falls in a sideways market. "
     "Wilder's smoothing over 14 days makes it slow: it confirms a trend that is already under way and cannot tell "
     "you when a new one starts. A falling ADX from a high level means the trend is losing strength, not that it "
     "has reversed.",
     "The daily ADX(14) with +DI and -DI is in the Nifty block; the 4-hour chart draws it as a lower panel."),
    ("Position sizing and the 1% rule",
     "Position sizing decides how many shares to hold before deciding anything else. One common method fixes the "
     "amount you are willing to lose if the stop is hit, such as 1% of the account, and divides it by the distance "
     "between the entry and the stop: quantity = amount at risk / (entry - stop). A wider stop therefore means fewer "
     "shares. The method limits the loss on one idea, but it cannot protect against a gap that jumps over the stop "
     "or against many ideas falling together.",
     "The morning email sizes each buy idea this way and shows the quantity, cost and stop."),
    ("Market regimes",
     "A regime is the broad state of the market: a steady uptrend, a downtrend, or a choppy range. Strategies that "
     "follow trends tend to do well in steady regimes and to lose small amounts repeatedly in choppy ones. Simple "
     "regime filters, such as whether an index is above its 200-day average, try to name the state in plain terms. "
     "They are late by construction: the regime is only recognised after it has begun, and it is sometimes "
     "named wrongly at the turning points.",
     "The morning email's mood line and the World markets trends use this idea."),
    ("Drawdown and recovery",
     "A drawdown is measured from the highest value reached to the lowest value that followed. The arithmetic is "
     "uneven: after a fall of 10% you need a rise of 11% to recover, after 30% a rise of 43%, after 50% a rise of "
     "100%. This is why many strategies limit the size of any one loss, and why the maximum drawdown of a past "
     "backtest matters as much as its average return: it shows what holding through the worst stretch felt like.",
     "The backtest and scorecard pages show the maximum drawdown."),
    ("Bulk deals, block deals and what they tell you",
     "Exchanges publish large trades. A bulk deal is a day's total trade of at least 0.5% of a company's shares by "
     "one client; a block deal is a single trade of at least Rs 25 crore in a separate window with a narrow price "
     "band. The published record names the client and says whether it bought or sold. A large buyer is not a "
     "forecast: it can be a fund rebalancing, an exit by a promoter group, or a deal between two parties that "
     "agreed on a price earlier. The record is a fact about the past, which is how this agent uses it.",
     "The deals section lists these trades for the investors you follow."),
    ("Taxes on share gains",
     "In India, gains on listed shares are taxed as short-term if you sell within one year of buying and as "
     "long-term after that, at different rates, with a yearly exemption on long-term gains. Losses can be set off "
     "against gains of the same kind and carried forward for eight years if the return is filed on time. Selling "
     "just before the one-year mark can change the tax, but tax rules change with each Budget, so confirm the "
     "current ones with the official sources or a tax professional.",
     "The taxes page of the app estimates your gains and the tax on them."),
    ("Orders: market, limit and stop",
     "A market order trades now at the best price available, so the price is not fixed. A limit order fixes the "
     "price but not the trade: it fills only if the market reaches it. A stop order waits until a trigger price "
     "is touched and then becomes a market or limit order. In a fast market a stop-market order can fill well "
     "past its trigger, and a stop-limit order may not fill at all. Knowing which is which explains most "
     "surprises in fills.",
     "The agent checks a slippage limit before it places an order."),
]


def trading_day_number(day: date) -> int:
    """Weekdays (Mon-Fri) counted from Monday 2000-01-03, so the number goes up by one on each working day and
    is the same wherever and whenever it is asked. Exchange holidays are not known here and count as days."""
    start = date(2000, 1, 3)
    days = (day - start).days
    weeks, extra = divmod(days, 7)
    return weeks * 5 + min(extra, 5)


def concept_for(day: date) -> dict[str, Any]:
    """The concept for ``day``. Friday: the longer concept of the week (rotating by week); other days: one of the
    short entries, advancing one per trading day."""
    if day.weekday() == 4:
        week = trading_day_number(day) // 5
        title, text, uses = WEEKLY[week % len(WEEKLY)]
        return {"kind": "week", "label": "Concept of the week", "title": title, "text": text, "uses": uses}
    n = trading_day_number(day)
    title, text, uses = CONCEPTS[n % len(CONCEPTS)]
    return {"kind": "day", "label": "Concept of the day", "title": title, "text": text, "uses": uses}
