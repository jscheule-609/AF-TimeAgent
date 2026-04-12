"""
Step 6: Timeline Assembly & Output

Builds the milestone table, scenario paths, risk flags, and summary
statistics.  Close-date predictions use the **two-track parallel
model**: the regulatory track (state machine + empirical pre-filing)
runs in parallel with the proxy/shareholder-vote track (fully
empirical from comparable deals).  Close = max(regulatory, proxy,
empirical total).
"""
import logging
from datetime import date, timedelta
from models.deal import DealParameters, DealStructure
from models.documents import ParsedMergerAgreement, PressReleaseData
from models.state_machine import FullSimulationResult
from models.timeline import (
    MilestoneRow, ScenarioPath, RiskFlag, DealTimingReport,
)

logger = logging.getLogger(__name__)


def _ts_val(
    timeline_stats: dict, interval: str, pct: str,
) -> float:
    """Safely extract a percentile value from timeline_stats."""
    entry = timeline_stats.get(interval, {})
    val = entry.get(pct)
    return float(val) if val is not None else 0.0


async def assemble_timeline(
    simulation: FullSimulationResult,
    press_release: PressReleaseData | None,
    merger_agreement: ParsedMergerAgreement | None,
    deal_params: DealParameters,
    timeline_stats: dict | None = None,
) -> DealTimingReport:
    """Assemble the final timeline report.

    When *timeline_stats* is provided (from step3b), close-date
    predictions use comparable-driven empirical durations.
    Otherwise falls back to the state-machine-only model.
    """
    announcement = deal_params.announcement_date
    ts = timeline_stats or {}
    is_tender = (
        deal_params.deal_structure == DealStructure.TENDER
    )

    # ── Compute close dates ──────────────────────────────

    if ts:
        # Track 1: Regulatory (empirical pre-filing + review)
        prefiling = _ts_val(ts, "announcement_to_filing", "p50")
        review = _ts_val(ts, "filing_to_clearance", "p50")
        reg_p50 = prefiling + review if (prefiling and review) else 0

        prefiling75 = _ts_val(ts, "announcement_to_filing", "p75")
        review75 = _ts_val(ts, "filing_to_clearance", "p75")
        reg_p75 = prefiling75 + review75 if (prefiling75 and review75) else 0

        prefiling90 = _ts_val(ts, "announcement_to_filing", "p90")
        review90 = _ts_val(ts, "filing_to_clearance", "p90")
        reg_p90 = prefiling90 + review90 if (prefiling90 and review90) else 0

        # Track 2: Proxy/shareholder vote (empirical)
        vote_p50 = _ts_val(ts, "announcement_to_vote", "p50")
        post_vote_p50 = _ts_val(ts, "vote_to_close", "p50")
        proxy_p50 = vote_p50 + post_vote_p50 if (vote_p50 and post_vote_p50) else 0

        vote_p75 = _ts_val(ts, "announcement_to_vote", "p75")
        post_vote_p75 = _ts_val(ts, "vote_to_close", "p75")
        proxy_p75 = vote_p75 + post_vote_p75 if (vote_p75 and post_vote_p75) else 0

        vote_p90 = _ts_val(ts, "announcement_to_vote", "p90")
        post_vote_p90 = _ts_val(ts, "vote_to_close", "p90")
        proxy_p90 = vote_p90 + post_vote_p90 if (vote_p90 and post_vote_p90) else 0

        # Track 3: Empirical total (sanity floor)
        total_p50 = _ts_val(ts, "announcement_to_close", "p50")
        total_p75 = _ts_val(ts, "announcement_to_close", "p75")
        total_p90 = _ts_val(ts, "announcement_to_close", "p90")

        # State machine regulatory duration (existing model)
        sm_p50 = simulation.critical_path_duration_p50 or 0
        sm_p75 = simulation.critical_path_duration_p75 or 0
        sm_p90 = simulation.critical_path_duration_p90 or 0

        if is_tender:
            # Tender offers: no proxy track
            p50_days = max(reg_p50, sm_p50, total_p50)
            p75_days = max(reg_p75, sm_p75, total_p75)
            p90_days = max(reg_p90, sm_p90, total_p90)
        else:
            # Merger: max of all tracks
            p50_days = max(reg_p50, proxy_p50, total_p50, sm_p50)
            p75_days = max(reg_p75, proxy_p75, total_p75, sm_p75)
            p90_days = max(reg_p90, proxy_p90, total_p90, sm_p90)

        logger.info(
            f"Timeline calibration: reg_p50={reg_p50:.0f} "
            f"proxy_p50={proxy_p50:.0f} "
            f"total_p50={total_p50:.0f} "
            f"sm_p50={sm_p50} → final_p50={p50_days:.0f}"
        )
    else:
        # Fallback: state-machine only (old behavior)
        p50_days = simulation.critical_path_duration_p50 or 0
        p75_days = simulation.critical_path_duration_p75 or 0
        p90_days = simulation.critical_path_duration_p90 or 0

    p50_date = (
        announcement + timedelta(days=int(p50_days))
        if p50_days else None
    )
    p75_date = (
        announcement + timedelta(days=int(p75_days))
        if p75_days else None
    )
    p90_date = (
        announcement + timedelta(days=int(p90_days))
        if p90_days else None
    )

    # ── Milestones ───────────────────────────────────────

    milestones = _build_milestones(
        simulation, merger_agreement, announcement, ts,
    )

    # ── Scenarios ────────────────────────────────────────

    from output.scenario_builder import build_joint_scenarios
    scenarios = _build_scenarios(simulation, announcement)
    joint = build_joint_scenarios(simulation, announcement)
    if joint:
        scenarios = joint

    # ── Risk flags ───────────────────────────────────────

    risk_flags = _build_risk_flags(simulation, merger_agreement)

    # Outside date check
    prob_by_outside = None
    outside_date = (
        merger_agreement.outside_date if merger_agreement else None
    )
    if outside_date and scenarios:
        prob_by_outside = sum(
            s.probability_pct for s in scenarios
            if s.expected_close_date
            and s.expected_close_date <= outside_date
        )

    comp_count = ts.get("comp_count", 0) if ts else 0

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
        critical_path_jurisdiction=(
            simulation.critical_path_jurisdiction
        ),
        enforcement_regime="normal",
        comparable_deals_used=comp_count,
        generated_at=date.today().isoformat(),
    )


def _build_milestones(
    simulation: FullSimulationResult,
    merger_agreement: ParsedMergerAgreement | None,
    announcement: date,
    timeline_stats: dict | None = None,
) -> list[MilestoneRow]:
    """Build milestone rows from simulation + empirical data."""
    milestones = []
    ts = timeline_stats or {}

    # Use empirical filing timing if available
    filing_p50 = _ts_val(ts, "announcement_to_filing", "p50")
    filing_p75 = _ts_val(ts, "announcement_to_filing", "p75")
    filing_p90 = _ts_val(ts, "announcement_to_filing", "p90")

    for jur_sim in simulation.jurisdictions:
        jur_name = jur_sim.jurisdiction.value
        contractual = (
            jur_sim.contractual_filing_deadline_days or None
        )

        # Filing milestone — empirical or contractual
        f_p50 = int(filing_p50 or contractual or 10)
        f_p75 = int(filing_p75 or contractual or 15)
        f_p90 = int(filing_p90 or contractual or 20)

        milestones.append(MilestoneRow(
            milestone=f"{jur_name} Filing",
            jurisdiction=jur_name,
            contractual_deadline=(
                jur_sim.contractual_filing_deadline
            ),
            base_case_date=(
                announcement + timedelta(days=f_p50)
            ),
            extended_case_date=(
                announcement + timedelta(days=f_p75)
            ),
            stress_case_date=(
                announcement + timedelta(days=f_p90)
            ),
        ))

        # Clearance milestone
        clear_p50 = jur_sim.expected_duration_days_p50
        clear_p75 = jur_sim.expected_duration_days_p75
        clear_p90 = jur_sim.expected_duration_days_p90
        milestones.append(MilestoneRow(
            milestone=f"{jur_name} Clearance",
            jurisdiction=jur_name,
            base_case_date=(
                announcement + timedelta(days=clear_p50)
            ),
            extended_case_date=(
                announcement + timedelta(days=clear_p75)
            ),
            stress_case_date=(
                announcement + timedelta(days=clear_p90)
            ),
            risk_flags=[
                p.path_label
                for p in jur_sim.possible_paths
                if not p.is_terminal_clear
                and p.path_probability > 0.05
            ],
        ))

    # Shareholder Vote milestone (empirical)
    vote_p50 = _ts_val(ts, "announcement_to_vote", "p50")
    if vote_p50:
        vote_p75 = _ts_val(ts, "announcement_to_vote", "p75")
        vote_p90 = _ts_val(ts, "announcement_to_vote", "p90")
        milestones.append(MilestoneRow(
            milestone="Shareholder Vote",
            jurisdiction="SEC",
            base_case_date=(
                announcement + timedelta(days=int(vote_p50))
            ),
            extended_case_date=(
                announcement + timedelta(days=int(vote_p75))
            ),
            stress_case_date=(
                announcement + timedelta(days=int(vote_p90))
            ),
        ))

    # Expected Close milestone
    total_p50 = _ts_val(ts, "announcement_to_close", "p50")
    total_p75 = _ts_val(ts, "announcement_to_close", "p75")
    total_p90 = _ts_val(ts, "announcement_to_close", "p90")
    # Use empirical total or state machine, whichever is larger
    sm_p50 = simulation.critical_path_duration_p50 or 0
    sm_p75 = simulation.critical_path_duration_p75 or 0
    sm_p90 = simulation.critical_path_duration_p90 or 0
    close_p50 = max(total_p50, sm_p50) if total_p50 else sm_p50
    close_p75 = max(total_p75, sm_p75) if total_p75 else sm_p75
    close_p90 = max(total_p90, sm_p90) if total_p90 else sm_p90

    milestones.append(MilestoneRow(
        milestone="Expected Close",
        jurisdiction="",
        contractual_deadline=(
            merger_agreement.outside_date
            if merger_agreement else None
        ),
        base_case_date=(
            announcement + timedelta(days=int(close_p50))
            if close_p50 else None
        ),
        extended_case_date=(
            announcement + timedelta(days=int(close_p75))
            if close_p75 else None
        ),
        stress_case_date=(
            announcement + timedelta(days=int(close_p90))
            if close_p90 else None
        ),
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
