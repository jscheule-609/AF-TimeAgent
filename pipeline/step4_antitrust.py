"""
Step 4: Antitrust Overlap Assessment

Compares 10-K business descriptions, checks MARS competitive analysis, runs web search.
"""
import logging
import httpx
from models.documents import ParsedTenK
from models.antitrust import OverlapAssessment
from parsers.llm_extraction import call_llm, ANTITRUST_OVERLAP_PROMPT
from db.queries_regulatory import get_deal_competitive_analysis
from config.settings import Settings

logger = logging.getLogger(__name__)


async def assess_antitrust_overlap(
    tenk_acquirer: ParsedTenK | None,
    tenk_target: ParsedTenK | None,
    mars_deal_pk: int | None = None,
) -> OverlapAssessment:
    """Assess competitive overlap between acquirer and target."""

    # Check MARS first if we have a deal_pk
    mars_analysis = None
    if mars_deal_pk:
        try:
            mars_analysis = await get_deal_competitive_analysis(mars_deal_pk)
        except Exception as e:
            logger.warning(f"MARS competitive analysis lookup failed: {e}")

    # If MARS has data, use it as primary source
    if mars_analysis:
        return _build_from_mars(mars_analysis)

    # Otherwise, use 10-K analysis + LLM + web search
    if tenk_acquirer and tenk_target:
        return await _assess_from_10k(tenk_acquirer, tenk_target)

    # Minimal assessment if no data available
    return OverlapAssessment(reasoning="Insufficient data for overlap assessment")


def _build_from_mars(mars: dict) -> OverlapAssessment:
    """Build OverlapAssessment from MARS competitive analysis data."""
    overlap_type = "none"
    if mars.get("product_market_overlap"):
        overlap_type = "horizontal"
    elif mars.get("geographic_market_overlap"):
        overlap_type = "horizontal"

    severity = "none"
    share = mars.get("combined_market_share_pct")
    if share:
        if share > 40:
            severity = "high"
        elif share > 25:
            severity = "medium"
        elif share > 10:
            severity = "low"

    mutual = (
        bool(mars.get("target_lists_acquirer_competitor"))
        and bool(mars.get("acquirer_lists_target_competitor"))
    )

    base_sr_prob = 0.03
    if severity == "high":
        base_sr_prob = 0.35
    elif severity == "medium":
        base_sr_prob = 0.18
    elif severity == "low":
        base_sr_prob = 0.08

    return OverlapAssessment(
        overlap_type=overlap_type,
        overlap_severity=severity,
        mutual_competitor_flag=mutual,
        estimated_combined_share_pct=share,
        hhi_delta_estimate=mars.get("hhi_delta"),
        second_request_probability_base=base_sr_prob,
        reasoning="Based on MARS competitive analysis data",
    )


async def _assess_from_10k(
    tenk_acquirer: ParsedTenK, tenk_target: ParsedTenK,
) -> OverlapAssessment:
    """Assess overlap using 10-K analysis, LLM, and web search."""
    # Check if they list each other as competitors
    acq_competitors = [c.name for c in tenk_acquirer.competitors]
    tgt_competitors = [c.name for c in tenk_target.competitors]

    # LLM-based overlap assessment
    prompt = ANTITRUST_OVERLAP_PROMPT.format(
        acquirer_name=tenk_acquirer.company_name,
        acquirer_business=tenk_acquirer.business_description[:5000],
        acquirer_competitors=", ".join(acq_competitors[:20]),
        target_name=tenk_target.company_name,
        target_business=tenk_target.business_description[:5000],
        target_competitors=", ".join(tgt_competitors[:20]),
    )

    try:
        llm_result = await call_llm(prompt)
    except Exception as e:
        logger.error(f"LLM overlap assessment failed: {e}")
        llm_result = {}

    # Web search signals
    web_signals = await _search_antitrust_signals(
        tenk_acquirer.company_name, tenk_target.company_name
    )

    overlap_type = llm_result.get("overlap_type", "none")
    severity = llm_result.get("overlap_severity", "none")

    base_sr_prob = {"high": 0.30, "medium": 0.15, "low": 0.05, "none": 0.03}.get(severity, 0.03)

    return OverlapAssessment(
        overlap_type=overlap_type,
        overlap_severity=severity,
        specific_overlap_markets=llm_result.get("horizontal_overlap_markets", []),
        mutual_competitor_flag=llm_result.get("lists_each_other", False),
        second_request_probability_base=base_sr_prob,
        web_search_signals=web_signals,
        reasoning=llm_result.get("reasoning", ""),
    )


async def _search_antitrust_signals(acquirer_name: str, target_name: str) -> list[str]:
    """Run Brave web searches to size antitrust risk."""
    settings = Settings()
    if not settings.brave_api_key:
        return []

    signals = []
    queries = [
        f"{acquirer_name} {target_name} antitrust competition",
        f"{acquirer_name} {target_name} market share",
    ]

    async with httpx.AsyncClient(timeout=30.0) as client:
        for query in queries:
            try:
                response = await client.get(
                    "https://api.search.brave.com/res/v1/web/search",
                    headers={"X-Subscription-Token": settings.brave_api_key},
                    params={"q": query, "count": 5},
                )
                if response.status_code == 200:
                    data = response.json()
                    for result in data.get("web", {}).get("results", [])[:3]:
                        signals.append(
                            f"{result.get('title', '')}: {result.get('description', '')[:200]}"
                        )
            except Exception as e:
                logger.warning(f"Brave search failed for '{query}': {e}")

    return signals
