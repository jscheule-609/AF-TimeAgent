"""Merger agreement parsing for regulatory provisions and timing data."""
import re
from datetime import date
from typing import Optional
from models.documents import ParsedMergerAgreement
from parsers.llm_extraction import (
    call_llm, MERGER_AGREEMENT_EXTRACTION_PROMPT,
)


# Keywords that anchor the regulatory/timing sections of a
# merger agreement or S-4 registration statement.
_SECTION_ANCHORS = [
    r"conditions\s+to\s+(the\s+)?merger",
    r"conditions\s+to\s+closing",
    r"regulatory\s+matters",
    r"regulatory\s+approv",
    r"antitrust",
    r"efforts?\s+standard",
    r"reasonable\s+best\s+efforts",
    r"outside\s+date",
    r"termination\s+fee",
    r"hart.scott.rodino",
]


def _extract_regulatory_excerpt(
    text: str, max_chars: int = 25000,
) -> str:
    """Extract the most relevant excerpt for merger agreement parsing.

    For short documents (< max_chars), return the full text.
    For long documents (e.g. S-4 registration statements), find
    the section containing regulatory provisions and return a
    window around it.
    """
    if len(text) <= max_chars:
        return text

    # Score positions by anchor keyword density
    text_lower = text.lower()
    hits: list[int] = []
    for pattern in _SECTION_ANCHORS:
        for m in re.finditer(pattern, text_lower):
            hits.append(m.start())

    if not hits:
        # No anchors found — fall back to first N chars
        return text[:max_chars]

    # Find the densest cluster of hits
    hits.sort()
    best_start = hits[0]
    best_count = 0
    window = max_chars

    for i, pos in enumerate(hits):
        # Count how many hits fall within a window starting at pos
        count = sum(
            1 for h in hits[i:]
            if h < pos + window
        )
        if count > best_count:
            best_count = count
            best_start = pos

    # Back up a bit to capture section headers
    start = max(0, best_start - 2000)
    end = min(len(text), start + max_chars)
    return text[start:end]


async def parse_merger_agreement(
    text: str,
) -> ParsedMergerAgreement:
    """Extract regulatory and timing provisions from text."""
    excerpt = _extract_regulatory_excerpt(text, max_chars=25000)
    prompt = MERGER_AGREEMENT_EXTRACTION_PROMPT.format(
        text=excerpt
    )
    extracted = await call_llm(prompt)

    outside_date = _parse_date(extracted.get("outside_date"))
    extended_outside_date = _parse_date(
        extracted.get("extended_outside_date")
    )

    approvals = _normalize_approval_list(
        extracted.get("required_regulatory_approvals", [])
    )

    return ParsedMergerAgreement(
        efforts_standard=(
            extracted.get("efforts_standard")
            or "reasonable best efforts"
        ),
        hsr_filing_deadline_days=extracted.get(
            "hsr_filing_deadline_days"
        ),
        ec_filing_deadline_days=extracted.get(
            "ec_filing_deadline_days"
        ),
        other_filing_deadlines=extracted.get(
            "other_filing_deadlines", {}
        ),
        required_regulatory_approvals=approvals,
        outside_date=outside_date,
        outside_date_extensions=extracted.get(
            "outside_date_extensions", []
        ),
        extended_outside_date=extended_outside_date,
        target_termination_fee_usd=extracted.get(
            "target_termination_fee_usd"
        ),
        reverse_termination_fee_usd=extracted.get(
            "reverse_termination_fee_usd"
        ),
        has_ticking_fee=extracted.get("has_ticking_fee", False),
        ticking_fee_details=extracted.get("ticking_fee_details"),
        divestiture_commitment=extracted.get(
            "divestiture_commitment"
        ),
        litigation_commitment=extracted.get(
            "litigation_commitment", False
        ),
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
        "hsr act": "HSR",
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
        elif a.upper() in (
            "HSR", "EC", "CMA", "SAMR", "CFIUS", "ACCC",
        ):
            normalized.append(a.upper())
        else:
            normalized.append(a)
    return normalized
