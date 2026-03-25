"""Antitrust overlap assessment models."""
from pydantic import BaseModel
from typing import Optional


class OverlapAssessment(BaseModel):
    """Result of antitrust competitive overlap analysis."""
    overlap_type: str = "none"  # "horizontal", "vertical", "conglomerate", "mixed", "none"
    overlap_severity: str = "none"  # "high", "medium", "low", "none"
    specific_overlap_markets: list[str] = []
    mutual_competitor_flag: bool = False
    estimated_combined_share_pct: Optional[float] = None
    hhi_delta_estimate: Optional[float] = None
    second_request_probability_base: float = 0.0
    web_search_signals: list[str] = []
    reasoning: str = ""
