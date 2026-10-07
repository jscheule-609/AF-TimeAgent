"""scripts.score_predictions: calibration summary math (no DB), plus the
post-hoc scoring rules against a real Postgres when TIMEAGENT_TEST_DSN is set
(any throwaway database; each test builds and drops its own schema)."""
import os
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest

import scripts.score_predictions as sp
from scripts.score_predictions import guidance_anchor, summarize


def _row(version, ann, p50_off, actual_off, p75_off=None, p90_off=None,
         aj=None, co=None, stored_err=True):
    ann = date.fromisoformat(ann)
    p50 = ann + timedelta(days=p50_off)
    p75 = ann + timedelta(days=p75_off if p75_off is not None else p50_off + 30)
    p90 = ann + timedelta(days=p90_off if p90_off is not None else p50_off + 60)
    actual = ann + timedelta(days=actual_off)
    row = dict(
        model_version=version, deal_pk=hash((version, ann, p50_off)) % 10**6,
        p50_close_date=p50, p75_close_date=p75, p90_close_date=p90,
        actual_close_date=actual, date_announced=ann,
        closing_guidance_arbjournal=aj, closing_guidance_companies=co,
    )
    if stored_err:
        row.update(
            p50_error_days=(actual - p50).days,
            close_within_p50=actual <= p50,
            close_within_p75=actual <= p75,
            close_within_p90=actual <= p90,
        )
    else:
        row.update(p50_error_days=None, close_within_p50=None,
                   close_within_p75=None, close_within_p90=None)
    return row


def test_summarize_per_version_and_all():
    rows = [
        _row("0.1.0", "2025-01-01", p50_off=100, actual_off=130),   # +30 late, miss P50, within P75
        _row("0.1.0", "2025-02-01", p50_off=100, actual_off=90),    # -10 early, within P50
        _row("0.2.0", "2025-03-01", p50_off=100, actual_off=100),   # exact
        _row("0.2.0", "2025-04-01", p50_off=100, actual_off=200,    # +100, miss everything
             stored_err=False),                                      # flags recomputed
    ]
    rep = summarize(rows, anchor_fn=lambda r: None)

    v1, v2, all_ = rep["0.1.0"], rep["0.2.0"], rep["all"]
    assert v1["n"] == 2 and v1["mae"] == 20.0 and v1["medae"] == 20.0
    assert v1["bias"] == 10.0
    assert v1["within_p50"] == 50.0 and v1["within_p75"] == 100.0
    assert v2["n"] == 2 and v2["mae"] == 50.0 and v2["bias"] == 50.0
    assert v2["within_p50"] == 50.0 and v2["within_p90"] == 50.0
    assert all_["n"] == 4 and all_["mae"] == 35.0
    assert all_["guided_n"] == 0 and "guidance_mae" not in all_


def test_summarize_guidance_baseline():
    rows = [
        _row("0.2.0", "2025-01-01", p50_off=100, actual_off=110),
        _row("0.2.0", "2025-01-01", p50_off=100, actual_off=150),
        _row("0.2.0", "2025-01-01", p50_off=100, actual_off=105),
    ]
    # guidance midpoint 120 days out for every row -> errors -10, +30, -15
    anchors = iter([120, 120, None])

    def _anchor(r):
        off = next(anchors)
        return None if off is None else r["date_announced"] + timedelta(days=off)

    rep = summarize(rows, anchor_fn=_anchor)["0.2.0"]
    assert rep["guided_n"] == 2
    assert rep["guidance_mae"] == 20.0          # (10 + 30) / 2
    assert rep["model_mae_on_guided"] == 30.0   # (10 + 50) / 2
    assert rep["model_beats_guidance_pct"] == 0.0


def test_summarize_empty():
    assert summarize([], anchor_fn=lambda r: None) == {
        "all": {"n": 0, "posthoc_n": 0}}


def test_summarize_excludes_posthoc_rows():
    # A post-hoc row with NULL errors would still be scored by _bucket's
    # date fallback; the posthoc flag must keep it out of every metric.
    rows = [
        _row("0.2.0", "2025-01-01", p50_off=100, actual_off=110),
        dict(_row("0.2.0", "2025-02-01", p50_off=100, actual_off=900,
                  stored_err=False), posthoc=True),
        dict(_row("0.2.0", "2025-03-01", p50_off=100, actual_off=700),
             posthoc=True),
    ]
    rep = summarize(rows, anchor_fn=lambda r: None)
    assert rep["0.2.0"]["n"] == 1 and rep["0.2.0"]["mae"] == 10.0
    assert rep["0.2.0"]["posthoc_n"] == 2
    assert rep["all"]["n"] == 1 and rep["all"]["posthoc_n"] == 2


def test_summarize_only_posthoc_rows():
    rows = [dict(_row("0.2.0", "2025-01-01", p50_off=100, actual_off=50),
                 posthoc=True)]
    assert summarize(rows, anchor_fn=lambda r: None)["0.2.0"] == {
        "n": 0, "posthoc_n": 1}


# -- post-hoc rules against Postgres ----------------------------------------

_DSN = os.environ.get("TIMEAGENT_TEST_DSN")
needs_db = pytest.mark.skipif(not _DSN, reason="TIMEAGENT_TEST_DSN not set")

_DDL = """
CREATE TABLE deals (
    deal_pk bigint PRIMARY KEY,
    deal_status varchar,
    date_announced date,
    actual_completion_date date,
    closing_guidance_arbjournal text,
    closing_guidance_companies text
);
CREATE TABLE timing_predictions (
    prediction_id text PRIMARY KEY,
    deal_pk bigint UNIQUE REFERENCES deals(deal_pk),
    prediction_date timestamptz NOT NULL DEFAULT now(),
    p50_close_date date, p75_close_date date, p90_close_date date,
    actual_close_date date, actual_timeline_days integer,
    actual_critical_path text, actual_outcome text DEFAULT 'pending',
    p50_error_days integer, p75_error_days integer, p90_error_days integer,
    close_within_p50 boolean, close_within_p75 boolean,
    close_within_p90 boolean,
    model_version text,
    created_at timestamptz DEFAULT now(),
    updated_at timestamptz DEFAULT now()
);
"""

_ANN = date(2025, 1, 1)
_CLOSE = date(2025, 6, 30)
_ERR_COLS = ("p50_error_days", "p75_error_days", "p90_error_days",
             "close_within_p50", "close_within_p75", "close_within_p90")


async def _db_pool(monkeypatch):
    import asyncpg

    schema = f"t_score_{uuid.uuid4().hex[:8]}"
    admin = await asyncpg.connect(_DSN)
    await admin.execute(f"CREATE SCHEMA {schema}")
    await admin.close()
    pool = await asyncpg.create_pool(
        _DSN, min_size=1, max_size=2,
        # GMT, not UTC: same offset as mars-db, and accepted by builds
        # shipped without a tz database
        server_settings={"search_path": schema, "TimeZone": "GMT"},
    )
    async with pool.acquire() as conn:
        await conn.execute(_DDL)

    async def _get_pool():
        return pool

    monkeypatch.setattr(sp, "get_pool", _get_pool)
    return pool, schema


async def _drop(pool, schema):
    import asyncpg

    await pool.close()
    admin = await asyncpg.connect(_DSN)
    await admin.execute(f"DROP SCHEMA {schema} CASCADE")
    await admin.close()


async def _add(conn, deal_pk, predicted_on, scored=False):
    """Completed deal (announced 2025-01-01, closed 2025-06-30) with one
    prediction: p50 2025-06-20 (10 d early), p75 07-20, p90 08-19."""
    await conn.execute(
        "INSERT INTO deals VALUES ($1, 'Completed', $2, $3, NULL, NULL)",
        deal_pk, _ANN, _CLOSE)
    p50 = date(2025, 6, 20)
    p75, p90 = p50 + timedelta(days=30), p50 + timedelta(days=60)
    pred_ts = datetime.combine(predicted_on, datetime.min.time(),
                               tzinfo=timezone.utc)
    await conn.execute(
        "INSERT INTO timing_predictions (prediction_id, deal_pk, "
        "prediction_date, p50_close_date, p75_close_date, p90_close_date, "
        "model_version) VALUES ($1, $2, $3, $4, $5, $6, '0.2.0')",
        f"p{deal_pk}", deal_pk, pred_ts, p50, p75, p90)
    if scored:   # what the 09-13 batch left behind: actuals + errors
        await conn.execute(
            "UPDATE timing_predictions SET actual_close_date = $2, "
            "actual_timeline_days = $2::date - $3::date, "
            "actual_outcome = 'Completed', "
            "p50_error_days = $2 - p50_close_date, "
            "p75_error_days = $2 - p75_close_date, "
            "p90_error_days = $2 - p90_close_date, "
            "close_within_p50 = $2 <= p50_close_date, "
            "close_within_p75 = $2 <= p75_close_date, "
            "close_within_p90 = $2 <= p90_close_date "
            "WHERE deal_pk = $1", deal_pk, _CLOSE, _ANN)


async def _get(conn, deal_pk):
    return dict(await conn.fetchrow(
        "SELECT actual_close_date, actual_timeline_days, actual_outcome, "
        + ", ".join(_ERR_COLS)
        + " FROM timing_predictions WHERE deal_pk = $1", deal_pk))


@needs_db
@pytest.mark.asyncio
async def test_preclose_scored_posthoc_gets_actuals_only(monkeypatch):
    pool, schema = await _db_pool(monkeypatch)
    try:
        async with pool.acquire() as conn:
            await _add(conn, 1, date(2025, 3, 1))     # forecast
            await _add(conn, 2, date(2026, 9, 13))    # predicted post-close
            await _add(conn, 3, _CLOSE)               # predicted on the day

        out = await sp.score(apply=True, rescore=True)
        assert out["closed_candidates"] == 2 and out["closed_posthoc"] == 1
        assert out["closed_written"] == 2 and out["posthoc_written"] == 1

        async with pool.acquire() as conn:
            pre, post, same = (await _get(conn, 1), await _get(conn, 2),
                               await _get(conn, 3))
        assert pre["actual_close_date"] == _CLOSE
        assert pre["p50_error_days"] == 10 and pre["p90_error_days"] == -50
        assert pre["close_within_p50"] is False
        assert pre["close_within_p75"] is True
        assert same["p50_error_days"] == 10
        assert post["actual_close_date"] == _CLOSE
        assert post["actual_timeline_days"] == (_CLOSE - _ANN).days
        assert post["actual_outcome"] == "Completed"
        assert all(post[c] is None for c in _ERR_COLS)

        # the weekly --apply --rescore must not score the post-hoc row
        again = await sp.score(apply=True, rescore=True)
        assert again["rescored"] == 0 and again["closed_written"] == 0
        async with pool.acquire() as conn:
            assert (await _get(conn, 2))["p50_error_days"] is None
            rows = [dict(r) for r in await conn.fetch(sp._SCORED_ROWS)]
        rep = summarize(rows, anchor_fn=lambda r: None)["0.2.0"]
        assert rep["n"] == 2 and rep["posthoc_n"] == 1
    finally:
        await _drop(pool, schema)


@needs_db
@pytest.mark.asyncio
async def test_unscore_posthoc_is_idempotent(monkeypatch):
    pool, schema = await _db_pool(monkeypatch)
    try:
        async with pool.acquire() as conn:
            await _add(conn, 1, date(2025, 3, 1), scored=True)
            await _add(conn, 2, date(2026, 9, 13), scored=True)
            await _add(conn, 3, date(2026, 9, 30), scored=True)

        dry = await sp.unscore_posthoc(apply=False)
        assert dry["posthoc_scored"] == 2 and dry["deal_pks"] == [2, 3]
        async with pool.acquire() as conn:
            assert (await _get(conn, 2))["p50_error_days"] == 10

        first = await sp.unscore_posthoc(apply=True)
        assert first["unscored"] == 2
        second = await sp.unscore_posthoc(apply=True)
        assert second["posthoc_scored"] == 0 and second["unscored"] == 0

        async with pool.acquire() as conn:
            pre, post = await _get(conn, 1), await _get(conn, 2)
        assert pre["p50_error_days"] == 10 and pre["close_within_p75"] is True
        assert post["actual_close_date"] == _CLOSE
        assert post["actual_outcome"] == "Completed"
        assert all(post[c] is None for c in _ERR_COLS)

        # and the weekly rescore leaves them unscored
        out = await sp.score(apply=True, rescore=True)
        assert out["rescored"] == 0 and out["posthoc_scored_rows"] == 0
    finally:
        await _drop(pool, schema)


@pytest.mark.parametrize("aj, co, expected", [
    ("Q4 2026", None, date(2026, 11, 15)),
    (None, "H1 2027", date(2027, 4, 1)),
    ("Q4 2026", "Q1 2027", date(2027, 2, 14)),     # later wins
    ("TBD", None, None),
    (None, None, None),
])
def test_guidance_anchor_later_wins(aj, co, expected):
    row = dict(date_announced=date(2026, 9, 1),
               closing_guidance_arbjournal=aj, closing_guidance_companies=co)
    assert guidance_anchor(row) == expected


def test_guidance_anchor_without_announcement_date():
    assert guidance_anchor(dict(date_announced=None,
                                closing_guidance_arbjournal="Q4 2026")) is None
