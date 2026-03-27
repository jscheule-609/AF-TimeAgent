"""
Step 4: Antitrust Overlap Assessment

Compares 10-K business descriptions and checks MARS competitive analysis.
Web search signals and deeper antitrust analysis are provided externally
(e.g. by an orchestrating agent or a dedicated antitrust analysis tool).
"""
import logging
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
    external_signals: list[str] | None = None,
    external_overlap: dict | None = None,
) -> OverlapAssessment:
    """Assess competitive overlap between acquirer and target.

    Args:
        external_signals: Web search snippets or other context
            provided by the orchestrating agent.
        external_overlap: Pre-computed overlap assessment from a
            dedicated antitrust analysis tool. When provided,
            used as the primary source.
    """
    # If a dedicated antitrust tool already assessed overlap, use it
    if external_overlap:
        return _build_from_external(external_overlap)

    # Check MARS if we have a deal_pk
    mars_analysis = None
    if mars_deal_pk:
        try:
            mars_analysis = await get_deal_competitive_analysis(
                mars_deal_pk
            )
        except Exception as e:
            logger.warning(
                f"MARS competitive analysis lookup failed: {e}"
            )

    if mars_analysis:
        return _build_from_mars(mars_analysis)

    # Fall back to 10-K analysis + LLM
    if tenk_acquirer and tenk_target:
        return await _assess_from_10k(
            tenk_acquirer, tenk_target,
            web_signals=external_signals or [],
        )

    # Minimal assessment if no data available
    return OverlapAssessment(
        reasoning="Insufficient data for overlap assessment"
    )


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


def _build_from_external(data: dict) -> OverlapAssessment:
    """Build OverlapAssessment from a dedicated antitrust tool."""
    severity = data.get("overlap_severity", "none")
    sr_map = {
        "high": 0.30, "medium": 0.15,
        "low": 0.05, "none": 0.03,
    }
    return OverlapAssessment(
        overlap_type=data.get("overlap_type", "none"),
        overlap_severity=severity,
        specific_overlap_markets=data.get(
            "horizontal_overlap_markets", []
        ),
        mutual_competitor_flag=data.get(
            "lists_each_other", False
        ),
        estimated_combined_share_pct=data.get(
            "combined_market_share_pct"
        ),
        hhi_delta_estimate=data.get("hhi_delta"),
        second_request_probability_base=sr_map.get(
            severity, 0.03
        ),
        web_search_signals=data.get(
            "web_search_signals", []
        ),
        reasoning=data.get("reasoning", ""),
    )


async def _assess_from_10k(
    tenk_acquirer: ParsedTenK,
    tenk_target: ParsedTenK,
    web_signals: list[str] | None = None,
) -> OverlapAssessment:
    """Assess overlap using 10-K analysis and LLM."""
    acq_competitors = [
        c.name for c in tenk_acquirer.competitors
    ]
    tgt_competitors = [
        c.name for c in tenk_target.competitors
    ]

    prompt = ANTITRUST_OVERLAP_PROMPT.format(
        acquirer_name=tenk_acquirer.company_name,
        acquirer_business=(
            tenk_acquirer.business_description[:5000]
        ),
        acquirer_competitors=", ".join(
            acq_competitors[:20]
        ),
        target_name=tenk_target.company_name,
        target_business=(
            tenk_target.business_description[:5000]
        ),
        target_competitors=", ".join(
            tgt_competitors[:20]
        ),
    )

    try:
        settings = Settings()
        llm_result = await call_llm(
            prompt, model=settings.reasoning_model
        )
    except Exception as e:
        logger.error(
            f"LLM overlap assessment failed: {e}"
        )
        llm_result = {}

    overlap_type = llm_result.get("overlap_type", "none")
    severity = llm_result.get("overlap_severity", "none")
    sr_map = {
        "high": 0.30, "medium": 0.15,
        "low": 0.05, "none": 0.03,
    }

    return OverlapAssessment(
        overlap_type=overlap_type,
        overlap_severity=severity,
        specific_overlap_markets=llm_result.get(
            "horizontal_overlap_markets", []
        ),
        mutual_competitor_flag=llm_result.get(
            "lists_each_other", False
        ),
        second_request_probability_base=sr_map.get(
            severity, 0.03
        ),
        web_search_signals=web_signals or [],
        reasoning=llm_result.get("reasoning", ""),
    )
