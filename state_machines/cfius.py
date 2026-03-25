"""
CFIUS state machine.

States:
  not_applicable → done
  voluntary_filing → accepted → initial_review (45 days)
    → cleared
    → investigation (45 days) → cleared | mitigation_agreement | presidential_review
      → cleared | blocked
"""
from models.state_machine import StateMachineState, StateTransition, JurisdictionName
from models.antitrust import OverlapAssessment
from models.climate import RegulatoryClimate
from config.constants import CFIUS_INITIAL_REVIEW_DAYS, CFIUS_INVESTIGATION_DAYS, CFIUS_PRESIDENTIAL_REVIEW_DAYS
from state_machines.base import BaseRegulatoryStateMachine


class CFIUSStateMachine(BaseRegulatoryStateMachine):

    def jurisdiction(self) -> JurisdictionName:
        return JurisdictionName.CFIUS

    def terminal_states(self) -> set[str]:
        return {"cleared", "cleared_with_mitigation", "blocked"}

    def _clear_terminal_states(self) -> set[str]:
        return {"cleared", "cleared_with_mitigation"}

    def define_states(self) -> list[StateMachineState]:
        return [
            StateMachineState(state_id="not_filed", state_name="Not Filed"),
            StateMachineState(state_id="filed", state_name="Voluntary Filing"),
            StateMachineState(state_id="accepted", state_name="Filing Accepted"),
            StateMachineState(state_id="initial_review", state_name="Initial Review"),
            StateMachineState(state_id="cleared", state_name="Cleared"),
            StateMachineState(state_id="investigation", state_name="Investigation"),
            StateMachineState(state_id="cleared_with_mitigation", state_name="Cleared with Mitigation Agreement"),
            StateMachineState(state_id="presidential_review", state_name="Presidential Review"),
            StateMachineState(state_id="blocked", state_name="Blocked"),
        ]

    def define_transitions(
        self, overlap: OverlapAssessment, climate: RegulatoryClimate, comparable_stats: dict,
    ) -> list[StateTransition]:
        return [
            StateTransition(from_state="not_filed", to_state="filed", probability=1.0, label="CFIUS Filing", base_probability=1.0),
            StateTransition(from_state="filed", to_state="accepted", probability=1.0, label="Accepted", base_probability=1.0),
            StateTransition(from_state="accepted", to_state="initial_review", probability=1.0, label="Initial Review Begins", base_probability=1.0),
            StateTransition(from_state="initial_review", to_state="cleared", probability=0.60, label="Cleared", base_probability=0.60),
            StateTransition(from_state="initial_review", to_state="investigation", probability=0.40, label="Extended to Investigation", base_probability=0.40),
            StateTransition(from_state="investigation", to_state="cleared", probability=0.30, label="Cleared", base_probability=0.30),
            StateTransition(from_state="investigation", to_state="cleared_with_mitigation", probability=0.50, label="Mitigation Agreement", base_probability=0.50),
            StateTransition(from_state="investigation", to_state="presidential_review", probability=0.20, label="Presidential Review", base_probability=0.20),
            StateTransition(from_state="presidential_review", to_state="cleared", probability=0.70, label="Cleared", base_probability=0.70),
            StateTransition(from_state="presidential_review", to_state="blocked", probability=0.30, label="Blocked", base_probability=0.30),
        ]

    def compute_duration_distributions(
        self, comparable_stats: dict, climate: RegulatoryClimate,
    ) -> dict[str, dict]:
        return {
            "not_filed": {"p50": 0, "p75": 0, "p90": 0},
            "filed": {"p50": 5, "p75": 10, "p90": 14},
            "accepted": {"p50": 7, "p75": 10, "p90": 14},
            "initial_review": {"p50": CFIUS_INITIAL_REVIEW_DAYS, "p75": 45, "p90": 45},
            "cleared": {"p50": 0, "p75": 0, "p90": 0},
            "investigation": {"p50": CFIUS_INVESTIGATION_DAYS, "p75": 45, "p90": 45},
            "cleared_with_mitigation": {"p50": 10, "p75": 20, "p90": 30},
            "presidential_review": {"p50": CFIUS_PRESIDENTIAL_REVIEW_DAYS, "p75": 15, "p90": 15},
            "blocked": {"p50": 0, "p75": 0, "p90": 0},
        }
