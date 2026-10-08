from trading_agent.quiver import QuiverClient, filter_by_investor, _norm_congress
from .conftest import FakeSession


def test_normalise_and_filter(sample_rows):
    trades = [_norm_congress(r) for r in sample_rows]
    mine = filter_by_investor(trades, "pelosi")
    assert [t.ticker for t in mine] == ["NVDA", "AVGO", "AAPL"] or len(mine) == 3
    assert all(t.investor == "Nancy Pelosi" for t in mine)
    assert mine[0].key != mine[1].key
    assert len(mine[0].key) == 16


def test_key_is_stable(sample_rows):
    a = _norm_congress(sample_rows[0]); b = _norm_congress(dict(sample_rows[0]))
    assert a.key == b.key


def test_client_sends_token_header(sample_rows):
    sess = FakeSession({("GET", "/live/congresstrading"): sample_rows})
    client = QuiverClient("secret", session=sess)
    trades = client.trades_for_investor("Nancy Pelosi")
    assert len(trades) == 3
    method, url, kw = sess.calls[0]
    assert kw["headers"]["Authorization"] == "Token secret"
    assert url.endswith("/beta/live/congresstrading")


def test_historical_endpoint_uses_ticker(sample_rows):
    sess = FakeSession({("GET", "/historical/congresstrading/NVDA"): sample_rows[:1]})
    client = QuiverClient("k", session=sess)
    assert client.congress_trades("nvda")[0].ticker == "NVDA"
