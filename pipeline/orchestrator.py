"""
Main pipeline orchestrator.

Runs the full pipeline with proper parallelism and error handling.
"""
import asyncio
import logging
from models.deal import DealInput
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


class PipelineError(Exception):
    """Error raised during pipeline execution."""
    def __init__(self, step: str, message: str, partial_report: DealTimingReport | None = None):
        self.step = step
        self.partial_report = partial_report
        super().__init__(f"Pipeline failed at {step}: {message}")


async def run_timing_estimation(
    deal_input: DealInput,
    external_signals: list[str] | None = None,
    external_overlap: dict | None = None,
) -> DealTimingReport:
    """
    Main entry point. Runs the full pipeline.

    Stage 0: Validation
    Stage 1: Press release + Document ingestion (parallel)
    Stage 2: Comparables + Antitrust assessment (parallel)
    Stage 3: Regulatory mapping + State machine simulation
    Stage 4: Timeline assembly + Prediction logging
    """
    logger.info(f"Starting timing estimation: {deal_input.acquirer_ticker} / {deal_input.target_ticker}")

    # ═══════════════════════════════════════════════════════
    # Stage 0: Validation
    # ═══════════════════════════════════════════════════════
    validation = await validate_deal(deal_input)
    if not validation.is_valid:
        raise PipelineError("step0_validation", f"Validation failed: {validation.errors}")

    deal_params = validation.deal_params
    logger.info(f"Deal validated: {deal_params.acquirer_name} / {deal_params.target_name}")

    if validation.warnings:
        for w in validation.warnings:
            logger.warning(f"Validation warning: {w}")

    # ═══════════════════════════════════════════════════════
    # Stage 1: Press release + Documents (parallel)
    # ═══════════════════════════════════════════════════════
    try:
        press_release_data, (tenk_acquirer, tenk_target, merger_agreement) = (
            await asyncio.gather(
                parse_deal_press_release(deal_params),
                ingest_documents(deal_params),
            )
        )
    except Exception as e:
        logger.error(f"Stage 1 failed: {e}")
        raise PipelineError("stage1_ingestion", str(e))

    logger.info(
        f"Stage 1 complete — Press release: {'found' if press_release_data.raw_timing_language else 'not found'}, "
        f"10-K acquirer: {'yes' if tenk_acquirer else 'no'}, "
        f"10-K target: {'yes' if tenk_target else 'no'}, "
        f"Merger agreement: {'yes' if merger_agreement else 'no'}"
    )

    # ═══════════════════════════════════════════════════════
    # Stage 2: Comparables + Antitrust (parallel)
    # ═══════════════════════════════════════════════════════
    try:
        overlap_assessment, comparable_groups = await asyncio.gather(
            assess_antitrust_overlap(
                tenk_acquirer, tenk_target, deal_params.mars_deal_pk,
                external_signals=external_signals,
                external_overlap=external_overlap,
            ),
            find_comparables(deal_params, tenk_acquirer, tenk_target, merger_agreement),
        )
    except Exception as e:
        logger.error(f"Stage 2 failed: {e}")
        raise PipelineError("stage2_analysis", str(e))

    total_comps = sum(g.count for g in comparable_groups)
    logger.info(
        f"Stage 2 complete — Overlap: {overlap_assessment.overlap_type}/{overlap_assessment.overlap_severity}, "
        f"Comparables: {total_comps}"
    )

    # ═══════════════════════════════════════════════════════
    # Stage 3: Regulatory mapping + Simulation (sequential)
    # ═══════════════════════════════════════════════════════
    try:
        regulatory_map = await map_jurisdictions(
            tenk_acquirer, tenk_target, merger_agreement,
            comparable_groups,
            mars_deal_pk=deal_params.mars_deal_pk,
        )
        logger.info(f"Jurisdictions mapped: {[r.jurisdiction for r in regulatory_map if r.is_required]}")

        simulation = await simulate_regulatory_paths(
            regulatory_map, overlap_assessment, comparable_groups,
            deal_params, merger_agreement,
        )
        logger.info(f"Simulation complete — Critical path: {simulation.critical_path_jurisdiction}")
    except Exception as e:
        logger.error(f"Stage 3 failed: {e}")
        raise PipelineError("stage3_simulation", str(e))

    # ═══════════════════════════════════════════════════════
    # Stage 4: Timeline assembly + Prediction logging
    # ═══════════════════════════════════════════════════════
    try:
        report = await assemble_timeline(
            simulation, press_release_data, merger_agreement, deal_params
        )
        report.overlap_type = overlap_assessment.overlap_type
        report.overlap_severity = overlap_assessment.overlap_severity
        report.comparable_deals_used = total_comps
    except Exception as e:
        logger.error(f"Timeline assembly failed: {e}")
        raise PipelineError("stage4_assembly", str(e))

    # Log prediction (non-fatal if it fails)
    try:
        await log_prediction(report, deal_params.mars_deal_pk)
    except Exception as e:
        logger.warning(f"Prediction logging failed (non-fatal): {e}")

    logger.info(
        f"Pipeline complete — P50: {report.p50_close_date}, "
        f"P75: {report.p75_close_date}, P90: {report.p90_close_date}"
    )

    return report
