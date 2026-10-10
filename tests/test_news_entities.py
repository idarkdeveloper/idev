"""Headline entity matching: group names must not match sibling companies."""

import pytest

from trading_agent.news import mentions

TATA = {"TATAMOTORS": "Tata Motors Limited", "TATAPOWER": "The Tata Power Company Limited",
        "TATASTEEL": "Tata Steel Limited", "TATACONSUM": "Tata Consumer Products Limited",
        "TATACHEM": "Tata Chemicals Limited", "TCS": "Tata Consultancy Services Limited"}
ADANI = {"ADANIENT": "Adani Enterprises Limited", "ADANIPORTS": "Adani Ports and Special Economic Zone Limited",
         "ADANIPOWER": "Adani Power Limited", "ADANIGREEN": "Adani Green Energy Limited",
         "ADANIENSOL": "Adani Energy Solutions Limited"}


def hits(title, universe):
    return sorted(s for s, n in universe.items() if mentions(title, s, n))


def test_tata_motors_does_not_match_power_or_steel():
    assert hits("Tata Motors shares jump after JLR sales beat estimates", TATA) == ["TATAMOTORS"]


def test_tata_power_does_not_match_motors():
    assert hits("Tata Power wins 500 MW solar order", TATA) == ["TATAPOWER"]


def test_tata_steel_only_matches_itself():
    assert hits("Tata Steel Q2 profit falls", TATA) == ["TATASTEEL"]


def test_bare_tata_group_matches_none():
    assert hits("Tata group plans new chip fab, Chandrasekaran says", TATA) == []
    assert hits("Tata Sons board meets on Friday", TATA) == []


def test_adani_alone_matches_no_single_company():
    assert hits("Adani shares surge as group announces buyback", ADANI) == []
    assert hits("Adani group stocks rally", ADANI) == []


def test_adani_specific_company_matches_only_that_company():
    assert hits("Adani Ports and Special Economic Zone volumes rise 12%", ADANI) == ["ADANIPORTS"]
    assert hits("Adani Power bags order", ADANI) == ["ADANIPOWER"]
    assert hits("Adani Enterprises QIP opens", ADANI) == ["ADANIENT"]


@pytest.mark.parametrize("title", ["Tata Motors", "TATAMOTORS rallies", "tata motors cuts prices"])
def test_tata_motors_positive(title):
    assert mentions(title, "TATAMOTORS", TATA["TATAMOTORS"])
