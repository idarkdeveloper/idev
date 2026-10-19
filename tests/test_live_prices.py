"""Groww live prices: the watch-side writer (fakes only, no network) and the dashboard's reader."""
import json
import threading
from datetime import datetime, timedelta, timezone

import pytest

from trading_agent import app_feed, live_prices
from trading_agent.live_prices import LivePrices, apply_live_prices, feed_symbols, load_live
from trading_agent.timezones import IST


class Hol:
    def __init__(self, closed=()):
        self.closed = set(closed)

    def is_trading_day(self, d):
        return d not in self.closed


MON_OPEN = datetime(2026, 10, 19, 11, 0, tzinfo=IST)


def _feed(state, symbols, now, source="groww"):
    d = app_feed.app_dir(state)
    d.mkdir(parents=True, exist_ok=True)
    (d / app_feed.FEED_FILE).write_text(json.dumps({"as_of": now.isoformat(), "source": source,
                                                    "holdings": [{"symbol": s, "qty": 1} for s in symbols]}), encoding="utf-8")


class Fake:
    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    def __call__(self, symbols):
        self.calls.append(list(symbols))
        if self.fail:
            raise self.fail
        return {s: 100.0 + i for i, s in enumerate(symbols)}


def make(settings, ltp, holidays=None, **over):
    for k, v in over.items():
        setattr(settings, k, v)
    return LivePrices(settings, ltp, holidays or Hol())


def test_tick_writes_the_file_atomically_and_only_one_call_for_16(settings):
    syms = [f"S{i}" for i in range(16)]
    _feed(settings.state_dir, syms, MON_OPEN)
    fake = Fake()
    out = make(settings, fake).tick(MON_OPEN)
    assert len(fake.calls) == 1 and out["source"] == "groww_ltp" and len(out["prices"]) == 16
    path = app_feed.app_dir(settings.state_dir) / live_prices.LIVE_FILE
    data = json.loads(path.read_text())
    assert datetime.fromisoformat(data["as_of"]).utcoffset() == timedelta(0) and data["prices"]["S0"] == 100.0
    assert not list(path.parent.glob("*.tmp"))


def test_sixty_symbols_make_two_calls(settings):
    _feed(settings.state_dir, [f"S{i}" for i in range(60)], MON_OPEN)
    fake = Fake()
    make(settings, fake).tick(MON_OPEN)
    assert [len(c) for c in fake.calls] == [50, 10]


@pytest.mark.parametrize("when", [datetime(2026, 10, 19, 8, 45, tzinfo=IST),    # window open, NSE not yet
                                  datetime(2026, 10, 19, 15, 45, tzinfo=IST),   # after the close
                                  datetime(2026, 10, 17, 11, 0, tzinfo=IST)])   # Saturday
def test_skips_outside_market_hours(settings, when):
    _feed(settings.state_dir, ["TCS"], when)
    fake = Fake()
    assert make(settings, fake).tick(when) is None and fake.calls == []


def test_skips_on_a_holiday_without_a_snapshot_and_for_bse(settings):
    _feed(settings.state_dir, ["TCS"], MON_OPEN)
    fake = Fake()
    assert make(settings, fake, Hol({MON_OPEN.date()})).tick(MON_OPEN) is None and not fake.calls
    assert feed_symbols(settings.state_dir, MON_OPEN) == ["TCS"]
    assert feed_symbols(settings.state_dir, MON_OPEN, "BSE") == []
    _feed(settings.state_dir, ["TCS", "ABC.BO"], MON_OPEN)
    assert feed_symbols(settings.state_dir, MON_OPEN) == ["TCS"]
    _feed(settings.state_dir, ["TCS"], MON_OPEN - timedelta(minutes=6))          # snapshot too old
    assert feed_symbols(settings.state_dir, MON_OPEN) == []
    _feed(settings.state_dir, ["TCS"], MON_OPEN, source="saved")
    assert feed_symbols(settings.state_dir, MON_OPEN) == []


def test_cadence_and_exponential_backoff_without_a_tight_loop(settings):
    _feed(settings.state_dir, ["TCS"], MON_OPEN)
    fake = Fake(fail=RuntimeError("boom with secret-token"))
    lp = make(settings, fake)
    t = MON_OPEN
    assert lp.tick(t) is None and len(fake.calls) == 1
    for s in (1, 30, 59):                       # inside the 60 s back-off: no call
        assert lp.tick(t + timedelta(seconds=s)) is None
    assert len(fake.calls) == 1
    t2 = t + timedelta(seconds=61)
    _feed(settings.state_dir, ["TCS"], t2)
    lp.tick(t2)
    assert len(fake.calls) == 2
    assert lp.tick(t2 + timedelta(seconds=100)) is None and len(fake.calls) == 2   # second failure: 120 s
    for _ in range(12):                          # many failures: never waits longer than 10 minutes
        lp._back_off(t2, RuntimeError("x"))
    assert (lp._next - t2).total_seconds() == live_prices.MAX_BACKOFF_S
    fake.fail = None                             # recovery resets the back-off
    t3 = t2 + timedelta(seconds=700)
    _feed(settings.state_dir, ["TCS"], t3)
    assert lp.tick(t3) is not None and lp._fails == 0


def test_failures_are_logged_without_the_error_text(settings, caplog):
    _feed(settings.state_dir, ["TCS"], MON_OPEN)
    caplog.set_level("WARNING")
    make(settings, Fake(fail=RuntimeError("secret-token-123"))).tick(MON_OPEN)
    assert "RuntimeError" in caplog.text and "secret-token-123" not in caplog.text
    assert not (app_feed.app_dir(settings.state_dir) / live_prices.LIVE_FILE).exists()


def test_token_cooldown_is_honoured_and_a_bad_answer_never_raises(settings):
    from trading_agent.groww import GrowwTokenUnavailable
    _feed(settings.state_dir, ["TCS"], MON_OPEN)
    until = MON_OPEN + timedelta(hours=1)
    lp = make(settings, Fake(fail=GrowwTokenUnavailable("cooling", until, 429)))
    assert lp.tick(MON_OPEN) is None and lp._next == until
    lp2 = make(settings, lambda syms: {"TCS": float("nan"), "X": "no", "Y": -1})
    assert lp2.tick(MON_OPEN) is None


def test_flag_off_starts_no_thread_and_writes_nothing(settings, tmp_path):
    assert settings.live_prices is False and settings.live_prices_every_s == 60
    before = {t.name for t in threading.enumerate()}
    assert "live-prices" not in before
    assert not (app_feed.app_dir(settings.state_dir) / live_prices.LIVE_FILE).exists()
    from pathlib import Path
    cli = (Path(__file__).resolve().parent.parent / "trading_agent" / "cli.py").read_text(encoding="utf-8")
    assert "if settings.live_prices and" in cli


def test_config_reads_env_with_a_minimum(monkeypatch, tmp_path):
    from trading_agent.config import load_settings
    monkeypatch.setenv("LIVE_PRICES", "true")
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.setenv("LIVE_PRICES_EVERY_S", "5")
    with pytest.raises(SystemExit):                      # below the 30 s minimum is refused, like the other intervals
        load_settings()
    monkeypatch.setenv("LIVE_PRICES_EVERY_S", "45")
    s = load_settings()
    assert s.live_prices is True and s.live_prices_every_s == 45


# ---- the dashboard's side ----

def _write_live(state, **over):
    body = {"as_of": datetime.now(timezone.utc).isoformat(), "source": "groww_ltp", "prices": {"TCS": 3300.5}}
    body.update(over)
    d = app_feed.app_dir(state)
    d.mkdir(parents=True, exist_ok=True)
    (d / live_prices.LIVE_FILE).write_text(json.dumps(body) if not isinstance(body, str) else body, encoding="utf-8")


def test_dashboard_reads_a_fresh_file_and_ignores_the_rest(settings):
    s = settings.state_dir
    assert load_live(s) is None                                           # no file
    _write_live(s)
    assert load_live(s)["prices"] == {"TCS": 3300.5}
    _write_live(s, as_of=(datetime.now(timezone.utc) - timedelta(seconds=181)).isoformat())
    assert load_live(s) is None                                           # stale
    _write_live(s, source="yahoo")
    assert load_live(s) is None                                           # wrong source
    _write_live(s, prices={"TCS": "x", "A": float("inf")})
    assert load_live(s) is None
    (app_feed.app_dir(s) / live_prices.LIVE_FILE).write_text("{not json", encoding="utf-8")
    assert load_live(s) is None                                           # malformed


def test_apply_live_prices_recomputes_the_covered_holding_only():
    out = {"linked": True, "at": "x", "invested": 5000.0, "value": 4500.0, "pl": -500.0, "pl_pct": -0.1, "unpriced": [],
           "holdings": [{"symbol": "TCS", "qty": 10, "avg_price": 300.0, "price": 290.0, "invested": 3000.0, "value": 2900.0, "pl": -100.0, "pl_pct": -0.03},
                        {"symbol": "INFY", "qty": 2, "avg_price": 1000.0, "price": 800.0, "invested": 2000.0, "value": 1600.0, "pl": -400.0, "pl_pct": -0.2}]}
    live = {"as_of": datetime(2026, 10, 19, 5, 30, tzinfo=timezone.utc), "prices": {"TCS": 310.0}}
    r = apply_live_prices(out, live)
    tcs, infy = r["holdings"]
    assert tcs["price"] == 310.0 and tcs["value"] == 3100.0 and tcs["pl"] == 100.0 and tcs["price_src"] == "groww"
    assert infy["price"] == 800.0 and "price_src" not in infy
    assert r["value"] == 4700.0 and r["pl"] == -300.0 and r["prices_as_of"].startswith("2026-10-19T05:30")
    assert out["holdings"][0]["price"] == 290.0                           # the cached answer is not mutated
    assert apply_live_prices(out, None) is out


def test_my_portfolio_uses_the_file_and_never_a_stale_one(settings, tmp_path, monkeypatch):
    from trading_agent.broker import LocalPaperBroker
    from trading_agent.ui import App
    app = App(settings, broker=LocalPaperBroker(tmp_path / "pb.json", starting_cash=1000, price_fn=lambda s: 1.0), dotenv=None)
    raw = {"linked": True, "at": "2026-10-19T11:00:00+05:30", "invested": 3000.0, "value": 2900.0, "pl": -100.0, "pl_pct": -0.03,
           "unpriced": [], "holdings": [{"symbol": "TCS", "qty": 10, "avg_price": 300.0, "price": 290.0, "invested": 3000.0, "value": 2900.0, "pl": -100.0, "pl_pct": -0.03}]}
    monkeypatch.setattr("trading_agent.ui.read_groww_portfolio", lambda *a, **k: json.loads(json.dumps(raw)))
    app._bands_for = lambda syms: {}
    _write_live(settings.state_dir)
    out = app.my_portfolio(refresh=True)
    assert out["holdings"][0]["price"] == 3300.5 and out["holdings"][0]["price_src"] == "groww" and out["prices_as_of"]
    _write_live(settings.state_dir, as_of=(datetime.now(timezone.utc) - timedelta(seconds=400)).isoformat())
    out = app.my_portfolio()                                              # served from the 60 s cache, re-checked every time
    assert out["holdings"][0]["price"] == 290.0 and "prices_as_of" not in out


def test_the_phone_feed_contract_and_the_order_paths_are_untouched():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "trading_agent"
    for name in ("app_api.py", "app_feed.py"):
        assert "live_prices" not in (root / name).read_text(encoding="utf-8"), name
    code = (root / "live_prices.py").read_text(encoding="utf-8")
    for bad in ("place_order", "submit_order", "cancel", "buy(", "sell(", "live_orders=True", "fresh=True", "force=True", "requests."):
        assert bad not in code, bad
    g = (root / "groww.py").read_text(encoding="utf-8")
    seg = g[g.index("def ltp_strict"):g.index("def opening_price")]
    assert "price_fallback" not in seg and '"GET"' in seg and "_require_live" not in seg
