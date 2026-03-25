"""Regulatory climate assessment — classify enforcement regime per jurisdiction."""
from datetime import date
from models.climate import EnforcementRegime, RegulatoryClimate
from config.constants import ENFORCEMENT_REGIMES
from db.queries_climate import get_enforcement_stats, get_sector_enforcement_intensity


# Historical baseline rates (approximate averages over 2015-2023)
HISTORICAL_BASELINES = {
    "hsr_second_request_rate": 0.03,  # ~3% of HSR filings get 2nd request
    "ec_phase_2_rate": 0.05,          # ~5% of EC filings go to Phase 2
    "cma_phase_2_rate": 0.10,         # ~10% of CMA Phase 1 cases referred
    "litigation_rate": 0.01,          # ~1% of deals face antitrust litigation
}


async def assess_regulatory_climate(
    sector: str | None = None,
    industry: str | None = None,
) -> RegulatoryClimate:
    """
    Assess current regulatory enforcement climate across jurisdictions.
    Compares recent enforcement stats to historical baselines.
    """
    stats = await get_enforcement_stats(months=24)
    sector_stats = await get_sector_enforcement_intensity()

    regimes = []

    # HSR regime assessment
    hsr_regime = _classify_hsr_regime(stats)
    regimes.append(hsr_regime)

    # EC regime assessment
    ec_regime = _classify_ec_regime(stats)
    regimes.append(ec_regime)

    # CMA regime assessment
    cma_regime = _classify_cma_regime(stats)
    regimes.append(cma_regime)

    # Sector-specific adjustments
    if sector or industry:
        _apply_sector_adjustments(regimes, sector_stats, sector, industry)

    # Overall regime = most common, default to normal
    regime_counts = {}
    for r in regimes:
        regime_counts[r.regime] = regime_counts.get(r.regime, 0) + 1
    overall = max(regime_counts, key=regime_counts.get) if regime_counts else "normal"

    return RegulatoryClimate(
        regimes=regimes,
        overall_regime=overall,
        assessment_date=date.today(),
        data_points_used=stats.get("total_deals", 0),
    )


def _classify_hsr_regime(stats: dict) -> EnforcementRegime:
    """Classify HSR enforcement regime based on second request rate."""
    total = stats.get("total_deals", 0)
    second_requests = stats.get("second_requests", 0)

    if total == 0:
        return _make_regime("HSR", "normal")

    rate = second_requests / total
    baseline = HISTORICAL_BASELINES["hsr_second_request_rate"]

    if rate > baseline * 1.5:
        return _make_regime("HSR", "aggressive")
    elif rate < baseline * 0.5:
        return _make_regime("HSR", "lenient")
    return _make_regime("HSR", "normal")


def _classify_ec_regime(stats: dict) -> EnforcementRegime:
    """Classify EC enforcement regime based on Phase 2 rate."""
    ec_total = stats.get("ec_total", 0)
    ec_phase_2 = stats.get("ec_phase_2_count", 0)

    if ec_total == 0:
        return _make_regime("EC", "normal")

    rate = ec_phase_2 / ec_total
    baseline = HISTORICAL_BASELINES["ec_phase_2_rate"]

    if rate > baseline * 1.5:
        return _make_regime("EC", "aggressive")
    elif rate < baseline * 0.5:
        return _make_regime("EC", "lenient")
    return _make_regime("EC", "normal")


def _classify_cma_regime(stats: dict) -> EnforcementRegime:
    """Classify CMA enforcement regime based on Phase 2 referral rate."""
    cma_total = stats.get("cma_total", 0)
    cma_phase_2 = stats.get("cma_phase_2_count", 0)

    if cma_total == 0:
        return _make_regime("CMA", "normal")

    rate = cma_phase_2 / cma_total
    baseline = HISTORICAL_BASELINES["cma_phase_2_rate"]

    if rate > baseline * 1.5:
        return _make_regime("CMA", "aggressive")
    elif rate < baseline * 0.5:
        return _make_regime("CMA", "lenient")
    return _make_regime("CMA", "normal")


def _make_regime(jurisdiction: str, regime: str) -> EnforcementRegime:
    """Create an EnforcementRegime with proper multipliers from constants."""
    regime_config = ENFORCEMENT_REGIMES.get(regime, ENFORCEMENT_REGIMES["normal"])
    return EnforcementRegime(
        jurisdiction=jurisdiction,
        regime=regime,
        label=regime_config["label"],
        multipliers={
            "second_request_multiplier": regime_config["second_request_multiplier"],
            "phase_2_multiplier": regime_config["phase_2_multiplier"],
            "pre_notification_multiplier": regime_config["pre_notification_multiplier"],
        },
    )


def _apply_sector_adjustments(
    regimes: list[EnforcementRegime],
    sector_stats: list[dict],
    sector: str | None,
    industry: str | None,
) -> None:
    """Adjust regime classifications based on sector-specific enforcement intensity."""
    # Find this sector's stats
    sector_data = None
    for s in sector_stats:
        if (industry and s.get("industry") == industry) or (sector and s.get("gics_sector") == sector):
            sector_data = s
            break

    if not sector_data:
        return

    litigation = sector_data.get("litigation_count", 0)
    reg_breaks = sector_data.get("regulatory_breaks", 0)

    # If sector has elevated litigation/breaks, bump all regimes toward aggressive
    if litigation >= 3 or reg_breaks >= 2:
        for regime in regimes:
            if regime.regime == "normal":
                regime.regime = "aggressive"
                aggressive_config = ENFORCEMENT_REGIMES["aggressive"]
                regime.label = aggressive_config["label"] + f" (sector: {industry or sector})"
                regime.multipliers = {
                    "second_request_multiplier": aggressive_config["second_request_multiplier"],
                    "phase_2_multiplier": aggressive_config["phase_2_multiplier"],
                    "pre_notification_multiplier": aggressive_config["pre_notification_multiplier"],
                }
