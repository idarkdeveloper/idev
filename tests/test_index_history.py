import json

from trading_agent.index_history import (build_history, changes_for, current_symbol, history_path,
                                         list_notices, parse_notice, point_in_time, validate)
from trading_agent.membership import membership_for

from .conftest import FakeSession

SEMI_ANNUAL = """PRESS RELEASE
Mumbai, August 10, 2026
Replacements in indices
These changes shall become effective from September 30, 2026 (close of September 29, 2026).
A. Replacements on account of semi-annual review of broad market indices:
1) Nifty 50
The following company is being excluded:
Sr. No. Company Name Symbol
1 Wipro Ltd. WIPRO
The following company is being included:
Sr. No. Company Name Symbol
1 BSE Ltd. BSE
Note:
1. BSE Ltd. (average free-float market capitalization Rs. 1,40,879 crores) has been included.
b) Nifty Midcap 150
The following companies are being excluded:
Sr. No. Company Name Symbol
1 3M India Ltd. 3MINDIA
2 BSE Ltd. BSE
The following companies are being included:
Sr. No. Company Name Symbol
1 Aster DM Quality Care Ltd. ASTERDM
2
ICICI Prudential Asset Management Company
Ltd. ICICIAMC
3 Kwality Wall's (India) Ltd. DUMMYHDLVR
The above replacements will also be applicable to Nifty Midcap Select.
"""

REVOCATION = """PRESS RELEASE
Replacements in indices
announced exclusion of Vodafone Idea Ltd. (IDEA) from various Nifty indices with effect from September 30, 2024
Sr.
No. Index Name Security Name Symbol Remarks
1 Nifty 500 Vodafone Idea Ltd. IDEA Exclusion revoked
Prism Johnson Ltd. PRSMJOHNSN Exclusion
2 Nifty Midcap 150 Vodafone Idea Ltd. IDEA Exclusion revoked
Central Bank of India CENTRALBK Inclusion revoked
4 Nifty Midcap
Select##
Vodafone Idea Ltd. IDEA Exclusion revoked
5 Nifty Midcap 50
Sona BLW Precision
Forgings Ltd. SONACOMS Inclusion revoked
"""

DELISTING = """These changes shall become effective from August 30, 2024 (close of August 29, 2024).
DVR (Symbol: TATAMTRDVR) shall be excluded from the following indices:
Sr. No.  Index Name
1 Nifty 100
2 Nifty 200
Consequently, the equity shares shall be revised.
"""

DEMERGER = """Kwality Wall's (India) Ltd. with a dummy symbol "DUMMYHDLVR" ... exclude Kwality Wall's (India) Ltd. (KWIL)
from various indices as listed hereunder effective from February 24, 2026 (close of February 23, 2026).
Sr. No. Index Name
1 Nifty 50
"""


def test_parse_semi_annual_with_wrapped_rows_and_dummies():
    out = {c["index"]: c for c in parse_notice(SEMI_ANNUAL, "Replacements in indices w.e.f. September 30, 2026")}
    assert out["NIFTY50"] == {"date": "2026-09-30", "index": "NIFTY50", "added": ["BSE"], "removed": ["WIPRO"]}
    mid = out["NIFTYMIDCAP150"]
    assert mid["removed"] == ["3MINDIA", "BSE"] and mid["added"] == ["ASTERDM", "ICICIAMC"]  # dummy dropped
    assert parse_notice(SEMI_ANNUAL, "Replacements in indices")[0]["date"] == "2026-09-30"  # date from body


def test_parse_revocation_delisting_and_demerger():
    rev = {c["index"]: c for c in parse_notice(REVOCATION, "Replacements in indices", "2024-09-25")}
    assert rev["NIFTY500"]["unremove"] == ["IDEA"] and rev["NIFTY500"]["removed"] == ["PRSMJOHNSN"]
    assert rev["NIFTYMIDCAP150"]["unremove"] == ["IDEA"] and rev["NIFTYMIDCAP150"]["unadd"] == ["CENTRALBK"]
    assert rev["NIFTYMIDCAP50"]["unadd"] == ["SONACOMS"] and "NIFTYMIDCAPSELECT" not in rev
    assert all(c["date"] == "2024-09-30" for c in rev.values())
    dl = {c["index"]: c for c in parse_notice(DELISTING, "Replacements in indices", "2024-08-23")}
    assert dl["NIFTY100"]["removed"] == ["TATAMTRDVR"] and dl["NIFTY200"]["date"] == "2024-08-30"
    assert parse_notice(DEMERGER, "Exclusion of Kwality Wall's (India) Ltd.", "2026-02-20") == []


def test_changes_for_renames_revocations_and_validation():
    renames = [("LTI", "LTIM", "2022-12-05"), ("LTIM", "LTM", "2026-02-27")]
    assert current_symbol("LTI", "2022-01-01", renames) == "LTM"
    assert current_symbol("LTIM", "2026-03-30", renames) == "LTIM"  # used after the rename: a different stock
    changes = [
        {"date": "2023-07-13", "index": "NIFTY50", "added": ["LTIM"], "removed": ["HDFC"]},
        {"date": "2024-09-30", "index": "NIFTY50", "added": ["BEL", "IDEA"], "removed": ["LTIM", "X"]},
        {"date": "2024-09-30", "index": "NIFTY50", "added": [], "removed": [], "unadd": ["IDEA"], "unremove": ["X"]},
        {"date": "2024-09-30", "index": "NIFTYMIDCAP150", "added": ["Q"], "removed": []},
    ]
    log = changes_for(changes, "Nifty 50", renames)
    assert log == [("2023-07-13", ("LTM",), ("HDFC",)), ("2024-09-30", ("BEL",), ("LTM",))]
    today = ["BEL", "A", "B"]
    assert validate(today, log, 3) == []
    bad = validate(["A", "B", "C"], log, 3)
    assert bad[0] == {"date": "2024-09-30", "added_not_in_index": ["BEL"], "removed_still_in_index": []}


def test_list_notices_and_build_history(tmp_path):
    page = """<div class="pressItem" data-date="Aug 10, 2026" hidden="true"><p>Aug 10, 2026</p>
      <a href='/Press_Release/ind_prs10082026.pdf' target="_blank">Replacements in indices w.e.f. September 30, 2026</a></div>
      <div class="pressItem" data-date="Sep 23, 2026"><a href='/Press_Release/ind_prs23092026.pdf'>Changes in Nifty Fixed Income indices</a></div>
      <div class="pressItem" data-date="Jan 05, 2019"><a href='/Press_Release/old.pdf'>Replacements in indices</a></div>"""
    session = FakeSession({("GET", "/press-release"): page, ("GET", "symbolchange.csv"): ""})
    notices = list_notices(session, since="2021-01-01")
    assert [n.title for n in notices] == ["Replacements in indices w.e.f. September 30, 2026"]
    # text already cached, so no PDF download is needed
    cache = tmp_path / "index_notices"
    cache.mkdir()
    (cache / "ind_prs10082026.txt").write_text(SEMI_ANNUAL)
    current = [f"S{i}" for i in range(148)] + ["ASTERDM", "ICICIAMC"]
    meta = build_history("NIFTYMIDCAP150", current, tmp_path, session=session)
    assert meta["changes"] == 1 and meta["problems"] == []
    assert history_path(tmp_path, "NIFTYMIDCAP150").read_text().splitlines()[1] == \
        "2026-09-30,ASTERDM ICICIAMC,3MINDIA BSE"
    m = membership_for("NIFTYMIDCAP150", current, state_dir=tmp_path)
    assert m.source == "NSE press releases" and "BSE" in m.members_on("2026-09-29") and not m.warnings
    assert len(m.members_on("2026-09-29")) == 150
    # a recorded problem becomes a warning on the membership
    meta_path = history_path(tmp_path, "NIFTYMIDCAP150").with_suffix(".json")
    meta_path.write_text(json.dumps({**meta, "problems": [{"date": "2022-03-31"}]}))
    assert "1 inconsistencies" in membership_for("NIFTYMIDCAP150", current, state_dir=tmp_path).warnings[0]
    # point_in_time reuses the fresh history instead of downloading again
    assert point_in_time("NIFTYMIDCAP150", current, tmp_path).source == "NSE press releases"
    assert point_in_time("NIFTY50", ["A"], tmp_path).source == "built-in"
