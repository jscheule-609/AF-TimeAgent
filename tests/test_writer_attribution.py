"""db.connection writer attribution (design 5.4): application_name on every
connection, mars.writer / mars.run_id set_config on every write path.
No database: the connection is a mock."""
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from db import connection as db_conn


@pytest.fixture(autouse=True)
def _clean_context():
    db_conn.clear_run_context()
    yield
    db_conn.clear_run_context()


def _conn():
    return SimpleNamespace(execute=AsyncMock(return_value="SELECT 1"))


def _set_config_calls(conn):
    return [(c.args[0], c.args[1]) for c in conn.execute.await_args_list]


# -- application_name ------------------------------------------------------

def test_application_name_default_and_env_override(monkeypatch):
    monkeypatch.delenv("MARS_APPLICATION_NAME", raising=False)
    assert db_conn.application_name() == "timeagent"
    assert db_conn.server_settings() == {"application_name": "timeagent"}
    monkeypatch.setenv("MARS_APPLICATION_NAME", "timeagent-staging")
    assert db_conn.server_settings() == {
        "application_name": "timeagent-staging"}
    monkeypatch.setenv("MARS_APPLICATION_NAME", "   ")
    assert db_conn.application_name() == "timeagent"


@pytest.mark.asyncio
async def test_get_pool_sets_application_name(monkeypatch):
    monkeypatch.delenv("MARS_APPLICATION_NAME", raising=False)
    monkeypatch.setattr(db_conn, "_pool", None)
    create = AsyncMock(return_value=MagicMock())
    monkeypatch.setattr(db_conn.asyncpg, "create_pool", create)
    try:
        await db_conn.get_pool()
    finally:
        monkeypatch.setattr(db_conn, "_pool", None)
    create.assert_awaited_once()
    assert create.await_args.kwargs["server_settings"] == {
        "application_name": "timeagent"}


# -- run context -----------------------------------------------------------

@pytest.mark.parametrize("trigger", sorted(db_conn.TRIGGERS))
def test_writer_name_and_run_id_format(trigger):
    assert db_conn.writer_name(trigger) == f"timeagent:{trigger}"
    run_id = db_conn.new_run_id(trigger)
    assert re.fullmatch(
        rf"{trigger}-\d{{8}}T\d{{6}}Z-[0-9a-f]{{6}}", run_id), run_id


def test_design_triggers_are_all_known():
    for t in ("batch", "notify", "api", "mcp", "run_deal", "score"):
        assert t in db_conn.TRIGGERS


def test_unknown_trigger_is_rejected():
    with pytest.raises(ValueError):
        db_conn.writer_name("cron")
    with pytest.raises(ValueError):
        db_conn.set_run_context("cron")
    assert db_conn.current_run_context() is None


def test_set_run_context_generates_or_keeps_run_id():
    rid = db_conn.set_run_context("batch")
    assert rid.startswith("batch-")
    assert db_conn.current_run_context() == ("batch", rid)
    assert db_conn.current_run_id() == rid
    assert db_conn.set_run_context("api", "api-fixed") == "api-fixed"
    assert db_conn.current_run_context() == ("api", "api-fixed")
    db_conn.clear_run_context()
    assert db_conn.current_run_id() is None


# -- attribute_writer ------------------------------------------------------

@pytest.mark.asyncio
async def test_attribute_writer_issues_the_two_set_configs():
    conn = _conn()
    out = await db_conn.attribute_writer(conn, "score", "score-20261101T063000Z-abc123")
    assert out == ("score", "score-20261101T063000Z-abc123")
    assert _set_config_calls(conn) == [
        ("SELECT set_config('mars.writer', $1, false)", "timeagent:score"),
        ("SELECT set_config('mars.run_id', $1, false)",
         "score-20261101T063000Z-abc123"),
    ]


@pytest.mark.asyncio
async def test_attribute_writer_reads_the_run_context():
    rid = db_conn.set_run_context("notify")
    conn = _conn()
    assert await db_conn.attribute_writer(conn) == ("notify", rid)
    assert _set_config_calls(conn) == [
        ("SELECT set_config('mars.writer', $1, false)", "timeagent:notify"),
        ("SELECT set_config('mars.run_id', $1, false)", rid),
    ]


@pytest.mark.asyncio
async def test_attribute_writer_explicit_run_id_overrides_context():
    db_conn.set_run_context("batch", "batch-ctx")
    conn = _conn()
    assert await db_conn.attribute_writer(conn, run_id="batch-row") == (
        "batch", "batch-row")
    assert _set_config_calls(conn)[1][1] == "batch-row"


@pytest.mark.asyncio
async def test_attribute_writer_without_context_refuses():
    conn = _conn()
    with pytest.raises(RuntimeError, match="set_run_context"):
        await db_conn.attribute_writer(conn)
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_acquire_for_write_tags_the_acquired_connection(monkeypatch):
    conn = _conn()
    pool = MagicMock()
    pool.acquire.return_value.__aenter__.return_value = conn
    monkeypatch.setattr(db_conn, "get_pool", AsyncMock(return_value=pool))
    db_conn.set_run_context("run_deal", "run_deal-x")
    async with db_conn.acquire_for_write() as got:
        assert got is conn
        assert _set_config_calls(conn) == [
            ("SELECT set_config('mars.writer', $1, false)",
             "timeagent:run_deal"),
            ("SELECT set_config('mars.run_id', $1, false)", "run_deal-x"),
        ]
