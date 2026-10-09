"""Classify who is behind a disclosed deal.

Evidence on Indian block trades says the market reaction depends on *who* buys: promoter
and institutional purchases carry information, while a broker's proprietary desk or an
arbitrage shop usually does not. Names are matched the way NSE prints them.
"""

from __future__ import annotations

import re

PROMOTER = "promoter_insider"
INSTITUTION = "institution"
INDIVIDUAL = "individual"
BROKER_DESK = "broker_desk"
CORPORATE = "corporate"
UNKNOWN = "unknown"

WEIGHT = {
    PROMOTER: "high: insider with information advantage",
    INSTITUTION: "high: institutional allocation, usually researched",
    INDIVIDUAL: "medium: known investor, check their track record",
    CORPORATE: "low: corporate treasury or group entity, often not a view on the stock",
    BROKER_DESK: "low: broker/prop/arbitrage desk, likely acting for a client or hedging",
    UNKNOWN: "unknown",
}

_BROKER = re.compile(r"\b(SECURITIES|BROKING|STOCK ?BROKERS?|SHARE ?BROKERS?|SHAREBROKERS|SHARES\b|"
                     r"FINANCIAL MARKETS|BNP PARIBAS|SOCIETE GENERALE|MORGAN STANLEY|GOLDMAN SACHS|"
                     r"CITIGROUP|CITIBANK|JP ?MORGAN|HSBC|BARCLAYS|UBS\b|NOMURA|MERRILL|DEUTSCHE|"
                     r"CLSA|JEFFERIES|MACQUARIE|KOTAK MAHINDRA|AXIS BANK|"
                     r"CAPITAL MARKETS|COMMODITIES|FINSERV|FINVEST|TRADERS?|ARBITRAGE|QUANT|"
                     r"ALPHAGREP|IRAGE|GRAVITON|TOWER RESEARCH|JANE STREET|CITADEL|OPTIVER|"
                     r"DOLAT|NUVAMA|MARWADI|GLOBE CAPITAL|ANGEL ONE|ZERODHA|MOTILAL OSWAL FINANCIAL|"
                     r"KOTAK SECURITIES|HDFC SECURITIES|ICICI SECURITIES|QE SECURITIES|APT PORTFOLIO)\b")
_INSTITUTION = re.compile(r"\b(MUTUAL FUND|MF\b|AMC|ASSET MANAGEMENT|FUND\b|FUNDS\b|AIF|ALTERNATIVE INVESTMENT|"
                          r"TRUST\b|TRUSTEE|INSURANCE|LIFE\b|BANK\b|FPI|FII|SICAV|PLC\b|LP\b|L\.P\.|"
                          r"PARTNERS\b|VENTURES\b|PRIVATE EQUITY|EQUITY PARTNERS|OPPORTUNITIES|"
                          r"MAURITIUS|SINGAPORE PTE|PTE\b|LUXEMBOURG|IRELAND|CAYMAN|PENSION|ENDOWMENT|"
                          r"SOVEREIGN|GOVERNMENT OF|GIC\b|ADIA|NORGES|VANGUARD|BLACKROCK|ISHARES|"
                          r"INVESTMENT MANAGERS?|PORTFOLIO MANAGERS?|PMS\b|SMALLCAP WORLD|"
                          r"EMERGING MARKETS?|INDIA FUND|MASTER FUND|OFFSHORE|ODI\b)\b")
_CORPORATE = re.compile(r"\b(LIMITED|LTD\.?|PRIVATE LIMITED|PVT\.? ?LTD\.?|LLP|CORPORATION|CORP\.?|"
                        r"INDUSTRIES|ENTERPRISES|HOLDINGS|INVESTMENTS?|TRADING|EXPORTS?|"
                        r"INFRA|FINANCE|FINANCIAL SERVICES|CAPITAL|ADVISORS?|CONSULTANTS?|"
                        r"PROPERTIES|REALTY|TEXTILES|TECHNOLOGIES|SOLUTIONS|ENTERPRISES?|ASSOCIATES|"
                        r"INC\.?|INTERNATIONAL|WEALTH|GEMS|JEWELS?|COMMODITY|PVT\.?|PRIVATELIMITED|"
                        r"PRIVATE\b|COMPANY|CO\.|AGENCIES|MARKETING|IMPEX|OVERSEAS|GLOBAL|GROUP)\b")
_INDIVIDUAL_HINT = re.compile(r"\b(HUF|MR\.?|MRS\.?|MS\.?|SHRI|SMT\.?|DR\.?)\b")


# Investment vehicles of well-known individual investors that NSE prints as companies.
KNOWN_INDIVIDUAL_VEHICLES = {"RARE ENTERPRISES", "RARE INVESTMENTS", "LUCKY INVESTMENT MANAGERS",
                             "BENGAL FINANCE & INVESTMENT", "BENGAL FINANCE AND INVESTMENT",
                             "KEDIA SECURITIES", "PARAM CAPITAL RESEARCH", "MALABAR INDIA FUND"}


def classify_client(name: str, source: str = "") -> str:
    """Return one of the category constants for a bulk/block/insider counterparty name."""
    n = (name or "").upper().strip()
    if not n:
        return UNKNOWN
    if source == "insider":
        return PROMOTER
    if any(n.startswith(v) for v in KNOWN_INDIVIDUAL_VEHICLES):
        return INDIVIDUAL
    if _BROKER.search(n):
        return BROKER_DESK
    if _INSTITUTION.search(n):
        return INSTITUTION
    if _CORPORATE.search(n):
        return CORPORATE
    if _INDIVIDUAL_HINT.search(n):
        return INDIVIDUAL
    # Plain personal names: 2-5 alphabetic words, no corporate suffix.
    words = n.split()
    if 1 <= len(words) <= 5 and all(w.replace(".", "").isalpha() for w in words):
        return INDIVIDUAL
    return UNKNOWN


def describe(category: str) -> str:
    return WEIGHT.get(category, WEIGHT[UNKNOWN])
