"""The Python "What this means" text (takeaway.py, for Telegram /lookup) says exactly what the page's
lookupTakeaway (ui/common.js) says, on the same inputs: the JS runs under node."""
import html
import json
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from trading_agent.takeaway import lookup_takeaway
from trading_agent.timezones import IST

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=IST)
NOW_MS = int(NOW.timestamp() * 1000)

CASES = [
    {"r": {"ticker": "TCS", "name": "Tata Consultancy Services Limited",
           "momentum": {"verdict": "strong", "ret_6m": 0.62, "ret_12_1": 0.41, "above_200dma": True, "pct_from_52w_high": -0.01},
           "announcements": [{"at": "2026-10-09 18:20:00", "category": "Trading Window", "text": "Closure of trading window"},
                             {"at": "2026-09-30 10:00:00", "category": "Scheme of Arrangement",
                              "text": "TCS has informed the Exchange about the Hon ble NCLT approving the scheme of arrangement between the company and its subsidiaries for the merger"},
                             {"at": "2026-09-01 10:00:00", "category": "Credit Rating", "text": "Credit rating reaffirmed"}],
           "news": {"items": [{"sentiment": "negative", "confidence": "high", "published": "2026-10-11T08:00:00+00:00",
                               "source": "Mint & Co <x>", "title": "TCS faces a probe over \"visa\" filings, & more in a very long headline that will need clipping at some point soon"}]},
           "position": {"qty": 10, "avg_entry_price": 3500.0, "stop": 3800.25, "stop_type": "trailing", "stop_label": "trailing"},
           "price": 4000.0},
     "now": NOW_MS, "ctx": {"regime": {"regime": "risk_off", "markets": {"nifty50": {"above_200dma": False}}},
                            "screen": {"top": [{"symbol": "A"}, {"symbol": "B<b>"}], "at": "2026-10-01T09:00:00"}}},
    {"r": {"ticker": "XYZ", "name": None, "momentum": {"verdict": "weak", "ret_6m": -0.2, "ret_12_1": None,
                                                         "above_200dma": False, "pct_from_52w_high": -0.45},
           "announcements": [{"at": "2026-10-05 10:00:00", "category": "Outcome of Board Meeting", "text": "Financial results"}],
           "position": {"qty": 2.5, "avg_entry_price": 120.0, "stop": None, "stop_type": "none"}, "price": 100.0},
     "now": NOW_MS, "ctx": {}},
    {"r": {"ticker": "MID", "name": "Midcap Ltd", "momentum": {"verdict": "neutral", "ret_6m": 0.05, "ret_12_1": 0.02,
                                                               "above_200dma": True, "pct_from_52w_high": -0.1}},
     "now": NOW_MS, "ctx": None},
    {"r": {"ticker": "ERR", "momentum": {"error": "no history"}}, "now": NOW_MS, "ctx": None},
]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_python_port_says_what_the_page_says(tmp_path):
    f = tmp_path / "cases.json"
    f.write_text(json.dumps(CASES))
    harness = Path(__file__).parent / "takeaway_harness.js"
    js = json.loads(subprocess.run(["node", str(harness), str(f)], capture_output=True, text=True, check=True,
                                   env={**os.environ, "TZ": "Asia/Kolkata"}).stdout)
    py = [lookup_takeaway(c["r"], c["now"], c["ctx"]) for c in CASES]
    for j, p in zip(js, py):
        assert html.unescape(j) == p
    assert "Bottom line" in py[0] and "Hon'ble NCLT" in py[0] and "₹3,800.25" in py[0] and py[3] == ""
