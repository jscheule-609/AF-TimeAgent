"""step 7 must report the prediction_id the UPSERT actually kept (no DB)."""
from datetime import date

import pytest

from models.timeline import DealTimingReport
from pipeline import step7_prediction_log as s7


def _report() -> DealTimingReport:
    return DealTimingReport(
        acquirer="A", target="T", deal_value_usd=1.0,
        announcement_date=date(2026, 1, 1),
        milestones=[], scenarios=[], risk_flags=[],
    )


@pytest.mark.asyncio
async def test_log_prediction_uses_prediction_id_returned_by_store(monkeypatch):
    # store_prediction keeps the existing prediction_id on UPSERT conflict and
    # RETURNs it; log_prediction must report that id, not its fresh uuid. The
    # 2026-09-13 LLM-off backtest re-predicted 120 already-stored deals and
    # update_prediction_actuals(<fresh uuid>) matched zero rows.
    async def _fake_store(record: dict) -> str:
        return "existing-row-id"
    monkeypatch.setattr(s7, "store_prediction", _fake_store)

    report = _report()
    pid = await s7.log_prediction(report, deal_pk=7)
    assert pid == "existing-row-id"
    assert report.prediction_id == "existing-row-id"


@pytest.mark.asyncio
async def test_log_prediction_falls_back_to_generated_id(monkeypatch):
    seen = {}

    async def _fake_store(record: dict):
        seen["pid"] = record["prediction_id"]
        return None  # defensive: a driver returning nothing
    monkeypatch.setattr(s7, "store_prediction", _fake_store)

    report = _report()
    pid = await s7.log_prediction(report, deal_pk=8)
    assert pid == seen["pid"] == report.prediction_id


@pytest.mark.asyncio
async def test_log_prediction_store_failure_is_non_fatal(monkeypatch):
    async def _boom(record: dict):
        raise RuntimeError("db down")
    monkeypatch.setattr(s7, "store_prediction", _boom)

    report = _report()
    pid = await s7.log_prediction(report, deal_pk=9)
    assert pid and report.prediction_id is None
