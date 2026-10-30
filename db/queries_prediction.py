"""Prediction storage and retrieval queries.

``timing_predictions`` is append-only (AF-AJ migration 048b, MARS
point-in-time design section 6): every run inserts a NEW row with a fresh
``prediction_id``; nothing here updates an earlier row's forecast.  The
latest row per deal is ``v_latest_timing_prediction``.  Scoring columns
(``actual_*``, ``*_error_days``, ``close_within_*``) are written only by
``update_prediction_actuals`` and ``scripts.score_predictions``, keyed on
``prediction_id``.

Every write goes through ``db.connection.acquire_for_write`` so the
session carries ``mars.writer`` / ``mars.run_id`` (design section 5.4).
"""
import json
import logging
from datetime import date, datetime
from typing import Optional

from db.connection import acquire_for_write, current_run_id, get_pool

logger = logging.getLogger(__name__)


def _json_serial(obj):
    """JSON serializer for date/datetime objects."""
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    raise TypeError(f"Type {type(obj)} not serializable")


def _as_of_date(prediction: dict) -> Optional[date]:
    """``as_of_date`` = the prediction date's calendar date."""
    explicit = prediction.get("as_of_date")
    if explicit is not None:
        return explicit
    pd = prediction.get("prediction_date")
    if isinstance(pd, datetime):
        return pd.date()
    return pd


# CREATE_PREDICTIONS_TABLE removed 2026-09-12: MARS DDL is authored in
# AF-AJ/migrations/mars/ (one tracker).  048b owns the append-only shape.

_INSERT_PREDICTION = """
    INSERT INTO timing_predictions (
        prediction_id, deal_pk, acquirer_ticker, target_ticker,
        prediction_date, p50_close_date, p75_close_date, p90_close_date,
        predicted_critical_path, predicted_scenarios, predicted_milestones,
        predicted_risk_flags, overlap_type, overlap_severity,
        enforcement_regime, comparable_deals_used,
        jurisdictions_modeled,
        guidance_reconciliation,
        model_version,
        run_id, as_of_date, input_snapshot_ref
    ) VALUES (
        $1, $2, $3, $4, $5, $6, $7, $8, $9,
        $10::jsonb, $11::jsonb, $12::jsonb,
        $13, $14, $15, $16, $17::jsonb,
        $18::jsonb, $19,
        $20, $21, $22::jsonb
    )
    RETURNING prediction_id
"""


async def store_prediction(prediction: dict) -> str:
    """Append a prediction row.  Returns the stored prediction_id.

    Plain INSERT, no ON CONFLICT: a re-prediction of a deal is a new row
    with its own ``prediction_id`` (the caller generates one per run).
    ``run_id`` defaults to the current run context, ``as_of_date`` to the
    prediction date, ``input_snapshot_ref`` to whatever the caller passed
    (see ``pipeline.step7_prediction_log``).
    """
    run_id = prediction.get("run_id") or current_run_id()
    snapshot_ref = prediction.get("input_snapshot_ref")
    async with acquire_for_write(run_id=run_id) as conn:
        row = await conn.fetchrow(
            _INSERT_PREDICTION,
            prediction["prediction_id"],
            prediction.get("deal_pk"),
            prediction["acquirer_ticker"],
            prediction["target_ticker"],
            prediction["prediction_date"],
            prediction.get("p50_close_date"),
            prediction.get("p75_close_date"),
            prediction.get("p90_close_date"),
            prediction.get("predicted_critical_path", ""),
            json.dumps(
                prediction.get("predicted_scenarios", []),
                default=_json_serial,
            ),
            json.dumps(
                prediction.get("predicted_milestones", []),
                default=_json_serial,
            ),
            json.dumps(
                prediction.get("predicted_risk_flags", []),
                default=_json_serial,
            ),
            prediction.get("overlap_type", ""),
            prediction.get("overlap_severity", ""),
            prediction.get("enforcement_regime", "normal"),
            prediction.get("comparable_deals_used", 0),
            json.dumps(
                prediction.get("jurisdictions_modeled", []),
                default=_json_serial,
            ),
            json.dumps(
                prediction.get("guidance_reconciliation"),
                default=_json_serial,
            ) if prediction.get("guidance_reconciliation") else None,
            prediction.get("model_version", "0.1.0"),
            run_id,
            _as_of_date(prediction),
            json.dumps(snapshot_ref, default=_json_serial)
            if snapshot_ref is not None else None,
        )
        return row["prediction_id"]


async def get_prediction(prediction_id: str) -> Optional[dict]:
    """Get a prediction by ID."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM timing_predictions WHERE prediction_id = $1",
            prediction_id,
        )
        return dict(row) if row else None


async def get_latest_prediction(deal_pk: int) -> Optional[dict]:
    """The latest row for a deal, via ``v_latest_timing_prediction``."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM v_latest_timing_prediction WHERE deal_pk = $1",
            deal_pk,
        )
        return dict(row) if row else None


async def update_prediction_actuals(
    prediction_id: str,
    actual_close_date,
    actual_timeline_days: int,
    actual_outcome: str,
    actual_critical_path: str,
) -> None:
    """Update ONE prediction row (by prediction_id) with actual results
    and compute its error metrics."""
    async with acquire_for_write() as conn:
        status = await conn.execute(
            """
            UPDATE timing_predictions SET
                actual_close_date = $2,
                actual_timeline_days = $3,
                actual_outcome = $4,
                actual_critical_path = $5,
                p50_error_days = $2 - p50_close_date,
                p75_error_days = $2 - p75_close_date,
                p90_error_days = $2 - p90_close_date,
                close_within_p50 = ($2 <= p50_close_date),
                close_within_p75 = ($2 <= p75_close_date),
                close_within_p90 = ($2 <= p90_close_date),
                updated_at = NOW()
            WHERE prediction_id = $1
            """,
            prediction_id,
            actual_close_date,
            actual_timeline_days,
            actual_outcome,
            actual_critical_path,
        )
        affected_rows = int(status.rsplit(" ", 1)[-1])
        if affected_rows == 0:
            logger.warning(
                "No prediction matched actuals update: prediction_id=%s (UPDATE 0)",
                prediction_id,
            )


async def get_calibration_data() -> list[dict]:
    """Get all predictions with actual outcomes for calibration."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM timing_predictions WHERE actual_close_date IS NOT NULL"
        )
        return [dict(r) for r in rows]
