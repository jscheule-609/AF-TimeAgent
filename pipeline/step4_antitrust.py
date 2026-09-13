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

# deal_competitive_analysis.product_market_overlap /
# geographic_market_overlap vocabulary (varchar, MARS v2):
#   NONE | LIMITED | MODERATE | SIGNIFICANT   (NULL = never assessed)
_OVERLAP_LEVEL_TO_SEVERITY = {
    "none": "none",
    "limited": "low",
    "moderate": "medium",
    "significant": "high",
}
_SEVERITY_RANK = {"none": 0, "low": 1, "medium": 2, "high": 3}


def _clean(value) -> str:
    return (value or "").strip().lower() if isinstance(value, str) else ""


def mars_row_is_informative(mars: dict | None) -> bool:
    """True when the MARS competitive-analysis row carries an actual
    assessment: an antitrust_risk_rating, or a product/geographic overlap
    flag (including an explicit NONE).

    deal_competitive_analysis has a row for ~5,400 deals but only ~965 of
    them were ever filled in; the rest are all-NULL placeholders.  A
    placeholder must NOT short-circuit the 10-K + LLM fallback (it did
    until 2026-09-13: ``if mars_analysis:`` is truthy for any dict).
    """
    if not mars:
        return False
    if _clean(mars.get("antitrust_risk_rating")) in ("high", "medium", "low"):
        return True
    return bool(
        _clean(mars.get("product_market_overlap"))
        or _clean(mars.get("geographic_market_overlap"))
    )


def _overlap_level_severity(mars: dict) -> str:
    """Strongest of the two overlap flags mapped to a severity bucket."""
    best = "none"
    for col in ("product_market_overlap", "geographic_market_overlap"):
        raw = _clean(mars.get(col))
        if not raw:
            continue
        # Unknown non-empty vocabulary counts as a weak positive flag
        sev = _OVERLAP_LEVEL_TO_SEVERITY.get(raw, "low")
        if _SEVERITY_RANK[sev] > _SEVERITY_RANK[best]:
            best = sev
    return best


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

    if mars_row_is_informative(mars_analysis):
        return _build_from_mars(mars_analysis)
    if mars_analysis:
        logger.info(
            f"MARS competitive analysis row for deal_pk={mars_deal_pk} "
            f"is a placeholder (no rating / overlap flag); using 10-K path"
        )

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
    """Build OverlapAssessment from MARS v2 competitive analysis data."""
    # Overlap flags are a NONE/LIMITED/MODERATE/SIGNIFICANT vocabulary;
    # the old truthiness test turned an explicit "NONE" into horizontal.
    level_severity = _overlap_level_severity(mars)
    overlap_type = "horizontal" if level_severity != "none" else "none"

    severity = "none"
    # v2 provides antitrust_risk_rating directly (NULL corpus-wide as of
    # 2026-09; kept as the highest-precedence signal)
    risk_rating = _clean(mars.get("antitrust_risk_rating"))
    share = mars.get("combined_market_share_pct")

    if risk_rating in ("high", "medium", "low"):
        severity = risk_rating
    elif share:
        if share > 40:
            severity = "high"
        elif share > 25:
            severity = "medium"
        elif share > 10:
            severity = "low"
    else:
        # No rating and no share estimate: the analyst's overlap level is
        # the only severity signal (SIGNIFICANT overlap was "none" before).
        severity = level_severity

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
        mutual_competitor_flag=False,
        estimated_combined_share_pct=share,
        hhi_delta_estimate=mars.get("hhi_delta"),
        second_request_probability_base=base_sr_prob,
        reasoning="Based on MARS v2 competitive analysis data",
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
