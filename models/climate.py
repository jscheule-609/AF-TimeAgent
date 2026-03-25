"""Regulatory climate and enforcement regime models."""
from pydantic import BaseModel
from typing import Optional
from datetime import date


class EnforcementRegime(BaseModel):
    """Enforcement regime classification for a single jurisdiction."""
    jurisdiction: str
    regime: str = "normal"  # "aggressive", "normal", "lenient"
    label: str = "Normal Enforcement"
    multipliers: dict[str, float] = {}


class RegulatoryClimate(BaseModel):
    """Overall regulatory climate assessment across jurisdictions."""
    regimes: list[EnforcementRegime] = []
    overall_regime: str = "normal"
    assessment_date: Optional[date] = None
    data_points_used: int = 0
