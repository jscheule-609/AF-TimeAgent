"""Final output models — the table and scenario paths."""
from pydantic import BaseModel
from typing import Optional
from datetime import date


class MilestoneRow(BaseModel):
    """One row in the output table."""
    milestone: str
    jurisdiction: str
    contractual_deadline: Optional[date] = None
    base_case_date: Optional[date] = None
    comparable_median_date: Optional[date] = None
    extended_case_date: Optional[date] = None
    stress_case_date: Optional[date] = None
    risk_flags: list[str] = []
    notes: str = ""


class ScenarioPath(BaseModel):
    """A scenario path with joint probability across all jurisdictions."""
    scenario_name: str
    probability_pct: float
    expected_close_date: Optional[date] = None
    duration_days: int
    description: str
    key_assumptions: list[str] = []
    jurisdiction_paths: dict[str, str] = {}


class RiskFlag(BaseModel):
    """A risk flag for the output."""
    flag: str
    severity: str  # "high", "medium", "low"
    jurisdiction: Optional[str] = None
    detail: str


class GuidanceAdjustment(BaseModel):
    """A single adjustment made during guidance reconciliation."""
    component: str       # e.g. "state_puc_timeline", "hsr_second_request"
    original_days: int   # what the model originally estimated
    adjusted_days: int   # what it was adjusted to
    reason: str          # human-readable explanation


class GuidanceReconciliation(BaseModel):
    """Result of comparing model estimate to company/AJ guidance."""
    guidance_source: str = ""          # "AJ", "company", "both"
    guidance_date: Optional[date] = None
    model_p50_date: Optional[date] = None
    gap_days: int = 0                  # positive = guidance later than model
    adjustments: list[GuidanceAdjustment] = []
    explained_days: int = 0            # how much of the gap we explained
    unexplained_days: int = 0          # remaining gap
    flag: str = "no_guidance"          # aligned, model_faster, model_slower, opportunity


class DealTimingReport(BaseModel):
    """The complete output of the tool."""
    acquirer: str
    target: str
    deal_value_usd: float
    announcement_date: date
    milestones: list[MilestoneRow]
    scenarios: list[ScenarioPath]
    risk_flags: list[RiskFlag]
    p50_close_date: Optional[date] = None
    p75_close_date: Optional[date] = None
    p90_close_date: Optional[date] = None
    probability_close_by_outside_date: Optional[float] = None
    outside_date: Optional[date] = None
    critical_path_jurisdiction: str = ""
    enforcement_regime: str = "normal"
    enforcement_regime_detail: str = ""
    overlap_type: str = ""
    overlap_severity: str = ""
    comparable_deals_used: int = 0
    guidance_reconciliation: Optional[GuidanceReconciliation] = None
    prediction_id: Optional[str] = None
    generated_at: Optional[str] = None
