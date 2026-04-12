"""
Step 8: Guidance Reconciliation

Compares the model's independent timeline estimate to company/AJ
guidance.  Instead of forcing the timeline to match guidance, this
step **diagnoses** why they differ and either:

  - Adjusts specific components (pre-notification, second request
    risk, PUC timelines) when the gap can be explained
  - Flags unexplained discrepancies for investigation — these may
    be opportunities (model faster) or missed risks (model slower)

The model's P50/P75/P90 remain model-driven.  Adjustments only
modify individual milestone dates, not the headline numbers.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Optional

from models.deal import DealParameters
from models.documents import ParsedMergerAgreement
from models.comparables import ComparableGroup
from models.state_machine import FullSimulationResult
from models.timeline import (
    DealTimingReport,
    GuidanceAdjustment,
    GuidanceReconciliation,
    RiskFlag,
)

logger = logging.getLogger(__name__)

# Gap thresholds
_MATERIAL_GAP_DAYS = 30   # anything under this is "aligned"
_OPPORTUNITY_LABEL = "opportunity"
_MODEL_FASTER = "model_faster"
_MODEL_SLOWER = "model_slower"
_ALIGNED = "aligned"


async def reconcile_with_guidance(
    report: DealTimingReport,
    guidance_anchor: Optional[date],
    deal_params: DealParameters,
    merger_agreement: Optional[ParsedMergerAgreement],
    simulation: FullSimulationResult,
    comparable_groups: list[ComparableGroup],
    timeline_stats: Optional[dict],
) -> DealTimingReport:
    """Compare model estimate to guidance and diagnose gaps."""

    if not guidance_anchor or not report.p50_close_date:
        report.guidance_reconciliation = GuidanceReconciliation(
            flag="no_guidance",
        )
        return report

    ann = report.announcement_date
    model_days = (report.p50_close_date - ann).days
    guidance_days = (guidance_anchor - ann).days
    gap = guidance_days - model_days  # positive = guidance later

    recon = GuidanceReconciliation(
        guidance_source="both",
        guidance_date=guidance_anchor,
        model_p50_date=report.p50_close_date,
        gap_days=gap,
    )

    if abs(gap) < _MATERIAL_GAP_DAYS:
        recon.flag = _ALIGNED
        recon.unexplained_days = 0
        report.guidance_reconciliation = recon
        logger.info(
            f"Guidance reconciliation: aligned "
            f"(gap={gap}d)"
        )
        return report

    adjustments: list[GuidanceAdjustment] = []
    explained = 0

    if gap > 0:
        # Guidance is LATER than model — try to explain
        recon.flag = _MODEL_FASTER

        # Diagnostic 1: Pre-notification / pre-filing gap
        adj = _check_prefiling_gap(
            report, simulation, timeline_stats,
        )
        if adj:
            adjustments.append(adj)
            explained += adj.adjusted_days - adj.original_days

        # Diagnostic 2: Second request / Phase 2 risk
        adj = _check_second_request_risk(
            report, simulation, deal_params,
        )
        if adj:
            adjustments.append(adj)
            explained += adj.adjusted_days - adj.original_days

        # Diagnostic 3: PUC / regulated industry timelines
        adj = _check_puc_timelines(
            report, simulation, timeline_stats,
        )
        if adj:
            adjustments.append(adj)
            explained += adj.adjusted_days - adj.original_days

        # Diagnostic 4: Multi-jurisdiction complexity
        adj = _check_multi_jurisdiction(
            report, simulation,
        )
        if adj:
            adjustments.append(adj)
            explained += adj.adjusted_days - adj.original_days

        # Diagnostic 5: Proxy complexity (stock deals)
        adj = _check_proxy_complexity(
            report, deal_params, timeline_stats,
        )
        if adj:
            adjustments.append(adj)
            explained += adj.adjusted_days - adj.original_days

    else:
        # Model is SLOWER than guidance — potential opportunity
        recon.flag = _OPPORTUNITY_LABEL
        report.risk_flags.append(RiskFlag(
            flag=(
                f"Model {abs(gap)}d slower than guidance "
                f"— potential opportunity"
            ),
            severity="low",
            detail=(
                f"Model P50={report.p50_close_date}, "
                f"guidance={guidance_anchor}. "
                f"Parties may have pre-cleared regulatory "
                f"issues or agreed simplified filing tracks."
            ),
        ))

    # Apply adjustments to milestone dates
    if adjustments:
        _apply_adjustments(report, adjustments, ann)

    remaining = max(0, gap - explained)

    recon.adjustments = adjustments
    recon.explained_days = explained
    recon.unexplained_days = remaining

    # Flag unexplained gap
    if remaining > _MATERIAL_GAP_DAYS and gap > 0:
        report.risk_flags.append(RiskFlag(
            flag=(
                f"Unexplained {remaining}d gap vs guidance"
            ),
            severity="medium",
            detail=(
                f"Model P50={report.p50_close_date}, "
                f"guidance={guidance_anchor}. "
                f"Explained {explained}d of {gap}d gap. "
                f"Remaining {remaining}d may indicate "
                f"risks not captured by the model — "
                f"investigate further."
            ),
        ))

    report.guidance_reconciliation = recon

    logger.info(
        f"Guidance reconciliation: {recon.flag} "
        f"gap={gap}d explained={explained}d "
        f"remaining={remaining}d "
        f"adjustments={len(adjustments)}"
    )

    return report


# ── Diagnostic checks ────────────────────────────────────

def _check_prefiling_gap(
    report: DealTimingReport,
    simulation: FullSimulationResult,
    timeline_stats: Optional[dict],
) -> Optional[GuidanceAdjustment]:
    """Check if pre-filing periods for EC/CMA are
    underestimated vs comparable deals."""
    if not timeline_stats:
        return None

    ts = timeline_stats
    emp_filing = ts.get("announcement_to_filing", {})
    emp_p75 = emp_filing.get("p75")
    emp_p50 = emp_filing.get("p50")

    if not emp_p75 or not emp_p50:
        return None

    # Check if any jurisdiction has a filing milestone
    # earlier than the p75 of comps
    for ms in report.milestones:
        if "Filing" not in ms.milestone:
            continue
        if not ms.base_case_date:
            continue
        filing_days = (
            ms.base_case_date - report.announcement_date
        ).days
        if filing_days < emp_p75 * 0.8:
            # We modeled filing sooner than 80% of p75
            new_days = int(emp_p75)
            return GuidanceAdjustment(
                component="pre_filing_period",
                original_days=filing_days,
                adjusted_days=new_days,
                reason=(
                    f"Comp p75 pre-filing is {emp_p75:.0f}d "
                    f"but modeled {filing_days}d — "
                    f"extending to match comps"
                ),
            )

    return None


def _check_second_request_risk(
    report: DealTimingReport,
    simulation: FullSimulationResult,
    deal_params: DealParameters,
) -> Optional[GuidanceAdjustment]:
    """If the deal has meaningful overlap but we predicted
    clean Phase 1, the guidance gap may be a second request."""
    overlap = report.overlap_severity
    if overlap not in ("medium", "high"):
        return None

    # Check if any HSR/EC simulation included second request
    for jur_sim in simulation.jurisdictions:
        jur = jur_sim.jurisdiction_label or jur_sim.jurisdiction.value
        if jur not in ("HSR", "EC"):
            continue
        for path in jur_sim.possible_paths:
            label = path.path_label or ""
            if "Second Request" in label or "Phase 2" in label:
                if path.path_probability > 0.15:
                    # Already modeled — not the gap
                    return None

    # Second request not modeled but overlap suggests it
    # Typical SR adds 120-180 days
    return GuidanceAdjustment(
        component="hsr_second_request_risk",
        original_days=0,
        adjusted_days=150,
        reason=(
            f"Overlap severity={overlap} but no second "
            f"request modeled — guidance gap may reflect "
            f"extended review risk (+150d)"
        ),
    )


def _check_puc_timelines(
    report: DealTimingReport,
    simulation: FullSimulationResult,
    timeline_stats: Optional[dict],
) -> Optional[GuidanceAdjustment]:
    """State PUC reviews typically take 9-15 months but
    GenericStateMachine defaults to 45-90 days."""
    puc_jurs = [
        s for s in simulation.jurisdictions
        if (s.jurisdiction_label or "").startswith("STATE_PUC")
    ]
    if not puc_jurs:
        return None

    # GenericStateMachine models ~45-90 day review
    # Real PUC reviews: 270-450 days (9-15 months)
    modeled_max = max(
        s.expected_duration_days_p50 for s in puc_jurs
    )
    if modeled_max > 200:
        return None  # already long enough

    typical_puc_days = 360  # 12 months
    return GuidanceAdjustment(
        component="state_puc_timeline",
        original_days=modeled_max,
        adjusted_days=typical_puc_days,
        reason=(
            f"{len(puc_jurs)} state PUC reviews modeled "
            f"at {modeled_max}d — typical PUC review "
            f"takes 9-15 months ({typical_puc_days}d)"
        ),
    )


def _check_multi_jurisdiction(
    report: DealTimingReport,
    simulation: FullSimulationResult,
) -> Optional[GuidanceAdjustment]:
    """Deals with 4+ jurisdictions have coordination
    overhead that adds ~30-60 days."""
    n_jurs = len(simulation.jurisdictions)
    if n_jurs < 4:
        return None

    overhead = min(30 + (n_jurs - 4) * 15, 90)
    return GuidanceAdjustment(
        component="multi_jurisdiction_overhead",
        original_days=0,
        adjusted_days=overhead,
        reason=(
            f"{n_jurs} jurisdictions — coordination "
            f"overhead adds ~{overhead}d"
        ),
    )


def _check_proxy_complexity(
    report: DealTimingReport,
    deal_params: DealParameters,
    timeline_stats: Optional[dict],
) -> Optional[GuidanceAdjustment]:
    """Stock-for-stock deals require S-4 registration which
    takes longer than a standard DEFM14A proxy."""
    from models.deal import DealStructure
    if deal_params.deal_structure not in (
        DealStructure.STOCK, DealStructure.MIXED,
    ):
        return None

    # S-4 review is typically 30-60 days longer than DEFM14A
    return GuidanceAdjustment(
        component="s4_registration_complexity",
        original_days=0,
        adjusted_days=45,
        reason=(
            "Stock/mixed consideration requires S-4 "
            "registration — SEC review typically 30-60d "
            "longer than standard proxy"
        ),
    )


# ── Apply adjustments ────────────────────────────────────

def _apply_adjustments(
    report: DealTimingReport,
    adjustments: list[GuidanceAdjustment],
    announcement: date,
) -> None:
    """Apply adjustments to specific milestone dates.

    Does NOT change P50/P75/P90 — only individual milestones.
    This lets the report show both the model estimate and
    where the diagnostics found gaps.
    """
    for adj in adjustments:
        comp = adj.component

        if comp == "state_puc_timeline":
            # Extend PUC clearance milestones
            for ms in report.milestones:
                if (
                    "STATE_PUC" in ms.milestone
                    and "Clearance" in ms.milestone
                ):
                    ms.base_case_date = (
                        announcement
                        + timedelta(days=adj.adjusted_days)
                    )
                    ms.notes = adj.reason

        elif comp == "pre_filing_period":
            # Extend filing milestones
            for ms in report.milestones:
                if "Filing" in ms.milestone:
                    ms.base_case_date = (
                        announcement
                        + timedelta(days=adj.adjusted_days)
                    )

        # Other adjustments are informational — they explain
        # the gap but don't change specific milestones
        # (e.g. second request risk, multi-jurisdiction
        # overhead, S-4 complexity)
