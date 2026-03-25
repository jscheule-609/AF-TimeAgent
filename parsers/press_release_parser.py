"""Press release / 8-K parsing for deal timing extraction."""
from datetime import date
from models.documents import PressReleaseData
from parsers.llm_extraction import call_llm, PRESS_RELEASE_EXTRACTION_PROMPT


JURISDICTION_MAP = {
    "hart-scott-rodino": "HSR",
    "hsr": "HSR",
    "antitrust": "HSR",
    "european commission": "EC",
    "ec merger": "EC",
    "eu antitrust": "EC",
    "cma": "CMA",
    "competition and markets authority": "CMA",
    "samr": "SAMR",
    "china antitrust": "SAMR",
    "cfius": "CFIUS",
    "committee on foreign investment": "CFIUS",
    "accc": "ACCC",
}


def normalize_jurisdictions(raw_jurisdictions: list[str]) -> list[str]:
    """Map raw jurisdiction mentions to standard codes."""
    normalized = set()
    for j in raw_jurisdictions:
        j_lower = j.lower().strip()
        if j_lower in JURISDICTION_MAP:
            normalized.add(JURISDICTION_MAP[j_lower])
        elif j.upper() in ("HSR", "EC", "CMA", "SAMR", "CFIUS", "ACCC"):
            normalized.add(j.upper())
        else:
            normalized.add(j)
    return sorted(normalized)


async def parse_press_release(text: str, announcement_date: date) -> PressReleaseData:
    """Extract deal timing data from a press release using LLM."""
    prompt = PRESS_RELEASE_EXTRACTION_PROMPT.format(text=text[:15000])
    extracted = await call_llm(prompt)

    jurisdictions = normalize_jurisdictions(
        extracted.get("mentioned_jurisdictions", [])
    )

    stated_close_date = None
    if extracted.get("stated_close_date"):
        try:
            stated_close_date = date.fromisoformat(extracted["stated_close_date"])
        except (ValueError, TypeError):
            pass

    return PressReleaseData(
        announcement_date=announcement_date,
        stated_close_timeline=extracted.get("stated_close_timeline"),
        stated_close_date=stated_close_date,
        mentioned_jurisdictions=jurisdictions,
        mentioned_conditions=extracted.get("mentioned_conditions", []),
        deal_rationale_excerpt="",
        stated_synergies=extracted.get("stated_synergies"),
        outside_date_mentioned=extracted.get("outside_date_mentioned"),
        raw_timing_language=extracted.get("timing_language", ""),
    )
