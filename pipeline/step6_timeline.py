"""
Step 6: Timeline Assembly & Output

Builds the milestone table, scenario paths, risk flags, and summary statistics.
"""
import logging
from datetime import date, timedelta
from models.deal import DealParameters
from models.documents import ParsedMergerAgreement, PressReleaseData
from models.state_machine import FullSimulationResult
from models.timeline import (
    MilestoneRow, ScenarioPath, RiskFlag, DealTimingReport,
)

logger = logging.getLogger(__name__)


async def assemble_timeline(
    simulation: FullSimulationResult,
    press_release: PressReleaseData | None,
    merger_agreement: ParsedMergerAgreement | None,
    deal_params: DealParameters,
) -> DealTimingReport:
    """Assemble the final timeline report from simulation results."""
    announcement = deal_params.announcement_date

    # Build milestone rows
    milestones = _build_milestones(simulation, merger_agreement, announcement)

    # Build scenario paths
    scenarios = _build_scenarios(simulation, announcement)

    # Build risk flags
    risk_flags = _build_risk_flags(simulation, merger_agreement)

    # Compute summary dates
    p50_date = announcement + timedelta(days=simulation.critical_path_duration_p50) if simulation.critical_path_duration_p50 else None
    p75_date = announcement + timedelta(days=simulation.critical_path_duration_p75) if simulation.critical_path_duration_p75 else None
    p90_date = announcement + timedelta(days=simulation.critical_path_duration_p90) if simulation.critical_path_duration_p90 else None

    # Probability of closing by outside date
    prob_by_outside = None
    outside_date = merger_agreement.outside_date if merger_agreement else None
    if outside_date and scenarios:
        prob_by_outside = sum(
            s.probability_pct for s in scenarios
            if s.expected_close_date and s.expected_close_date <= outside_date
        )

    return DealTimingReport(
        acquirer=deal_params.acquirer_name,
        target=deal_params.target_name,
        deal_value_usd=deal_params.deal_value_usd,
        announcement_date=announcement,
        milestones=milestones,
        scenarios=scenarios,
        risk_flags=risk_flags,
        p50_close_date=p50_date,
        p75_close_date=p75_date,
        p90_close_date=p90_date,
        probability_close_by_outside_date=prob_by_outside,
        outside_date=outside_date,
        critical_path_jurisdiction=simulation.critical_path_jurisdiction,
        enforcement_regime="normal",
        comparable_deals_used=0,
        generated_at=date.today().isoformat(),
    )


def _build_milestones(
    simulation: FullSimulationResult,
    merger_agreement: ParsedMergerAgreement | None,
    announcement: date,
) -> list[MilestoneRow]:
    """Build milestone rows from jurisdiction simulations."""
    milestones = []

    for jur_sim in simulation.jurisdictions:
        jur_name = jur_sim.jurisdiction.value
        filing_offset = jur_sim.contractual_filing_deadline_days or 10

        # Filing milestone
        filing_date = announcement + timedelta(days=filing_offset)
        milestones.append(MilestoneRow(
            milestone=f"{jur_name} Filing",
            jurisdiction=jur_name,
            contractual_deadline=jur_sim.contractual_filing_deadline,
            base_case_date=filing_date,
            extended_case_date=filing_date,
            stress_case_date=filing_date,
        ))

        # Clearance milestone
        milestones.append(MilestoneRow(
            milestone=f"{jur_name} Clearance",
            jurisdiction=jur_name,
            base_case_date=announcement + timedelta(days=jur_sim.expected_duration_days_p50),
            extended_case_date=announcement + timedelta(days=jur_sim.expected_duration_days_p75),
            stress_case_date=announcement + timedelta(days=jur_sim.expected_duration_days_p90),
            risk_flags=[p.path_label for p in jur_sim.possible_paths if not p.is_terminal_clear and p.path_probability > 0.05],
        ))

    # Expected Close milestone
    milestones.append(MilestoneRow(
        milestone="Expected Close",
        jurisdiction="",
        contractual_deadline=merger_agreement.outside_date if merger_agreement else None,
        base_case_date=announcement + timedelta(days=simulation.critical_path_duration_p50) if simulation.critical_path_duration_p50 else None,
        extended_case_date=announcement + timedelta(days=simulation.critical_path_duration_p75) if simulation.critical_path_duration_p75 else None,
        stress_case_date=announcement + timedelta(days=simulation.critical_path_duration_p90) if simulation.critical_path_duration_p90 else None,
    ))

    return milestones


def _build_scenarios(
    simulation: FullSimulationResult, announcement: date,
) -> list[ScenarioPath]:
    """Build joint scenario paths across all jurisdictions."""
    scenarios = []

    if not simulation.jurisdictions:
        return scenarios

    # Scenario 1: Clean — all Phase 1 / initial clears
    clean_prob = 1.0
    clean_duration = 0
    clean_paths = {}
    for jur_sim in simulation.jurisdictions:
        clean_path = None
        for p in jur_sim.possible_paths:
            if p.is_terminal_clear and "Clean" in p.path_label:
                clean_path = p
                break
        if not clean_path and jur_sim.possible_paths:
            clean_path = jur_sim.possible_paths[0]
        if clean_path:
            clean_prob *= clean_path.path_probability
            clean_duration = max(clean_duration, clean_path.total_duration_days_p50)
            clean_paths[jur_sim.jurisdiction.value] = clean_path.path_id

    scenarios.append(ScenarioPath(
        scenario_name="Clean — No Extended Reviews",
        probability_pct=round(clean_prob * 100, 1),
        expected_close_date=announcement + timedelta(days=clean_duration),
        duration_days=clean_duration,
        description="All jurisdictions clear at initial review / Phase 1",
        jurisdiction_paths=clean_paths,
    ))

    # Scenario 2+: Extended reviews per jurisdiction
    for jur_sim in simulation.jurisdictions:
        for path in jur_sim.possible_paths:
            if not path.is_terminal_clear:
                continue
            if "Phase 2" in path.path_label or "Second Request" in path.path_label:
                scenarios.append(ScenarioPath(
                    scenario_name=f"{jur_sim.jurisdiction.value} Extended Review",
                    probability_pct=round(path.path_probability * 100, 1),
                    expected_close_date=announcement + timedelta(days=path.total_duration_days_p75),
                    duration_days=path.total_duration_days_p75,
                    description=path.path_label,
                    jurisdiction_paths={jur_sim.jurisdiction.value: path.path_id},
                ))

    # Scenario: Break
    break_prob = 0.0
    for jur_sim in simulation.jurisdictions:
        for path in jur_sim.possible_paths:
            if not path.is_terminal_clear:
                break_prob += path.path_probability
    if break_prob > 0.01:
        scenarios.append(ScenarioPath(
            scenario_name="Break — Deal Terminates",
            probability_pct=round(min(break_prob, 1.0) * 100, 1),
            duration_days=0,
            description="Deal fails to close due to regulatory block or walk-away",
        ))

    # Normalize probabilities
    total = sum(s.probability_pct for s in scenarios)
    if total > 0 and total != 100:
        for s in scenarios:
            s.probability_pct = round(s.probability_pct / total * 100, 1)

    scenarios.sort(key=lambda s: s.probability_pct, reverse=True)
    return scenarios


def _build_risk_flags(
    simulation: FullSimulationResult,
    merger_agreement: ParsedMergerAgreement | None,
) -> list[RiskFlag]:
    """Build risk flag list from simulation results."""
    flags = []

    for jur_sim in simulation.jurisdictions:
        jur = jur_sim.jurisdiction.value

        # Second request / Phase 2 probability flags
        for path in jur_sim.possible_paths:
            if "Second Request" in path.path_label and path.path_probability > 0.15:
                flags.append(RiskFlag(
                    flag=f"Second request probability: {path.path_probability:.0%}",
                    severity="high" if path.path_probability > 0.25 else "medium",
                    jurisdiction=jur,
                    detail=f"{jur} second request probability elevated",
                ))
            if "Phase 2" in path.path_label and path.path_probability > 0.10:
                flags.append(RiskFlag(
                    flag=f"Phase 2 probability: {path.path_probability:.0%}",
                    severity="high" if path.path_probability > 0.20 else "medium",
                    jurisdiction=jur,
                    detail=f"{jur} Phase 2 referral probability elevated",
                ))

    # Ticking fee flag
    if merger_agreement and merger_agreement.has_ticking_fee:
        flags.append(RiskFlag(
            flag="Ticking fee active",
            severity="low",
            detail=merger_agreement.ticking_fee_details or "Ticking fee applies",
        ))

    return flags
