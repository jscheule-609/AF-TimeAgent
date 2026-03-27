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
) -> DealTimingReport:
    """Run the pipeline for a single deal with data leakage prevention.

    The deal identified by exclude_deal_pk is stripped from:
      - Step 0 MARS enrichment (mars_deal_pk set to None)
      - Step 2 comparable groups (filtered out post-query)
      - Step 4 antitrust MARS lookup (skipped)
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
    )

    simulation = await simulate_regulatory_paths(
        regulatory_map, overlap_assessment, comparable_groups,
        deal_params, merger_agreement,
    )

    # ── Stage 4: Timeline assembly ───────────────────────
    report = await assemble_timeline(
        simulation, press_release_data,
        merger_agreement, deal_params,
    )
    report.overlap_type = overlap_assessment.overlap_type
    report.overlap_severity = overlap_assessment.overlap_severity
    report.comparable_deals_used = total_comps

    # Log prediction (non-fatal)
    try:
        await log_prediction(report, exclude_deal_pk)
    except Exception as e:
        logger.warning(f"Prediction logging failed: {e}")

    return report
