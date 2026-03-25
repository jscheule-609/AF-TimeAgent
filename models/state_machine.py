"""
Regulatory state machine models.
Central to the "regulatory path forecaster" design.
"""
from pydantic import BaseModel
from typing import Optional
from datetime import date
from enum import Enum


class JurisdictionName(str, Enum):
    HSR = "HSR"
    EC = "EC"
    CMA = "CMA"
    SAMR = "SAMR"
    CFIUS = "CFIUS"
    ACCC = "ACCC"
    GENERIC = "GENERIC"


class StateStatus(str, Enum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    BLOCKED = "blocked"


class StateMachineState(BaseModel):
    """A single state in a regulatory state machine."""
    state_id: str
    state_name: str
    status: StateStatus = StateStatus.NOT_STARTED
    entered_date: Optional[date] = None
    expected_duration_days: int = 0
    expected_completion_date: Optional[date] = None
    actual_completion_date: Optional[date] = None
    duration_p50_days: int = 0
    duration_p75_days: int = 0
    duration_p90_days: int = 0


class StateTransition(BaseModel):
    """A transition between states with associated probability."""
    from_state: str
    to_state: str
    probability: float
    label: str
    base_probability: float
    adjustments: dict[str, float] = {}


class PathOutcome(BaseModel):
    """A complete path through the state machine with probability and timeline."""
    path_id: str
    path_label: str
    states: list[str]
    total_duration_days_p50: int
    total_duration_days_p75: int
    total_duration_days_p90: int
    path_probability: float
    is_terminal_clear: bool


class JurisdictionSimulation(BaseModel):
    """Complete simulation output for one jurisdiction."""
    jurisdiction: JurisdictionName
    is_required: bool
    confidence_required: float
    source_of_requirement: str
    states: list[StateMachineState]
    transitions: list[StateTransition]
    possible_paths: list[PathOutcome]
    expected_duration_days_p50: int
    expected_duration_days_p75: int
    expected_duration_days_p90: int
    contractual_filing_deadline: Optional[date] = None
    contractual_filing_deadline_days: Optional[int] = None


class FullSimulationResult(BaseModel):
    """Combined simulation across all jurisdictions."""
    jurisdictions: list[JurisdictionSimulation]
    critical_path_jurisdiction: str
    critical_path_duration_p50: int
    critical_path_duration_p75: int
    critical_path_duration_p90: int
    scenario_paths: list[dict] = []
