"""LLM-based extraction helpers via OpenRouter."""
import json
import re
import httpx
from typing import Optional


async def call_llm(
    prompt: str,
    system_prompt: str = "You are a financial document extraction assistant. Return ONLY valid JSON.",
    model: Optional[str] = None,
    api_key: Optional[str] = None,
) -> dict:
    """Call OpenRouter LLM and return parsed JSON response."""
    from config.settings import Settings
    settings = Settings()

    model = model or settings.extraction_model
    api_key = api_key or settings.openrouter_api_key

    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.0,
            },
        )
        response.raise_for_status()
        data = response.json()

    content = data["choices"][0]["message"]["content"]
    return parse_json_response(content)


def parse_json_response(text: str) -> dict:
    """Parse JSON from LLM response, handling markdown fences and malformed output."""
    text = text.strip()

    # Strip markdown code fences
    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()

    # Try direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try to find JSON object or array in the text
    for pattern in [r"\{.*\}", r"\[.*\]"]:
        match = re.search(pattern, text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                continue

    raise ValueError(f"Could not parse JSON from LLM response: {text[:200]}")


# ═══════════════════════════════════════════════════════════
# Prompt Templates
# ═══════════════════════════════════════════════════════════

PRESS_RELEASE_EXTRACTION_PROMPT = """You are extracting deal timing data from an M&A press release.

Extract the following as JSON (use null for missing values):
{{
  "stated_close_timeline": "string — e.g., 'second half of 2024', 'Q1 2025'",
  "stated_close_date": "YYYY-MM-DD or null",
  "mentioned_jurisdictions": ["list of regulatory bodies mentioned — e.g., 'HSR', 'European Commission', 'SAMR', 'CMA'"],
  "mentioned_conditions": ["list of closing conditions mentioned"],
  "timing_language": "exact quote of the timing/closing sentence(s)",
  "outside_date_mentioned": "string or null",
  "stated_synergies": "string or null"
}}

Rules:
- Map jurisdiction mentions to standard codes: HSR, EC, CMA, SAMR, CFIUS, ACCC
- "antitrust" alone without jurisdiction should map to ["HSR"] if US-only deal
- "customary regulatory approvals" = note but don't enumerate
- Return ONLY valid JSON, no other text.

Press release text:
{text}"""

TENK_GEOGRAPHIC_EXTRACTION_PROMPT = """Extract all geographic/regional revenue data from this 10-K excerpt.

Return as JSON:
{{
  "fiscal_year": "YYYY",
  "total_revenue_usd": number or null,
  "segments": [
    {{
      "region": "standard region name",
      "revenue_usd": number or null,
      "revenue_pct": number or null
    }}
  ]
}}

Standardize regions to: "United States", "EMEA", "Europe", "Asia-Pacific",
"China", "Japan", "Americas", "Latin America", "United Kingdom", "Rest of World".
If the filing uses non-standard regions, map to the closest standard name.
Return ONLY valid JSON.

10-K excerpt:
{text}"""

MERGER_AGREEMENT_EXTRACTION_PROMPT = """From this merger agreement text, extract regulatory and timing provisions.

Return as JSON:
{{
  "efforts_standard": "exact wording — e.g., 'reasonable best efforts'",
  "hsr_filing_deadline_days": integer or null,
  "ec_filing_deadline_days": integer or null,
  "other_filing_deadlines": {{"jurisdiction": days}},
  "required_regulatory_approvals": ["list of explicitly required approvals"],
  "outside_date": "YYYY-MM-DD or null",
  "outside_date_extensions": ["description of extension conditions"],
  "extended_outside_date": "YYYY-MM-DD or null",
  "target_termination_fee_usd": number or null,
  "reverse_termination_fee_usd": number or null,
  "has_ticking_fee": boolean,
  "ticking_fee_details": "string or null",
  "divestiture_commitment": "string or null — e.g., 'no cap', 'up to $500M revenue', 'hell or high water'",
  "litigation_commitment": boolean,
  "hell_or_high_water": boolean
}}

Rules:
- "business days" vs "calendar days" matters — specify which
- Map regulatory mentions to standard codes: HSR, EC, CMA, SAMR, CFIUS, ACCC
- For outside_date, use the initial date, not the extended date
- Return ONLY valid JSON.

Merger agreement text:
{text}"""

ANTITRUST_OVERLAP_PROMPT = """Given these two company business descriptions and competitor lists, assess competitive overlap.

Company A ({acquirer_name}):
Business: {acquirer_business}
Competitors mentioned: {acquirer_competitors}

Company B ({target_name}):
Business: {target_business}
Competitors mentioned: {target_competitors}

Return as JSON:
{{
  "overlap_type": "horizontal" | "vertical" | "conglomerate" | "mixed" | "none",
  "overlap_severity": "high" | "medium" | "low" | "none",
  "horizontal_overlap_markets": ["list of specific product/service markets where both compete"],
  "vertical_relationships": ["describe any buyer-supplier relationships"],
  "mutual_competitors": ["companies listed as competitors by both"],
  "lists_each_other": boolean,
  "estimated_competitive_intensity": "description of how directly they compete",
  "antitrust_concern_level": "high" | "medium" | "low" | "none",
  "reasoning": "brief explanation"
}}

Return ONLY valid JSON."""
