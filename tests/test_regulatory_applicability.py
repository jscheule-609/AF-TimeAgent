"""Population applicability is a tail possibility, not a filing obligation."""
from datetime import date
from types import SimpleNamespace

import numpy as np
import pytest

from models.antitrust import OverlapAssessment
from models.climate import RegulatoryClimate
from models.regulatory import JurisdictionRequirement
from output.scenario_builder import build_joint_scenarios
from pipeline.step5_regulatory_map import _apply_calibrated_activation, _check_cfius
from pipeline.step5_5_state_machine import simulate_regulatory_paths
from pipeline.step6_timeline import _build_milestones, _build_scenarios, modeled_jurisdictions
from scoring.distribution import build_close_distribution, sample_jurisdiction
from state_machines.base import expected_path_durations
from state_machines.cfius import CFIUSStateMachine

ANNOUNCEMENT = date(2025, 1, 1)


def requirement(name, p=1.0, required=False):
    return JurisdictionRequirement(
        jurisdiction=name, is_required=required, confidence=p, source="test",
    )


async def simulate(requirements):
    return await simulate_regulatory_paths(
        requirements, OverlapAssessment(overlap_type="none", overlap_severity="none"),
        [], SimpleNamespace(
            announcement_date=ANNOUNCEMENT, acquirer_ticker="A", target_ticker="T",
        ), None, climate=RegulatoryClimate(),
    )


@pytest.mark.parametrize("rate, present", [(0.0325, True), (0.03, True), (0.02, False), (None, False)])
def test_calibrated_activation_respects_floor(monkeypatch, rate, present):
    monkeypatch.setattr("config.calibration.get_rate", lambda key: rate if key == "cfius" else 0.0074)
    requirements = {}
    _apply_calibrated_activation(requirements)
    assert ("CFIUS" in requirements) == present
    assert "SAMR" not in requirements
    if present:
        cfius = requirements["CFIUS"]
        assert not cfius.is_required
        assert cfius.confidence == cfius.applicability == rate
        assert cfius.source == "calibrated_activation_rate"


def test_calibrated_activation_preserves_existing_requirement(monkeypatch):
    monkeypatch.setattr("config.calibration.get_rate", lambda key: 0.0325)
    existing = requirement("CFIUS", 0.4, required=True)
    requirements = {"CFIUS": existing}
    _apply_calibrated_activation(requirements)
    assert requirements["CFIUS"] is existing
    assert existing.applicability == 1.0


@pytest.mark.asyncio
async def test_rare_cfius_is_tail_not_critical_path(caplog):
    caplog.set_level("INFO", logger="pipeline.step5_5_state_machine")
    result = await simulate([requirement("HSR", required=True), requirement("CFIUS", 0.0325)])
    cfius = result.jurisdictions[1]
    assert cfius.applicability == 0.0325
    assert 2 <= cfius.expected_duration_days_p50 <= 3
    assert sum(p.path_probability for p in cfius.possible_paths) == pytest.approx(1)
    absent = cfius.possible_paths[0]
    assert absent.path_label == "not applicable"
    assert absent.states == [] and absent.is_terminal_clear
    assert absent.path_probability == pytest.approx(0.9675)
    assert (absent.total_duration_days_p50, absent.total_duration_days_p75,
            absent.total_duration_days_p90) == (0, 0, 0)
    assert (cfius.expected_duration_days_p50, cfius.expected_duration_days_p75,
            cfius.expected_duration_days_p90) == expected_path_durations(cfius.possible_paths)
    assert result.critical_path_jurisdiction == "HSR"
    assert modeled_jurisdictions(result) == ["HSR", "CFIUS"]
    messages = [r.message for r in caplog.records if "applicability" in r.message]
    assert len(messages) == 1
    assert "A/T" in messages[0] and "CFIUS" in messages[0] and "0.0325" in messages[0]


@pytest.mark.asyncio
async def test_required_cfius_is_unchanged():
    result = await simulate([requirement("HSR", required=True), requirement("CFIUS", 0.4, required=True)])
    cfius = result.jurisdictions[1]
    original = CFIUSStateMachine().simulate(
        ANNOUNCEMENT, OverlapAssessment(overlap_type="none", overlap_severity="none"),
        RegulatoryClimate(), {},
    )
    assert cfius.applicability == 1.0
    assert cfius.possible_paths == original.possible_paths
    assert (cfius.expected_duration_days_p50, cfius.expected_duration_days_p75,
            cfius.expected_duration_days_p90) == (78, 88, 98)
    assert result.critical_path_jurisdiction == "CFIUS"


@pytest.mark.asyncio
async def test_keyword_cfius_uses_the_same_applicability():
    requirements = {}
    target = SimpleNamespace(business_description="semiconductor", products_and_services="chips")
    _check_cfius(None, target, requirements)
    assert requirements["CFIUS"].applicability == 0.4
    result = await simulate(list(requirements.values()))
    cfius = result.jurisdictions[0]
    assert cfius.applicability == 0.4
    assert cfius.possible_paths[0].path_probability == pytest.approx(0.6)


@pytest.mark.asyncio
@pytest.mark.parametrize("p, included", [(0.0325, False), (0.4, False), (0.5, True), (1.0, True)])
async def test_milestones_follow_applicability(p, included):
    result = await simulate([requirement("HSR", required=True), requirement("CFIUS", p, required=p == 1)])
    rows = _build_milestones(result, None, ANNOUNCEMENT)
    names = {r.milestone for r in rows}
    assert "HSR Filing" in names and "HSR Clearance" in names
    assert ("CFIUS Filing" in names) == included
    assert ("CFIUS Clearance" in names) == included


@pytest.mark.asyncio
async def test_not_applicable_only_used_in_joint_clean_scenarios():
    result = await simulate([requirement("HSR", required=True), requirement("CFIUS", 0.0325)])
    cfius = result.jurisdictions[1]
    for builder in (_build_scenarios, build_joint_scenarios):
        scenarios = builder(result, ANNOUNCEMENT)
        assert not any("not applicable" in s.scenario_name.lower() for s in scenarios)
        clean = next(s for s in scenarios if s.scenario_name.startswith("Clean"))
        assert clean.duration_days < 57  # absent CFIUS imposes no initial-review floor
    clean = next(s for s in _build_scenarios(result, ANNOUNCEMENT) if s.scenario_name.startswith("Clean"))
    assert clean.jurisdiction_paths["CFIUS"] == cfius.possible_paths[0].path_id


@pytest.mark.asyncio
async def test_critical_path_threshold_and_fallbacks():
    result = await simulate([requirement("HSR", 0.01), requirement("CFIUS", 0.4)])
    assert result.critical_path_jurisdiction == "HSR"
    result = await simulate([requirement("EC", 0.01), requirement("CFIUS", 0.4)])
    assert result.critical_path_jurisdiction == "CFIUS"
    result = await simulate([requirement("HSR", 0.01), requirement("CFIUS", 0.5)])
    assert result.critical_path_jurisdiction == "CFIUS"


@pytest.mark.asyncio
async def test_rare_cfius_preserves_regulatory_median_and_adds_tail():
    result = await simulate([requirement("HSR", required=True), requirement("CFIUS", 0.0325)])
    n = 200_000
    rng = np.random.default_rng(7)
    hsr = sample_jurisdiction(result.jurisdictions[0], n, rng)
    cfius = sample_jurisdiction(result.jurisdictions[1], n, rng)
    combined = np.maximum(hsr, cfius)
    assert np.isfinite(cfius).all()
    assert (cfius == 0).mean() == pytest.approx(0.9675, abs=0.005)
    assert abs(np.median(combined) - np.median(hsr)) < 1.0
    assert 0.01 < (combined >= 60).mean() - (hsr >= 60).mean() < 0.045
    baseline = result.model_copy(update={"jurisdictions": result.jurisdictions[:1]})
    options = dict(is_tender=True, dma_close_gap=0, n=n, seed=7)
    before = build_close_distribution(simulation=baseline, **options)
    after = build_close_distribution(simulation=result, **options)
    assert abs(before["p50"] - after["p50"]) < 1.0
    assert after["p50"] <= after["p75"] <= after["p90"]


@pytest.mark.asyncio
async def test_zero_applicability_samples_exactly_zero():
    result = await simulate([requirement("CFIUS", 0.0)])
    samples = sample_jurisdiction(result.jurisdictions[0], 1000, np.random.default_rng(7))
    assert np.all(samples == 0)
