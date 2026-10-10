"""Quote panel: Yahoo summary parsing, crore formatting, the per-ticker cache and GET /api/quote."""

from trading_agent.fundamentals import parse_summary
from trading_agent.quote import QuoteService, build_quote, format_market_cap, group_indian

from .test_ui import _get, server  # noqa: F401  (server is a fixture)


def raw(v):
    return None if v is None else {"raw": v, "fmt": str(v)}


def summary(pe=22.5, cap=1.5e12, dy=0.0123, lo=300.0, hi=450.0, qtype="EQUITY"):
    return {"summaryDetail": {"trailingPE": raw(pe), "marketCap": raw(cap), "dividendYield": raw(dy),
                              "fiftyTwoWeekLow": raw(lo), "fiftyTwoWeekHigh": raw(hi)},
            "quoteType": {"quoteType": qtype}}


def test_parse_summary_reads_the_quote_fields():
    f = parse_summary(summary())
    assert f["market_cap"] == 1.5e12 and f["dividend_yield"] == 0.0123
    assert (f["week52_low"], f["week52_high"], f["pe"], f["quote_type"]) == (300.0, 450.0, 22.5, "EQUITY")


def test_missing_fields_become_none_not_errors():
    q = build_quote("ABC", parse_summary({"summaryDetail": {}}), fetched_at=1.0)
    assert q["error"] is None and not q["fund"]
    assert q["pe"] is None and q["market_cap_text"] is None and q["dividend_yield_text"] is None
    assert q["week52_low"] is None and q["week52_high"] is None


def test_loss_maker_has_no_pe_and_a_bad_range_is_dropped():
    q = build_quote("ABC", parse_summary(summary(pe=-4.0, lo=500.0, hi=400.0)), fetched_at=1.0)
    assert q["pe"] is None and q["week52_low"] is None and q["week52_high"] is None


def test_etf_hides_company_ratios():
    q = build_quote("SOMEFUND", parse_summary(summary(pe=22.0, qtype="ETF")), fetched_at=1.0)
    assert q["fund"] and q["pe"] is None
    assert build_quote("GOLDBEES", parse_summary(summary(qtype="EQUITY")), fetched_at=1.0)["fund"]  # ticker pattern
    assert not build_quote("INFY", parse_summary(summary()), fetched_at=1.0)["fund"]


def test_format_market_cap_in_crore_and_lakh_crore():
    assert format_market_cap(1.5e12) == "₹1.50 lakh Cr"          # 1.5 trillion rupees = 1,50,000 crore
    assert format_market_cap(123_450_000_000) == "₹12,345 Cr"
    assert format_market_cap(1.842e13) == "₹18.42 lakh Cr"
    assert format_market_cap(8.7654e11) == "₹87,654 Cr"
    assert format_market_cap(5e6) == "₹0.50 Cr"
    assert format_market_cap(None) is None and format_market_cap(0) is None
    assert group_indian(12345678) == "1,23,45,678" and group_indian(999) == "999" and group_indian(1000) == "1,000"
    assert format_market_cap(9.9e11) == "₹99,000 Cr" and format_market_cap(1.234e13) == "₹12.34 lakh Cr"


def test_error_payload_and_service_cache():
    calls = []
    now = [1000.0]

    class Prov:
        def get(self, t):
            calls.append(t)
            return {"error": "LookupError: none"} if t == "NOPE" else parse_summary(summary())

    svc = QuoteService(Prov(), ttl=1800, clock=lambda: now[0])
    a = svc.get("infy")
    assert a["ticker"] == "INFY" and a["pe"] == 22.5 and a["source"] == "Yahoo Finance"
    now[0] += 600
    assert svc.get("INFY") is a and calls == ["INFY"]            # inside 30 min: no new request
    now[0] += 1300
    svc.get("INFY")
    assert calls == ["INFY", "INFY"]                              # expired: fetched again
    assert svc.get("NOPE")["error"]
    assert svc.get("NOPE")["error"] and calls.count("NOPE") == 2  # errors are not cached


def test_quote_endpoint(server):  # noqa: F811
    base, app = server
    app.quote_service = QuoteService(type("P", (), {"get": lambda self, t: parse_summary(summary())})())
    status, q = _get(base + "/api/quote?ticker=infy")
    assert status == 200 and q["ticker"] == "INFY" and q["market_cap_text"] == "₹1.50 lakh Cr"
    assert _get(base + "/api/quote")[0] == 400
    assert _get(base + "/api/quote?ticker=")[0] == 400
    assert _get(base + "/api/quote?ticker=bad%20ticker!")[0] == 400
