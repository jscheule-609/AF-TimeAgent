"""api.server._auto_predict: only live deals are auto-predicted (no DB)."""
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import api.server as server


def _pool(row):
    pool = MagicMock()
    pool.acquire.return_value.__aenter__.return_value = SimpleNamespace(
        fetchrow=AsyncMock(return_value=row))
    return pool


@pytest.fixture
def wired(monkeypatch):
    run = AsyncMock(return_value=(object(), 1.0))
    monkeypatch.setattr(server, "_run_pipeline", run)
    monkeypatch.setattr(server.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(server, "_autopredict_skipped", 0)
    monkeypatch.delenv("TIMEAGENT_AUTOPREDICT_MAX_AGE_DAYS", raising=False)

    def _set(row):
        monkeypatch.setattr(server, "pool", _pool(row))
        return run

    return _set


def _ago(days):
    return date.today() - timedelta(days=days)


@pytest.mark.asyncio
async def test_completed_deal_is_skipped(wired):
    run = wired({"deal_status": "Completed", "date_announced": _ago(10)})
    await server._auto_predict(93642)
    run.assert_not_awaited()
    assert server._autopredict_skipped == 1


@pytest.mark.asyncio
async def test_recent_active_deal_runs(wired):
    run = wired({"deal_status": "Active", "date_announced": _ago(10)})
    await server._auto_predict(93700)
    run.assert_awaited_once_with(93700)
    assert server._autopredict_skipped == 0


@pytest.mark.asyncio
async def test_old_active_deal_is_skipped(wired):
    run = wired({"deal_status": "Active", "date_announced": _ago(400)})
    await server._auto_predict(93363)
    run.assert_not_awaited()
    assert server._autopredict_skipped == 1


@pytest.mark.asyncio
async def test_max_age_env_override(wired, monkeypatch):
    monkeypatch.setenv("TIMEAGENT_AUTOPREDICT_MAX_AGE_DAYS", "500")
    run = wired({"deal_status": "Active", "date_announced": _ago(400)})
    await server._auto_predict(93363)
    run.assert_awaited_once_with(93363)


@pytest.mark.asyncio
async def test_missing_or_unclassified_rows_are_skipped(wired):
    run = wired(None)
    await server._auto_predict(1)
    run = wired({"deal_status": None, "date_announced": None})
    await server._auto_predict(2)
    run.assert_not_awaited()
    assert server._autopredict_skipped == 2


@pytest.mark.parametrize("row, reason", [
    ({"deal_status": "Active", "date_announced": date(2026, 6, 9)}, None),
    ({"deal_status": "Active", "date_announced": date(2026, 6, 8)},
     "announced 2026-06-08 (> 120 d ago)"),
    ({"deal_status": "Active", "date_announced": None}, "no date_announced"),
    ({"deal_status": "Terminated", "date_announced": date(2026, 9, 1)},
     "deal_status=Terminated"),
    (None, "not found"),
])
def test_skip_reason_boundary(row, reason):
    today = date(2026, 10, 7)
    assert server._autopredict_skip_reason(row, today, 120) == reason
