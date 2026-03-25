"""Merger agreement parsing for regulatory provisions and timing data."""
from datetime import date
from typing import Optional
from models.documents import ParsedMergerAgreement, MergerAgreementProvision
from parsers.llm_extraction import call_llm, MERGER_AGREEMENT_EXTRACTION_PROMPT


async def parse_merger_agreement(text: str) -> ParsedMergerAgreement:
    """Extract regulatory and timing provisions from merger agreement text."""
    prompt = MERGER_AGREEMENT_EXTRACTION_PROMPT.format(text=text[:25000])
    extracted = await call_llm(prompt)

    outside_date = _parse_date(extracted.get("outside_date"))
    extended_outside_date = _parse_date(extracted.get("extended_outside_date"))

    # Normalize jurisdiction names in required approvals
    approvals = _normalize_approval_list(
        extracted.get("required_regulatory_approvals", [])
    )

    return ParsedMergerAgreement(
        efforts_standard=extracted.get("efforts_standard", "reasonable best efforts"),
        hsr_filing_deadline_days=extracted.get("hsr_filing_deadline_days"),
        ec_filing_deadline_days=extracted.get("ec_filing_deadline_days"),
        other_filing_deadlines=extracted.get("other_filing_deadlines", {}),
        required_regulatory_approvals=approvals,
        outside_date=outside_date,
        outside_date_extensions=extracted.get("outside_date_extensions", []),
        extended_outside_date=extended_outside_date,
        target_termination_fee_usd=extracted.get("target_termination_fee_usd"),
        reverse_termination_fee_usd=extracted.get("reverse_termination_fee_usd"),
        has_ticking_fee=extracted.get("has_ticking_fee", False),
        ticking_fee_details=extracted.get("ticking_fee_details"),
        divestiture_commitment=extracted.get("divestiture_commitment"),
        litigation_commitment=extracted.get("litigation_commitment", False),
    )


def _parse_date(value: Optional[str]) -> Optional[date]:
    """Safely parse a date string."""
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def _normalize_approval_list(approvals: list[str]) -> list[str]:
    """Normalize regulatory approval names to standard codes."""
    mapping = {
        "hart-scott-rodino": "HSR",
        "hsr": "HSR",
        "european commission": "EC",
        "eu merger regulation": "EC",
        "cma": "CMA",
        "competition and markets authority": "CMA",
        "samr": "SAMR",
        "state administration for market regulation": "SAMR",
        "cfius": "CFIUS",
        "accc": "ACCC",
    }
    normalized = []
    for a in approvals:
        a_lower = a.lower().strip()
        if a_lower in mapping:
            normalized.append(mapping[a_lower])
        elif a.upper() in ("HSR", "EC", "CMA", "SAMR", "CFIUS", "ACCC"):
            normalized.append(a.upper())
        else:
            normalized.append(a)
    return normalized
