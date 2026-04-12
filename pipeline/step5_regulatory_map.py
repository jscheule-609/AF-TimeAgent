"""
Step 5: Cross-Border Regulatory Mapping

Maps required jurisdictions based on merger agreement, 10-K revenue, and precedent.
"""
import logging
from models.documents import ParsedTenK, ParsedMergerAgreement
from models.comparables import ComparableGroup
from models.regulatory import JurisdictionRequirement
from config.constants import (
    JURISDICTION_REVENUE_THRESHOLDS,
    MIN_ACTIVATION_RATE_THRESHOLD,
)

logger = logging.getLogger(__name__)


async def map_jurisdictions(
    tenk_acquirer: ParsedTenK | None,
    tenk_target: ParsedTenK | None,
    merger_agreement: ParsedMergerAgreement | None,
    comparable_groups: list[ComparableGroup],
    mars_deal_pk: int | None = None,
) -> list[JurisdictionRequirement]:
    """Determine which jurisdictions are required for this deal."""
    requirements = {}

    # 0. MARS regulatory flags — autoresearch-determined
    if mars_deal_pk:
        try:
            from db.read_autoresearch import (
                load_regulatory_flags_from_mars,
            )
            mars_flags = await load_regulatory_flags_from_mars(
                mars_deal_pk
            )
            for jur, is_required in mars_flags.items():
                requirements[jur] = JurisdictionRequirement(
                    jurisdiction=jur,
                    is_required=is_required,
                    confidence=1.0,
                    source="mars_autoresearch",
                    notes="Determined by autoresearch",
                )
            if mars_flags:
                logger.info(
                    "MARS regulatory flags: "
                    f"{mars_flags}"
                )
        except Exception as e:
            logger.warning(
                f"MARS regulatory flags load failed: {e}"
            )

    # 1. Merger agreement — highest confidence
    if merger_agreement:
        for jur in merger_agreement.required_regulatory_approvals:
            jur_upper = jur.upper()
            requirements[jur_upper] = JurisdictionRequirement(
                jurisdiction=jur_upper,
                is_required=True,
                confidence=1.0,
                source="merger_agreement",
                notes=f"Explicitly required in merger agreement",
            )

    # 2. Revenue threshold analysis
    if tenk_acquirer and tenk_target:
        _check_revenue_thresholds(tenk_acquirer, tenk_target, requirements)

    # 3. Comparable deal precedent
    _check_comparable_precedent(comparable_groups, requirements)

    # 4. CFIUS assessment
    _check_cfius(tenk_acquirer, tenk_target, requirements)

    # HSR is almost always required for US public company deals
    if "HSR" not in requirements:
        requirements["HSR"] = JurisdictionRequirement(
            jurisdiction="HSR",
            is_required=True,
            confidence=0.9,
            source="default_us_public",
            notes="HSR assumed required for US public company M&A",
        )

    return list(requirements.values())


def _check_revenue_thresholds(
    tenk_acquirer: ParsedTenK, tenk_target: ParsedTenK,
    requirements: dict[str, JurisdictionRequirement],
) -> None:
    """Check if geographic revenue triggers filing requirements."""
    acq_segments = {s.region.lower(): s for s in tenk_acquirer.geographic_segments}
    tgt_segments = {s.region.lower(): s for s in tenk_target.geographic_segments}

    # EC check — EU revenue
    eu_regions = {"europe", "emea", "eu", "european union"}
    acq_eu_rev = sum(s.revenue_usd or 0 for k, s in acq_segments.items() if any(r in k for r in eu_regions))
    tgt_eu_rev = sum(s.revenue_usd or 0 for k, s in tgt_segments.items() if any(r in k for r in eu_regions))

    ec_threshold = JURISDICTION_REVENUE_THRESHOLDS["EC"]["eu_turnover_each_party_eur"]
    if acq_eu_rev > ec_threshold and tgt_eu_rev > ec_threshold and "EC" not in requirements:
        requirements["EC"] = JurisdictionRequirement(
            jurisdiction="EC",
            is_required=True,
            confidence=0.8,
            source="revenue_threshold",
            revenue_data={"acquirer_eu_revenue": acq_eu_rev, "target_eu_revenue": tgt_eu_rev},
            notes="EU revenue exceeds EC filing thresholds",
        )

    # CMA check — UK revenue
    uk_regions = {"united kingdom", "uk", "great britain"}
    acq_uk_rev = sum(s.revenue_usd or 0 for k, s in acq_segments.items() if any(r in k for r in uk_regions))
    tgt_uk_rev = sum(s.revenue_usd or 0 for k, s in tgt_segments.items() if any(r in k for r in uk_regions))

    cma_threshold = JURISDICTION_REVENUE_THRESHOLDS["CMA"]["uk_turnover_gbp"]
    if (acq_uk_rev > cma_threshold or tgt_uk_rev > cma_threshold) and "CMA" not in requirements:
        requirements["CMA"] = JurisdictionRequirement(
            jurisdiction="CMA",
            is_required=True,
            confidence=0.8,
            source="revenue_threshold",
            revenue_data={"acquirer_uk_revenue": acq_uk_rev, "target_uk_revenue": tgt_uk_rev},
            notes="UK revenue exceeds CMA filing threshold",
        )

    # SAMR check — China revenue
    china_regions = {"china", "prc", "greater china"}
    acq_cn_rev = sum(s.revenue_usd or 0 for k, s in acq_segments.items() if any(r in k for r in china_regions))
    tgt_cn_rev = sum(s.revenue_usd or 0 for k, s in tgt_segments.items() if any(r in k for r in china_regions))

    samr_threshold = JURISDICTION_REVENUE_THRESHOLDS["SAMR"]["china_turnover_each_party_cny"]
    samr_threshold_usd = samr_threshold / 7.2  # Approximate CNY to USD
    if acq_cn_rev > samr_threshold_usd and tgt_cn_rev > samr_threshold_usd and "SAMR" not in requirements:
        requirements["SAMR"] = JurisdictionRequirement(
            jurisdiction="SAMR",
            is_required=True,
            confidence=0.8,
            source="revenue_threshold",
            revenue_data={"acquirer_china_revenue": acq_cn_rev, "target_china_revenue": tgt_cn_rev},
            notes="China revenue exceeds SAMR filing thresholds",
        )


def _check_comparable_precedent(
    comparable_groups: list[ComparableGroup],
    requirements: dict[str, JurisdictionRequirement],
) -> None:
    """Check if comparable deals suggest jurisdictions not yet identified.

    For SAMR and CFIUS, also uses calibrated activation rates from
    config/calibration.json when comparable precedent is inconclusive
    (confidence < 0.8).
    """
    from config.calibration import get_rate

    all_deals = []
    for group in comparable_groups:
        all_deals.extend(group.deals[:10])

    if not all_deals:
        # No comparables — fall back to calibrated rates only
        _apply_calibrated_activation(requirements)
        return

    jur_counts: dict[str, int] = {}
    for deal in all_deals:
        for jur in deal.jurisdictions_required:
            jur_counts[jur] = jur_counts.get(jur, 0) + 1

    total = len(all_deals)
    for jur, count in jur_counts.items():
        rate = count / total
        if rate > 0.5 and jur not in requirements:
            requirements[jur] = JurisdictionRequirement(
                jurisdiction=jur,
                is_required=True,
                confidence=0.6,
                source="comparable_precedent",
                notes=(
                    f"{count}/{total} comparables "
                    f"required {jur} ({rate:.0%})"
                ),
            )

    # For SAMR/CFIUS not yet in requirements, use calibrated
    # activation rates as a weak signal
    _apply_calibrated_activation(requirements)


def _apply_calibrated_activation(
    requirements: dict[str, JurisdictionRequirement],
) -> None:
    """Add SAMR/CFIUS from calibrated activation rates if missing.

    Only fires when comparable precedent didn't already add them
    and the calibrated rate exceeds MIN_ACTIVATION_RATE_THRESHOLD.
    """
    from config.calibration import get_rate

    cal_map = {
        "SAMR": "samr",
        "CFIUS": "cfius",
    }
    for jur, cal_key in cal_map.items():
        if jur in requirements:
            continue
        cal_rate = get_rate(cal_key)
        if cal_rate is None:
            continue
        if cal_rate < MIN_ACTIVATION_RATE_THRESHOLD:
            continue
        requirements[jur] = JurisdictionRequirement(
            jurisdiction=jur,
            is_required=True,
            confidence=min(cal_rate, 0.6),
            source="calibrated_activation_rate",
            notes=(
                f"Calibrated {jur} activation rate: "
                f"{cal_rate:.1%}"
            ),
        )


def _check_cfius(
    tenk_acquirer: ParsedTenK | None,
    tenk_target: ParsedTenK | None,
    requirements: dict[str, JurisdictionRequirement],
) -> None:
    """Assess CFIUS requirement based on cross-border and sector signals."""
    if "CFIUS" in requirements:
        return

    if not tenk_target:
        return

    # Check for critical technology/infrastructure keywords in target's business
    critical_keywords = [
        "defense", "national security", "critical infrastructure",
        "personal data", "semiconductor", "telecom", "aerospace",
        "encryption", "cyber", "military",
    ]

    target_text = (tenk_target.business_description + " " + tenk_target.products_and_services).lower()
    hits = [kw for kw in critical_keywords if kw in target_text]

    if hits:
        requirements["CFIUS"] = JurisdictionRequirement(
            jurisdiction="CFIUS",
            is_required=False,  # Voluntary but recommended
            confidence=0.4,
            source="sector_assessment",
            notes=f"Target business mentions: {', '.join(hits)}. CFIUS voluntary filing may be advisable.",
        )
