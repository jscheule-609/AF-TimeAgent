"""GET /results/active is served by get_active_results, not by
/results/{deal_pk}. FastAPI matches routes in declaration order; with the
per-deal route declared first, "active" was parsed as deal_pk and the call
returned 422. No database: the pool is a mock and the app's lifespan (which
opens the pool) never runs because the TestClient is not entered."""
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

import api.server as server


def _pool(**methods):
    pool = MagicMock()
    pool.acquire.return_value.__aenter__.return_value = SimpleNamespace(
        **{k: AsyncMock(return_value=v) for k, v in methods.items()})
    return pool


def _conn(pool):
    return pool.acquire.return_value.__aenter__.return_value


ROW = {"deal_pk": 7, "target": "T", "acquirer": "A",
       "p50_close_date": date(2027, 1, 1), "run_id": "batch-x"}


def test_results_active_is_declared_before_the_per_deal_route():
    paths = [getattr(r, "path", None) for r in server.app.routes]
    assert paths.index("/results/active") < paths.index("/results/{deal_pk}")


def test_results_active_routes_to_the_active_handler(monkeypatch):
    pool = _pool(fetchval=True, fetch=[ROW], fetchrow=None)
    monkeypatch.setattr(server, "pool", pool)
    client = TestClient(server.app)   # not entered: no lifespan, no real pool

    r = client.get("/results/active")

    assert r.status_code == 200, r.text
    assert r.json() == [{**ROW, "p50_close_date": "2027-01-01"}]
    sql = _conn(pool).fetch.await_args.args[0]
    assert "FROM v_latest_timing_prediction tp" in sql
    assert "WHERE d.deal_status = 'Active'" in sql
    _conn(pool).fetchrow.assert_not_awaited()


def test_results_per_deal_still_routes_by_deal_pk(monkeypatch):
    pool = _pool(fetchrow=ROW, fetchval=True, fetch=[])
    monkeypatch.setattr(server, "pool", pool)
    client = TestClient(server.app)

    r = client.get("/results/7")

    assert r.status_code == 200, r.text
    assert r.json()["deal_pk"] == 7
    assert _conn(pool).fetchrow.await_args.args[1] == 7
    _conn(pool).fetch.assert_not_awaited()
    assert client.get("/results/not-a-pk").status_code == 422
