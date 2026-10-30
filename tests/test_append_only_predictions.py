"""timing_predictions is append-only (AF-AJ 048b): store_prediction is a
plain INSERT carrying run_id / as_of_date / input_snapshot_ref, every
log_prediction call gets a fresh prediction_id, and the scorer's SQL has
no one-row-per-deal assumption.  No database."""
import json
import re
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import scripts.score_predictions as sp
from db import connection as db_conn
from db import queries_prediction as qp
from models.timeline import DealTimingReport, GuidanceReconciliation
from pipeline import step7_prediction_log as s7


@pytest.fixture(autouse=True)
def _clean_context():
    db_conn.clear_run_context()
    yield
    db_conn.clear_run_context()


def _report(**kw) -> DealTimingReport:
    base = dict(
        acquirer="A", target="T", deal_value_usd=1.0,
        announcement_date=date(2026, 1, 1),
        milestones=[], scenarios=[], risk_flags=[],
        p50_close_date=date(2026, 4, 1),
        critical_path_jurisdiction="HSR",
    )
    base.update(kw)
    return DealTimingReport(**base)


def _record(**kw) -> dict:
    rec = dict(
        prediction_id="pid-1", deal_pk=42, acquirer_ticker="A",
        target_ticker="T", prediction_date=date(2026, 10, 31),
        p50_close_date=date(2027, 1, 15), model_version="0.2.0",
    )
    rec.update(kw)
    return rec


def _wire_pool(monkeypatch, returned="pid-1"):
    conn = SimpleNamespace(
        execute=AsyncMock(return_value="SELECT 1"),
        fetchrow=AsyncMock(return_value={"prediction_id": returned}),
    )
    pool = MagicMock()
    pool.acquire.return_value.__aenter__.return_value = conn
    monkeypatch.setattr(db_conn, "get_pool", AsyncMock(return_value=pool))
    return conn


# -- store_prediction ------------------------------------------------------

@pytest.mark.asyncio
async def test_store_prediction_is_a_plain_insert_with_run_columns(monkeypatch):
    conn = _wire_pool(monkeypatch)
    db_conn.set_run_context("batch", "batch-20261031T032500Z-5180ef")
    ref = {"deal_updated_at": datetime(2026, 10, 30, 1, 2, tzinfo=timezone.utc),
           "comparable_deal_pks": [3, 5], "model_version": "0.2.0",
           "guidance_source": "AJ"}

    pid = await qp.store_prediction(_record(input_snapshot_ref=ref))

    assert pid == "pid-1"
    sql, *args = conn.fetchrow.await_args.args
    assert "ON CONFLICT" not in sql.upper()
    assert "INSERT INTO timing_predictions" in sql
    assert re.search(r"model_version,\s*run_id, as_of_date, input_snapshot_ref", sql)
    assert "$22::jsonb" in sql
    assert "RETURNING prediction_id" in sql
    assert args[0] == "pid-1"
    assert args[19] == "batch-20261031T032500Z-5180ef"     # run_id
    assert args[20] == date(2026, 10, 31)                   # as_of_date
    assert json.loads(args[21]) == {                        # input_snapshot_ref
        "deal_updated_at": "2026-10-30T01:02:00+00:00",
        "comparable_deal_pks": [3, 5], "model_version": "0.2.0",
        "guidance_source": "AJ",
    }
    # scoring columns are never written by store_prediction
    for col in ("actual_close_date", "actual_outcome", "p50_error_days",
                "close_within_p50"):
        assert col not in sql
    # attribution ran on the same connection, before the INSERT
    assert [c.args for c in conn.execute.await_args_list] == [
        ("SELECT set_config('mars.writer', $1, false)", "timeagent:batch"),
        ("SELECT set_config('mars.run_id', $1, false)",
         "batch-20261031T032500Z-5180ef"),
    ]


@pytest.mark.asyncio
async def test_store_prediction_derives_as_of_from_a_datetime(monkeypatch):
    conn = _wire_pool(monkeypatch)
    db_conn.set_run_context("api")
    await qp.store_prediction(_record(
        prediction_date=datetime(2026, 10, 31, 23, 59, tzinfo=timezone.utc)))
    args = conn.fetchrow.await_args.args[1:]
    assert args[20] == date(2026, 10, 31)
    assert args[21] is None          # no snapshot ref given -> NULL


@pytest.mark.asyncio
async def test_store_prediction_explicit_run_id_wins_and_tags_session(monkeypatch):
    conn = _wire_pool(monkeypatch)
    db_conn.set_run_context("mcp", "mcp-ctx")
    await qp.store_prediction(_record(run_id="mcp-row"))
    assert conn.fetchrow.await_args.args[20] == "mcp-row"
    assert conn.execute.await_args_list[1].args[1] == "mcp-row"


@pytest.mark.asyncio
async def test_store_prediction_without_run_context_does_not_insert(monkeypatch):
    conn = _wire_pool(monkeypatch)
    with pytest.raises(RuntimeError):
        await qp.store_prediction(_record())
    conn.fetchrow.assert_not_awaited()


# -- log_prediction --------------------------------------------------------

@pytest.mark.asyncio
async def test_log_prediction_new_id_per_run_and_passes_run_columns(monkeypatch):
    stored = []

    async def _fake_store(record):
        stored.append(record)
        return record["prediction_id"]
    monkeypatch.setattr(s7, "store_prediction", _fake_store)
    report = _report(guidance_reconciliation=GuidanceReconciliation(
        guidance_source="both", flag="aligned"))

    db_conn.set_run_context("batch", "batch-1")
    pid1 = await s7.log_prediction(report, deal_pk=42)
    db_conn.set_run_context("batch", "batch-2")
    pid2 = await s7.log_prediction(report, deal_pk=42)

    assert pid1 != pid2                       # fresh prediction_id per run
    assert [r["run_id"] for r in stored] == ["batch-1", "batch-2"]
    assert report.prediction_id == pid2 and report.run_id == "batch-2"
    rec = stored[-1]
    assert rec["as_of_date"] == rec["prediction_date"] == date.today()
    ref = rec["input_snapshot_ref"]
    assert set(ref) >= {"deal_updated_at", "comparable_deal_pks",
                        "model_version", "guidance_source"}
    assert ref["model_version"] == "0.2.0"
    assert ref["guidance_source"] == "both"


def test_build_input_snapshot_ref_minimum_keys():
    ts = datetime(2026, 10, 30, 1, 2, tzinfo=timezone.utc)
    ref = s7.build_input_snapshot_ref(
        _report(), deal_updated_at=ts, comparable_deal_pks=[9, 3, 9],
        extra={"as_of": date(2026, 10, 31)},
    )
    assert ref["deal_updated_at"] == ts
    assert ref["comparable_deal_pks"] == [3, 9]
    assert ref["model_version"] == "0.2.0"
    assert ref["guidance_source"] is None
    assert ref["as_of"] == date(2026, 10, 31)
    assert "calibration_fingerprint" in ref


# -- scorer SQL: per row, keyed on prediction_id ---------------------------

_SELECTS = {
    "_CLOSED_CANDIDATES": sp._CLOSED_CANDIDATES,
    "_CLOSED_POSTHOC": sp._CLOSED_POSTHOC,
    "_TERMINATED_CANDIDATES": sp._TERMINATED_CANDIDATES,
    "_CLOSED_ANOMALIES": sp._CLOSED_ANOMALIES,
    "_UNSCORABLE": sp._UNSCORABLE,
}


@pytest.mark.parametrize("name", sorted(_SELECTS))
def test_scoring_selection_is_per_row(name):
    sql = _SELECTS[name]
    up = sql.upper()
    assert "DISTINCT ON" not in up and "LIMIT" not in up
    assert "MAX(" not in up and "GROUP BY" not in up
    assert "JOIN deals d USING (deal_pk)" in sql
    assert "tp.actual_close_date IS NULL" in sql     # unscored, per row


@pytest.mark.parametrize("sql", [
    sp._CLOSED_UPDATE, sp._POSTHOC_UPDATE, sp._TERMINATED_UPDATE])
def test_scoring_updates_key_on_prediction_id(sql):
    assert "WHERE c.prediction_id = tp.prediction_id" in sql
    assert "DISTINCT ON" not in sql.upper()


@pytest.mark.parametrize("sql", [sp._RESCORE_UPDATE, sp._UNSCORE_POSTHOC])
def test_rescore_and_unscore_act_on_every_row(sql):
    up = sql.upper()
    assert "UPDATE TIMING_PREDICTIONS TP SET" in up
    assert "DISTINCT ON" not in up and "LIMIT" not in up
    assert "deals" not in sql                 # no per-deal join or key
    assert "tp.actual_close_date IS NOT NULL" in sql


def test_calibration_rows_are_per_row():
    up = sp._SCORED_ROWS.upper()
    assert "DISTINCT ON" not in up and "LIMIT" not in up


# -- scorer attribution ----------------------------------------------------

class _Txn:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _scorer_conn(log):
    async def _fetch(sql, *a):
        log.append(("fetch", sql))
        return []

    async def _fetchval(sql, *a):
        return 0

    async def _execute(sql, *a):
        log.append(("execute", sql, a))
        return "SELECT 1"

    return SimpleNamespace(fetch=_fetch, fetchval=_fetchval,
                           execute=_execute, transaction=lambda: _Txn())


@pytest.mark.asyncio
async def test_score_apply_tags_session_before_updates(monkeypatch):
    log = []
    pool = MagicMock()
    pool.acquire.return_value.__aenter__.return_value = _scorer_conn(log)
    monkeypatch.setattr(sp, "get_pool", AsyncMock(return_value=pool))

    out = await sp.score(apply=True, rescore=True)

    assert out["run_id"].startswith("score-")
    assert db_conn.current_run_context() == ("score", out["run_id"])
    execs = [e for e in log if e[0] == "execute"]
    assert [(e[1], e[2][0]) for e in execs] == [
        ("SELECT set_config('mars.writer', $1, false)", "timeagent:score"),
        ("SELECT set_config('mars.run_id', $1, false)", out["run_id"]),
    ]
    first_update = next(i for i, e in enumerate(log)
                        if e[0] == "fetch" and "UPDATE" in e[1])
    assert all(log.index(e) < first_update for e in execs)
    assert sum(1 for e in log if e[0] == "fetch" and "UPDATE" in e[1]) == 4


@pytest.mark.asyncio
async def test_score_dry_run_writes_nothing(monkeypatch):
    log = []
    pool = MagicMock()
    pool.acquire.return_value.__aenter__.return_value = _scorer_conn(log)
    monkeypatch.setattr(sp, "get_pool", AsyncMock(return_value=pool))

    out = await sp.score(apply=False)

    assert out["apply"] is False and "closed_written" not in out
    assert not [e for e in log if e[0] == "execute"]
    assert not [e for e in log if "UPDATE" in e[1]]
