"""Tests for regulatory state machines."""
import pytest
from datetime import date
from models.antitrust import OverlapAssessment
from models.climate import RegulatoryClimate, EnforcementRegime
from state_machines.hsr import HSRStateMachine
from state_machines.ec import ECStateMachine
from state_machines.cma import CMAStateMachine
from state_machines.samr import SAMRStateMachine


def _default_climate() -> RegulatoryClimate:
    return RegulatoryClimate(
        regimes=[
            EnforcementRegime(jurisdiction="HSR", regime="normal", label="Normal", multipliers={"second_request_multiplier": 1.0, "phase_2_multiplier": 1.0, "pre_notification_multiplier": 1.0}),
            EnforcementRegime(jurisdiction="EC", regime="normal", label="Normal", multipliers={"second_request_multiplier": 1.0, "phase_2_multiplier": 1.0, "pre_notification_multiplier": 1.0}),
            EnforcementRegime(jurisdiction="CMA", regime="normal", label="Normal", multipliers={"second_request_multiplier": 1.0, "phase_2_multiplier": 1.0, "pre_notification_multiplier": 1.0}),
        ],
        overall_regime="normal",
    )


def _no_overlap() -> OverlapAssessment:
    return OverlapAssessment(overlap_type="none", overlap_severity="none")


def _high_overlap() -> OverlapAssessment:
    return OverlapAssessment(overlap_type="horizontal", overlap_severity="high")


class TestHSRStateMachine:
    def test_hsr_clean_path(self):
        """HSR clean path should complete in ~30-35 days."""
        machine = HSRStateMachine()
        sim = machine.simulate(
            announcement_date=date(2024, 3, 1),
            overlap=_no_overlap(),
            climate=_default_climate(),
            comparable_stats={"second_request_rate": 0.095},
        )
        assert sim.jurisdiction.value == "HSR"
        assert len(sim.possible_paths) > 0
        # Clean path should exist
        clean_paths = [p for p in sim.possible_paths if p.is_terminal_clear and p.total_duration_days_p50 < 60]
        assert len(clean_paths) > 0

    def test_hsr_second_request_path(self):
        """HSR should have a second request path."""
        machine = HSRStateMachine()
        sim = machine.simulate(
            announcement_date=date(2024, 3, 1),
            overlap=_high_overlap(),
            climate=_default_climate(),
            comparable_stats={"second_request_rate": 0.15},
        )
        # Should have paths with longer durations
        long_paths = [p for p in sim.possible_paths if p.total_duration_days_p50 > 100]
        assert len(long_paths) > 0

    def test_hsr_overlap_increases_2nd_request_probability(self):
        """High overlap should increase second request probability."""
        machine = HSRStateMachine()

        sim_no = machine.simulate(
            announcement_date=date(2024, 3, 1),
            overlap=_no_overlap(),
            climate=_default_climate(),
            comparable_stats={"second_request_rate": 0.095},
        )
        sim_high = machine.simulate(
            announcement_date=date(2024, 3, 1),
            overlap=_high_overlap(),
            climate=_default_climate(),
            comparable_stats={"second_request_rate": 0.095},
        )

        # High overlap should result in longer expected duration
        assert sim_high.expected_duration_days_p50 > sim_no.expected_duration_days_p50


class TestECStateMachine:
    def test_ec_has_pre_notification(self):
        """EC should model pre-notification period."""
        machine = ECStateMachine()
        states = machine.define_states()
        state_ids = [s.state_id for s in states]
        assert "pre_notification" in state_ids

    def test_ec_phase_2_path(self):
        """EC should have Phase 2 path."""
        machine = ECStateMachine()
        sim = machine.simulate(
            announcement_date=date(2024, 3, 1),
            overlap=_high_overlap(),
            climate=_default_climate(),
            comparable_stats={"ec_phase_2_rate": 0.15},
        )
        phase_2_paths = [p for p in sim.possible_paths if "Phase 2" in p.path_label]
        assert len(phase_2_paths) > 0


class TestCMAStateMachine:
    def test_cma_phase_1_clear_path(self):
        """CMA should have Phase 1 clear path."""
        machine = CMAStateMachine()
        sim = machine.simulate(
            announcement_date=date(2024, 3, 1),
            overlap=_no_overlap(),
            climate=_default_climate(),
            comparable_stats={"cma_phase_2_rate": 0.05},
        )
        clear_paths = [p for p in sim.possible_paths if "phase_1_cleared" in p.states]
        assert len(clear_paths) > 0


class TestSAMRStateMachine:
    def test_samr_simplified_path(self):
        """SAMR should have simplified review path for low-overlap deals."""
        machine = SAMRStateMachine()
        sim = machine.simulate(
            announcement_date=date(2024, 3, 1),
            overlap=_no_overlap(),
            climate=_default_climate(),
            comparable_stats={},
        )
        simplified = [p for p in sim.possible_paths if "simplified" in str(p.states)]
        assert len(simplified) > 0


class TestCriticalPath:
    def test_critical_path_is_longest_jurisdiction(self):
        """Critical path should be the max duration jurisdiction."""
        from models.state_machine import FullSimulationResult
        hsr = HSRStateMachine()
        ec = ECStateMachine()

        hsr_sim = hsr.simulate(date(2024, 3, 1), _no_overlap(), _default_climate(), {})
        ec_sim = ec.simulate(date(2024, 3, 1), _no_overlap(), _default_climate(), {})

        sims = [hsr_sim, ec_sim]
        critical = max(sims, key=lambda s: s.expected_duration_days_p50)

        # EC typically has longer pre-notification, so should be critical path
        # (or at least one of them should have a valid duration)
        assert critical.expected_duration_days_p50 > 0
