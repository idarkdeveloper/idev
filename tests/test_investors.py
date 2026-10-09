import pytest

from trading_agent.investors import (BROKER_DESK, CORPORATE, INDIVIDUAL, INSTITUTION, PROMOTER,
                                     classify_client, describe)


@pytest.mark.parametrize("name,expected", [
    ("ASHISH KACHOLIA", INDIVIDUAL),
    ("THAKKAR NILESHKUMAR FARSHURAM HUF", INDIVIDUAL),
    ("RARE ENTERPRISES", INDIVIDUAL),
    ("ALPHAGREP SECURITIES PRIVATE LIMITED", BROKER_DESK),
    ("IRAGE BROKING SERVICES LLP", BROKER_DESK),
    ("PI OPPORTUNITIES AIF V LLP", INSTITUTION),
    ("SBI MUTUAL FUND", INSTITUTION),
    ("GOLDMAN SACHS (SINGAPORE) PTE", BROKER_DESK),  # global bank desks trade as prop/ODI
    ("L7 HITECH PRIVATE LIMITED", CORPORATE),
])
def test_classify(name, expected):
    assert classify_client(name) == expected


def test_insider_source_is_promoter_and_describe():
    assert classify_client("Salil Parekh", "insider") == PROMOTER
    assert "insider" in describe(PROMOTER) and describe("nope") == describe("unknown")


@pytest.mark.parametrize("name,expected", [
    ("BNP PARIBAS FINANCIAL MARKETS", BROKER_DESK),
    ("SOCIETE GENERALE", BROKER_DESK),
    ("SHRENI SHARES PVT", BROKER_DESK),
    ("BAAHUBALI ENTERPRISE", CORPORATE),
    ("CORE INC", CORPORATE),
    ("KACHOLIA ASHISH", INDIVIDUAL),
    ("VISHAL MAHESH WAGHELA", INDIVIDUAL),
])
def test_classify_survey_names(name, expected):
    assert classify_client(name) == expected
