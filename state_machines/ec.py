"""
European Commission state machine.

States:
  not_filed → pre_notification → filed → phase_1_review
    → phase_1_cleared_unconditionally
    → phase_1_cleared_with_commitments
    → phase_2_opened → phase_2_cleared_unconditionally
                     → phase_2_cleared_with_commitments
                     → phase_2_prohibited
                     → withdrawn

CRITICAL: Pre-notification period is often the LONGEST part.
"""
from models.state_machine import StateMachineState, StateTransition, JurisdictionName
from models.antitrust import OverlapAssessment
from models.climate import RegulatoryClimate
from config.constants import (
    EC_PRE_NOTIFICATION_TYPICAL_DAYS_RANGE,
    EC_PHASE_1_WORKING_DAYS,
    EC_PHASE_1_EXTENSION_WORKING_DAYS,
    EC_PHASE_2_WORKING_DAYS,
    EC_PHASE_2_EXTENSION_WORKING_DAYS,
)
from state_machines.base import BaseRegulatoryStateMachine


def _working_to_calendar(working_days: int) -> int:
    """Convert EU working days to approximate calendar days (5/7 ratio + buffer)."""
    return int(working_days * 7 / 5) + 2


class ECStateMachine(BaseRegulatoryStateMachine):

    def jurisdiction(self) -> JurisdictionName:
        return JurisdictionName.EC

    def terminal_states(self) -> set[str]:
        return {
            "phase_1_cleared_unconditionally", "phase_1_cleared_with_commitments",
            "phase_2_cleared_unconditionally", "phase_2_cleared_with_commitments",
            "phase_2_prohibited", "withdrawn",
        }

    def _clear_terminal_states(self) -> set[str]:
        return {
            "phase_1_cleared_unconditionally", "phase_1_cleared_with_commitments",
            "phase_2_cleared_unconditionally", "phase_2_cleared_with_commitments",
        }

    def define_states(self) -> list[StateMachineState]:
        return [
            StateMachineState(state_id="not_filed", state_name="Not Filed"),
            StateMachineState(state_id="pre_notification", state_name="Pre-Notification Period"),
            StateMachineState(state_id="filed", state_name="EC Filing (Form CO)"),
            StateMachineState(state_id="phase_1_review", state_name="Phase I Review"),
            StateMachineState(state_id="phase_1_cleared_unconditionally", state_name="Phase I Cleared"),
            StateMachineState(state_id="phase_1_cleared_with_commitments", state_name="Phase I Cleared with Commitments"),
            StateMachineState(state_id="phase_2_opened", state_name="Phase II In-Depth Investigation"),
            StateMachineState(state_id="phase_2_cleared_unconditionally", state_name="Phase II Cleared"),
            StateMachineState(state_id="phase_2_cleared_with_commitments", state_name="Phase II Cleared with Commitments"),
            StateMachineState(state_id="phase_2_prohibited", state_name="Phase II Prohibited"),
            StateMachineState(state_id="withdrawn", state_name="Withdrawn"),
        ]

    def define_transitions(
        self, overlap: OverlapAssessment, climate: RegulatoryClimate, comparable_stats: dict,
    ) -> list[StateTransition]:
        # Base Phase 2 probability.
        # Calibration against current MARS data (scripts/calibration_report.py)
        # shows essentially zero observed Phase 2 openings in the sample.
        # To avoid over-stating risk while still leaving a non-zero tail, the
        # fallback is reduced from 0.05 to 0.03.
        base_p2 = comparable_stats.get("ec_phase_2_rate", 0.03)

        adjustments = {}
        adj_p2 = base_p2

        overlap_adj = {"high": 0.20, "medium": 0.10, "low": 0.03, "none": 0.0}.get(
            overlap.overlap_severity, 0.0
        )
        if overlap_adj:
            adjustments["overlap_severity"] = overlap_adj
            adj_p2 += overlap_adj

        ec_regime = self._get_regime(climate, "EC")
        if ec_regime:
            mult = ec_regime.get("phase_2_multiplier", 1.0)
            if mult != 1.0:
                adjustments["enforcement_regime"] = mult - 1.0
                adj_p2 *= mult

        phase_2_prob = min(max(adj_p2, 0.01), 0.60)
        phase_1_clear_prob = (1.0 - phase_2_prob) * 0.85
        phase_1_commitments_prob = (1.0 - phase_2_prob) * 0.15

        return [
            StateTransition(from_state="not_filed", to_state="pre_notification", probability=1.0, label="Begin Pre-Notification", base_probability=1.0),
            StateTransition(from_state="pre_notification", to_state="filed", probability=1.0, label="EC Filing", base_probability=1.0),
            StateTransition(from_state="filed", to_state="phase_1_review", probability=1.0, label="Phase I Begins", base_probability=1.0),
            StateTransition(from_state="phase_1_review", to_state="phase_1_cleared_unconditionally", probability=phase_1_clear_prob, label="Phase I Cleared", base_probability=0.80, adjustments=adjustments),
            StateTransition(from_state="phase_1_review", to_state="phase_1_cleared_with_commitments", probability=phase_1_commitments_prob, label="Phase I Cleared w/ Commitments", base_probability=0.10),
            StateTransition(from_state="phase_1_review", to_state="phase_2_opened", probability=phase_2_prob, label="Phase II Opened", base_probability=base_p2, adjustments=adjustments),
            StateTransition(from_state="phase_2_opened", to_state="phase_2_cleared_unconditionally", probability=0.30, label="Phase II Cleared", base_probability=0.30),
            StateTransition(from_state="phase_2_opened", to_state="phase_2_cleared_with_commitments", probability=0.50, label="Phase II Cleared w/ Commitments", base_probability=0.50),
            StateTransition(from_state="phase_2_opened", to_state="phase_2_prohibited", probability=0.05, label="Prohibited", base_probability=0.05),
            StateTransition(from_state="phase_2_opened", to_state="withdrawn", probability=0.15, label="Withdrawn", base_probability=0.15),
        ]

    def compute_duration_distributions(
        self, comparable_stats: dict, climate: RegulatoryClimate,
    ) -> dict[str, dict]:
        pre_notif_mult = 1.0
        ec_regime = self._get_regime(climate, "EC")
        if ec_regime:
            pre_notif_mult = ec_regime.get("pre_notification_multiplier", 1.0)

        p1_cal = _working_to_calendar(EC_PHASE_1_WORKING_DAYS)
        p2_cal = _working_to_calendar(EC_PHASE_2_WORKING_DAYS)

        return {
            "not_filed": {"p50": 0, "p75": 0, "p90": 0},
            "pre_notification": {
                "p50": int(EC_PRE_NOTIFICATION_TYPICAL_DAYS_RANGE[0] * pre_notif_mult),
                "p75": int(55 * pre_notif_mult),
                "p90": int(EC_PRE_NOTIFICATION_TYPICAL_DAYS_RANGE[1] * pre_notif_mult),
            },
            "filed": {"p50": 3, "p75": 5, "p90": 7},
            "phase_1_review": {"p50": p1_cal, "p75": p1_cal + 7, "p90": _working_to_calendar(EC_PHASE_1_WORKING_DAYS + EC_PHASE_1_EXTENSION_WORKING_DAYS)},
            "phase_1_cleared_unconditionally": {"p50": 0, "p75": 0, "p90": 0},
            "phase_1_cleared_with_commitments": {"p50": 5, "p75": 10, "p90": 14},
            "phase_2_opened": {"p50": p2_cal, "p75": _working_to_calendar(105), "p90": _working_to_calendar(EC_PHASE_2_WORKING_DAYS + EC_PHASE_2_EXTENSION_WORKING_DAYS)},
            "phase_2_cleared_unconditionally": {"p50": 0, "p75": 0, "p90": 0},
            "phase_2_cleared_with_commitments": {"p50": 10, "p75": 15, "p90": 20},
            "phase_2_prohibited": {"p50": 0, "p75": 0, "p90": 0},
            "withdrawn": {"p50": 0, "p75": 0, "p90": 0},
        }

    def _get_regime(self, climate: RegulatoryClimate, jurisdiction: str) -> dict | None:
        for r in climate.regimes:
            if r.jurisdiction == jurisdiction:
                return r.multipliers
        return None
