"""Comparable deal models with feature-weighted scoring."""
from pydantic import BaseModel
from typing import Optional
from datetime import date
from enum import Enum


class ComparableSource(str, Enum):
    ACQUIRER_HISTORY = "acquirer_history"
    SECTOR_MATCH = "sector_match"
    SIZE_MATCH = "size_match"


class RegulatoryMilestone(BaseModel):
    """A single regulatory milestone from a comparable deal."""
    jurisdiction: str
    event: str
    event_date: Optional[date] = None
    days_from_announcement: Optional[int] = None


class ComparableDeal(BaseModel):
    """A single comparable deal pulled from MARS."""
    deal_pk: int
    deal_id: str
    acquirer: str
    target: str
    sector: str
    industry: str
    deal_value_usd: Optional[float] = None
    deal_structure: str
    buyer_type: str
    announcement_date: date
    close_date: Optional[date] = None
    timeline_days: Optional[int] = None
    deal_outcome: str
    jurisdictions_required: list[str] = []
    had_second_request: bool = False
    had_ec_phase_2: bool = False
    had_cma_phase_2: bool = False
    regulatory_milestones: list[RegulatoryMilestone] = []
    horizontal_overlap: bool = False
    remedy_required: bool = False
    remedy_type: Optional[str] = None
    similarity_score: float = 0.0
    feature_scores: dict[str, float] = {}
    time_weight: float = 1.0
    weighted_score: float = 0.0
    source: ComparableSource = ComparableSource.SECTOR_MATCH


class ComparableGroup(BaseModel):
    """A group of comparable deals with aggregate statistics."""
    source: ComparableSource
    deals: list[ComparableDeal]
    count: int = 0
    median_timeline_days: Optional[int] = None
    p25_timeline_days: Optional[int] = None
    p75_timeline_days: Optional[int] = None
    p90_timeline_days: Optional[int] = None
    jurisdiction_stats: dict[str, dict] = {}
