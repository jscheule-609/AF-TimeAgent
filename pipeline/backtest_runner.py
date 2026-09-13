"""
Backtest-specific pipeline runner.

Mirrors the main orchestrator but excludes the test deal from
MARS comparables, antitrust lookups, and validation enrichment
to prevent data leakage.
"""
import asyncio
import logging
from models.deal import DealInput, DealParameters
from models.timeline import DealTimingReport
from pipeline.step0_validation import validate_deal
from pipeline.step1_press_release import parse_deal_press_release
from pipeline.step2_document_ingestion import ingest_documents
from pipeline.step3_comparables import find_comparables
from pipeline.step4_antitrust import assess_antitrust_overlap
from pipeline.step5_regulatory_map import map_jurisdictions
from pipeline.step5_5_state_machine import simulate_regulatory_paths
from pipeline.step6_timeline import assemble_timeline
from pipeline.step7_prediction_log import log_prediction

logger = logging.getLogger(__name__)


async def run_backtest_deal(
    deal_input: DealInput,
    exclude_deal_pk: int,
    no_persist: bool = False,
) -> DealTimingReport:
    """Run the pipeline for a single deal with data leakage prevention.

    The deal identified by exclude_deal_pk is stripped from:
      - Step 0 MARS enrichment (mars_deal_pk set to None)
      - Step 2 comparable groups (filtered out post-query)
      - Step 4 antitrust MARS lookup (skipped)

    With no_persist=True, never log a prediction to timing_predictions.
    """
    logger.info(
        f"Backtest run: {deal_input.acquirer_ticker} / "
        f"{deal_input.target_ticker} (excluding pk={exclude_deal_pk})"
    )

    # ── Stage 0: Validation ──────────────────────────────
    validation = await validate_deal(deal_input)
    if not validation.is_valid:
        raise RuntimeError(
            f"Validation failed: {validation.errors}"
        )

    deal_params = validation.deal_params

    # Blind the pipeline to the deal's own MARS entry
    deal_params.mars_deal_pk = None
    deal_params.mars_deal_id = None

    # ── Stage 1: Press release + Documents (parallel) ────
    press_release_data, (tenk_acquirer, tenk_target, merger_agreement) = (
        await asyncio.gather(
            parse_deal_press_release(deal_params),
            ingest_documents(deal_params),
        )
    )

    # ── Stage 2: Comparables + Antitrust (parallel) ──────
    overlap_assessment, comparable_groups = await asyncio.gather(
        # mars_deal_pk=None skips MARS antitrust lookup
        assess_antitrust_overlap(tenk_acquirer, tenk_target, None),
        find_comparables(
            deal_params, tenk_acquirer, tenk_target, merger_agreement,
        ),
    )

    # Remove the test deal from every comparable group
    for group in comparable_groups:
        before = len(group.deals)
        group.deals = [
            d for d in group.deals
            if d.deal_pk != exclude_deal_pk
        ]
        group.count = len(group.deals)
        if len(group.deals) < before:
            logger.debug(
                f"Excluded deal pk={exclude_deal_pk} from "
                f"{group.source.value} group"
            )

    total_comps = sum(g.count for g in comparable_groups)
    logger.info(
        f"Stage 2 — Overlap: "
        f"{overlap_assessment.overlap_type}/"
        f"{overlap_assessment.overlap_severity}, "
        f"Comparables: {total_comps} (after exclusion)"
    )

    # ── Stage 3: Regulatory mapping + Simulation ─────────
    regulatory_map = await map_jurisdictions(
        tenk_acquirer, tenk_target,
        merger_agreement, comparable_groups,
        mars_deal_pk=None,  # Backtest: no MARS shortcuts
    )

    simulation = await simulate_regulatory_paths(
        regulatory_map, overlap_assessment, comparable_groups,
        deal_params, merger_agreement,
    )

    # ── Stage 3b: Timeline calibration (production parity) ─
    # Comp groups are already blinded to the test deal above;
    # filter target history the same way.
    timeline_stats = None
    try:
        from db.queries_comparables import get_target_prior_deals
        from pipeline.step3b_timeline_calibration import (
            calibrate_deal_timeline,
        )
        target_prior = await get_target_prior_deals(
            deal_params.target_name,
        )
        target_prior = [
            r for r in target_prior
            if r.get("deal_pk") != exclude_deal_pk
        ]
        timeline_stats = await calibrate_deal_timeline(
            comparable_groups, target_prior,
        )
    except Exception as e:
        logger.warning(f"Timeline calibration failed: {e}")

    # ── Stage 3c: Guidance anchor (production parity) ────
    # Guidance was public on announcement day — reading it is
    # NOT leakage; it is exactly what a live prediction sees.
    guidance_anchor = None
    dma_close_gap = 3
    try:
        from pipeline.step2b_guidance_anchor import (
            load_guidance_anchor,
            parse_dma_close_gap_days,
        )
        from db.connection import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            dma_row = await conn.fetchrow(
                "SELECT closing_guidance_dma "
                "FROM deals WHERE deal_pk = $1",
                exclude_deal_pk,
            )
        if dma_row and dma_row["closing_guidance_dma"]:
            dma_close_gap = parse_dma_close_gap_days(
                dma_row["closing_guidance_dma"],
            )
        guidance_anchor = await load_guidance_anchor(
            exclude_deal_pk,
            deal_params.announcement_date,
        )
    except Exception as e:
        logger.warning(f"Guidance anchor load failed: {e}")

    # ── Stage 4: Timeline assembly ───────────────────────
    # as_of=None → day-0 prediction: no elapsed-time
    # conditioning, no today-floor, no observed milestones.
    report = await assemble_timeline(
        simulation, press_release_data,
        merger_agreement, deal_params,
        timeline_stats=timeline_stats,
        guidance_anchor=guidance_anchor,
        dma_close_gap=dma_close_gap,
        as_of=None,
    )
    report.overlap_type = overlap_assessment.overlap_type
    report.overlap_severity = overlap_assessment.overlap_severity
    report.comparable_deals_used = total_comps

    # Log prediction (non-fatal)
    if not no_persist:
        try:
            await log_prediction(report, exclude_deal_pk)
        except Exception as e:
            logger.warning(f"Prediction logging failed: {e}")

    return report
