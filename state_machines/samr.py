"""
China SAMR state machine.

States:
  not_filed → filed → acceptance
    → phase_1 (30 days) → phase_1_cleared
    → phase_2 (90 days) → phase_2_cleared
    → phase_3 (60 days) → cleared_with_conditions | prohibited
    → simplified_review (30 days)
"""
from models.state_machine import StateMachineState, StateTransition, JurisdictionName
from models.antitrust import OverlapAssessment
from models.climate import RegulatoryClimate
from config.constants import (
    SAMR_PHASE_1_DAYS, SAMR_PHASE_2_DAYS, SAMR_PHASE_3_DAYS,
    SAMR_SIMPLIFIED_REVIEW_DAYS, SAMR_PRE_ACCEPTANCE_TYPICAL_DAYS_RANGE,
)
from state_machines.base import BaseRegulatoryStateMachine


class SAMRStateMachine(BaseRegulatoryStateMachine):

    def jurisdiction(self) -> JurisdictionName:
        return JurisdictionName.SAMR

    def terminal_states(self) -> set[str]:
        return {"phase_1_cleared", "phase_2_cleared", "cleared_with_conditions", "prohibited", "simplified_cleared"}

    def _clear_terminal_states(self) -> set[str]:
        return {"phase_1_cleared", "phase_2_cleared", "cleared_with_conditions", "simplified_cleared"}

    def define_states(self) -> list[StateMachineState]:
        return [
            StateMachineState(state_id="not_filed", state_name="Not Filed"),
            StateMachineState(state_id="filed", state_name="SAMR Filed"),
            StateMachineState(state_id="acceptance", state_name="Filing Accepted"),
            StateMachineState(state_id="phase_1", state_name="Phase 1 Review"),
            StateMachineState(state_id="phase_1_cleared", state_name="Phase 1 Cleared"),
            StateMachineState(state_id="phase_2", state_name="Phase 2 Review"),
            StateMachineState(state_id="phase_2_cleared", state_name="Phase 2 Cleared"),
            StateMachineState(state_id="phase_3", state_name="Phase 3 Additional Review"),
            StateMachineState(state_id="cleared_with_conditions", state_name="Cleared with Conditions"),
            StateMachineState(state_id="prohibited", state_name="Prohibited"),
            StateMachineState(state_id="simplified_review", state_name="Simplified Review"),
            StateMachineState(state_id="simplified_cleared", state_name="Simplified Cleared"),
        ]

    def define_transitions(
        self, overlap: OverlapAssessment, climate: RegulatoryClimate, comparable_stats: dict,
    ) -> list[StateTransition]:
        # Simplified vs normal track
        simplified_prob = 0.30 if overlap.overlap_severity in ("none", "low") else 0.05
        normal_prob = 1.0 - simplified_prob

        # Phase progression probabilities
        p1_clear = 0.50 if overlap.overlap_severity in ("none", "low") else 0.25
        p2_prob = 1.0 - p1_clear
        p2_clear = 0.60
        p3_prob = 0.40

        return [
            StateTransition(from_state="not_filed", to_state="filed", probability=1.0, label="SAMR Filing", base_probability=1.0),
            StateTransition(from_state="filed", to_state="acceptance", probability=1.0, label="Filing Accepted", base_probability=1.0),
            StateTransition(from_state="acceptance", to_state="simplified_review", probability=simplified_prob, label="Simplified Procedure", base_probability=0.30),
            StateTransition(from_state="acceptance", to_state="phase_1", probability=normal_prob, label="Normal Review", base_probability=0.70),
            StateTransition(from_state="simplified_review", to_state="simplified_cleared", probability=1.0, label="Simplified Cleared", base_probability=1.0),
            StateTransition(from_state="phase_1", to_state="phase_1_cleared", probability=p1_clear, label="Phase 1 Cleared", base_probability=0.50),
            StateTransition(from_state="phase_1", to_state="phase_2", probability=p2_prob, label="Extended to Phase 2", base_probability=0.50),
            StateTransition(from_state="phase_2", to_state="phase_2_cleared", probability=p2_clear, label="Phase 2 Cleared", base_probability=0.60),
            StateTransition(from_state="phase_2", to_state="phase_3", probability=p3_prob, label="Extended to Phase 3", base_probability=0.40),
            StateTransition(from_state="phase_3", to_state="cleared_with_conditions", probability=0.90, label="Cleared with Conditions", base_probability=0.90),
            StateTransition(from_state="phase_3", to_state="prohibited", probability=0.10, label="Prohibited", base_probability=0.10),
        ]

    def compute_duration_distributions(
        self, comparable_stats: dict, climate: RegulatoryClimate,
    ) -> dict[str, dict]:
        # SAMR has gotten slower post-2020 — apply 1.2x multiplier by default
        slow_mult = 1.2

        return {
            "not_filed": {"p50": 0, "p75": 0, "p90": 0},
            "filed": {"p50": 5, "p75": 10, "p90": 15},
            "acceptance": {
                "p50": int(SAMR_PRE_ACCEPTANCE_TYPICAL_DAYS_RANGE[0] * slow_mult),
                "p75": int(30 * slow_mult),
                "p90": int(SAMR_PRE_ACCEPTANCE_TYPICAL_DAYS_RANGE[1] * slow_mult),
            },
            "phase_1": {
                "p50": int(SAMR_PHASE_1_DAYS * slow_mult),
                "p75": int(SAMR_PHASE_1_DAYS * slow_mult * 1.1),
                "p90": int(SAMR_PHASE_1_DAYS * slow_mult * 1.2),
            },
            "phase_1_cleared": {"p50": 0, "p75": 0, "p90": 0},
            "phase_2": {
                "p50": int(SAMR_PHASE_2_DAYS * slow_mult),
                "p75": int(SAMR_PHASE_2_DAYS * slow_mult * 1.1),
                "p90": int(SAMR_PHASE_2_DAYS * slow_mult * 1.2),
            },
            "phase_2_cleared": {"p50": 0, "p75": 0, "p90": 0},
            "phase_3": {
                "p50": int(SAMR_PHASE_3_DAYS * slow_mult),
                "p75": int(SAMR_PHASE_3_DAYS * slow_mult * 1.1),
                "p90": int(SAMR_PHASE_3_DAYS * slow_mult * 1.2),
            },
            "cleared_with_conditions": {"p50": 5, "p75": 10, "p90": 15},
            "prohibited": {"p50": 0, "p75": 0, "p90": 0},
            "simplified_review": {
                "p50": int(SAMR_SIMPLIFIED_REVIEW_DAYS * slow_mult),
                "p75": int(SAMR_SIMPLIFIED_REVIEW_DAYS * slow_mult * 1.15),
                "p90": int(SAMR_SIMPLIFIED_REVIEW_DAYS * slow_mult * 1.3),
            },
            "simplified_cleared": {"p50": 0, "p75": 0, "p90": 0},
        }
