"""Scenario path construction utilities."""
from models.timeline import ScenarioPath
from models.state_machine import FullSimulationResult
from datetime import date, timedelta


def build_joint_scenarios(
    simulation: FullSimulationResult,
    announcement_date: date,
    max_scenarios: int = 6,
) -> list[ScenarioPath]:
    """
    Build joint scenario paths across all jurisdictions.
    Combines per-jurisdiction paths into overall deal scenarios.
    """
    if not simulation.jurisdictions:
        return []

    scenarios = []

    # Clean scenario — all clear at first opportunity
    clean_prob = 1.0
    clean_max_duration = 0
    for jur_sim in simulation.jurisdictions:
        best_clear = None
        for path in jur_sim.possible_paths:
            if path.is_terminal_clear:
                if best_clear is None or path.total_duration_days_p50 < best_clear.total_duration_days_p50:
                    best_clear = path
        if best_clear:
            clean_prob *= best_clear.path_probability
            clean_max_duration = max(clean_max_duration, best_clear.total_duration_days_p50)

    if clean_prob > 0.01:
        scenarios.append(ScenarioPath(
            scenario_name="Clean — All Phase 1 Clears",
            probability_pct=round(clean_prob * 100, 1),
            expected_close_date=announcement_date + timedelta(days=clean_max_duration),
            duration_days=clean_max_duration,
            description="All jurisdictions clear at initial review",
        ))

    # Per-jurisdiction extended scenarios
    for jur_sim in simulation.jurisdictions:
        for path in jur_sim.possible_paths:
            if path.is_terminal_clear and path.path_probability > 0.05:
                if "Phase 2" in path.path_label or "Second Request" in path.path_label or "Extended" in path.path_label:
                    scenarios.append(ScenarioPath(
                        scenario_name=f"{jur_sim.jurisdiction.value}: {path.path_label}",
                        probability_pct=round(path.path_probability * 100, 1),
                        expected_close_date=announcement_date + timedelta(days=path.total_duration_days_p75),
                        duration_days=path.total_duration_days_p75,
                        description=f"{jur_sim.jurisdiction.value} enters extended review",
                        jurisdiction_paths={jur_sim.jurisdiction.value: path.path_id},
                    ))

    # Break scenario
    break_prob = 0.0
    for jur_sim in simulation.jurisdictions:
        for path in jur_sim.possible_paths:
            if not path.is_terminal_clear:
                break_prob = max(break_prob, path.path_probability)

    if break_prob > 0.01:
        scenarios.append(ScenarioPath(
            scenario_name="Deal Terminates",
            probability_pct=round(break_prob * 100, 1),
            duration_days=0,
            description="Regulatory block or parties walk away",
        ))

    # Sort and limit
    scenarios.sort(key=lambda s: s.probability_pct, reverse=True)
    return scenarios[:max_scenarios]
