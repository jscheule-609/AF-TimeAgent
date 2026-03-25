"""Regulatory jurisdiction requirement models."""
from pydantic import BaseModel


class JurisdictionRequirement(BaseModel):
    """A single jurisdiction's filing requirement assessment."""
    jurisdiction: str
    is_required: bool
    confidence: float  # 1.0 = merger agreement, 0.8 = revenue, 0.6 = precedent, 0.4 = sector
    source: str  # "merger_agreement", "revenue_threshold", "comparable_precedent", "sector_assessment"
    revenue_data: dict = {}
    notes: str = ""
