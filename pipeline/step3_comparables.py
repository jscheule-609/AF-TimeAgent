"""
Step 3: Comparable Deal Lookup + Feature-Weighted Scoring

Queries MARS for 3 groups of comparables, enriches with regulatory data, scores.
"""
import logging
import numpy as np
from datetime import date
from models.deal import DealParameters
from models.documents import ParsedTenK, ParsedMergerAgreement
from models.comparables import ComparableDeal, ComparableGroup, ComparableSource, RegulatoryMilestone
from models.antitrust import OverlapAssessment
from scoring.similarity import compute_similarity_score
from scoring.feature_weights import compute_time_weight
from db.queries_comparables import (
    get_acquirer_prior_deals, get_sector_comparable_deals,
    get_size_matched_deals, get_regulatory_milestones,
)
from config.settings import Settings

logger = logging.getLogger(__name__)


async def find_comparables(
    deal_params: DealParameters,
    tenk_acquirer: ParsedTenK | None,
    tenk_target: ParsedTenK | None,
    merger_agreement: ParsedMergerAgreement | None,
    overlap: OverlapAssessment | None = None,
) -> list[ComparableGroup]:
    """Find and score three groups of comparable deals."""
    import asyncio
    settings = Settings()

    # Query all three groups in parallel
    group_i_raw, group_ii_raw, group_iii_raw = await asyncio.gather(
        get_acquirer_prior_deals(deal_params.acquirer_name, settings.max_comparables_per_group),
        get_sector_comparable_deals(deal_params.industry, settings.comparable_lookback_years, settings.max_comparables_per_group),
        get_size_matched_deals(deal_params.deal_value_usd, settings.comparable_lookback_years, settings.max_comparables_per_group),
        return_exceptions=True,
    )

    # Handle errors gracefully
    if isinstance(group_i_raw, Exception):
        logger.warning(f"Acquirer history query failed: {group_i_raw}")
        group_i_raw = []
    if isinstance(group_ii_raw, Exception):
        logger.warning(f"Sector query failed: {group_ii_raw}")
        group_ii_raw = []
    if isinstance(group_iii_raw, Exception):
        logger.warning(f"Size query failed: {group_iii_raw}")
        group_iii_raw = []

    # Build deal jurisdictions set from merger agreement
    deal_jurisdictions = set()
    if merger_agreement:
        deal_jurisdictions = set(merger_agreement.required_regulatory_approvals)

    # Use a default overlap if none provided
    if overlap is None:
        overlap = OverlapAssessment()

    # Process each group
    groups = []
    for raw_deals, source in [
        (group_i_raw, ComparableSource.ACQUIRER_HISTORY),
        (group_ii_raw, ComparableSource.SECTOR_MATCH),
        (group_iii_raw, ComparableSource.SIZE_MATCH),
    ]:
        deals = []
        for row in raw_deals:
            comp = _row_to_comparable(row, source)
            score, features = compute_similarity_score(
                deal_params, overlap, comp, deal_jurisdictions,
                half_life_months=settings.time_weight_half_life_months,
            )
            # Time weight
            if comp.close_date:
                months = (date.today() - comp.close_date).days / 30.44
            else:
                months = (date.today() - comp.announcement_date).days / 30.44
            tw = compute_time_weight(months, settings.time_weight_half_life_months)

            comp.similarity_score = score
            comp.feature_scores = features
            comp.time_weight = tw
            comp.weighted_score = score * tw
            deals.append(comp)

        # Sort by weighted score
        deals.sort(key=lambda d: d.weighted_score, reverse=True)

        group = _build_group(deals, source)
        groups.append(group)

    return groups


def _row_to_comparable(row: dict, source: ComparableSource) -> ComparableDeal:
    """Convert a MARS database row to a ComparableDeal model."""
    # Build jurisdictions list
    jurisdictions = []
    if row.get("is_hsr_applicable"):
        jurisdictions.append("HSR")
    if row.get("is_ec_approval_required"):
        jurisdictions.append("EC")
    if row.get("is_cma_approval_required"):
        jurisdictions.append("CMA")
    if row.get("is_samr_approval_required"):
        jurisdictions.append("SAMR")
    if row.get("is_cfius_review_required"):
        jurisdictions.append("CFIUS")

    return ComparableDeal(
        deal_pk=row.get("deal_pk", 0),
        deal_id=row.get("deal_id", ""),
        acquirer=row.get("acquirer_name", ""),
        target=row.get("target_name", ""),
        sector=row.get("gics_sector", ""),
        industry=row.get("industry", ""),
        deal_value_usd=row.get("deal_value_usd"),
        deal_structure=row.get("type_of_consideration") or "cash",
        buyer_type=row.get("acquirer_type") or "strategic",
        announcement_date=row.get("date_announced", date.today()),
        close_date=row.get("actual_completion_date"),
        timeline_days=row.get("timeline_days"),
        deal_outcome=row.get("deal_outcome", ""),
        jurisdictions_required=jurisdictions,
        had_second_request=bool(row.get("has_second_request")),
        had_ec_phase_2=row.get("phase_2_date") is not None,
        had_cma_phase_2=row.get("cma_phase_2_outcome") is not None,
        horizontal_overlap=bool(row.get("product_market_overlap")),
        remedy_required=row.get("remedy_feasibility") is not None,
        remedy_type=row.get("remedy_feasibility"),
        source=source,
    )


def _build_group(deals: list[ComparableDeal], source: ComparableSource) -> ComparableGroup:
    """Build a ComparableGroup with aggregate statistics."""
    timeline_days = [d.timeline_days for d in deals if d.timeline_days and d.timeline_days > 0]

    median = int(np.median(timeline_days)) if timeline_days else None
    p25 = int(np.percentile(timeline_days, 25)) if timeline_days else None
    p75 = int(np.percentile(timeline_days, 75)) if timeline_days else None
    p90 = int(np.percentile(timeline_days, 90)) if timeline_days else None

    # Per-jurisdiction stats
    jurisdiction_stats = {}
    for jur in ["HSR", "EC", "CMA", "SAMR"]:
        jur_deals = [d for d in deals if jur in d.jurisdictions_required]
        if not jur_deals:
            continue
        stats = {}
        if jur == "HSR":
            sr_count = sum(1 for d in jur_deals if d.had_second_request)
            stats["second_request_rate"] = sr_count / len(jur_deals) if jur_deals else 0
        if jur == "EC":
            p2_count = sum(1 for d in jur_deals if d.had_ec_phase_2)
            stats["ec_phase_2_rate"] = p2_count / len(jur_deals) if jur_deals else 0
        if jur == "CMA":
            p2_count = sum(1 for d in jur_deals if d.had_cma_phase_2)
            stats["cma_phase_2_rate"] = p2_count / len(jur_deals) if jur_deals else 0
        jurisdiction_stats[jur] = stats

    return ComparableGroup(
        source=source,
        deals=deals,
        count=len(deals),
        median_timeline_days=median,
        p25_timeline_days=p25,
        p75_timeline_days=p75,
        p90_timeline_days=p90,
        jurisdiction_stats=jurisdiction_stats,
    )
