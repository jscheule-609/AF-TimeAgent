"""
US Hart-Scott-Rodino state machine.

States:
  not_filed → filed → waiting_period
    → early_termination → cleared
    → waiting_period_expired → cleared
    → second_request_issued → compliance_period
      → substantial_compliance → extended_review
        → cleared_with_conditions
        → cleared_unconditionally
        → consent_decree
        → litigation → deal_blocked | settlement_and_clear
      → pull_and_refile → filed (restart clock)
"""
from models.state_machine import (
    StateMachineState, StateTransition, JurisdictionName, StateStatus,
)
from models.antitrust import OverlapAssessment
from models.climate import RegulatoryClimate
from config.constants import (
    HSR_INITIAL_WAITING_PERIOD_DAYS,
    HSR_CASH_TENDER_WAITING_PERIOD_DAYS,
    HSR_TYPICAL_SECOND_REQUEST_COMPLIANCE_DAYS_RANGE,
    HSR_EARLY_TERMINATION_TYPICAL_DAYS,
)
from state_machines.base import BaseRegulatoryStateMachine


class HSRStateMachine(BaseRegulatoryStateMachine):

    def jurisdiction(self) -> JurisdictionName:
        return JurisdictionName.HSR

    def terminal_states(self) -> set[str]:
        return {
            "cleared", "cleared_with_conditions", "cleared_unconditionally",
            "consent_decree", "deal_blocked", "settlement_and_clear",
        }

    def _clear_terminal_states(self) -> set[str]:
        return {
            "cleared", "cleared_with_conditions", "cleared_unconditionally",
            "consent_decree", "settlement_and_clear",
        }

    def define_states(self) -> list[StateMachineState]:
        return [
            StateMachineState(state_id="not_filed", state_name="Not Filed"),
            StateMachineState(state_id="filed", state_name="HSR Filed"),
            StateMachineState(
                state_id="waiting_period",
                state_name="Initial Waiting Period",
                expected_duration_days=HSR_INITIAL_WAITING_PERIOD_DAYS,
            ),
            StateMachineState(
                state_id="early_termination",
                state_name="Early Termination Granted",
            ),
            StateMachineState(state_id="cleared", state_name="HSR Cleared (Waiting Period Expired)"),
            StateMachineState(
                state_id="second_request_issued",
                state_name="Second Request Issued",
            ),
            StateMachineState(
                state_id="compliance_period",
                state_name="Second Request Compliance Period",
            ),
            StateMachineState(
                state_id="extended_review",
                state_name="Extended Review (Post-Compliance)",
            ),
            StateMachineState(state_id="cleared_unconditionally", state_name="Cleared Unconditionally"),
            StateMachineState(state_id="cleared_with_conditions", state_name="Cleared with Conditions"),
            StateMachineState(state_id="consent_decree", state_name="Consent Decree"),
            StateMachineState(state_id="litigation", state_name="Antitrust Litigation"),
            StateMachineState(state_id="deal_blocked", state_name="Deal Blocked"),
            StateMachineState(state_id="settlement_and_clear", state_name="Settlement & Clear"),
        ]

    def define_transitions(
        self,
        overlap: OverlapAssessment,
        climate: RegulatoryClimate,
        comparable_stats: dict,
    ) -> list[StateTransition]:
        # Base second request probability
        base_2r = comparable_stats.get("second_request_rate", 0.03)

        # Adjustments
        adjustments = {}
        adj_2r = base_2r

        # Overlap severity adjustment
        overlap_adj = {
            "high": 0.25, "medium": 0.12, "low": 0.05, "none": 0.0
        }.get(overlap.overlap_severity, 0.0)
        if overlap_adj:
            adjustments["overlap_severity"] = overlap_adj
            adj_2r += overlap_adj

        # Enforcement regime
        hsr_regime = self._get_regime(climate, "HSR")
        regime_mult = hsr_regime.get("second_request_multiplier", 1.0) if hsr_regime else 1.0
        if regime_mult != 1.0:
            adjustments["enforcement_regime"] = regime_mult - 1.0
            adj_2r *= regime_mult

        # Cap probability
        second_request_prob = min(max(adj_2r, 0.01), 0.80)
        early_term_prob = max(0.05, 0.40 - second_request_prob)
        expire_prob = 1.0 - second_request_prob - early_term_prob

        return [
            StateTransition(
                from_state="not_filed", to_state="filed",
                probability=1.0, label="HSR Filing",
                base_probability=1.0,
            ),
            StateTransition(
                from_state="filed", to_state="waiting_period",
                probability=1.0, label="Waiting Period Begins",
                base_probability=1.0,
            ),
            StateTransition(
                from_state="waiting_period", to_state="early_termination",
                probability=early_term_prob,
                label="Early Termination Granted",
                base_probability=0.40,
                adjustments=adjustments,
            ),
            StateTransition(
                from_state="early_termination", to_state="cleared",
                probability=1.0, label="Cleared via ET",
                base_probability=1.0,
            ),
            StateTransition(
                from_state="waiting_period", to_state="cleared",
                probability=expire_prob,
                label="Waiting Period Expired — Cleared",
                base_probability=0.57,
            ),
            StateTransition(
                from_state="waiting_period", to_state="second_request_issued",
                probability=second_request_prob,
                label="Second Request Issued",
                base_probability=base_2r,
                adjustments=adjustments,
            ),
            StateTransition(
                from_state="second_request_issued", to_state="compliance_period",
                probability=0.95, label="Enter Compliance Period",
                base_probability=0.95,
            ),
            StateTransition(
                from_state="second_request_issued", to_state="filed",
                probability=0.05, label="Pull and Refile",
                base_probability=0.05,
            ),
            StateTransition(
                from_state="compliance_period", to_state="extended_review",
                probability=1.0, label="Substantial Compliance",
                base_probability=1.0,
            ),
            StateTransition(
                from_state="extended_review", to_state="cleared_unconditionally",
                probability=0.35, label="Cleared Unconditionally",
                base_probability=0.35,
            ),
            StateTransition(
                from_state="extended_review", to_state="cleared_with_conditions",
                probability=0.30, label="Cleared with Conditions",
                base_probability=0.30,
            ),
            StateTransition(
                from_state="extended_review", to_state="consent_decree",
                probability=0.15, label="Consent Decree",
                base_probability=0.15,
            ),
            StateTransition(
                from_state="extended_review", to_state="litigation",
                probability=0.20, label="FTC/DOJ Sues to Block",
                base_probability=0.20,
            ),
            StateTransition(
                from_state="litigation", to_state="deal_blocked",
                probability=0.40, label="Deal Blocked",
                base_probability=0.40,
            ),
            StateTransition(
                from_state="litigation", to_state="settlement_and_clear",
                probability=0.60, label="Settlement Reached",
                base_probability=0.60,
            ),
        ]

    def compute_duration_distributions(
        self, comparable_stats: dict, climate: RegulatoryClimate,
    ) -> dict[str, dict]:
        regime_mult = 1.0
        hsr_regime = self._get_regime(climate, "HSR")
        if hsr_regime:
            regime_mult = hsr_regime.get("second_request_multiplier", 1.0)

        comp_median = comparable_stats.get("hsr_median_days_to_clear", 35)

        return {
            "not_filed": {"p50": 0, "p75": 0, "p90": 0},
            "filed": {"p50": 5, "p75": 7, "p90": 10},
            "waiting_period": {"p50": HSR_INITIAL_WAITING_PERIOD_DAYS, "p75": 30, "p90": 30},
            "early_termination": {
                "p50": HSR_EARLY_TERMINATION_TYPICAL_DAYS[0],
                "p75": HSR_EARLY_TERMINATION_TYPICAL_DAYS[1],
                "p90": 25,
            },
            "cleared": {"p50": 0, "p75": 0, "p90": 0},
            "second_request_issued": {"p50": 5, "p75": 10, "p90": 15},
            "compliance_period": {
                "p50": int(HSR_TYPICAL_SECOND_REQUEST_COMPLIANCE_DAYS_RANGE[0] * regime_mult),
                "p75": int(135 * regime_mult),
                "p90": int(HSR_TYPICAL_SECOND_REQUEST_COMPLIANCE_DAYS_RANGE[1] * regime_mult),
            },
            "extended_review": {
                "p50": int(45 * regime_mult),
                "p75": int(75 * regime_mult),
                "p90": int(120 * regime_mult),
            },
            "cleared_unconditionally": {"p50": 0, "p75": 0, "p90": 0},
            "cleared_with_conditions": {"p50": 5, "p75": 10, "p90": 15},
            "consent_decree": {"p50": 15, "p75": 30, "p90": 45},
            "litigation": {"p50": 120, "p75": 180, "p90": 270},
            "deal_blocked": {"p50": 0, "p75": 0, "p90": 0},
            "settlement_and_clear": {"p50": 30, "p75": 60, "p90": 90},
        }

    def _get_regime(self, climate: RegulatoryClimate, jurisdiction: str) -> dict | None:
        for r in climate.regimes:
            if r.jurisdiction == jurisdiction:
                return r.multipliers
        return None
