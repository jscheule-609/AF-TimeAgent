"""
Step 5: Cross-Border Regulatory Mapping

Maps required jurisdictions from five signal layers:
  0. MARS regulatory flags (regulatory_reviews)
  0b. MARS deal_conditions (PUCs, banking, SAMR, etc.)
  1. Merger agreement required approvals
  2. Revenue thresholds (10-K geographic segments)
  2b. State-level revenue for regulated industries (PUC inference)
  3. Comparable deal precedent (jurisdiction flags + condition patterns)
  4. CFIUS sector assessment
"""
import logging
from db.connection import get_pool
from models.deal import DealParameters
from models.documents import ParsedTenK, ParsedMergerAgreement
from models.comparables import ComparableGroup
from models.regulatory import JurisdictionRequirement
from config.constants import (
    JURISDICTION_REVENUE_THRESHOLDS,
    MIN_ACTIVATION_RATE_THRESHOLD,
)

logger = logging.getLogger(__name__)

# Map known condition names → jurisdiction codes
_CONDITION_TO_JURISDICTION = {
    "china antitrust": "SAMR",
    "samr": "SAMR",
    "uk antitrust": "CMA",
    "cma": "CMA",
    "ec antitrust": "EC",
    "eu antitrust": "EC",
    "european commission": "EC",
    "cfius": "CFIUS",
    "investment canada": "INVESTMENT_CANADA",
    "australia firb": "AUSTRALIA_FIRB",
    "accc": "ACCC",
    "federal reserve": "FED_RESERVE",
    "fdic": "FDIC",
    "occ": "OCC",
    "state insurance": "STATE_INSURANCE",
    "other foreign investment": "FOREIGN_INVESTMENT",
    "foreign investment": "FOREIGN_INVESTMENT",
    "korea ftc": "KFTC",
    "japan antitrust": "JFTC",
    "japan ftc": "JFTC",
    "brazil antitrust": "CADE",
    "india cci": "CCI",
}

# US states with PUC regulatory authority over utilities
_PUC_STATES = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "de",
    "fl", "ga", "hi", "id", "il", "in", "ia", "ks",
    "ky", "la", "me", "md", "ma", "mi", "mn", "ms",
    "mo", "mt", "ne", "nv", "nh", "nj", "nm", "ny",
    "nc", "nd", "oh", "ok", "or", "pa", "ri", "sc",
    "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv",
    "wi", "wy",
}


async def map_jurisdictions(
    tenk_acquirer: ParsedTenK | None,
    tenk_target: ParsedTenK | None,
    merger_agreement: ParsedMergerAgreement | None,
    comparable_groups: list[ComparableGroup],
    mars_deal_pk: int | None = None,
    deal_params: DealParameters | None = None,
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

    # 0b. MARS deal_conditions — PUCs, banking, foreign
    if mars_deal_pk:
        await _check_deal_conditions(
            mars_deal_pk, requirements,
        )

    # 1. Merger agreement — highest confidence
    if merger_agreement:
        for jur in merger_agreement.required_regulatory_approvals:
            # Normalize known jurisdiction names
            jur_norm = _CONDITION_TO_JURISDICTION.get(
                jur.lower().strip(), jur.upper(),
            )
            if jur_norm not in requirements:
                requirements[jur_norm] = JurisdictionRequirement(
                    jurisdiction=jur_norm,
                    is_required=True,
                    confidence=1.0,
                    source="merger_agreement",
                    notes=(
                        f"Explicitly required: {jur}"
                    ),
                )

    # 2. Revenue threshold analysis
    if tenk_acquirer and tenk_target:
        _check_revenue_thresholds(
            tenk_acquirer, tenk_target, requirements,
            deal_params=deal_params,
        )

    # 3. Comparable deal precedent (jurisdiction flags +
    #    condition patterns)
    await _check_comparable_precedent(
        comparable_groups, requirements,
    )

    # 4. CFIUS assessment
    _check_cfius(tenk_acquirer, tenk_target, requirements)

    # HSR is almost always required for US public company deals
    if "HSR" not in requirements:
        requirements["HSR"] = JurisdictionRequirement(
            jurisdiction="HSR",
            is_required=True,
            confidence=0.9,
            source="default_us_public",
            notes=(
                "HSR assumed required for US public "
                "company M&A"
            ),
        )

    return list(requirements.values())


async def _check_deal_conditions(
    deal_pk: int,
    requirements: dict[str, JurisdictionRequirement],
) -> None:
    """Map deal_conditions to jurisdiction requirements.

    Reads ALL condition families — not just antitrust.
    State PUCs, banking regulators, and foreign investment
    conditions are mapped to jurisdiction codes that route
    through GenericStateMachine.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT DISTINCT condition_name, condition_family "
            "FROM deal_conditions WHERE deal_pk = $1",
            deal_pk,
        )

    if not rows:
        return

    for row in rows:
        name = (row["condition_name"] or "").strip()
        family = (row["condition_family"] or "").lower()
        name_lower = name.lower()

        # Skip antitrust conditions already handled by
        # regulatory_reviews + merger agreement path
        if family == "antitrust" and name_lower in (
            "hsr", "hart-scott-rodino",
        ):
            continue

        # Known condition → jurisdiction mapping
        jur = _CONDITION_TO_JURISDICTION.get(name_lower)

        # PUC pattern: "{STATE} PUC" or "{STATE} PUC {detail}"
        if jur is None and "puc" in name_lower:
            parts = name.split()
            if parts:
                state = parts[0].upper()
                if state.lower() in _PUC_STATES:
                    jur = f"STATE_PUC_{state}"
                elif "state" in name_lower or "various" in name_lower:
                    jur = "STATE_PUC_MULTI"
                else:
                    jur = f"STATE_PUC_{state}"

        # Antitrust conditions not in the standard map
        # (e.g. "China Antitrust", "Korea FTC")
        if jur is None and family == "antitrust":
            if "china" in name_lower:
                jur = "SAMR"
            elif "korea" in name_lower:
                jur = "KFTC"
            elif "japan" in name_lower:
                jur = "JFTC"
            elif "brazil" in name_lower:
                jur = "CADE"
            elif "india" in name_lower:
                jur = "CCI"
            else:
                # Generic antitrust — use the condition name
                jur = name.upper().replace(" ", "_")

        # Foreign investment conditions
        if jur is None and family == "foreign_investment":
            if "other" in name_lower:
                jur = "FOREIGN_INVESTMENT_OTHER"
            else:
                jur = name.upper().replace(" ", "_")

        # SEC conditions (informational — not modeled as
        # a separate jurisdiction, affects proxy timeline)
        if family == "sec_effectiveness":
            continue

        # Shareholder approval (handled by proxy timeline)
        if "sh approval" in name_lower:
            continue

        if jur and jur not in requirements:
            requirements[jur] = JurisdictionRequirement(
                jurisdiction=jur,
                is_required=True,
                confidence=0.95,
                source="deal_conditions",
                notes=f"Condition: {name} ({family})",
            )

    condition_count = sum(
        1 for j in requirements.values()
        if j.source == "deal_conditions"
    )
    if condition_count:
        logger.info(
            f"Deal conditions: {condition_count} "
            f"jurisdictions from deal_conditions"
        )


def _check_revenue_thresholds(
    tenk_acquirer: ParsedTenK, tenk_target: ParsedTenK,
    requirements: dict[str, JurisdictionRequirement],
    deal_params: DealParameters | None = None,
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
    samr_threshold_usd = samr_threshold / 7.2
    if (
        acq_cn_rev > samr_threshold_usd
        and tgt_cn_rev > samr_threshold_usd
        and "SAMR" not in requirements
    ):
        requirements["SAMR"] = JurisdictionRequirement(
            jurisdiction="SAMR",
            is_required=True,
            confidence=0.8,
            source="revenue_threshold",
            revenue_data={
                "acquirer_china_revenue": acq_cn_rev,
                "target_china_revenue": tgt_cn_rev,
            },
            notes="China revenue exceeds SAMR filing thresholds",
        )

    # State PUC inference for regulated industries
    # (utilities, insurance, banking)
    regulated_sectors = {
        "utilities", "financials",
    }
    sector = ""
    if deal_params:
        sector = (deal_params.gics_sector or "").lower()

    if sector in regulated_sectors:
        _check_state_puc_revenue(
            tenk_target, requirements, sector,
        )


def _check_state_puc_revenue(
    tenk_target: ParsedTenK,
    requirements: dict[str, JurisdictionRequirement],
    sector: str,
) -> None:
    """For regulated industries, check target's state-level
    revenue to infer PUC approval requirements.

    Utilities with >$100M revenue in a state likely need
    that state's PUC approval.
    """
    threshold = 100_000_000  # $100M
    if sector == "financials":
        threshold = 500_000_000  # $500M for banking

    tgt_segs = {
        s.region.lower(): s
        for s in tenk_target.geographic_segments
    }

    for region_key, seg in tgt_segs.items():
        rev = seg.revenue_usd or 0
        if rev < threshold:
            continue
        # Match state abbreviations or names
        for state in _PUC_STATES:
            if state in region_key or state.upper() in region_key:
                jur = f"STATE_PUC_{state.upper()}"
                if jur not in requirements:
                    requirements[jur] = JurisdictionRequirement(
                        jurisdiction=jur,
                        is_required=True,
                        confidence=0.7,
                        source="revenue_threshold_state",
                        revenue_data={
                            "target_state_revenue": rev,
                        },
                        notes=(
                            f"Target has ${rev/1e6:.0f}M "
                            f"revenue in {state.upper()}"
                        ),
                    )


async def _check_comparable_precedent(
    comparable_groups: list[ComparableGroup],
    requirements: dict[str, JurisdictionRequirement],
) -> None:
    """Check if comparable deals suggest jurisdictions not yet
    identified — both from their jurisdiction flags AND from
    their deal_conditions patterns.

    If >60% of comps had a specific condition (e.g. "CA PUC"),
    infer this deal likely needs it too.
    """
    from config.calibration import get_rate

    all_deals = []
    for group in comparable_groups:
        all_deals.extend(group.deals[:10])

    if not all_deals:
        _apply_calibrated_activation(requirements)
        return

    # Jurisdiction flags from comps
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

    # Condition patterns from comps — infer PUC/banking/
    # foreign investment conditions that this deal may need
    await _infer_conditions_from_comps(
        all_deals, requirements,
    )

    _apply_calibrated_activation(requirements)


async def _infer_conditions_from_comps(
    comp_deals: list,
    requirements: dict[str, JurisdictionRequirement],
) -> None:
    """Query deal_conditions for comparable deal PKs and
    infer jurisdiction requirements from condition patterns.

    If >60% of comps had a specific condition type (e.g.
    PUC approvals), add it for this deal.
    """
    comp_pks = [d.deal_pk for d in comp_deals if d.deal_pk]
    if not comp_pks:
        return

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT condition_name, condition_family,
                   COUNT(DISTINCT deal_pk) AS n
            FROM deal_conditions
            WHERE deal_pk = ANY($1::bigint[])
              AND condition_family IN ('other',
                  'foreign_investment')
            GROUP BY condition_name, condition_family
            ORDER BY n DESC
            """,
            comp_pks,
        )

    total = len(comp_pks)
    threshold = 0.6  # 60% of comps must have the condition

    for row in rows:
        rate = row["n"] / total
        if rate < threshold:
            continue

        name = (row["condition_name"] or "").strip()
        name_lower = name.lower()

        # Map to jurisdiction code
        jur = _CONDITION_TO_JURISDICTION.get(name_lower)
        if jur is None and "puc" in name_lower:
            parts = name.split()
            if parts:
                state = parts[0].upper()
                jur = f"STATE_PUC_{state}"

        if jur and jur not in requirements:
            requirements[jur] = JurisdictionRequirement(
                jurisdiction=jur,
                is_required=True,
                confidence=0.55,
                source="comparable_conditions",
                notes=(
                    f"{row['n']}/{total} comps had "
                    f"'{name}' ({rate:.0%})"
                ),
            )


def _apply_calibrated_activation(
    requirements: dict[str, JurisdictionRequirement],
) -> None:
    """Add probabilistic SAMR/CFIUS applicability from population rates.

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
            is_required=False,
            confidence=cal_rate,
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
