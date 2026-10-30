"""The prediction_id RETURNed by store_prediction must survive the backtest
pipeline, and the backtest must run under a 'backtest' run context (no
database)."""
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from db import connection as db_conn
from db import queries_prediction
from models.timeline import DealTimingReport
from pipeline import backtest_runner, step7_prediction_log
from scripts import backtest


@pytest.mark.asyncio
async def test_backtest_actuals_use_stored_prediction_id(monkeypatch):
    announcement = date(2025, 1, 1)
    close = date(2025, 4, 1)
    row = dict(
        deal_pk=7, acquirer_ticker="A", target_ticker="T",
        date_announced=announcement, actual_completion_date=close,
        timeline_days=90,
    )
    report = DealTimingReport(
        acquirer="A", target="T", deal_value_usd=1.0,
        announcement_date=announcement, milestones=[], scenarios=[], risk_flags=[],
        p50_close_date=close, p75_close_date=close, p90_close_date=close,
    )
    params = SimpleNamespace(
        mars_deal_pk=7, mars_deal_id="7", target_name="T",
        announcement_date=announcement,
    )
    # Keep run_backtest -> run_backtest_deal -> log_prediction real; stub all
    # enrichment/estimation boundaries so no DB, SEC, or LLM is contacted.
    stages = {
        "validate_deal": SimpleNamespace(is_valid=True, deal_params=params),
        "parse_deal_press_release": None,
        "ingest_documents": (None, None, None),
        "assess_antitrust_overlap": SimpleNamespace(
            overlap_type="none", overlap_severity="none",
        ),
        "find_comparables": [], "map_jurisdictions": [],
        "simulate_regulatory_paths": None, "assemble_timeline": report,
    }
    for name, value in stages.items():
        monkeypatch.setattr(backtest_runner, name, AsyncMock(return_value=value))
    monkeypatch.setattr(
        "db.queries_comparables.get_target_prior_deals", AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "pipeline.step3b_timeline_calibration.calibrate_deal_timeline",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "pipeline.step2b_guidance_anchor.load_guidance_anchor",
        AsyncMock(return_value=None),
    )
    conn = SimpleNamespace(fetchrow=AsyncMock(return_value=None))
    pool = MagicMock()
    pool.acquire.return_value.__aenter__.return_value = conn
    monkeypatch.setattr("db.connection.get_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(backtest, "fetch_backtest_universe", AsyncMock(return_value=[row]))
    seen_ctx = {}

    async def _store(record):
        seen_ctx["ctx"] = db_conn.current_run_context()
        return "existing-id"
    store = AsyncMock(side_effect=_store)
    monkeypatch.setattr(step7_prediction_log, "store_prediction", store)
    update = AsyncMock()
    monkeypatch.setattr(backtest, "update_prediction_actuals", update)

    results = await backtest.run_backtest(save_results=False)

    store.assert_awaited_once()
    assert store.call_args.args[0]["prediction_id"] != "existing-id"
    # append-only: the row carries the backtest run id, and the write ran
    # under mars.writer = timeagent:backtest
    trigger, run_id = seen_ctx["ctx"]
    assert trigger == "backtest" and run_id.startswith("backtest-")
    assert store.call_args.args[0]["run_id"] == run_id
    assert results[0]["prediction_id"] == report.prediction_id == "existing-id"
    update.assert_awaited_once_with(
        prediction_id="existing-id", actual_close_date=close,
        actual_timeline_days=90, actual_outcome="closed", actual_critical_path="",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status, warns", [("UPDATE 0", True), ("UPDATE 1", False)])
async def test_actuals_warn_only_when_no_rows_match(monkeypatch, caplog, status, warns):
    conn = SimpleNamespace(execute=AsyncMock(return_value=status))
    pool = MagicMock()
    pool.acquire.return_value.__aenter__.return_value = conn
    # writes go through db.connection.acquire_for_write, which needs a
    # run context for the mars.writer / mars.run_id tags
    monkeypatch.setattr(db_conn, "get_pool", AsyncMock(return_value=pool))
    db_conn.set_run_context("backtest", "backtest-test-run")

    await queries_prediction.update_prediction_actuals(
        "existing-id", date(2025, 4, 1), 90, "closed", "HSR",
    )

    # the two set_config tags precede the UPDATE, which stays keyed on
    # prediction_id
    sqls = [c.args[0] for c in conn.execute.await_args_list]
    assert "set_config('mars.writer'" in sqls[0]
    assert conn.execute.await_args_list[0].args[1] == "timeagent:backtest"
    assert "set_config('mars.run_id'" in sqls[1]
    assert conn.execute.await_args_list[1].args[1] == "backtest-test-run"
    assert "WHERE prediction_id = $1" in sqls[2]
    assert conn.execute.call_args.args[1] == "existing-id"
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert bool(warnings) == warns
    if warns:
        assert "prediction_id=existing-id" in warnings[0].message
        assert "UPDATE 0" in warnings[0].message
