"""api.server after 048b: readers use v_latest_timing_prediction; every
predict path declares its run context (api / mcp / notify); batch_run and
run_deal declare theirs.  No database."""
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

import api.server as server
from db import connection as db_conn


@pytest.fixture(autouse=True)
def _clean_context():
    db_conn.clear_run_context()
    yield
    db_conn.clear_run_context()


def _pool(**methods):
    pool = MagicMock()
    pool.acquire.return_value.__aenter__.return_value = SimpleNamespace(
        **{k: AsyncMock(return_value=v) for k, v in methods.items()})
    return pool


def _conn(pool):
    return pool.acquire.return_value.__aenter__.return_value


def _report():
    return SimpleNamespace(
        target="T", acquirer="A", p50_close_date=date(2027, 1, 1),
        p75_close_date=None, p90_close_date=None,
        critical_path_jurisdiction="HSR", prediction_id="pid",
        run_id=None, guidance_reconciliation=None, risk_flags=[],
    )


# -- readers ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_results_reads_latest_view(monkeypatch):
    row = {"deal_pk": 7, "prediction_id": "p2", "run_id": "batch-x",
           "p50_close_date": date(2027, 1, 1)}
    pool = _pool(fetchrow=row)
    monkeypatch.setattr(server, "pool", pool)

    out = await server.get_results(7)

    sql, pk = _conn(pool).fetchrow.await_args.args
    assert "FROM v_latest_timing_prediction" in sql
    assert "WHERE deal_pk = $1" in sql and pk == 7
    assert "FROM timing_predictions" not in sql
    assert out["run_id"] == "batch-x" and out["p50_close_date"] == "2027-01-01"


@pytest.mark.asyncio
async def test_results_404_when_no_row(monkeypatch):
    monkeypatch.setattr(server, "pool", _pool(fetchrow=None))
    with pytest.raises(HTTPException) as exc:
        await server.get_results(7)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_results_active_reads_latest_view(monkeypatch):
    pool = _pool(fetchval=True, fetch=[{"deal_pk": 1, "run_id": "r"}])
    monkeypatch.setattr(server, "pool", pool)

    out = await server.get_active_results()

    exists_sql = _conn(pool).fetchval.await_args.args[0]
    assert "v_latest_timing_prediction" in exists_sql
    sql = _conn(pool).fetch.await_args.args[0]
    assert "FROM v_latest_timing_prediction tp" in sql
    assert "DISTINCT ON" not in sql.upper()
    assert "WHERE d.deal_status = 'Active'" in sql
    assert out == [{"deal_pk": 1, "run_id": "r"}]


@pytest.mark.asyncio
async def test_results_active_empty_before_048b(monkeypatch):
    pool = _pool(fetchval=False, fetch=[])
    monkeypatch.setattr(server, "pool", pool)
    assert await server.get_active_results() == []
    _conn(pool).fetch.assert_not_awaited()


# -- predict triggers ------------------------------------------------------

@pytest.fixture
def captured_run(monkeypatch):
    seen = {}

    async def _run(deal_pk):
        seen["ctx"] = db_conn.current_run_context()
        return _report(), 1.0
    monkeypatch.setattr(server, "_run_pipeline", _run)
    monkeypatch.setattr(server, "pool", _pool(fetchrow=None, fetch=[]))
    return seen


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", ["api", "mcp"])
async def test_predict_deal_declares_trigger(captured_run, trigger):
    res = await server.predict_deal(7, trigger=trigger)
    t, run_id = captured_run["ctx"]
    assert t == trigger and run_id.startswith(f"{trigger}-")
    assert res.run_id == run_id and res.prediction_id == "pid"


@pytest.mark.asyncio
async def test_predict_deal_defaults_to_api(captured_run):
    await server.predict_deal(7)
    assert captured_run["ctx"][0] == "api"


@pytest.mark.asyncio
async def test_predict_deal_rejects_unknown_trigger(captured_run):
    with pytest.raises(HTTPException) as exc:
        await server.predict_deal(7, trigger="cron")
    assert exc.value.status_code == 422
    assert "ctx" not in captured_run


@pytest.mark.asyncio
async def test_predict_batch_one_run_id(monkeypatch):
    seen = []

    async def _run(deal_pk):
        seen.append(db_conn.current_run_context())
        return _report(), 1.0
    monkeypatch.setattr(server, "_run_pipeline", _run)
    rows = [{"deal_pk": 1, "target": "T", "acquirer": "A"},
            {"deal_pk": 2, "target": "U", "acquirer": "B"}]
    monkeypatch.setattr(server, "pool", _pool(fetch=rows))

    out = await server.predict_batch(count=2, trigger="mcp")

    assert out["evaluated"] == 2 and out["run_id"].startswith("mcp-")
    assert seen == [("mcp", out["run_id"])] * 2
    assert {r["run_id"] for r in out["results"]} == {out["run_id"]}


@pytest.mark.asyncio
async def test_auto_predict_declares_notify(monkeypatch):
    seen = {}

    async def _run(deal_pk):
        seen["ctx"] = db_conn.current_run_context()
        return object(), 1.0
    monkeypatch.setattr(server, "_run_pipeline", _run)
    monkeypatch.setattr(server.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(server, "_autopredict_skipped", 0)
    monkeypatch.delenv("TIMEAGENT_AUTOPREDICT_MAX_AGE_DAYS", raising=False)
    monkeypatch.setattr(server, "pool", _pool(fetchrow={
        "deal_status": "Active",
        "date_announced": date.today() - timedelta(days=3)}))

    await server._auto_predict(93700)

    t, run_id = seen["ctx"]
    assert t == "notify" and run_id.startswith("notify-")


# -- CLI entry points ------------------------------------------------------

@pytest.mark.asyncio
async def test_run_deal_declares_run_deal(monkeypatch):
    from models.deal import DealInput
    from scripts import run_deal
    seen = {}

    async def _est(deal_input):
        seen["ctx"] = db_conn.current_run_context()
        return "report"
    monkeypatch.setattr(run_deal, "run_timing_estimation", _est)
    monkeypatch.setattr(run_deal, "close_pool", AsyncMock())

    assert await run_deal._run(DealInput(deal_pk=7)) == "report"
    assert seen["ctx"][0] == "run_deal"


@pytest.mark.asyncio
async def test_batch_run_one_run_id_for_the_batch(monkeypatch, capsys):
    from scripts import batch_run
    seen = []

    async def _est(deal_input):
        seen.append(db_conn.current_run_context())
        return SimpleNamespace(
            target="Target", p50_close_date=date(2027, 1, 1),
            p75_close_date=date(2027, 2, 1), critical_path_jurisdiction="HSR",
            guidance_reconciliation=None, comparable_deals_used=3,
            risk_flags=[],
        )
    monkeypatch.setattr(batch_run, "run_timing_estimation", _est)
    monkeypatch.setattr(batch_run, "close_pool", AsyncMock())
    deals = [{"deal_pk": 1, "tgt": "T", "val": 1.0, "aj": None},
             {"deal_pk": 2, "tgt": None, "val": 1.0, "aj": "Q1 2027"}]
    monkeypatch.setattr(batch_run, "get_pool",
                        AsyncMock(return_value=_pool(fetch=deals)))

    await batch_run.main(quiet=True)

    assert len(seen) == 2 and len({c for c in seen}) == 1
    trigger, run_id = seen[0]
    assert trigger == "batch" and run_id.startswith("batch-")
    out = capsys.readouterr().out
    assert "2/2 ok, 0 failed, 1 no-ticker" in out
    assert f"run_id={run_id}" in out
