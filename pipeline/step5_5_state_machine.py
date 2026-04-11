"""
Step 5.5: Regulatory State Machine Simulation

Runs state machine simulation for each required jurisdiction.
"""
import logging
from models.deal import DealParameters
from models.documents import ParsedMergerAgreement
from models.regulatory import JurisdictionRequirement
from models.antitrust import OverlapAssessment
from models.climate import RegulatoryClimate
from models.comparables import ComparableGroup
from models.state_machine import FullSimulationResult, JurisdictionSimulation
from state_machines.hsr import HSRStateMachine
from state_machines.ec import ECStateMachine
from state_machines.cma import CMAStateMachine
from state_machines.samr import SAMRStateMachine
from state_machines.cfius import CFIUSStateMachine
from state_machines.accc import ACCCStateMachine
from state_machines.generic import GenericStateMachine
from scoring.climate import assess_regulatory_climate

logger = logging.getLogger(__name__)

MACHINE_MAP = {
    "HSR": HSRStateMachine,
    "EC": ECStateMachine,
    "CMA": CMAStateMachine,
    "SAMR": SAMRStateMachine,
    "CFIUS": CFIUSStateMachine,
    "ACCC": ACCCStateMachine,
}


async def simulate_regulatory_paths(
    regulatory_map: list[JurisdictionRequirement],
    overlap: OverlapAssessment,
    comparable_groups: list[ComparableGroup],
    deal_params: DealParameters,
    merger_agreement: ParsedMergerAgreement | None,
    climate: RegulatoryClimate | None = None,
) -> FullSimulationResult:
    """Run state machine simulation for all required jurisdictions."""

    # Assess regulatory climate if not provided
    if climate is None:
        try:
            climate = await assess_regulatory_climate(
                sector=deal_params.sector, industry=deal_params.industry
            )
        except Exception as e:
            logger.warning(f"Climate assessment failed, using defaults: {e}")
            climate = RegulatoryClimate()

    # Aggregate comparable stats across all groups
    comparable_stats = _aggregate_comparable_stats(comparable_groups)

    # Run simulation for each required jurisdiction
    simulations: list[JurisdictionSimulation] = []

    for req in regulatory_map:
        if not req.is_required and req.confidence < 0.6:
            continue

        machine_class = MACHINE_MAP.get(req.jurisdiction, GenericStateMachine)
        machine = machine_class()

        # Get contractual filing deadline
        filing_deadline_days = None
        if merger_agreement:
            if req.jurisdiction == "HSR":
                filing_deadline_days = merger_agreement.hsr_filing_deadline_days
            elif req.jurisdiction == "EC":
                filing_deadline_days = merger_agreement.ec_filing_deadline_days
            else:
                filing_deadline_days = merger_agreement.other_filing_deadlines.get(req.jurisdiction)

        try:
            sim = machine.simulate(
                announcement_date=deal_params.announcement_date,
                overlap=overlap,
                climate=climate,
                comparable_stats=comparable_stats,
                contractual_filing_deadline_days=filing_deadline_days,
            )
            sim.is_required = req.is_required
            sim.confidence_required = req.confidence
            sim.source_of_requirement = req.source
            simulations.append(sim)
        except Exception as e:
            logger.error(f"Simulation failed for {req.jurisdiction}: {e}")

    if not simulations:
        return FullSimulationResult(
            jurisdictions=[],
            critical_path_jurisdiction="",
            critical_path_duration_p50=0,
            critical_path_duration_p75=0,
            critical_path_duration_p90=0,
        )

    # Determine critical path — jurisdiction with longest expected duration
    critical = max(simulations, key=lambda s: s.expected_duration_days_p50)

    # Also check p75/p90 for potential bottleneck shifts
    critical_p75 = max(simulations, key=lambda s: s.expected_duration_days_p75)
    critical_p90 = max(simulations, key=lambda s: s.expected_duration_days_p90)

    return FullSimulationResult(
        jurisdictions=simulations,
        critical_path_jurisdiction=critical.jurisdiction.value,
        critical_path_duration_p50=critical.expected_duration_days_p50,
        critical_path_duration_p75=critical_p75.expected_duration_days_p75,
        critical_path_duration_p90=critical_p90.expected_duration_days_p90,
    )


def _aggregate_comparable_stats(groups: list[ComparableGroup]) -> dict:
    """Aggregate jurisdiction-level stats across all comparable groups."""
    stats = {
        "second_request_rate": 0.095,
        "ec_phase_2_rate": 0.03,
        "cma_phase_2_rate": 0.05,
        "hsr_median_days_to_clear": 35,
    }

    all_jur_stats: dict[str, list[dict]] = {}
    for group in groups:
        for jur, jstats in group.jurisdiction_stats.items():
            all_jur_stats.setdefault(jur, []).append(jstats)

    # Average across groups
    for jur, stat_list in all_jur_stats.items():
        for key in stat_list[0]:
            values = [s.get(key, 0) for s in stat_list if key in s]
            if values:
                stats[key] = sum(values) / len(values)

    return stats
