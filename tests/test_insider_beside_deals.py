"""WATCH_SOURCE=all: bulk and block deals (NSE and BSE) plus NSE insider (PIT) filings in one read. The insider side is
read at most every 15 minutes, a failure there keeps its last rows and never stops the deals, and its outcome is the
"insider" run on the Health page. Fake NSE methods, a fake BSE and a fake clock: no network."""
from datetime import datetime

import pytest

from trading_agent import health, nse
from trading_agent.nse import NSEClient
from trading_agent.quiver import DisclosedTrade
from trading_agent.replay.deals import KINDS_BY_SOURCE
from trading_agent.timezones import IST
from trading_agent.ui import WATCH_SOURCES

WHO = "ASHISH KACHOLIA"


def trade(source, ticker, who=WHO, exchange="NSE"):
    return DisclosedTrade(source=source, investor=who, ticker=ticker, transaction="Purchase", transaction_date="2026-10-15",
                          report_date="2026-10-15", size="100 sh", raw={}, exchange=exchange)


class FakeBSE:
    def __init__(self):
        self.kinds = []

    def deals(self, start, end, kinds, investors=None):
        self.kinds.append(tuple(kinds))
        return [trade("bulk", "BSEONLY", exchange="BSE")]


@pytest.fixture
def client(tmp_path, monkeypatch):
    c = NSEClient(cache_dir=tmp_path / "cache")
    c.historical_deals = lambda days, kind="bulk", end=None: [trade(kind, "SENCO" if kind == "bulk" else "ZAGGLE"),
                                                              trade(kind, "OTHER", who="SOMEONE ELSE")]
    c.calls = 0
    c.insider_rows = [trade("insider", "SAFARI")]

    def insider(days=30, end=None, symbol=None):
        c.calls += 1
        if isinstance(c.insider_rows, BaseException):
            raise c.insider_rows
        return list(c.insider_rows)
    c.insider_trades = insider
    c.bse = FakeBSE()
    clock = [1000.0]
    monkeypatch.setattr(nse.time, "monotonic", lambda: clock[0])
    c.clock = clock
    return c


def test_all_reads_the_deals_and_the_insider_filings_of_the_followed_names(client):
    got = client.trades_for_investors([WHO], "all")
    assert sorted((t.source, t.ticker) for t in got) == [("block", "ZAGGLE"), ("bulk", "BSEONLY"), ("bulk", "SENCO"),
                                                         ("insider", "SAFARI")]
    assert client.bse.kinds == [("bulk", "block")]
    assert {t.ticker for t in client.trades_for_investors([WHO], "deals")} == {"SENCO", "ZAGGLE", "BSEONLY"}
    assert {t.ticker for t in client.trades_for_investors([WHO], "insider")} == {"SAFARI"}


def test_insider_side_is_read_every_15_minutes_and_a_failure_keeps_the_deals(client, tmp_path):
    client.trades_for_investors([WHO], "all")
    client.clock[0] += 14 * 60
    client.trades_for_investors([WHO], "all")
    assert client.calls == 1                                    # within 15 minutes: the last insider rows
    runs = health.read_runs(tmp_path)
    assert runs["insider"]["ok"] and runs["insider"]["rows"] == 1
    client.clock[0] += 2 * 60
    client.insider_rows = ConnectionError("NSE refused")
    got = client.trades_for_investors([WHO], "all")
    assert client.calls == 2 and {t.ticker for t in got} == {"SENCO", "ZAGGLE", "BSEONLY", "SAFARI"}   # last rows kept
    assert "NSE refused" in health.read_runs(tmp_path)["insider"]["error"]
    client.clock[0] += 16 * 60
    client.insider_rows = [trade("insider", "NEWONE")]
    assert "NEWONE" in {t.ticker for t in client.trades_for_investors([WHO], "all")}


class Cal:
    def is_trading_day(self, d):
        return d.weekday() < 5


@pytest.mark.parametrize("source, on", [("deals", {"deals", "bse"}), ("insider", {"insider"}),
                                         ("all", {"deals", "bse", "insider"})])
def test_health_rows_follow_the_source(settings, source, on):
    settings.data_source, settings.watch_source, settings.bse_deals = "nse", source, True
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    rows = {r["id"]: r for r in health.report(settings, datetime(2026, 10, 12, 11, 0, tzinfo=IST), Cal())["sources"]}
    assert {sid for sid in ("deals", "bse", "insider") if rows[sid]["status_text"] != "not configured"} == on
    if source == "all":
        assert "every 15 minutes beside the deals" in rows["insider"]["schedule"]


def test_the_setting_accepts_all_and_the_replay_reads_every_kind():
    assert "all" in WATCH_SOURCES["in"] and "all" not in WATCH_SOURCES["us"]
    assert KINDS_BY_SOURCE["all"] == ("bulk", "block", "insider")


def test_settings_offer_the_combined_source():
    from pathlib import Path
    index = (Path(__file__).resolve().parents[1] / "trading_agent" / "ui" / "index.html").read_text(encoding="utf-8")
    assert '["all","Bulk + block deals and insider filings"]' in index
