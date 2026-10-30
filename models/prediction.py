"""Prediction tracking and calibration models."""
from pydantic import BaseModel
from typing import Optional
from datetime import date


class PredictionRecord(BaseModel):
    """A stored prediction for future calibration."""
    prediction_id: str
    deal_pk: Optional[int] = None
    acquirer_ticker: str
    target_ticker: str
    prediction_date: date
    p50_close_date: Optional[date] = None
    p75_close_date: Optional[date] = None
    p90_close_date: Optional[date] = None
    predicted_critical_path: str = ""
    predicted_scenarios: list[dict] = []
    predicted_milestones: list[dict] = []
    predicted_risk_flags: list[dict] = []
    overlap_type: str = ""
    overlap_severity: str = ""
    enforcement_regime: str = "normal"
    comparable_deals_used: int = 0
    jurisdictions_modeled: list[str] = []
    guidance_reconciliation: Optional[dict] = None
    actual_close_date: Optional[date] = None
    actual_timeline_days: Optional[int] = None
    actual_outcome: str = "pending"
    model_version: str = "0.2.0"
    # Append-only columns (AF-AJ migration 048b): the run that wrote the
    # row, the date the prediction is "as of", and a reference to the
    # inputs it was computed from (deal row updated_at, comparable
    # deal_pks, model / calibration version, guidance source).
    run_id: Optional[str] = None
    as_of_date: Optional[date] = None
    input_snapshot_ref: Optional[dict] = None


class CalibrationMetrics(BaseModel):
    """Aggregate calibration metrics across predictions."""
    total_predictions: int = 0
    predictions_with_actuals: int = 0
    pct_within_p50: float = 0.0
    pct_within_p75: float = 0.0
    pct_within_p90: float = 0.0
    mean_absolute_error_days: float = 0.0
    median_absolute_error_days: float = 0.0
    jurisdiction_biases: dict[str, float] = {}
    sector_biases: dict[str, float] = {}
