"""Order history as trades: the page text carries the hooks, the words and the tokens-only CSS."""
from pathlib import Path

UI = Path(__file__).resolve().parent.parent / "trading_agent" / "ui"
HTML = (UI / "index.html").read_text(encoding="utf-8")
CSS = (UI / "nocturne.css").read_text(encoding="utf-8")


def test_kept_ids_and_the_toggle():
    for hook in ('id="orders"', 'id="fees-note"', 'id="orders-more"', 'id="orders-foot"', 'id="orders-count"',
                 'data-ov="trades"', 'data-ov="all"', 'id="orders-sum"', 'id="orders-cards"'):
        assert hook in HTML, hook


def test_trades_view_uses_the_helper_output_and_words_not_colour_only():
    assert "S.trade_view" in HTML
    assert "Profit" in HTML and "Loss" in HTML and "after charges" in HTML and "unrealised" in HTML
    assert "▲" in HTML and "▼" in HTML
    assert 'reason === "stop-loss"' in HTML and "Closed ·" in HTML


def test_live_orders_show_na_charges_and_no_order_placing_added():
    assert 'x.fees != null ? money(x.fees, 2)' in HTML and "n/a" in HTML
    assert "place_order" not in HTML.split("function renderOrders")[1].split("function renderScreen")[0]


def test_css_tokens_only_and_phone_targets():
    block = CSS.split("Order history as trades")[1]
    import re
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgb\(|hsl\(", block)
    assert "min-height: 44px" in block and "@media (max-width: 700px)" in block


def test_css_braces_are_balanced():
    """One unclosed @media block once swallowed every rule after it (Live/Practice header, order cards, P/L colours)."""
    import re
    css = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
    assert css.count("{") == css.count("}")


def test_every_page_loads_the_shared_palette_before_nocturne():
    from trading_agent.ui import STATIC_FILES
    assert "/static/colors.css" in STATIC_FILES
    for page in ("index.html", "replay.html", "screener.html"):
        html = (UI / page).read_text(encoding="utf-8")
        assert html.index("/static/colors.css") < html.index("/static/nocturne.css"), page
