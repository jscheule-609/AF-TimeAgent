"""API response shaping and the TIMEAGENT_LLM_ENABLED switch (no DB, no network)."""
from datetime import date

import pytest

from models.timeline import DealTimingReport, GuidanceReconciliation, RiskFlag
from models.prediction import PredictionRecord


def _report(**over) -> DealTimingReport:
    base = dict(
        acquirer="Copart, Inc.",
        target="ACV Auctions Inc.",
        deal_value_usd=1.0e9,
        announcement_date=date(2026, 9, 10),
        milestones=[],
        scenarios=[],
        risk_flags=[
            RiskFlag(flag="Model 34d faster than guidance", severity="low",
                     jurisdiction=None, detail="simplified filing tracks."),
        ],
        p50_close_date=date(2026, 12, 19),
        p75_close_date=date(2027, 2, 27),
        p90_close_date=date(2027, 5, 18),
        critical_path_jurisdiction="CFIUS",
        guidance_reconciliation=GuidanceReconciliation(flag="opportunity"),
        prediction_id="1d66d500-5ab2-4a17-8801-2de026fc32df",
    )
    base.update(over)
    return DealTimingReport(**base)


def test_report_to_result_accepts_riskflag_objects():
    # Regression: PredictionResult.risk_flags was list[str]; any report with
    # >=1 RiskFlag failed validation -> HTTP 500 after the row was written.
    from api.server import _report_to_result

    res = _report_to_result(_report(), 2.46, 93516)
    assert res.deal_pk == 93516
    assert res.target == "ACV Auctions Inc." and res.acquirer == "Copart, Inc."
    assert res.p50_date == "2026-12-19" and res.p90_date == "2027-05-18"
    assert res.critical_path == "CFIUS"
    assert res.model_version == PredictionRecord.model_fields["model_version"].default == "0.2.0"
    assert res.prediction_id == "1d66d500-5ab2-4a17-8801-2de026fc32df"
    assert res.guidance_flag == "opportunity"
    assert res.risk_flags == [{
        "flag": "Model 34d faster than guidance", "severity": "low",
        "jurisdiction": None, "detail": "simplified filing tracks.",
    }]
    assert res.elapsed_seconds == 2.5
    res.model_dump_json()  # serialisable end to end


def test_report_to_result_with_no_flags_or_guidance():
    from api.server import _report_to_result

    res = _report_to_result(_report(risk_flags=[], guidance_reconciliation=None,
                                    prediction_id=None), 0.0, 1)
    assert res.risk_flags == [] and res.guidance_flag is None and res.prediction_id is None


@pytest.mark.asyncio
async def test_call_llm_short_circuits_when_disabled(monkeypatch):
    import httpx
    from parsers import llm_extraction as le

    def _no_network(*a, **k):  # pragma: no cover - fails the test if reached
        raise AssertionError("httpx must not be used when the LLM leg is off")
    monkeypatch.setattr(httpx, "AsyncClient", _no_network)

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setenv("TIMEAGENT_LLM_ENABLED", "0")
    assert le.llm_available() is False
    with pytest.raises(le.LLMDisabled, match="TIMEAGENT_LLM_ENABLED=0"):
        await le.call_llm("x")

    monkeypatch.setenv("TIMEAGENT_LLM_ENABLED", "1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    assert le.llm_available() is False
    with pytest.raises(le.LLMDisabled, match="OPENROUTER_API_KEY"):
        await le.call_llm("x")

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    assert le.llm_available() is True


@pytest.mark.asyncio
async def test_ingestion_skips_edgar_when_llm_disabled(monkeypatch):
    # With the switch off, step 1/2 must return before importing sec_api_tools
    # (i.e. before any EDGAR download), not after a failed LLM call.
    import sys
    from models.deal import DealParameters
    from pipeline import step1_press_release as s1
    from pipeline import step2_document_ingestion as s2

    monkeypatch.setenv("TIMEAGENT_LLM_ENABLED", "0")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setitem(sys.modules, "sec_api_tools", None)  # import -> ImportError

    params = DealParameters(
        acquirer_ticker="CPRT", target_ticker="ACVA",
        acquirer_name="Copart", target_name="ACV",
        acquirer_cik="900075", target_cik="1637873",
        announcement_date=date(2026, 9, 10), deal_value_usd=1.0e9,
        deal_structure="cash", buyer_type="strategic",
        sector="Industrials", industry="Auto auctions",
    )
    pr = await s1.parse_deal_press_release(params)
    assert pr.announcement_date == date(2026, 9, 10) and not pr.mentioned_jurisdictions
    assert await s2._ingest_tenk("900075", "CPRT", "Copart") is None
    assert await s2._ingest_merger_agreement(params) is None
