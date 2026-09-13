"""jurisdictions_modeled: step 6 derives it from the simulation, step 7
persists it (it was hard-coded to [] before 2026-09-13). No DB."""
from datetime import date

import pytest

from models.state_machine import (
    FullSimulationResult, JurisdictionName, JurisdictionSimulation,
)
from models.timeline import DealTimingReport
from pipeline import step7_prediction_log as s7
from pipeline.step6_timeline import modeled_jurisdictions


def _sim(name: JurisdictionName, label: str = "", p50: int = 40):
    return JurisdictionSimulation(
        jurisdiction=name, jurisdiction_label=label,
        is_required=True, confidence_required=1.0,
        source_of_requirement="test", states=[], transitions=[],
        possible_paths=[], expected_duration_days_p50=p50,
        expected_duration_days_p75=p50 + 10,
        expected_duration_days_p90=p50 + 20,
    )


def test_modeled_jurisdictions_uses_labels_and_dedups():
    sim = FullSimulationResult(
        jurisdictions=[
            _sim(JurisdictionName.HSR, "HSR"),
            _sim(JurisdictionName.GENERIC, "STATE_PUC_CA"),
            _sim(JurisdictionName.GENERIC, "STATE_PUC_NY"),
            _sim(JurisdictionName.CFIUS),            # no label -> enum value
            _sim(JurisdictionName.HSR, "HSR"),       # duplicate dropped
        ],
        critical_path_jurisdiction="CFIUS",
        critical_path_duration_p50=78, critical_path_duration_p75=88,
        critical_path_duration_p90=98,
    )
    assert modeled_jurisdictions(sim) == [
        "HSR", "STATE_PUC_CA", "STATE_PUC_NY", "CFIUS",
    ]


def test_modeled_jurisdictions_empty_simulation():
    sim = FullSimulationResult(
        jurisdictions=[], critical_path_jurisdiction="",
        critical_path_duration_p50=0, critical_path_duration_p75=0,
        critical_path_duration_p90=0,
    )
    assert modeled_jurisdictions(sim) == []


@pytest.mark.asyncio
async def test_log_prediction_persists_jurisdictions_modeled(monkeypatch):
    stored = {}

    async def _fake_store(record: dict) -> str:
        stored.update(record)
        return record["prediction_id"]
    monkeypatch.setattr(s7, "store_prediction", _fake_store)

    report = DealTimingReport(
        acquirer="A", target="T", deal_value_usd=1.0,
        announcement_date=date(2026, 1, 1),
        milestones=[], scenarios=[], risk_flags=[],
        p50_close_date=date(2026, 4, 1),
        critical_path_jurisdiction="EC",
        jurisdictions_modeled=["HSR", "EC", "STATE_PUC_CA"],
    )
    pid = await s7.log_prediction(report, deal_pk=123)

    assert stored["prediction_id"] == pid == report.prediction_id
    assert stored["deal_pk"] == 123
    assert stored["jurisdictions_modeled"] == ["HSR", "EC", "STATE_PUC_CA"]
    assert stored["predicted_critical_path"] == "EC"
    assert stored["model_version"] == "0.2.0"


def test_report_default_is_empty_list():
    report = DealTimingReport(
        acquirer="A", target="T", deal_value_usd=1.0,
        announcement_date=date(2026, 1, 1),
        milestones=[], scenarios=[], risk_flags=[],
    )
    assert report.jurisdictions_modeled == []
