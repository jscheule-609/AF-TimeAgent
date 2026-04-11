"""
UK CMA state machine.

States:
  not_filed → pre_notification → phase_1
    → phase_1_cleared
    → phase_1_referred_to_phase_2 → phase_2
      → cleared → cleared_with_remedies → prohibited → abandoned
"""
from models.state_machine import StateMachineState, StateTransition, JurisdictionName
from models.antitrust import OverlapAssessment
from models.climate import RegulatoryClimate
from config.constants import (
    CMA_PRE_NOTIFICATION_TYPICAL_DAYS_RANGE,
    CMA_PHASE_1_STATUTORY_DAYS,
    CMA_PHASE_2_STATUTORY_WEEKS,
    CMA_PHASE_2_EXTENSION_WEEKS,
)
from state_machines.base import BaseRegulatoryStateMachine


class CMAStateMachine(BaseRegulatoryStateMachine):

    def jurisdiction(self) -> JurisdictionName:
        return JurisdictionName.CMA

    def terminal_states(self) -> set[str]:
        return {"phase_1_cleared", "phase_2_cleared", "cleared_with_remedies", "prohibited", "abandoned"}

    def _clear_terminal_states(self) -> set[str]:
        return {"phase_1_cleared", "phase_2_cleared", "cleared_with_remedies"}

    def define_states(self) -> list[StateMachineState]:
        return [
            StateMachineState(state_id="not_filed", state_name="Not Filed"),
            StateMachineState(state_id="pre_notification", state_name="Pre-Notification"),
            StateMachineState(state_id="phase_1", state_name="Phase 1 Review"),
            StateMachineState(state_id="phase_1_cleared", state_name="Phase 1 Cleared"),
            StateMachineState(state_id="phase_2", state_name="Phase 2 In-Depth"),
            StateMachineState(state_id="phase_2_cleared", state_name="Phase 2 Cleared"),
            StateMachineState(state_id="cleared_with_remedies", state_name="Cleared with Remedies"),
            StateMachineState(state_id="prohibited", state_name="Prohibited"),
            StateMachineState(state_id="abandoned", state_name="Abandoned"),
        ]

    def define_transitions(
        self, overlap: OverlapAssessment, climate: RegulatoryClimate, comparable_stats: dict,
    ) -> list[StateTransition]:
        # Base Phase 2 referral probability.
        # Current MARS data shows no Phase 2 outcomes in the calibration
        # sample; the fallback is therefore reduced from 0.10 to 0.05 to
        # better reflect observed UK CMA behavior while preserving a
        # conservative non-zero rate.
        base_p2 = comparable_stats.get("cma_phase_2_rate", 0.05)
        adj_p2 = base_p2
        adjustments = {}

        overlap_adj = {"high": 0.20, "medium": 0.10, "low": 0.03, "none": 0.0}.get(overlap.overlap_severity, 0.0)
        if overlap_adj:
            adjustments["overlap_severity"] = overlap_adj
            adj_p2 += overlap_adj

        phase_2_prob = min(max(adj_p2, 0.02), 0.60)
        phase_1_clear_prob = 1.0 - phase_2_prob

        return [
            StateTransition(from_state="not_filed", to_state="pre_notification", probability=1.0, label="Begin Pre-Notification", base_probability=1.0),
            StateTransition(from_state="pre_notification", to_state="phase_1", probability=1.0, label="Phase 1 Begins", base_probability=1.0),
            StateTransition(from_state="phase_1", to_state="phase_1_cleared", probability=phase_1_clear_prob, label="Phase 1 Cleared", base_probability=0.90, adjustments=adjustments),
            StateTransition(from_state="phase_1", to_state="phase_2", probability=phase_2_prob, label="Referred to Phase 2", base_probability=base_p2, adjustments=adjustments),
            StateTransition(from_state="phase_2", to_state="phase_2_cleared", probability=0.25, label="Phase 2 Cleared", base_probability=0.25),
            StateTransition(from_state="phase_2", to_state="cleared_with_remedies", probability=0.40, label="Cleared with Remedies", base_probability=0.40),
            StateTransition(from_state="phase_2", to_state="prohibited", probability=0.20, label="Prohibited", base_probability=0.20),
            StateTransition(from_state="phase_2", to_state="abandoned", probability=0.15, label="Abandoned", base_probability=0.15),
        ]

    def compute_duration_distributions(
        self, comparable_stats: dict, climate: RegulatoryClimate,
    ) -> dict[str, dict]:
        # CMA Phase 1 uses working days, Phase 2 uses weeks
        p1_cal = int(CMA_PHASE_1_STATUTORY_DAYS * 7 / 5) + 2
        p2_cal = CMA_PHASE_2_STATUTORY_WEEKS * 7
        p2_ext = (CMA_PHASE_2_STATUTORY_WEEKS + CMA_PHASE_2_EXTENSION_WEEKS) * 7

        return {
            "not_filed": {"p50": 0, "p75": 0, "p90": 0},
            "pre_notification": {
                "p50": CMA_PRE_NOTIFICATION_TYPICAL_DAYS_RANGE[0],
                "p75": 40,
                "p90": CMA_PRE_NOTIFICATION_TYPICAL_DAYS_RANGE[1],
            },
            "phase_1": {"p50": p1_cal, "p75": p1_cal + 5, "p90": p1_cal + 10},
            "phase_1_cleared": {"p50": 0, "p75": 0, "p90": 0},
            "phase_2": {"p50": p2_cal, "p75": int(p2_cal * 1.15), "p90": p2_ext},
            "phase_2_cleared": {"p50": 0, "p75": 0, "p90": 0},
            "cleared_with_remedies": {"p50": 10, "p75": 15, "p90": 20},
            "prohibited": {"p50": 0, "p75": 0, "p90": 0},
            "abandoned": {"p50": 0, "p75": 0, "p90": 0},
        }
