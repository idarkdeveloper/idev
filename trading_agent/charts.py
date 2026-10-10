"""PNG charts for the evening bulletin (matplotlib Figure + Agg canvas: no pyplot, no window, no GUI).

``nifty_intraday_png`` draws the last session's 15-minute candles with the 21 EMA, the previous close and the watch
levels; ``nifty_4h_png`` draws the 4-hour candles with an ADX panel underneath. Both are 1280x720 pixels (shown at
640 wide in the email, so text stays readable on a phone) in the dark colours of the email header, use only the font
that ships with matplotlib, and stay under MAX_BYTES (re-quantized when over, None when still over). They return
``None`` when there is nothing to draw. matplotlib is imported when the first chart is made, so the rest of the app
does not need it. Drawing is serialised by a lock: rc_context changes matplotlib's global settings for its duration.
"""

from __future__ import annotations

import io
import threading
from datetime import datetime
from typing import Any

WIDTH, HEIGHT, DPI = 1280, 720, 200
FIGSIZE = (WIDTH / DPI, HEIGHT / DPI)
MAX_BYTES = 120 * 1024
TICK_PT, LABEL_PT, TITLE_PT = 11, 11, 13
BG, PANEL, FG, MUTED, GRID = "#111827", "#111827", "#e5e7eb", "#9ca3af", "#1f2937"
UP, DOWN, EMA, PREV, LEVEL, ADX = "#34d399", "#f87171", "#fbbf24", "#9ca3af", "#60a5fa", "#fbbf24"
RC = {"font.family": "DejaVu Sans", "font.size": TICK_PT, "axes.unicode_minus": True}
SHORT = {"Resistance": "R", "Support": "S", "Pivot": "P"}
_LOCK = threading.RLock()


def _new_figure() -> Any:
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    fig = Figure(figsize=FIGSIZE, dpi=DPI, facecolor=BG)
    FigureCanvasAgg(fig)
    return fig


def _plain(v: float, _pos: Any = None) -> str:
    """A tick label as a plain whole number with thousands separators: never an offset ("+2.24e4") or scientific form."""
    return f"{v:,.0f}"


def _style(ax: Any) -> None:
    from matplotlib.ticker import FuncFormatter
    ax.set_facecolor(PANEL)
    ax.yaxis.set_major_formatter(FuncFormatter(_plain))   # a plain formatter has no offset text
    ax.minorticks_off()
    for axis in (ax.xaxis, ax.yaxis):
        axis.get_offset_text().set_visible(False)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=MUTED, labelsize=TICK_PT, length=3)
    ax.grid(True, color=GRID, linewidth=0.7)
    ax.set_axisbelow(True)


def _candles(ax: Any, bars: list[dict[str, Any]], width: float = 0.6) -> None:
    for i, b in enumerate(bars):
        colour = UP if b["close"] >= b["open"] else DOWN
        ax.vlines(i, b["low"], b["high"], color=colour, linewidth=1.4, zorder=2)
        lo, hi = sorted((b["open"], b["close"]))
        ax.bar(i, max(hi - lo, (b["high"] - b["low"]) * 0.005 or 0.01), bottom=lo, width=width, color=colour, zorder=3)


def _time_label(ts: str, with_date: bool = False) -> str:
    dt = datetime.fromisoformat(ts)
    return f"{dt.day} {dt:%b} {dt:%H:%M}" if with_date else f"{dt:%H:%M}"


def _png(fig: Any) -> bytes | None:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=DPI, facecolor=BG, metadata={"Software": None})
    data = buf.getvalue()
    if len(data) > MAX_BYTES:   # very busy charts: fewer colours, same size
        from PIL import Image
        im = Image.open(io.BytesIO(data)).convert("RGB").quantize(colors=32)
        out = io.BytesIO()
        im.save(out, format="PNG", optimize=True)
        data = out.getvalue()
    return data if len(data) <= MAX_BYTES else None


def _in_view(values: list[Any], lo: float, hi: float, span: float) -> list[float]:
    """Level values close enough to the candles to be worth stretching the axis for."""
    return [v for v in values if v is not None and lo - 0.6 * span <= v <= hi + 0.6 * span]


def _spread(values: list[float], gap: float) -> list[float]:
    """Label heights for lines at ``values`` (same order back), pushed apart so neighbours are at least ``gap``
    apart (two levels a few points apart would otherwise print on top of each other); the group stays centred."""
    if len(values) < 2:
        return list(values)
    order = sorted(range(len(values)), key=lambda i: values[i])
    groups: list[list[float]] = []   # each group: the original values of labels that touch
    for i in order:
        v = values[i]
        if groups:
            g = groups[-1]
            mid = sum(g) / len(g)
            top = mid + gap * (len(g) - 1) / 2   # the highest label of the group, laid out centred
            if v - top < gap:
                g.append(v)
                continue
        groups.append([v])
    ys: list[float] = []
    for g in groups:
        mid = sum(g) / len(g)
        ys += [mid + gap * (k - (len(g) - 1) / 2) for k in range(len(g))]
    out = [0.0] * len(values)
    for k, i in enumerate(order):
        out[i] = ys[k]
    return out


# Fixed margins (fractions of the picture): the right-hand labels ("prev 22,565") get a full fifth of the width and
# never depend on tight-layout guessing, so nothing is clipped at the frame.
AX_LEFT, AX_RIGHT = 0.11, 0.80


def _draw_intraday(bars15: list[dict[str, Any]], ema21: list[float | None] | None,
                   levels: list[dict[str, Any]] | None, prev_close: float | None) -> Any:
    """The 15-minute figure (call inside the lock and the rc_context): one axes, no twin."""
    fig = _new_figure()
    ax = fig.add_axes([AX_LEFT, 0.12, AX_RIGHT - AX_LEFT, 0.74])
    _style(ax)
    _candles(ax, bars15)
    n = len(bars15)
    if ema21:
        pts = [(i, v) for i, v in enumerate(ema21[:n]) if v is not None]
        if pts:
            ax.plot([p[0] for p in pts], [p[1] for p in pts], color=EMA, linewidth=2.2, zorder=4)
    lo_y, hi_y = min(b["low"] for b in bars15), max(b["high"] for b in bars15)
    span = hi_y - lo_y or 1.0
    labels: list[tuple[float, str, str]] = []
    if prev_close and _in_view([prev_close], lo_y, hi_y, span):
        ax.axhline(prev_close, color=PREV, linestyle=":", linewidth=1.8, zorder=1)
        labels.append((prev_close, f"prev {prev_close:,.0f}", PREV))
    for lv in levels or []:
        v = lv.get("value")
        if not _in_view([v], lo_y, hi_y, span):
            continue
        ax.axhline(v, color=LEVEL, linestyle=(0, (1, 3)), linewidth=1.8, zorder=1)
        labels.append((float(v), f"{SHORT.get(lv.get('label', ''), lv.get('label', ''))} {v:,.0f}", LEVEL))
    pad = span * 0.08
    ticks = _in_view([prev_close] + [lv.get("value") for lv in levels or []], lo_y, hi_y, span)
    y0, y1 = min([lo_y] + ticks) - pad, max([hi_y] + ticks) + pad
    ax.set_ylim(y0, y1)
    for y, text, colour in zip(_spread([v for v, _, _ in labels], (y1 - y0) * 0.075), (t for _, t, _ in labels), (c for _, _, c in labels)):
        ax.annotate(text, (n - 0.5, y), xytext=(5, 0), textcoords="offset points",
                    color=colour, fontsize=LABEL_PT, va="center", annotation_clip=False)
    ax.set_xlim(-1, n)
    step = max(1, n // 5)
    ax.set_xticks(range(0, n, step))
    ax.set_xticklabels([_time_label(bars15[i]["ts"]) for i in range(0, n, step)])
    fig.text(AX_LEFT, 0.92, "Nifty 50, 15-minute, 21 EMA", color=FG, fontsize=TITLE_PT, fontweight="bold")
    return fig


def nifty_intraday_png(bars15: list[dict[str, Any]] | None, ema21: list[float | None] | None,
                       levels: list[dict[str, Any]] | None, prev_close: float | None) -> bytes | None:
    """15-minute candles of the last session, the 21 EMA (gaps where it is not defined), a dotted previous-close line
    and labelled horizontal watch levels ({label, value}; R, S and P for resistance, support and pivot). None when
    there are no bars or the picture cannot be made small enough."""
    if not bars15:
        return None
    import matplotlib
    with _LOCK, matplotlib.rc_context(RC):  # type: ignore[arg-type]
        return _png(_draw_intraday(bars15, ema21, levels, prev_close))


def _draw_4h(bars4h: list[dict[str, Any]], adx_series: dict[str, list[float | None]] | None) -> Any:
    """The 4-hour figure (call inside the lock and the rc_context); the ADX panel is its own axes below, not a twin."""
    n = len(bars4h)
    series = adx_series or {}
    has_adx = any(v is not None for v in (series.get("adx") or []))
    fig = _new_figure()
    if has_adx:
        ax = fig.add_axes([0.11, 0.38, 0.86, 0.50])
        ax2 = fig.add_axes([0.11, 0.13, 0.86, 0.19], sharex=ax)
    else:
        ax = fig.add_axes([0.11, 0.13, 0.86, 0.75])
        ax2 = None
    _style(ax)
    _candles(ax, bars4h, 0.65)
    ax.set_xlim(-1, n)
    step = max(1, n // 4)
    ticks = list(range(0, n, step))
    target = ax2 or ax
    if ax2 is not None:
        _style(ax2)
        for key, colour, width in (("adx", ADX, 2.4), ("plus_di", UP, 1.4), ("minus_di", DOWN, 1.4)):
            pts = [(i, v) for i, v in enumerate((series.get(key) or [])[:n]) if v is not None]
            if pts:
                ax2.plot([p[0] for p in pts], [p[1] for p in pts], color=colour, linewidth=width,
                         label={"adx": "ADX", "plus_di": "+DI", "minus_di": "−DI"}[key])
        ax2.axhline(25, color=MUTED, linestyle=":", linewidth=1.4)
        top = max(v for k in ("adx", "plus_di", "minus_di") for v in (series.get(k) or []) if v is not None)
        ax2.set_ylim(0, max(50.0, top + 20))
        ax2.legend(loc="upper left", fontsize=LABEL_PT, frameon=False, labelcolor=MUTED, ncol=3)
        ax.tick_params(axis="x", which="both", labelbottom=False, bottom=False)   # the shared time axis is labelled once, below
    target.set_xticks(ticks)
    target.set_xticklabels([_time_label(bars4h[i]["ts"], True) for i in ticks])
    fig.text(0.11, 0.92, "Nifty 50, 4-hour" + (", ADX(14)" if has_adx else ""), color=FG, fontsize=TITLE_PT, fontweight="bold")
    return fig


def nifty_4h_png(bars4h: list[dict[str, Any]] | None, adx_series: dict[str, list[float | None]] | None) -> bytes | None:
    """4-hour candles with an ADX(14) panel (ADX, +DI and -DI) underneath. The ADX panel is left out when the series
    is missing or empty. None when there are no candles or the picture cannot be made small enough."""
    if not bars4h:
        return None
    import matplotlib
    with _LOCK, matplotlib.rc_context(RC):  # type: ignore[arg-type]
        return _png(_draw_4h(bars4h, adx_series))


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
