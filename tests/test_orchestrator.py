"""Integration test for the full pipeline orchestrator."""
import pytest
from datetime import date
from models.deal import DealInput


@pytest.mark.integration
@pytest.mark.requires_network
@pytest.mark.asyncio
async def test_full_pipeline_avgo_vmw():
    """
    Run full pipeline on AVGO/VMW deal and check output structure.

    This test requires:
    - MARS database access
    - AF-SECAPI access
    - OpenRouter API key

    Skip with: pytest -m "not integration"
    """
    from pipeline.orchestrator import run_timing_estimation

    deal_input = DealInput(
        acquirer_ticker="AVGO",
        target_ticker="VMW",
        deal_value_usd=69_000_000_000,
        announcement_date=date(2022, 5, 26),
    )

    report = await run_timing_estimation(deal_input)

    # Check output structure
    assert report.acquirer
    assert report.target
    assert report.deal_value_usd > 0
    assert report.announcement_date == date(2022, 5, 26)
    assert len(report.milestones) > 0
    assert len(report.scenarios) > 0
    assert report.p50_close_date is not None
    assert report.critical_path_jurisdiction != ""
    assert report.generated_at is not None

    # Scenarios should sum to ~100%
    total_prob = sum(s.probability_pct for s in report.scenarios)
    assert 90 <= total_prob <= 110  # Allow some rounding

    # P50 should be before P75 which should be before P90
    if report.p50_close_date and report.p75_close_date and report.p90_close_date:
        assert report.p50_close_date <= report.p75_close_date
        assert report.p75_close_date <= report.p90_close_date
