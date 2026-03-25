"""Australia ACCC state machine — simplified informal/formal review."""
from models.state_machine import StateMachineState, StateTransition, JurisdictionName
from models.antitrust import OverlapAssessment
from models.climate import RegulatoryClimate
from config.constants import ACCC_INFORMAL_REVIEW_TYPICAL_WEEKS, ACCC_FORMAL_REVIEW_WEEKS
from state_machines.base import BaseRegulatoryStateMachine


class ACCCStateMachine(BaseRegulatoryStateMachine):

    def jurisdiction(self) -> JurisdictionName:
        return JurisdictionName.ACCC

    def terminal_states(self) -> set[str]:
        return {"cleared", "cleared_with_conditions", "opposed"}

    def _clear_terminal_states(self) -> set[str]:
        return {"cleared", "cleared_with_conditions"}

    def define_states(self) -> list[StateMachineState]:
        return [
            StateMachineState(state_id="not_filed", state_name="Not Filed"),
            StateMachineState(state_id="informal_review", state_name="Informal Review"),
            StateMachineState(state_id="cleared", state_name="Cleared"),
            StateMachineState(state_id="formal_review", state_name="Formal Review"),
            StateMachineState(state_id="cleared_with_conditions", state_name="Cleared with Conditions"),
            StateMachineState(state_id="opposed", state_name="Opposed"),
        ]

    def define_transitions(
        self, overlap: OverlapAssessment, climate: RegulatoryClimate, comparable_stats: dict,
    ) -> list[StateTransition]:
        formal_prob = 0.15 if overlap.overlap_severity in ("high", "medium") else 0.05
        return [
            StateTransition(from_state="not_filed", to_state="informal_review", probability=1.0, label="Begin Informal Review", base_probability=1.0),
            StateTransition(from_state="informal_review", to_state="cleared", probability=1.0 - formal_prob, label="Cleared", base_probability=0.90),
            StateTransition(from_state="informal_review", to_state="formal_review", probability=formal_prob, label="Formal Review", base_probability=0.10),
            StateTransition(from_state="formal_review", to_state="cleared_with_conditions", probability=0.70, label="Cleared with Conditions", base_probability=0.70),
            StateTransition(from_state="formal_review", to_state="opposed", probability=0.30, label="Opposed", base_probability=0.30),
        ]

    def compute_duration_distributions(
        self, comparable_stats: dict, climate: RegulatoryClimate,
    ) -> dict[str, dict]:
        inf_low = ACCC_INFORMAL_REVIEW_TYPICAL_WEEKS[0] * 7
        inf_high = ACCC_INFORMAL_REVIEW_TYPICAL_WEEKS[1] * 7
        formal = ACCC_FORMAL_REVIEW_WEEKS * 7
        return {
            "not_filed": {"p50": 0, "p75": 0, "p90": 0},
            "informal_review": {"p50": inf_low, "p75": (inf_low + inf_high) // 2, "p90": inf_high},
            "cleared": {"p50": 0, "p75": 0, "p90": 0},
            "formal_review": {"p50": formal, "p75": int(formal * 1.15), "p90": int(formal * 1.3)},
            "cleared_with_conditions": {"p50": 5, "p75": 10, "p90": 14},
            "opposed": {"p50": 0, "p75": 0, "p90": 0},
        }
