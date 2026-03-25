"""
Step 7: Prediction Logging

Stores predictions for future calibration against actual outcomes.
"""
import uuid
import logging
from datetime import date
from models.timeline import DealTimingReport
from models.prediction import PredictionRecord
from db.queries_prediction import store_prediction

logger = logging.getLogger(__name__)


async def log_prediction(report: DealTimingReport, deal_pk: int | None = None) -> str:
    """Store a prediction record and return the prediction ID."""
    prediction_id = str(uuid.uuid4())

    record = PredictionRecord(
        prediction_id=prediction_id,
        deal_pk=deal_pk,
        acquirer_ticker=report.acquirer,
        target_ticker=report.target,
        prediction_date=date.today(),
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
        jurisdictions_modeled=[],
    )

    try:
        await store_prediction(record.model_dump())
        report.prediction_id = prediction_id
        logger.info(f"Prediction stored: {prediction_id}")
    except Exception as e:
        logger.error(f"Failed to store prediction: {e}")

    return prediction_id
