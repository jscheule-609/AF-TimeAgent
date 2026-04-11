"""Calibration report generator for regulatory state machines.

Queries the MARS database for closed deals with populated regulatory
tables and computes observed base rates:

* HSR second request rate (overall + by GICS sector)
* EC Phase 2 opening rate
* CMA Phase 2 referral rate
* Duration percentiles for:
    - HSR clearance (filing → clearance)
    - EC Phase 1 (filing → Phase 1 cleared)
    - EC Phase 2 (Phase 2 open → final clearance)

The script is read-only and does not modify any schema.  It is intended
to provide input data for later calibration of the hardcoded transition
probabilities in ``state_machines/hsr.py``, ``ec.py``, and ``cma.py``.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

from db.connection import get_pool
from state_machines.hsr import HSRStateMachine
from state_machines.ec import ECStateMachine
from state_machines.cma import CMAStateMachine
from state_machines.samr import SAMRStateMachine
from state_machines.cfius import CFIUSStateMachine


@dataclass
class RateSummary:
    name: str
    observed: Optional[float]
    model_base: Optional[float]


@dataclass
class DurationSummary:
    name: str
    p50: Optional[float]
    p75: Optional[float]
    p90: Optional[float]


async def _fetch_scalar(pool, sql: str, *params) -> Optional[float]:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(sql, *params)
        if not row:
            return None
        val = row[0]
        return float(val) if val is not None else None


async def _fetch_rows(pool, sql: str, *params) -> List[Dict[str, Any]]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
        return [dict(r) for r in rows]


async def compute_hsr_stats(pool) -> Dict[str, Any]:
    """Compute HSR second request rates and durations using v1 tables."""
    # Overall second request rate where HSR is applicable and deal closed
    overall_rate_sql = """
        SELECT
            COUNT(*) FILTER (WHERE da.has_second_request) ::float
            / NULLIF(COUNT(*), 0) AS second_request_rate
        FROM deals d
        JOIN deal_antitrust da ON d.deal_pk = da.deal_pk
        WHERE d.deal_outcome = 'Closed'
          AND da.is_hsr_applicable = TRUE
    """
    overall_rate = await _fetch_scalar(pool, overall_rate_sql)

    # By GICS sector
    by_sector_sql = """
        SELECT
            d.gics_sector AS sector,
            COUNT(*) FILTER (WHERE da.has_second_request) ::float
                / NULLIF(COUNT(*), 0) AS second_request_rate
        FROM deals d
        JOIN deal_antitrust da ON d.deal_pk = da.deal_pk
        WHERE d.deal_outcome = 'Closed'
          AND da.is_hsr_applicable = TRUE
        GROUP BY d.gics_sector
        ORDER BY d.gics_sector
    """
    by_sector = await _fetch_rows(pool, by_sector_sql)

    # Clearance duration: filing → clearance (early termination or SR clearance)
    duration_sql = """
        SELECT
            percentile_cont(0.5) WITHIN GROUP (ORDER BY (clear_date - hsr_filing_date)) AS p50,
            percentile_cont(0.75) WITHIN GROUP (ORDER BY (clear_date - hsr_filing_date)) AS p75,
            percentile_cont(0.90) WITHIN GROUP (ORDER BY (clear_date - hsr_filing_date)) AS p90
        FROM (
            SELECT
                da.hsr_filing_date,
                COALESCE(da.second_request_clearance_date, da.early_termination_date) AS clear_date
            FROM deals d
            JOIN deal_antitrust da ON d.deal_pk = da.deal_pk
            WHERE d.deal_outcome = 'Closed'
              AND da.is_hsr_applicable = TRUE
              AND da.hsr_filing_date IS NOT NULL
              AND COALESCE(da.second_request_clearance_date, da.early_termination_date) IS NOT NULL
        ) sub
    """
    duration_row = await _fetch_rows(pool, duration_sql)
    dur = duration_row[0] if duration_row else {}

    # Model base probability: use base_2r default from state machine
    hsr_sm = HSRStateMachine()
    # The state machine uses comparable_stats["second_request_rate"] as base_2r;
    # default when missing is 0.095.
    model_base_rate = 0.095

    return {
        "rates": RateSummary(
            name="hsr_second_request_rate",
            observed=overall_rate,
            model_base=model_base_rate,
        ),
        "rates_by_sector": by_sector,
        "durations": DurationSummary(
            name="hsr_clearance_days_from_filing",
            p50=float(dur.get("p50")) if dur.get("p50") is not None else None,
            p75=float(dur.get("p75")) if dur.get("p75") is not None else None,
            p90=float(dur.get("p90")) if dur.get("p90") is not None else None,
        ),
    }


async def compute_ec_stats(pool) -> Dict[str, Any]:
    """Compute EC Phase 2 rates and durations using v1 tables."""
    # Phase 2 rate where EC approval is required and deal closed
    rate_sql = """
        SELECT
            COUNT(*) FILTER (WHERE ec.phase_2_date IS NOT NULL) ::float
            / NULLIF(COUNT(*), 0) AS phase_2_rate
        FROM deals d
        JOIN deal_ec_antitrust ec ON d.deal_pk = ec.deal_pk
        WHERE d.deal_outcome = 'Closed'
          AND ec.is_ec_approval_required = TRUE
    """
    phase2_rate = await _fetch_scalar(pool, rate_sql)

    # Phase 1 duration: filing → phase_1_cleared_date
    phase1_sql = """
        SELECT
            percentile_cont(0.5) WITHIN GROUP (ORDER BY (phase_1_cleared_date - ec_filing_date)) AS p50,
            percentile_cont(0.75) WITHIN GROUP (ORDER BY (phase_1_cleared_date - ec_filing_date)) AS p75,
            percentile_cont(0.90) WITHIN GROUP (ORDER BY (phase_1_cleared_date - ec_filing_date)) AS p90
        FROM deal_ec_antitrust
        WHERE ec_filing_date IS NOT NULL
          AND phase_1_cleared_date IS NOT NULL
    """
    p1_rows = await _fetch_rows(pool, phase1_sql)
    p1 = p1_rows[0] if p1_rows else {}

    # Phase 2 duration: phase_2_date → ec_final_clearance_date
    phase2_sql = """
        SELECT
            percentile_cont(0.5) WITHIN GROUP (ORDER BY (ec_final_clearance_date - phase_2_date)) AS p50,
            percentile_cont(0.75) WITHIN GROUP (ORDER BY (ec_final_clearance_date - phase_2_date)) AS p75,
            percentile_cont(0.90) WITHIN GROUP (ORDER BY (ec_final_clearance_date - phase_2_date)) AS p90
        FROM deal_ec_antitrust
        WHERE phase_2_date IS NOT NULL
          AND ec_final_clearance_date IS NOT NULL
    """
    p2_rows = await _fetch_rows(pool, phase2_sql)
    p2 = p2_rows[0] if p2_rows else {}

    # Model base Phase 2 rate default is 0.03 in ECStateMachine
    ec_sm = ECStateMachine()
    model_base_rate = 0.03

    return {
        "rates": RateSummary(
            name="ec_phase_2_rate",
            observed=phase2_rate,
            model_base=model_base_rate,
        ),
        "phase1_durations": DurationSummary(
            name="ec_phase1_days_from_filing",
            p50=float(p1.get("p50")) if p1.get("p50") is not None else None,
            p75=float(p1.get("p75")) if p1.get("p75") is not None else None,
            p90=float(p1.get("p90")) if p1.get("p90") is not None else None,
        ),
        "phase2_durations": DurationSummary(
            name="ec_phase2_days_from_phase2_open",
            p50=float(p2.get("p50")) if p2.get("p50") is not None else None,
            p75=float(p2.get("p75")) if p2.get("p75") is not None else None,
            p90=float(p2.get("p90")) if p2.get("p90") is not None else None,
        ),
    }


async def compute_cma_stats(pool) -> Dict[str, Any]:
    """Compute CMA Phase 2 referral rates using v1 tables."""
    rate_sql = """
        SELECT
            COUNT(*) FILTER (WHERE cma.cma_phase_2_outcome IS NOT NULL) ::float
            / NULLIF(COUNT(*), 0) AS phase_2_rate
        FROM deals d
        JOIN deal_cma_antitrust cma ON d.deal_pk = cma.deal_pk
        WHERE d.deal_outcome = 'Closed'
          AND cma.is_cma_approval_required = TRUE
    """
    phase2_rate = await _fetch_scalar(pool, rate_sql)

    cma_sm = CMAStateMachine()
    model_base_rate = 0.05

    return {
        "rates": RateSummary(
            name="cma_phase_2_rate",
            observed=phase2_rate,
            model_base=model_base_rate,
        ),
    }


async def compute_samr_stats(pool) -> Dict[str, Any]:
    """Compute SAMR approval applicability rates using v1 tables.

    The current schema does not expose detailed Phase 2/3 timing, so we
    focus on how often SAMR approval is required as a proxy for the
    baseline jurisdiction activation rate.
    """
    rate_sql = """
        SELECT
            COUNT(*) FILTER (WHERE samr.is_samr_approval_required) ::float
            / NULLIF(COUNT(*), 0) AS samr_applicable_rate
        FROM deals d
        JOIN deal_samr_antitrust samr ON d.deal_pk = samr.deal_pk
        WHERE d.deal_outcome = 'Closed'
    """
    applicable_rate = await _fetch_scalar(pool, rate_sql)

    samr_sm = SAMRStateMachine()

    return {
        "rates": RateSummary(
            name="samr_applicable_rate",
            observed=applicable_rate,
            model_base=None,
        ),
    }


async def compute_cfius_stats(pool) -> Dict[str, Any]:
    """Compute CFIUS review rates using v1 tables."""
    rate_sql = """
        SELECT
            COUNT(*) FILTER (WHERE cfius.is_cfius_review_required) ::float
            / NULLIF(COUNT(*), 0) AS cfius_review_rate
        FROM deals d
        JOIN deal_cfius cfius ON d.deal_pk = cfius.deal_pk
        WHERE d.deal_outcome = 'Closed'
    """
    review_rate = await _fetch_scalar(pool, rate_sql)

    cfius_sm = CFIUSStateMachine()

    return {
        "rates": RateSummary(
            name="cfius_review_rate",
            observed=review_rate,
            model_base=None,
        ),
    }


async def generate_report() -> Dict[str, Any]:
    pool = await get_pool()
    try:
        hsr = await compute_hsr_stats(pool)
        ec = await compute_ec_stats(pool)
        cma = await compute_cma_stats(pool)
        samr = await compute_samr_stats(pool)
        cfius = await compute_cfius_stats(pool)

        def _serialize(obj: Any) -> Any:
            if isinstance(obj, (RateSummary, DurationSummary)):
                return asdict(obj)
            return obj

        return {
            "hsr": {
                "rates": _serialize(hsr["rates"]),
                "rates_by_sector": hsr["rates_by_sector"],
                "durations": _serialize(hsr["durations"]),
            },
            "ec": {
                "rates": _serialize(ec["rates"]),
                "phase1_durations": _serialize(ec["phase1_durations"]),
                "phase2_durations": _serialize(ec["phase2_durations"]),
            },
            "cma": {
                "rates": _serialize(cma["rates"]),
            },
            "samr": {
                "rates": _serialize(samr["rates"]),
            },
            "cfius": {
                "rates": _serialize(cfius["rates"]),
            },
        }
    finally:
        from db.connection import close_pool  # type: ignore
        await close_pool()


def main() -> None:
    """Entry point for ``python -m scripts.calibration_report``."""
    report = asyncio.run(generate_report())
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
