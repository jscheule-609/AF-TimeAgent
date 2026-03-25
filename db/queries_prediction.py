"""Prediction storage and retrieval queries."""
import json
from typing import Optional
from db.connection import get_pool


CREATE_PREDICTIONS_TABLE = """
CREATE TABLE IF NOT EXISTS timing_predictions (
    prediction_id TEXT PRIMARY KEY,
    deal_pk BIGINT REFERENCES deals(deal_pk),
    acquirer_ticker TEXT NOT NULL,
    target_ticker TEXT NOT NULL,
    prediction_date TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Predicted outcomes
    p50_close_date DATE,
    p75_close_date DATE,
    p90_close_date DATE,
    predicted_critical_path TEXT,
    predicted_scenarios JSONB,
    predicted_milestones JSONB,
    predicted_risk_flags JSONB,
    -- Model inputs
    overlap_type TEXT,
    overlap_severity TEXT,
    enforcement_regime TEXT,
    comparable_deals_used INTEGER,
    jurisdictions_modeled JSONB,
    -- Actuals (filled in when deal closes)
    actual_close_date DATE,
    actual_timeline_days INTEGER,
    actual_critical_path TEXT,
    actual_outcome TEXT DEFAULT 'pending',
    -- Calibration metrics (filled in post-close)
    p50_error_days INTEGER,
    p75_error_days INTEGER,
    p90_error_days INTEGER,
    close_within_p50 BOOLEAN,
    close_within_p75 BOOLEAN,
    close_within_p90 BOOLEAN,
    -- Metadata
    model_version TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_timing_predictions_deal ON timing_predictions(deal_pk);
CREATE INDEX IF NOT EXISTS idx_timing_predictions_date ON timing_predictions(prediction_date);
"""


async def store_prediction(prediction: dict) -> str:
    """Store a new prediction record. Returns prediction_id."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO timing_predictions (
                prediction_id, deal_pk, acquirer_ticker, target_ticker,
                prediction_date, p50_close_date, p75_close_date, p90_close_date,
                predicted_critical_path, predicted_scenarios, predicted_milestones,
                predicted_risk_flags, overlap_type, overlap_severity,
                enforcement_regime, comparable_deals_used, jurisdictions_modeled,
                model_version
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11::jsonb,
                $12::jsonb, $13, $14, $15, $16, $17::jsonb, $18
            )
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
            json.dumps(prediction.get("predicted_scenarios", [])),
            json.dumps(prediction.get("predicted_milestones", [])),
            json.dumps(prediction.get("predicted_risk_flags", [])),
            prediction.get("overlap_type", ""),
            prediction.get("overlap_severity", ""),
            prediction.get("enforcement_regime", "normal"),
            prediction.get("comparable_deals_used", 0),
            json.dumps(prediction.get("jurisdictions_modeled", [])),
            prediction.get("model_version", "0.1.0"),
        )
        return prediction["prediction_id"]


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
