"""
Step 6: Timeline Assembly & Output

Builds the milestone table, scenario paths, risk flags, and summary
statistics.  Close-date predictions come from the **Monte Carlo
mixture model** (scoring.distribution): structural constraints
(regulatory clearance, shareholder vote) combine by per-sample max,
alternative estimators (mechanism chain, AFT/empirical corpus model,
guidance + residuals) combine as a weighted mixture, and P50/P75/P90
are read off the empirical quantiles of the final sample array.

Supports mid-deal re-prediction: pass ``as_of`` and
``observed_milestones`` to condition on elapsed time and realized
milestone dates.  ``as_of=None`` means a day-0 prediction
(backtest semantics — no conditioning, no today-floor).
"""
import logging
from datetime import date, timedelta
from config.calibration import load_calibration
from models.deal import DealParameters, DealStructure
from models.documents import ParsedMergerAgreement, PressReleaseData
from models.state_machine import FullSimulationResult
from models.timeline import (
    MilestoneRow, ScenarioPath, RiskFlag, DealTimingReport,
)
from scoring.aft import predict_quantiles
from scoring.distribution import build_close_distribution

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
    guidance_anchor: date | None = None,
    dma_close_gap: int = 3,
    as_of: date | None = None,
    observed_milestones: dict | None = None,
) -> DealTimingReport:
    """Assemble the final timeline report.

    Close dates come from the Monte Carlo mixture over the
    mechanism (regulatory ∧ vote), corpus (AFT / empirical
    total), and guidance (anchor + residuals) tracks.

    *as_of* is the prediction date: samples are conditioned on
    the deal still being open, and no percentile may fall before
    it.  ``None`` = day-0 prediction (backtests).
    *observed_milestones* maps milestone_type → realized date for
    THIS deal (antitrust_filing / antitrust_clearance /
    shareholder_vote); realized components collapse to their
    actual values.
    """
    announcement = deal_params.announcement_date
    ts = timeline_stats or {}
    is_tender = (
        deal_params.deal_structure == DealStructure.TENDER
    )

    # ── Compute close dates (Monte Carlo mixture) ────────

    cal = load_calibration()

    guidance_days = None
    if guidance_anchor and guidance_anchor > announcement:
        guidance_days = float(
            (guidance_anchor - announcement).days
        )

    observed_offsets = None
    if observed_milestones:
        observed_offsets = {
            k: float((v - announcement).days)
            for k, v in observed_milestones.items()
            if v is not None and v > announcement
        }

    elapsed_days = None
    if as_of is not None and as_of > announcement:
        elapsed_days = float((as_of - announcement).days)

    aft_quantiles = None
    try:
        aft_quantiles = predict_quantiles(deal_params)
    except Exception as e:
        logger.warning(f"AFT quantiles failed: {e}")

    dist = build_close_distribution(
        simulation=simulation,
        timeline_stats=ts or None,
        aft_quantiles=aft_quantiles,
        guidance_days=guidance_days,
        guidance_residuals=(
            (cal.get("guidance") or {}).get("residual_quantiles")
        ),
        is_tender=is_tender,
        dma_close_gap=dma_close_gap,
        weights=cal.get("track_weights"),
        elapsed_days=elapsed_days,
        observed=observed_offsets,
        seed=(deal_params.mars_deal_pk or 42),
    )

    if dist:
        p50_days = dist["p50"]
        p75_days = dist["p75"]
        p90_days = dist["p90"]
        logger.info(
            f"Close distribution: p50={p50_days:.0f}d "
            f"p75={p75_days:.0f}d p90={p90_days:.0f}d "
            f"tracks={dist['tracks_used']} "
            f"track_p50s={dist['track_p50s']} "
            f"conditioned={dist['conditioned']}"
        )
    else:
        # Fallback: state-machine only (old behavior)
        p50_days = simulation.critical_path_duration_p50 or 0
        p75_days = simulation.critical_path_duration_p75 or 0
        p90_days = simulation.critical_path_duration_p90 or 0
        logger.warning(
            "No distribution tracks available — falling back "
            "to state-machine percentiles"
        )

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

    # A prediction made today can never have a close date in
    # the past (April-batch bug: P50s up to 407 days stale).
    if as_of is not None:
        floor = as_of + timedelta(days=1)
        if p50_date and p50_date < floor:
            p50_date = floor
        if p75_date and p75_date < floor:
            p75_date = floor
        if p90_date and p90_date < floor:
            p90_date = floor

    # ── Milestones ───────────────────────────────────────

    milestones = _build_milestones(
        simulation, merger_agreement, announcement, ts,
        dma_close_gap=dma_close_gap,
    )

    # Sync Expected Close milestone with guidance-adjusted
    # P50/P75/P90 (guidance anchor may have shifted them)
    for ms in milestones:
        if ms.milestone == "Expected Close":
            ms.base_case_date = p50_date
            ms.extended_case_date = p75_date
            ms.stress_case_date = p90_date
            break

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
    dma_close_gap: int = 3,
) -> list[MilestoneRow]:
    """Build full milestone timeline from simulation + empirical
    data + DMA provisions.

    Milestone sequence for a typical merger:
      1. Antitrust filings (HSR, EC, SAMR, PUCs...)
      2. Preliminary Proxy / S-4 Filed
      3. Antitrust clearances
      4. Definitive Proxy Mailed
      5. Shareholder Vote
      6. Expected Close (last condition + DMA gap)
    """
    milestones = []
    ts = timeline_stats or {}

    # ── Efforts standard multiplier ──────────────────────
    efforts = ""
    if merger_agreement:
        efforts = (
            merger_agreement.efforts_standard or ""
        ).lower()
    if "hell" in efforts or "best" in efforts:
        filing_mult = 0.85
    elif "reasonable" in efforts:
        filing_mult = 1.0
    else:
        filing_mult = 1.1

    # ── Empirical timing defaults ────────────────────────
    emp_filing_p50 = _ts_val(ts, "announcement_to_filing", "p50") or 15
    emp_filing_p75 = _ts_val(ts, "announcement_to_filing", "p75") or 25
    emp_filing_p90 = _ts_val(ts, "announcement_to_filing", "p90") or 40
    emp_clear_p50 = _ts_val(ts, "filing_to_clearance", "p50") or 30
    emp_clear_p75 = _ts_val(ts, "filing_to_clearance", "p75") or 60
    emp_clear_p90 = _ts_val(ts, "filing_to_clearance", "p90") or 90

    # ── 1. Antitrust / regulatory filings ────────────────
    latest_clear_p50 = 0
    latest_clear_p75 = 0
    latest_clear_p90 = 0

    for jur_sim in simulation.jurisdictions:
        # Use jurisdiction_label (actual name) not enum
        jur_label = (
            jur_sim.jurisdiction_label
            or jur_sim.jurisdiction.value
        )

        # Filing date: DMA contractual deadline > empirical
        contractual = (
            jur_sim.contractual_filing_deadline_days
        )
        if contractual:
            f_p50 = contractual
            f_p75 = contractual
            f_p90 = contractual
        else:
            f_p50 = int(emp_filing_p50 * filing_mult)
            f_p75 = int(emp_filing_p75 * filing_mult)
            f_p90 = int(emp_filing_p90 * filing_mult)

        milestones.append(MilestoneRow(
            milestone=f"{jur_label} Filing",
            jurisdiction=jur_label,
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
            notes=(
                f"DMA: {efforts}" if efforts else ""
            ),
        ))

        # Clearance date: filing + review duration
        c_p50 = f_p50 + int(emp_clear_p50)
        c_p75 = f_p75 + int(emp_clear_p75)
        c_p90 = f_p90 + int(emp_clear_p90)

        # Use state machine duration if longer
        sm_p50 = jur_sim.expected_duration_days_p50
        sm_p75 = jur_sim.expected_duration_days_p75
        sm_p90 = jur_sim.expected_duration_days_p90
        c_p50 = max(c_p50, sm_p50)
        c_p75 = max(c_p75, sm_p75)
        c_p90 = max(c_p90, sm_p90)

        milestones.append(MilestoneRow(
            milestone=f"{jur_label} Clearance",
            jurisdiction=jur_label,
            base_case_date=(
                announcement + timedelta(days=c_p50)
            ),
            extended_case_date=(
                announcement + timedelta(days=c_p75)
            ),
            stress_case_date=(
                announcement + timedelta(days=c_p90)
            ),
            risk_flags=[
                p.path_label
                for p in jur_sim.possible_paths
                if not p.is_terminal_clear
                and p.path_probability > 0.05
            ],
        ))

        latest_clear_p50 = max(latest_clear_p50, c_p50)
        latest_clear_p75 = max(latest_clear_p75, c_p75)
        latest_clear_p90 = max(latest_clear_p90, c_p90)

    # ── 2. Preliminary Proxy / S-4 Filed ─────────────────
    proxy_p50 = _ts_val(ts, "announcement_to_proxy", "p50")
    proxy_s4 = _ts_val(ts, "announcement_to_s4", "p50")
    prelim_p50 = proxy_p50 or proxy_s4 or 30
    prelim_p75 = (
        _ts_val(ts, "announcement_to_proxy", "p75")
        or _ts_val(ts, "announcement_to_s4", "p75")
        or 45
    )
    prelim_p90 = (
        _ts_val(ts, "announcement_to_proxy", "p90")
        or _ts_val(ts, "announcement_to_s4", "p90")
        or 60
    )

    milestones.append(MilestoneRow(
        milestone="Preliminary Proxy/S-4 Filed",
        jurisdiction="SEC",
        base_case_date=(
            announcement + timedelta(days=int(prelim_p50))
        ),
        extended_case_date=(
            announcement + timedelta(days=int(prelim_p75))
        ),
        stress_case_date=(
            announcement + timedelta(days=int(prelim_p90))
        ),
    ))

    # ── 3. Definitive Proxy Mailed ───────────────────────
    # ~30-60 days after preliminary (SEC review + revisions)
    def_p50 = int(prelim_p50) + 45
    def_p75 = int(prelim_p75) + 55
    def_p90 = int(prelim_p90) + 70

    milestones.append(MilestoneRow(
        milestone="Definitive Proxy Mailed",
        jurisdiction="SEC",
        base_case_date=(
            announcement + timedelta(days=def_p50)
        ),
        extended_case_date=(
            announcement + timedelta(days=def_p75)
        ),
        stress_case_date=(
            announcement + timedelta(days=def_p90)
        ),
    ))

    # ── 4. Shareholder Vote ──────────────────────────────
    vote_p50 = _ts_val(ts, "announcement_to_vote", "p50")
    vote_p75 = _ts_val(ts, "announcement_to_vote", "p75")
    vote_p90 = _ts_val(ts, "announcement_to_vote", "p90")

    # If no empirical, derive from def proxy + 25 days
    if not vote_p50:
        vote_p50 = def_p50 + 25
        vote_p75 = def_p75 + 30
        vote_p90 = def_p90 + 35

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

    # ── 5. Expected Close ────────────────────────────────
    # Close = max(last regulatory clearance, vote) + DMA gap
    last_condition_p50 = max(latest_clear_p50, vote_p50)
    last_condition_p75 = max(latest_clear_p75, vote_p75)
    last_condition_p90 = max(latest_clear_p90, vote_p90)

    close_p50 = int(last_condition_p50) + dma_close_gap
    close_p75 = int(last_condition_p75) + dma_close_gap
    close_p90 = int(last_condition_p90) + dma_close_gap

    # Also check empirical total as floor
    emp_total_p50 = _ts_val(ts, "announcement_to_close", "p50")
    emp_total_p75 = _ts_val(ts, "announcement_to_close", "p75")
    emp_total_p90 = _ts_val(ts, "announcement_to_close", "p90")
    if emp_total_p50:
        close_p50 = max(close_p50, int(emp_total_p50))
    if emp_total_p75:
        close_p75 = max(close_p75, int(emp_total_p75))
    if emp_total_p90:
        close_p90 = max(close_p90, int(emp_total_p90))

    milestones.append(MilestoneRow(
        milestone="Expected Close",
        jurisdiction="",
        contractual_deadline=(
            merger_agreement.outside_date
            if merger_agreement else None
        ),
        base_case_date=(
            announcement + timedelta(days=close_p50)
        ),
        extended_case_date=(
            announcement + timedelta(days=close_p75)
        ),
        stress_case_date=(
            announcement + timedelta(days=close_p90)
        ),
        notes=(
            f"{dma_close_gap} BD after all conditions "
            f"per DMA"
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
