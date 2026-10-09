from trading_agent.fundamentals import (YahooFundamentals, apply_fundamentals, is_financial,
                                        parse_summary)
from trading_agent.screen import run_screen
from .conftest import FakeSession, Seq


def summary(eps=48.83, bv=283.96, de=7.518, growth=0.309, pe=38.25, pb=6.58, roe=None):
    raw = lambda v: None if v is None else {"raw": v, "fmt": str(v)}  # noqa: E731
    return {"financialData": {"returnOnEquity": raw(roe), "debtToEquity": raw(de), "earningsGrowth": raw(growth),
                              "profitMargins": raw(0.1)},
            "defaultKeyStatistics": {"trailingEps": raw(eps), "bookValue": raw(bv), "priceToBook": raw(pb)},
            "summaryDetail": {"trailingPE": raw(pe)}}


def test_parse_summary_derives_roe_and_ratios():
    f = parse_summary(summary())
    assert round(f["roe"], 4) == round(48.83 / 283.96, 4)  # EPS / book value per share
    assert abs(f["debt_to_equity"] - 0.07518) < 1e-9  # Yahoo's 7.518 is a percentage
    assert abs(f["earnings_yield"] - 1 / 38.25) < 1e-9 and abs(f["book_to_price"] - 1 / 6.58) < 1e-9
    f = parse_summary(summary(eps=None, roe=0.21, pe=-5))
    assert f["roe"] == 0.21 and f["earnings_yield"] is None  # no meaningful yield for a loss
    assert is_financial("Financial Services") and is_financial("Banks") and not is_financial("Information Technology")


def test_crumb_retry_cache_and_errors(tmp_path):
    ok = {"quoteSummary": {"result": [summary()]}}
    sess = FakeSession({("GET", "getcrumb"): Seq("CRUMB1", "CRUMB2"),
                        ("GET", "quoteSummary/COFORGE.NS"): ok,
                        ("GET", "quoteSummary/NOPE.NS"): {"quoteSummary": {"result": []}}})
    sleeps = []
    y = YahooFundamentals(tmp_path, session=sess, sleep=sleeps.append)
    f = y.get("coforge")
    assert f["pe"] == 38.25
    q = [kw["params"]["crumb"] for m, u, kw in sess.calls if "quoteSummary" in u]
    assert q == ["CRUMB1"]
    n = len(sess.calls)
    assert y.get("COFORGE")["pe"] == 38.25 and len(sess.calls) == n  # cached on disk
    assert "error" in y.get("NOPE")
    assert (tmp_path / "fundamentals" / "COFORGE.json").exists()
    assert not (tmp_path / "fundamentals" / "NOPE.json").exists()  # errors aren't cached


def test_stale_crumb_is_refreshed_once(tmp_path):
    from .conftest import FakeResponse

    class Session(FakeSession):
        def request(self, method, url, **kw):
            if "quoteSummary" in url and kw.get("params", {}).get("crumb") == "OLD":
                self.calls.append((method, url, kw))
                return FakeResponse({"finance": {"error": "Invalid Crumb"}}, 401)
            return super().request(method, url, **kw)

    sess = Session({("GET", "getcrumb"): Seq("OLD", "NEW"),
                    ("GET", "quoteSummary/"): {"quoteSummary": {"result": [summary()]}}})
    f = YahooFundamentals(None, session=sess, sleep=lambda s: None).get("TCS")
    assert f["pe"] == 38.25
    assert [kw["params"]["crumb"] for m, u, kw in sess.calls if "quoteSummary" in u] == ["OLD", "NEW"]


def rows_for(*syms):
    return [{"symbol": s, "score": 1.0 - i * 0.01, "eligible": True} for i, s in enumerate(syms)]


def test_quality_reranks_and_applies_floors():
    rows = rows_for("MOMO", "QUAL", "LOSS", "DEBT", "BANK", "NODATA")
    funds = {
        "MOMO": parse_summary(summary(eps=5, bv=100, de=80, growth=0.0, pe=60)),     # ROE 5%, 0.8x debt
        "QUAL": parse_summary(summary(eps=30, bv=100, de=5, growth=0.4, pe=25)),     # ROE 30%, little debt
        "LOSS": parse_summary(summary(eps=-4, bv=100, de=20, growth=-0.5, pe=None)),  # loss-making
        "DEBT": parse_summary(summary(eps=20, bv=100, de=350, growth=0.1, pe=20)),   # 3.5x debt
        "BANK": parse_summary(summary(eps=15, bv=100, de=900, growth=0.1, pe=12)),   # bank: debt ignored
        "NODATA": {"error": "LookupError: none"},
    }
    out = apply_fundamentals(rows, funds, quality=1.0, value=0.0,
                             industries={"BANK": "Financial Services"})
    elig = [r["symbol"] for r in out if r["eligible"]]
    assert elig[0] == "QUAL" and "BANK" in elig and "NODATA" in elig
    assert next(r for r in out if r["symbol"] == "LOSS")["excluded"].startswith("loss-making")
    assert next(r for r in out if r["symbol"] == "DEBT")["excluded"].startswith("debt above")
    bank = next(r for r in out if r["symbol"] == "BANK")
    assert bank["financial"] and bank["debt_to_equity"] is None
    assert next(r for r in out if r["symbol"] == "NODATA")["no_fundamentals"]
    assert [r["rank"] for r in out] == list(range(1, len(out) + 1))


def test_value_weight_favours_cheap_stocks():
    rows = rows_for("PRICEY", "CHEAP")
    funds = {"PRICEY": parse_summary(summary(eps=20, bv=100, pe=80, pb=12)),
             "CHEAP": parse_summary(summary(eps=20, bv=100, pe=10, pb=1.5))}
    out = apply_fundamentals(rows, funds, quality=0.0, value=1.0)
    assert out[0]["symbol"] == "CHEAP" and out[0]["value_score"] > out[1]["value_score"]


class Prices:
    """300 rising daily bars per symbol, liquid enough for the screen's turnover floor."""

    def history(self, sym, range_="2y"):
        drift = 1 + (sum(map(ord, sym)) % 7) / 1000
        out, px = [], 100.0
        for i in range(300):
            px *= drift
            out.append({"date": f"2025-{1 + i // 28 % 12:02d}-{1 + i % 28:02d}", "close": px, "adj_close": px,
                        "volume": 1e6})
        return out


class Provider:
    def __init__(self, data):
        self.data, self.calls = data, []

    def get(self, sym):
        self.calls.append(sym)
        return self.data.get(sym, {"error": "none"})


def test_screen_unchanged_unless_asked():
    members = [{"symbol": s, "name": s, "industry": "Industrials"} for s in ("AAA", "BBB", "CCC")]
    p = Provider({})
    plain = run_screen(members, Prices(), top=3, fundamentals=p)  # weights default to 0
    assert p.calls == [] and plain["fundamentals"] is None
    p = Provider({"AAA": parse_summary(summary(eps=-1)), "BBB": parse_summary(summary()),
                  "CCC": parse_summary(summary())})
    r = run_screen(members, Prices(), top=3, fundamentals=p, quality=1.0)
    assert sorted(p.calls) == ["AAA", "BBB", "CCC"]
    assert r["fundamentals"]["excluded"] == 1 and "AAA" not in [x["symbol"] for x in r["top"]]
    assert all("roe" in x for x in r["top"])
