"""D2 / migration 046: historical cohorts carry the outcome_reason filter.

Every training / comparable / calibration / backtest population leaves out
rows whose deals.outcome_reason is no_signal, autoresearch_shell or
not_a_deal, and completed rows with no actual_completion_date.  The SQL must
stay valid on a DB one migration behind (no outcome_reason column), so the
reason is read through to_jsonb(d), never as d.outcome_reason.  The Active
set the agent predicts for is not filtered.

The queries run against a mocked pool that records the SQL it is handed.
"""
from pathlib import Path

import pytest

from db import queries_climate, queries_comparables, queries_timeline_stats
from db.queries_comparables import D2_COHORT_FILTER
import scripts.backtest as backtest
import scripts.calibration_report as calibration_report
import scripts.fit_model as fit_model

_REPO = Path(__file__).resolve().parent.parent

_REASON = "COALESCE(to_jsonb(d) ->> 'outcome_reason', '')"
_EXCLUDED = "NOT IN ('coverage_dropped', 'no_signal', 'autoresearch_shell', 'not_a_deal')"
_UNDATED = "d.actual_completion_date IS NOT NULL"


class _Conn:
    def __init__(self, sink: list[str]):
        self.sink = sink

    async def fetch(self, sql, *args):
        self.sink.append(sql)
        return []

    async def fetchrow(self, sql, *args):
        self.sink.append(sql)
        return None


class _Acquire:
    def __init__(self, conn: _Conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class _Pool:
    def __init__(self):
        self.sql: list[str] = []

    def acquire(self):
        return _Acquire(_Conn(self.sql))


def _patch_pool(monkeypatch, *modules) -> _Pool:
    pool = _Pool()

    async def _get_pool():
        return pool

    for mod in modules:
        monkeypatch.setattr(mod, "get_pool", _get_pool)
    return pool


def _assert_filtered(sql: str, times: int = 1) -> None:
    assert "{D2_COHORT_FILTER}" not in sql  # f-string rendered
    assert sql.count(_REASON) == times
    assert sql.count(_EXCLUDED) == times
    assert sql.count(_UNDATED) >= times


def test_filter_reads_reason_through_to_jsonb_only():
    """Valid without the column: never a bare d.outcome_reason."""
    assert _REASON in D2_COHORT_FILTER
    assert _EXCLUDED in D2_COHORT_FILTER
    assert "d.outcome_reason" not in D2_COHORT_FILTER


def test_filter_drops_undated_completions_null_safely():
    """Completed / Closed without a date out; Terminated (no date) stays.

    IS DISTINCT FROM keeps a NULL deal_status / deal_outcome from turning
    the whole predicate NULL (which would drop the row silently).
    """
    assert _UNDATED in D2_COHORT_FILTER
    assert "d.deal_status IS DISTINCT FROM 'Completed'" in D2_COHORT_FILTER
    assert "d.deal_outcome IS DISTINCT FROM 'Closed'" in D2_COHORT_FILTER
    assert D2_COHORT_FILTER.lstrip().startswith("AND ")


@pytest.mark.asyncio
async def test_comparables_queries_carry_filter(monkeypatch):
    pool = _patch_pool(monkeypatch, queries_comparables)
    await queries_comparables.get_acquirer_prior_deals("Acme")
    await queries_comparables.get_target_prior_deals("Target Co")
    await queries_comparables.get_sector_comparable_deals("Software")
    await queries_comparables.get_size_matched_deals(1e9)
    assert len(pool.sql) == 4
    for sql in pool.sql:
        _assert_filtered(sql)
        assert "AND COALESCE(d.is_ma_deal, TRUE)" in sql


@pytest.mark.asyncio
async def test_single_deal_lookups_are_not_cohorts(monkeypatch):
    """find_deal_by_tickers resolves the deal being predicted: unfiltered."""
    pool = _patch_pool(monkeypatch, queries_comparables)
    await queries_comparables.find_deal_by_tickers("AAA", "BBB")
    assert "outcome_reason" not in pool.sql[0]


@pytest.mark.asyncio
async def test_global_baseline_intervals_carry_filter(monkeypatch):
    pool = _patch_pool(monkeypatch, queries_timeline_stats)
    await queries_timeline_stats.get_global_baseline_intervals()
    assert len(pool.sql) == len(queries_timeline_stats._INTERVAL_PAIRS)
    for sql in pool.sql:
        _assert_filtered(sql)


@pytest.mark.asyncio
async def test_milestone_intervals_inherit_filter_from_comp_pks(monkeypatch):
    """Per-comp intervals are keyed by the (already filtered) comp pks."""
    pool = _patch_pool(monkeypatch, queries_timeline_stats)
    await queries_timeline_stats.get_milestone_intervals([1, 2])
    assert "outcome_reason" not in pool.sql[0]


@pytest.mark.asyncio
async def test_enforcement_stats_carry_filter(monkeypatch):
    pool = _patch_pool(monkeypatch, queries_climate)
    await queries_climate.get_enforcement_stats(months=24)
    _assert_filtered(pool.sql[0])


@pytest.mark.asyncio
async def test_fit_model_cohort_carries_filter():
    pool = _Pool()
    await fit_model.fetch_closed_deals(pool, cutoff=None)
    _assert_filtered(pool.sql[0])
    assert "d.timeline_days BETWEEN 5 AND 1000" in pool.sql[0]


@pytest.mark.asyncio
async def test_fit_model_cutoff_still_appends_after_filter():
    from datetime import date

    pool = _Pool()
    await fit_model.fetch_closed_deals(pool, cutoff=date(2024, 1, 1))
    sql = pool.sql[0]
    _assert_filtered(sql)
    assert sql.rstrip().endswith("AND d.date_announced < $1")


@pytest.mark.asyncio
async def test_calibration_report_rate_queries_carry_filter():
    pool = _Pool()
    await calibration_report.compute_hsr_stats(pool)
    await calibration_report.compute_ec_stats(pool)
    await calibration_report.compute_cma_stats(pool)
    await calibration_report.compute_samr_stats(pool)
    await calibration_report.compute_cfius_stats(pool)
    closed = [s for s in pool.sql if "deal_outcome = 'Closed'" in s]
    # HSR rate / by-sector / duration, EC rate, CMA rate, SAMR, CFIUS
    assert len(closed) == 7
    for sql in closed:
        # SAMR / CFIUS filter the numerator and the denominator subquery
        times = 2 if "COUNT(DISTINCT rr.deal_pk)" in sql else 1
        _assert_filtered(sql, times=times)
    # EC phase 1/2 and CMA durations read regulatory_reviews alone
    assert sum("outcome_reason" in s for s in pool.sql) == 7


@pytest.mark.asyncio
async def test_backtest_universe_carries_filter(monkeypatch):
    pool = _patch_pool(monkeypatch, backtest)
    await backtest.fetch_backtest_universe(lookback_years=2, max_deals=5)
    _assert_filtered(pool.sql[0])


def test_active_selection_is_not_filtered():
    """The Active book the agent predicts for keeps its own selection."""
    for rel in ("scripts/batch_run.py", "api/server.py",
                "pipeline/step0_validation.py"):
        src = (_REPO / rel).read_text(encoding="utf-8")
        assert "outcome_reason" not in src, rel
        assert "D2_COHORT_FILTER" not in src, rel
