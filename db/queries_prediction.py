"""Prediction storage and retrieval queries."""
import json
from datetime import date, datetime
from typing import Optional
from db.connection import get_pool


def _json_serial(obj):
    """JSON serializer for date/datetime objects."""
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    raise TypeError(f"Type {type(obj)} not serializable")


# CREATE_PREDICTIONS_TABLE removed 2026-09-12: it was never invoked, omitted
# guidance_reconciliation and the UNIQUE (deal_pk) that store_prediction()
# relies on. MARS DDL is authored in AF-AJ/migrations/mars/ (one tracker).



async def store_prediction(prediction: dict) -> str:
    """Store a new prediction record. Returns prediction_id.

    On conflict (deal_pk already has a row), keeps the existing prediction_id
    stable — downstream `update_prediction_actuals` looks up by prediction_id,
    so overwriting it on re-predict would strand any external references.
    Returns the actual prediction_id from the DB via RETURNING.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO timing_predictions (
                prediction_id, deal_pk, acquirer_ticker, target_ticker,
                prediction_date, p50_close_date, p75_close_date, p90_close_date,
                predicted_critical_path, predicted_scenarios, predicted_milestones,
                predicted_risk_flags, overlap_type, overlap_severity,
                enforcement_regime, comparable_deals_used,
                jurisdictions_modeled,
                guidance_reconciliation,
                model_version
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9,
                $10::jsonb, $11::jsonb, $12::jsonb,
                $13, $14, $15, $16, $17::jsonb,
                $18::jsonb, $19
            )
            ON CONFLICT (deal_pk) DO UPDATE SET
                acquirer_ticker         = EXCLUDED.acquirer_ticker,
                target_ticker           = EXCLUDED.target_ticker,
                prediction_date         = EXCLUDED.prediction_date,
                p50_close_date          = EXCLUDED.p50_close_date,
                p75_close_date          = EXCLUDED.p75_close_date,
                p90_close_date          = EXCLUDED.p90_close_date,
                predicted_critical_path = EXCLUDED.predicted_critical_path,
                predicted_scenarios     = EXCLUDED.predicted_scenarios,
                predicted_milestones    = EXCLUDED.predicted_milestones,
                predicted_risk_flags    = EXCLUDED.predicted_risk_flags,
                overlap_type            = EXCLUDED.overlap_type,
                overlap_severity        = EXCLUDED.overlap_severity,
                enforcement_regime      = EXCLUDED.enforcement_regime,
                comparable_deals_used   = EXCLUDED.comparable_deals_used,
                jurisdictions_modeled   = EXCLUDED.jurisdictions_modeled,
                guidance_reconciliation = EXCLUDED.guidance_reconciliation,
                model_version           = EXCLUDED.model_version,
                updated_at              = NOW()
            RETURNING prediction_id
            """,
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


async def update_prediction_actuals(
    prediction_id: str,
    actual_close_date,
    actual_timeline_days: int,
    actual_outcome: str,
    actual_critical_path: str,
) -> None:
    """Update a prediction with actual results and compute error metrics."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
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


async def get_calibration_data() -> list[dict]:
    """Get all predictions with actual outcomes for calibration."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM timing_predictions WHERE actual_close_date IS NOT NULL"
        )
        return [dict(r) for r in rows]
