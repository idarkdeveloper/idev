import hashlib

import pytest

from trading_agent.broker import LocalPaperBroker
from trading_agent.groww import GrowwBroker, request_access_token, totp_now
from .conftest import FakeSession

ROUTES = {
    ("GET", "/holdings/user"): {"status": "SUCCESS", "payload": {"holdings": [
        {"isin": "INE002A01018", "trading_symbol": "RELIANCE", "quantity": 10, "average_price": 2500.0},
        {"isin": "INE467B01029", "trading_symbol": "TCS", "quantity": 0, "average_price": 0},
    ]}},
    ("GET", "/margins/detail/user"): {"status": "SUCCESS", "payload": {"clear_cash": 50000.0}},
    ("GET", "/live-data/ltp"): {"status": "SUCCESS", "payload": {"NSE_RELIANCE": 2862.0, "NSE_SENCO": 346.2}},
    ("POST", "/order/create"): {"status": "SUCCESS", "payload": {"groww_order_id": "GMK1", "order_status": "OPEN"}},
    ("GET", "/order/status/GMK1"): {"status": "SUCCESS", "payload": {"groww_order_id": "GMK1", "order_status": "OPEN",
                                                                    "filled_quantity": 0}},
}


def test_read_only_portfolio_and_prices():
    sess = FakeSession(ROUTES)
    b = GrowwBroker("tok", session=sess)
    pos = b.positions()
    assert [p.symbol for p in pos] == ["RELIANCE"] and pos[0].current_price == 2862.0
    acct = b.account()
    assert acct.currency == "INR" and acct.cash == 50000.0 and acct.equity == 50000.0 + 28620.0
    assert b.latest_price("senco") == 346.2
    with pytest.raises(LookupError):
        b.latest_price("NOPE")
    headers = sess.calls[0][2]["headers"]
    assert headers["Authorization"] == "Bearer tok" and headers["X-API-VERSION"] == "1.0"


def test_orders_blocked_unless_live_enabled():
    b = GrowwBroker("tok", session=FakeSession(ROUTES))
    with pytest.raises(PermissionError):
        b.submit_order("RELIANCE", "buy", notional=10000)
    with pytest.raises(ValueError):  # less than one whole share
        GrowwBroker("tok", session=FakeSession(ROUTES), live_orders=True) \
            .submit_order("RELIANCE", "buy", notional=100)


def test_live_order_body_uses_whole_shares():
    sess = FakeSession(ROUTES)
    b = GrowwBroker("tok", session=sess, live_orders=True, sleep=lambda s: None)
    order = b.submit_order("reliance", "buy", notional=10000)  # 10000 / 2862 -> 3 shares
    assert order["qty"] == 3 and order["id"] == "GMK1" and order["live"] is True
    body = next(c[2]["json"] for c in sess.calls if c[1].endswith("/order/create"))
    assert body["trading_symbol"] == "RELIANCE" and body["quantity"] == 3
    assert body["transaction_type"] == "BUY" and body["segment"] == "CASH"
    assert body["product"] == "CNC" and body["order_type"] == "LIMIT" and body["validity"] == "DAY"


def test_api_failure_raises():
    sess = FakeSession({("GET", "/holdings/user"): {"status": "FAILURE",
                                                   "error": {"code": "GA005", "message": "nope"}}})
    with pytest.raises(RuntimeError, match="GA005"):
        GrowwBroker("tok", session=sess).holdings()


def test_access_token_checksum_flow():
    sess = FakeSession({("POST", "/token/api/access"): {"token": "T123", "expiry": "x"}})
    tok = request_access_token("APIKEY", secret="s3cret", session=sess)["token"]
    assert tok == "T123"
    method, url, kw = sess.calls[0]
    body = kw["json"]
    assert kw["headers"]["Authorization"] == "Bearer APIKEY"
    assert body["key_type"] == "approval"
    assert body["checksum"] == hashlib.sha256(("s3cret" + body["timestamp"]).encode()).hexdigest()


def test_totp_matches_rfc6238_vector(monkeypatch):
    import trading_agent.groww as g
    monkeypatch.setattr(g.time, "time", lambda: 59)
    # RFC 6238 appendix B, SHA-1, T=59 -> 94287082 (8 digits); last 6 are 287082
    assert totp_now("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ", digits=8) == "94287082"
    assert totp_now("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ") == "287082"


def test_paper_mirror_seeds_from_groww(tmp_path):
    groww = GrowwBroker("tok", session=FakeSession(ROUTES))
    sim = LocalPaperBroker(tmp_path / "pb.json", price_fn=groww.latest_price)
    assert sim.is_fresh
    sim.seed(groww.positions(), cash=groww.account().cash, label="groww")
    assert not sim.is_fresh and sim.name == "local-paper (mirrors groww)"
    assert sim.account().cash == 50000.0 and sim.positions()[0].symbol == "RELIANCE"
    assert sim.performance()["pnl"] == 0
    sim.submit_order("SENCO", "buy", notional=3462)  # 10 shares
    assert sim.positions()[-1].qty == 10
