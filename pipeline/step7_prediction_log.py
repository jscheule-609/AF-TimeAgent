"""
Step 7: Prediction Logging

Stores predictions for future calibration against actual outcomes.

Append-only: every call inserts a new ``timing_predictions`` row with a
fresh ``prediction_id`` (no upsert), stamped with the current run's
``run_id`` (``db.connection.set_run_context``), ``as_of_date`` and an
``input_snapshot_ref`` describing the inputs the forecast was computed
from.
"""
import uuid
import logging
from datetime import date
from models.timeline import DealTimingReport
from models.prediction import PredictionRecord
from db.connection import current_run_id
from db.queries_prediction import store_prediction

logger = logging.getLogger(__name__)


def build_input_snapshot_ref(
    report: DealTimingReport,
    *,
    deal_updated_at=None,
    comparable_deal_pks: list[int] | None = None,
    extra: dict | None = None,
) -> dict:
    """The ``input_snapshot_ref`` JSONB for a stored prediction.

    Minimum keys (design section 6): the deal row's ``updated_at`` as read,
    the comparable deal_pks, the engine ``model_version`` and the
    guidance source the reconciliation used.  ``calibration_fingerprint``
    identifies the ``config/calibration.json`` (base rates) in force.
    """
    from config.calibration import calibration_fingerprint

    gr = report.guidance_reconciliation
    ref = {
        "deal_updated_at": deal_updated_at,
        "comparable_deal_pks": sorted(set(comparable_deal_pks or [])),
        "model_version": PredictionRecord.model_fields["model_version"].default,
        "guidance_source": (gr.guidance_source or None) if gr else None,
        "calibration_fingerprint": calibration_fingerprint(),
    }
    if extra:
        ref.update(extra)
    return ref


async def log_prediction(
    report: DealTimingReport,
    deal_pk: int | None = None,
    input_snapshot_ref: dict | None = None,
    run_id: str | None = None,
) -> str:
    """Append a prediction row and return its prediction ID.

    ``run_id`` defaults to the current run context (set by the entry
    point: batch_run, the API, the NOTIFY listener, run_deal, backtest).
    """
    prediction_id = str(uuid.uuid4())
    run_id = run_id or current_run_id()
    today = date.today()
    if input_snapshot_ref is None:
        input_snapshot_ref = build_input_snapshot_ref(report)

    record = PredictionRecord(
        prediction_id=prediction_id,
        deal_pk=deal_pk,
        acquirer_ticker=report.acquirer,
        target_ticker=report.target,
        prediction_date=today,
        p50_close_date=report.p50_close_date,
        p75_close_date=report.p75_close_date,
        p90_close_date=report.p90_close_date,
        predicted_critical_path=report.critical_path_jurisdiction,
        predicted_scenarios=[s.model_dump() for s in report.scenarios],
        predicted_milestones=[m.model_dump() for m in report.milestones],
        predicted_risk_flags=[f.model_dump() for f in report.risk_flags],
        overlap_type=report.overlap_type,
        overlap_severity=report.overlap_severity,
        enforcement_regime=report.enforcement_regime,
        comparable_deals_used=report.comparable_deals_used,
        jurisdictions_modeled=list(report.jurisdictions_modeled),
        guidance_reconciliation=(
            report.guidance_reconciliation.model_dump(
                mode="json"
            )
            if report.guidance_reconciliation else None
        ),
        run_id=run_id,
        as_of_date=today,
        input_snapshot_ref=input_snapshot_ref,
    )

    try:
        # store_prediction RETURNs the prediction_id it wrote; report that
        # one (callers key on it: update_prediction_actuals in the
        # backtest, the MCP trigger response).
        stored_id = await store_prediction(record.model_dump())
        prediction_id = stored_id or prediction_id
        report.prediction_id = prediction_id
        report.run_id = run_id
        logger.info(f"Prediction stored: {prediction_id} (run {run_id})")
    except Exception as e:
        logger.error(f"Failed to store prediction: {e}")

    return prediction_id
