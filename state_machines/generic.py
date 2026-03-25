"""Generic foreign filing state machine for jurisdictions without specific models."""
from models.state_machine import StateMachineState, StateTransition, JurisdictionName
from models.antitrust import OverlapAssessment
from models.climate import RegulatoryClimate
from state_machines.base import BaseRegulatoryStateMachine


class GenericStateMachine(BaseRegulatoryStateMachine):

    def jurisdiction(self) -> JurisdictionName:
        return JurisdictionName.GENERIC

    def terminal_states(self) -> set[str]:
        return {"cleared", "cleared_with_conditions", "blocked"}

    def _clear_terminal_states(self) -> set[str]:
        return {"cleared", "cleared_with_conditions"}

    def define_states(self) -> list[StateMachineState]:
        return [
            StateMachineState(state_id="not_filed", state_name="Not Filed"),
            StateMachineState(state_id="filed", state_name="Filed"),
            StateMachineState(state_id="review", state_name="Under Review"),
            StateMachineState(state_id="cleared", state_name="Cleared"),
            StateMachineState(state_id="extended_review", state_name="Extended Review"),
            StateMachineState(state_id="cleared_with_conditions", state_name="Cleared with Conditions"),
            StateMachineState(state_id="blocked", state_name="Blocked"),
        ]

    def define_transitions(
        self, overlap: OverlapAssessment, climate: RegulatoryClimate, comparable_stats: dict,
    ) -> list[StateTransition]:
        ext_prob = 0.15 if overlap.overlap_severity in ("high", "medium") else 0.05
        return [
            StateTransition(from_state="not_filed", to_state="filed", probability=1.0, label="Filing", base_probability=1.0),
            StateTransition(from_state="filed", to_state="review", probability=1.0, label="Review Begins", base_probability=1.0),
            StateTransition(from_state="review", to_state="cleared", probability=1.0 - ext_prob, label="Cleared", base_probability=0.90),
            StateTransition(from_state="review", to_state="extended_review", probability=ext_prob, label="Extended Review", base_probability=0.10),
            StateTransition(from_state="extended_review", to_state="cleared_with_conditions", probability=0.80, label="Cleared with Conditions", base_probability=0.80),
            StateTransition(from_state="extended_review", to_state="blocked", probability=0.20, label="Blocked", base_probability=0.20),
        ]

    def compute_duration_distributions(
        self, comparable_stats: dict, climate: RegulatoryClimate,
    ) -> dict[str, dict]:
        return {
            "not_filed": {"p50": 0, "p75": 0, "p90": 0},
            "filed": {"p50": 5, "p75": 10, "p90": 14},
            "review": {"p50": 45, "p75": 60, "p90": 90},
            "cleared": {"p50": 0, "p75": 0, "p90": 0},
            "extended_review": {"p50": 60, "p75": 90, "p90": 120},
            "cleared_with_conditions": {"p50": 5, "p75": 10, "p90": 14},
            "blocked": {"p50": 0, "p75": 0, "p90": 0},
        }
