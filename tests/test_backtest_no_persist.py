"""Experimental backtests must never write prediction rows or actuals."""
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from models.timeline import DealTimingReport
from pipeline import backtest_runner
from scripts import backtest


@pytest.fixture
def pipeline_stubs(monkeypatch):
    announcement = date(2025, 1, 1)
    close = date(2025, 4, 1)
    report = DealTimingReport(
        acquirer="A", target="T", deal_value_usd=1.0,
        announcement_date=announcement, milestones=[], scenarios=[], risk_flags=[],
        p50_close_date=close, p75_close_date=close, p90_close_date=close,
        prediction_id="preexisting-report-id",  # guard even when an ID is present
    )
    row = dict(
        deal_pk=7, acquirer_ticker="A", target_ticker="T",
        date_announced=announcement, actual_completion_date=close, timeline_days=90,
    )
    params = SimpleNamespace(
        mars_deal_pk=7, mars_deal_id="7", target_name="T", announcement_date=announcement,
    )
    stages = {
        "validate_deal": SimpleNamespace(is_valid=True, deal_params=params),
        "parse_deal_press_release": None, "ingest_documents": (None, None, None),
        "assess_antitrust_overlap": SimpleNamespace(overlap_type="none", overlap_severity="none"),
        "find_comparables": [], "map_jurisdictions": [],
        "simulate_regulatory_paths": None, "assemble_timeline": report,
    }
    for name, value in stages.items():
        monkeypatch.setattr(backtest_runner, name, AsyncMock(return_value=value))
    monkeypatch.setattr("db.queries_comparables.get_target_prior_deals", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        "pipeline.step3b_timeline_calibration.calibrate_deal_timeline", AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "pipeline.step2b_guidance_anchor.load_guidance_anchor", AsyncMock(return_value=None),
    )
    pool = MagicMock()
    pool.acquire.return_value.__aenter__.return_value = SimpleNamespace(fetchrow=AsyncMock(return_value=row))
    monkeypatch.setattr("db.connection.get_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(backtest, "get_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(backtest, "fetch_backtest_universe", AsyncMock(return_value=[row]))
    log = AsyncMock()
    update = AsyncMock()
    monkeypatch.setattr(backtest_runner, "log_prediction", log)
    monkeypatch.setattr(backtest, "update_prediction_actuals", update)
    return log, update


@pytest.mark.asyncio
@pytest.mark.parametrize("no_persist", [True, False])
async def test_backtest_write_guards_and_json(monkeypatch, tmp_path, pipeline_stubs, no_persist):
    monkeypatch.setattr(backtest, "OUTPUT_DIR", tmp_path)
    results = await backtest.run_backtest(no_persist=no_persist)
    assert "error" not in results[0]
    assert len(list(tmp_path.glob("backtest_*.json"))) == 1
    log, update = pipeline_stubs
    assert log.await_count == update.await_count == (0 if no_persist else 1)


@pytest.mark.asyncio
async def test_single_deal_no_persist(pipeline_stubs):
    row = await backtest.run_single_deal("A", "T", no_persist=True)
    assert row["deal_pk"] == 7
    log, update = pipeline_stubs
    log.assert_not_awaited()
    update.assert_not_awaited()


@pytest.mark.parametrize("single", [True, False])
def test_cli_threads_no_persist(monkeypatch, single):
    argv = ["backtest", "--no-persist"] + (["--single", "A/T"] if single else [])
    monkeypatch.setattr("sys.argv", argv)
    run = AsyncMock()
    monkeypatch.setattr(backtest, "run_single_deal" if single else "run_backtest", run)
    monkeypatch.setattr(backtest, "close_pool", AsyncMock())
    backtest.main()
    assert run.await_args.kwargs["no_persist"] is True
