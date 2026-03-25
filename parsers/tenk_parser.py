"""10-K geographic revenue, competitors, and business description parser."""
from datetime import date
from typing import Optional
from models.documents import ParsedTenK, GeographicSegment, CompetitorInfo
from parsers.llm_extraction import call_llm, TENK_GEOGRAPHIC_EXTRACTION_PROMPT


async def parse_tenk(
    text: str,
    company_ticker: str,
    company_name: str,
    fiscal_year_end: date,
    filing_date: date,
) -> ParsedTenK:
    """Parse a 10-K filing text to extract geographic, competitive, and business data."""
    # Extract geographic segments via LLM
    geo_prompt = TENK_GEOGRAPHIC_EXTRACTION_PROMPT.format(text=text[:20000])
    geo_data = await call_llm(geo_prompt)

    segments = []
    for seg in geo_data.get("segments", []):
        segments.append(GeographicSegment(
            region=seg.get("region", "Unknown"),
            revenue_usd=seg.get("revenue_usd"),
            revenue_pct=seg.get("revenue_pct"),
        ))

    total_revenue = geo_data.get("total_revenue_usd")

    # Extract competitors from text
    competitors = await _extract_competitors(text, company_name)

    # Split text into sections
    item1_text = _extract_section(text, "item 1", "item 1a") or text[:30000]
    item1a_text = _extract_section(text, "item 1a", "item 2") or ""
    competition_section = _extract_subsection(item1_text, "competition") or ""
    products_section = _extract_subsection(item1_text, "products") or ""

    return ParsedTenK(
        company_ticker=company_ticker,
        company_name=company_name,
        fiscal_year_end=fiscal_year_end,
        filing_date=filing_date,
        geographic_segments=segments,
        total_revenue_usd=total_revenue,
        competitors=competitors,
        business_description=item1_text[:10000],
        competition_section=competition_section[:5000],
        risk_factors_excerpt=item1a_text[:5000],
        products_and_services=products_section[:5000],
        full_item1_text=item1_text,
        full_item1a_text=item1a_text,
    )


async def _extract_competitors(text: str, company_name: str) -> list[CompetitorInfo]:
    """Extract competitor mentions from 10-K text using LLM."""
    prompt = f"""From this 10-K excerpt for {company_name}, extract all mentioned competitors.

Return as JSON:
{{
  "competitors": [
    {{
      "name": "competitor name",
      "context": "sentence where mentioned",
      "relationship": "direct" | "indirect" | "potential"
    }}
  ]
}}

Return ONLY valid JSON.

Text:
{text[:15000]}"""

    result = await call_llm(prompt)
    competitors = []
    for c in result.get("competitors", []):
        competitors.append(CompetitorInfo(
            name=c.get("name", ""),
            context=c.get("context", ""),
            relationship=c.get("relationship", "direct"),
        ))
    return competitors


def _extract_section(text: str, start_marker: str, end_marker: str) -> Optional[str]:
    """Extract a section of text between two markers (case-insensitive)."""
    text_lower = text.lower()
    start_idx = text_lower.find(start_marker)
    if start_idx == -1:
        return None
    end_idx = text_lower.find(end_marker, start_idx + len(start_marker))
    if end_idx == -1:
        return text[start_idx:start_idx + 50000]
    return text[start_idx:end_idx]


def _extract_subsection(text: str, keyword: str) -> Optional[str]:
    """Extract a subsection around a keyword."""
    text_lower = text.lower()
    idx = text_lower.find(keyword)
    if idx == -1:
        return None
    # Take ~5000 chars around the keyword
    start = max(0, idx - 500)
    end = min(len(text), idx + 5000)
    return text[start:end]
