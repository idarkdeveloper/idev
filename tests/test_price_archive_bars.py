"""Golden test: PriceArchive.bars() (tuple-reading, column-pruned) equals the old SELECT * / sqlite3.Row version."""
import sqlite3

from trading_agent.price_archive import PriceArchive


def _old_bars(a, symbol, raw=False):
    c = sqlite3.connect(a.path)
    c.row_factory = sqlite3.Row
    try:
        rows = c.execute("SELECT * FROM bars WHERE symbol=? ORDER BY date", (symbol,)).fetchall()
    finally:
        c.close()
    return [{"date": r["date"], "close": r["close_raw"] if raw else r["close"],
             "adj_close": r["adj_close"], "volume": r["volume_raw"] if raw else r["volume"]} for r in rows]


def test_bars_matches_reference_implementation(tmp_path):
    a = PriceArchive(tmp_path / "a.sqlite")
    c = sqlite3.connect(a.path)
    for s in ("AAA", "BBB", "CCC"):
        for i in range(30):   # inserted out of date order, so ORDER BY matters
            d = f"2024-01-{30 - i:02d}"
            c.execute("INSERT INTO bars VALUES (?,?,?,?,?,?,?,?)",
                      (s, d, 10.5 + i / 7, 9.25 + i / 3, 1000.0 + i, 21.0 + i / 5, 500.0 + i, "t"))
    c.commit()
    c.close()
    for s in ("AAA", "BBB", "CCC"):
        for raw in (False, True):
            new = a.bars(s, raw=raw)
            assert new == _old_bars(a, s, raw) and len(new) == 30
            assert [list(b) for b in new] == [["date", "close", "adj_close", "volume"]] * 30
    assert a.bars("NOPE") == [] and a.bars("NOPE", raw=True) == []
    assert a.bars("AAA", raw=True) != a.bars("AAA")
