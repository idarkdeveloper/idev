"""PNG charts for the evening bulletin (matplotlib, Agg backend: no window, no GUI).

``nifty_intraday_png`` draws the last session's 15-minute candles with the 21 EMA, the previous close and the watch
levels; ``nifty_4h_png`` draws the 4-hour candles with an ADX panel underneath. Both are 640x360 pixels in the dark
colours of the email header, use only the font that ships with matplotlib, and stay under MAX_BYTES. They return
``None`` when there is nothing to draw. matplotlib is imported when the first chart is made, so the rest of the app
does not need it.
"""

from __future__ import annotations

import io
from datetime import datetime
from typing import Any

WIDTH, HEIGHT, DPI = 640, 360, 100
MAX_BYTES = 120 * 1024
BG, PANEL, FG, MUTED, GRID = "#111827", "#111827", "#e5e7eb", "#9ca3af", "#1f2937"
UP, DOWN, EMA, PREV, LEVEL, ADX = "#34d399", "#f87171", "#fbbf24", "#9ca3af", "#60a5fa", "#fbbf24"


def _plt() -> Any:
    import matplotlib
    matplotlib.use("Agg", force=True)
    matplotlib.rcParams.update({"font.family": "DejaVu Sans", "font.size": 8, "axes.unicode_minus": True,
                                "svg.hashsalt": "bulletin"})
    import matplotlib.pyplot as plt
    return plt


def _style(ax: Any) -> None:
    ax.set_facecolor(PANEL)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=MUTED, labelsize=7, length=2)
    ax.grid(True, color=GRID, linewidth=0.5)
    ax.set_axisbelow(True)


def _candles(ax: Any, bars: list[dict[str, Any]], width: float = 0.6) -> None:
    for i, b in enumerate(bars):
        colour = UP if b["close"] >= b["open"] else DOWN
        ax.vlines(i, b["low"], b["high"], color=colour, linewidth=0.8, zorder=2)
        lo, hi = sorted((b["open"], b["close"]))
        ax.bar(i, max(hi - lo, (b["high"] - b["low"]) * 0.005 or 0.01), bottom=lo, width=width, color=colour, zorder=3)


def _time_label(ts: str, with_date: bool = False) -> str:
    dt = datetime.fromisoformat(ts)
    return f"{dt.day} {dt:%b} {dt:%H:%M}" if with_date else f"{dt:%H:%M}"


def _png(fig: Any, plt: Any) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=DPI, facecolor=BG, metadata={"Software": None})
    plt.close(fig)
    data = buf.getvalue()
    if len(data) > MAX_BYTES:   # very busy charts: fewer colours, same size
        from PIL import Image
        im = Image.open(io.BytesIO(data)).convert("RGB").quantize(colors=48)
        out = io.BytesIO()
        im.save(out, format="PNG", optimize=True)
        data = out.getvalue()
    return data


def nifty_intraday_png(bars15: list[dict[str, Any]] | None, ema21: list[float | None] | None,
                       levels: list[dict[str, Any]] | None, prev_close: float | None) -> bytes | None:
    """15-minute candles of the last session, the 21 EMA (gaps where it is not defined), a dotted previous-close line
    and labelled horizontal watch levels ({label, value}). None when there are no bars."""
    if not bars15:
        return None
    plt = _plt()
    fig = plt.figure(figsize=(WIDTH / DPI, HEIGHT / DPI), dpi=DPI, facecolor=BG)
    ax = fig.add_axes([0.085, 0.12, 0.80, 0.76])
    _style(ax)
    _candles(ax, bars15)
    n = len(bars15)
    if ema21:
        pts = [(i, v) for i, v in enumerate(ema21[:n]) if v is not None]
        if pts:
            ax.plot([p[0] for p in pts], [p[1] for p in pts], color=EMA, linewidth=1.2, zorder=4, label="21 EMA")
    lows = [b["low"] for b in bars15]
    highs = [b["high"] for b in bars15]
    lo_y, hi_y = min(lows), max(highs)
    span = hi_y - lo_y or 1.0
    if prev_close and _in_view([prev_close], lo_y, hi_y, span):
        ax.axhline(prev_close, color=PREV, linestyle=":", linewidth=1.0, zorder=1)
        ax.annotate(f"prev close {prev_close:,.0f}", (n - 0.5, prev_close), xytext=(4, 0), textcoords="offset points",
                    color=PREV, fontsize=7, va="center", annotation_clip=False)
    for lv in levels or []:
        v = lv.get("value")
        if not _in_view([v], lo_y, hi_y, span):
            continue
        ax.axhline(v, color=LEVEL, linestyle=(0, (1, 3)), linewidth=1.0, zorder=1)
        ax.annotate(f"{lv.get('label', '')} {v:,.0f}", (n - 0.5, v), xytext=(4, 0), textcoords="offset points",
                    color=LEVEL, fontsize=7, va="center", annotation_clip=False)
    pad = span * 0.08
    ticks = _in_view([prev_close] + [lv.get("value") for lv in levels or []], lo_y, hi_y, span)
    ax.set_ylim(min([lo_y] + ticks) - pad, max([hi_y] + ticks) + pad)
    ax.set_xlim(-1, n)
    step = max(1, n // 6)
    ax.set_xticks(range(0, n, step))
    ax.set_xticklabels([_time_label(bars15[i]["ts"]) for i in range(0, n, step)])
    ax.yaxis.set_major_formatter(lambda v, _p: f"{v:,.0f}")
    fig.text(0.085, 0.94, "Nifty 50, 15-minute candles, 21 EMA", color=FG, fontsize=9, fontweight="bold")
    return _png(fig, plt)


def _in_view(values: list[Any], lo: float, hi: float, span: float) -> list[float]:
    """Level values close enough to the candles to be worth stretching the axis for."""
    return [v for v in values if v is not None and lo - 0.6 * span <= v <= hi + 0.6 * span]


def nifty_4h_png(bars4h: list[dict[str, Any]] | None, adx_series: dict[str, list[float | None]] | None) -> bytes | None:
    """4-hour candles with an ADX(14) panel (ADX, +DI and -DI) underneath. The ADX panel is left out when the series
    is missing or empty. None when there are no candles."""
    if not bars4h:
        return None
    plt = _plt()
    n = len(bars4h)
    series = adx_series or {}
    has_adx = any(v is not None for v in (series.get("adx") or []))
    fig = plt.figure(figsize=(WIDTH / DPI, HEIGHT / DPI), dpi=DPI, facecolor=BG)
    if has_adx:
        ax = fig.add_axes([0.085, 0.36, 0.89, 0.54])
        ax2 = fig.add_axes([0.085, 0.10, 0.89, 0.20], sharex=ax)
    else:
        ax = fig.add_axes([0.085, 0.12, 0.89, 0.78])
        ax2 = None
    _style(ax)
    _candles(ax, bars4h, 0.65)
    ax.set_xlim(-1, n)
    ax.yaxis.set_major_formatter(lambda v, _p: f"{v:,.0f}")
    step = max(1, n // 4)
    ticks = list(range(0, n, step))
    target = ax2 or ax
    if ax2 is not None:
        _style(ax2)
        for key, colour, width in (("adx", ADX, 1.3), ("plus_di", UP, 0.8), ("minus_di", DOWN, 0.8)):
            pts = [(i, v) for i, v in enumerate((series.get(key) or [])[:n]) if v is not None]
            if pts:
                ax2.plot([p[0] for p in pts], [p[1] for p in pts], color=colour, linewidth=width,
                         label={"adx": "ADX", "plus_di": "+DI", "minus_di": "−DI"}[key])
        ax2.axhline(25, color=MUTED, linestyle=":", linewidth=0.8)
        ax2.set_ylim(0, max(50.0, max(v for k in ("adx", "plus_di", "minus_di") for v in (series.get(k) or []) if v is not None) + 15))
        ax2.legend(loc="upper left", fontsize=6, frameon=False, labelcolor=MUTED, ncol=3)
        plt.setp(ax.get_xticklabels(), visible=False)
    target.set_xticks(ticks)
    target.set_xticklabels([_time_label(bars4h[i]["ts"], True) for i in ticks], rotation=0)
    fig.text(0.085, 0.94, "Nifty 50, 4-hour candles" + (" and ADX(14)" if has_adx else ""), color=FG, fontsize=9, fontweight="bold")
    return _png(fig, plt)


def bulletin_images(bulletin: dict[str, Any]) -> list[dict[str, Any]]:
    """The images for a bulletin dict (see bulletin.build_bulletin): a list of {cid, filename, content, alt}. A chart
    that cannot be drawn is left out (the rest of the email does not depend on it); never raises."""
    nifty = bulletin.get("nifty") if isinstance(bulletin, dict) else None
    if not isinstance(nifty, dict) or "unavailable" in nifty:
        return []
    ci = nifty.get("chart_input") or {}
    out: list[dict[str, Any]] = []
    jobs = (
        ("nifty15", "nifty-15min.png", "Nifty 50 15-minute candles for the last session with the 21 EMA, the previous close and watch levels",
         lambda: nifty_intraday_png(ci.get("bars15"), ci.get("ema21"), ci.get("levels"), ci.get("prev_close"))),
        ("nifty4h", "nifty-4hour.png", "Nifty 50 4-hour candles with the ADX(14) panel",
         lambda: nifty_4h_png(ci.get("bars4h"), ci.get("adx4h"))))
    for cid, filename, alt, make in jobs:
        try:
            png = make()
        except Exception:  # noqa: BLE001 - a broken chart costs the chart, not the email
            import logging
            logging.getLogger(__name__).warning("chart %s could not be drawn", cid, exc_info=True)
            png = None
        if png:
            out.append({"cid": cid, "filename": filename, "content": png, "alt": alt})
    return out
