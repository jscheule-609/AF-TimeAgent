"""Calibration report generator for regulatory state machines.

Queries the MARS v2 database for closed deals with populated regulatory
reviews and computes observed base rates:

* HSR second request rate (overall + by GICS sector)
* EC Phase 2 opening rate
* CMA Phase 2 referral rate
* SAMR / CFIUS activation rates
* Duration percentiles for HSR clearance, EC Phase 1/Phase 2

The script is read-only and does not modify any schema.  It is intended
to provide input data for calibration of the hardcoded transition
probabilities in ``state_machines/hsr.py``, ``ec.py``, and ``cma.py``.

v2 migration (2026-04-12): all queries rewritten against
``regulatory_reviews`` with ``jurisdiction_code`` filter.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from db.connection import get_pool


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


async def _fetch_rows(
    pool, sql: str, *params,
) -> List[Dict[str, Any]]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
        return [dict(r) for r in rows]


async def compute_hsr_stats(pool) -> Dict[str, Any]:
    """Compute HSR second request rates and durations."""
    overall_rate_sql = """
        SELECT
            COUNT(*) FILTER (
                WHERE rr.phase_2_start_date IS NOT NULL
            )::float
            / NULLIF(COUNT(*), 0) AS second_request_rate
        FROM deals d
        JOIN regulatory_reviews rr
            ON d.deal_pk = rr.deal_pk
            AND rr.jurisdiction_code = 'US'
            AND rr.review_status != 'not_filed'
        WHERE d.deal_outcome = 'Closed'
    """
    overall_rate = await _fetch_scalar(pool, overall_rate_sql)

    by_sector_sql = """
        SELECT
            d.gics_sector AS sector,
            COUNT(*) FILTER (
                WHERE rr.phase_2_start_date IS NOT NULL
            )::float
            / NULLIF(COUNT(*), 0) AS second_request_rate
        FROM deals d
        JOIN regulatory_reviews rr
            ON d.deal_pk = rr.deal_pk
            AND rr.jurisdiction_code = 'US'
            AND rr.review_status != 'not_filed'
        WHERE d.deal_outcome = 'Closed'
        GROUP BY d.gics_sector
        ORDER BY d.gics_sector
    """
    by_sector = await _fetch_rows(pool, by_sector_sql)

    duration_sql = """
        SELECT
            percentile_cont(0.5)  WITHIN GROUP (
                ORDER BY (clear_date - filing_date)
            ) AS p50,
            percentile_cont(0.75) WITHIN GROUP (
                ORDER BY (clear_date - filing_date)
            ) AS p75,
            percentile_cont(0.90) WITHIN GROUP (
                ORDER BY (clear_date - filing_date)
            ) AS p90
        FROM (
            SELECT
                rr.filing_date,
                rr.clearance_date AS clear_date
            FROM deals d
            JOIN regulatory_reviews rr
                ON d.deal_pk = rr.deal_pk
                AND rr.jurisdiction_code = 'US'
                AND rr.review_status != 'not_filed'
            WHERE d.deal_outcome = 'Closed'
              AND rr.filing_date IS NOT NULL
              AND rr.clearance_date IS NOT NULL
        ) sub
    """
    duration_row = await _fetch_rows(pool, duration_sql)
    dur = duration_row[0] if duration_row else {}

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
            p50=_to_float(dur.get("p50")),
            p75=_to_float(dur.get("p75")),
            p90=_to_float(dur.get("p90")),
        ),
    }


async def compute_ec_stats(pool) -> Dict[str, Any]:
    """Compute EC Phase 2 rates and durations."""
    rate_sql = """
        SELECT
            COUNT(*) FILTER (
                WHERE rr.phase_2_start_date IS NOT NULL
            )::float
            / NULLIF(COUNT(*), 0) AS phase_2_rate
        FROM deals d
        JOIN regulatory_reviews rr
            ON d.deal_pk = rr.deal_pk
            AND rr.jurisdiction_code = 'EU'
            AND rr.review_status != 'not_filed'
        WHERE d.deal_outcome = 'Closed'
    """
    phase2_rate = await _fetch_scalar(pool, rate_sql)

    phase1_sql = """
        SELECT
            percentile_cont(0.5)  WITHIN GROUP (
                ORDER BY (rr.clearance_date - rr.filing_date)
            ) AS p50,
            percentile_cont(0.75) WITHIN GROUP (
                ORDER BY (rr.clearance_date - rr.filing_date)
            ) AS p75,
            percentile_cont(0.90) WITHIN GROUP (
                ORDER BY (rr.clearance_date - rr.filing_date)
            ) AS p90
        FROM regulatory_reviews rr
        WHERE rr.jurisdiction_code = 'EU'
          AND rr.review_status != 'not_filed'
          AND rr.filing_date IS NOT NULL
          AND rr.clearance_date IS NOT NULL
          AND rr.phase_2_start_date IS NULL
    """
    p1_rows = await _fetch_rows(pool, phase1_sql)
    p1 = p1_rows[0] if p1_rows else {}

    phase2_sql = """
        SELECT
            percentile_cont(0.5)  WITHIN GROUP (
                ORDER BY (rr.clearance_date - rr.phase_2_start_date)
            ) AS p50,
            percentile_cont(0.75) WITHIN GROUP (
                ORDER BY (rr.clearance_date - rr.phase_2_start_date)
            ) AS p75,
            percentile_cont(0.90) WITHIN GROUP (
                ORDER BY (rr.clearance_date - rr.phase_2_start_date)
            ) AS p90
        FROM regulatory_reviews rr
        WHERE rr.jurisdiction_code = 'EU'
          AND rr.review_status != 'not_filed'
          AND rr.phase_2_start_date IS NOT NULL
          AND rr.clearance_date IS NOT NULL
    """
    p2_rows = await _fetch_rows(pool, phase2_sql)
    p2 = p2_rows[0] if p2_rows else {}

    model_base_rate = 0.03

    return {
        "rates": RateSummary(
            name="ec_phase_2_rate",
            observed=phase2_rate,
            model_base=model_base_rate,
        ),
        "phase1_durations": DurationSummary(
            name="ec_phase1_days_from_filing",
            p50=_to_float(p1.get("p50")),
            p75=_to_float(p1.get("p75")),
            p90=_to_float(p1.get("p90")),
        ),
        "phase2_durations": DurationSummary(
            name="ec_phase2_days_from_phase2_open",
            p50=_to_float(p2.get("p50")),
            p75=_to_float(p2.get("p75")),
            p90=_to_float(p2.get("p90")),
        ),
    }


async def compute_cma_stats(pool) -> Dict[str, Any]:
    """Compute CMA Phase 2 referral rates and durations."""
    rate_sql = """
        SELECT
            COUNT(*) FILTER (
                WHERE rr.phase_2_outcome IS NOT NULL
            )::float
            / NULLIF(COUNT(*), 0) AS phase_2_rate
        FROM deals d
        JOIN regulatory_reviews rr
            ON d.deal_pk = rr.deal_pk
            AND rr.jurisdiction_code = 'GB'
            AND rr.review_status != 'not_filed'
        WHERE d.deal_outcome = 'Closed'
    """
    phase2_rate = await _fetch_scalar(pool, rate_sql)

    duration_sql = """
        SELECT
            percentile_cont(0.5)  WITHIN GROUP (
                ORDER BY (rr.clearance_date - rr.filing_date)
            ) AS p50,
            percentile_cont(0.75) WITHIN GROUP (
                ORDER BY (rr.clearance_date - rr.filing_date)
            ) AS p75,
            percentile_cont(0.90) WITHIN GROUP (
                ORDER BY (rr.clearance_date - rr.filing_date)
            ) AS p90
        FROM regulatory_reviews rr
        WHERE rr.jurisdiction_code = 'GB'
          AND rr.review_status != 'not_filed'
          AND rr.filing_date IS NOT NULL
          AND rr.clearance_date IS NOT NULL
    """
    dur_rows = await _fetch_rows(pool, duration_sql)
    dur = dur_rows[0] if dur_rows else {}

    model_base_rate = 0.05

    return {
        "rates": RateSummary(
            name="cma_phase_2_rate",
            observed=phase2_rate,
            model_base=model_base_rate,
        ),
        "durations": DurationSummary(
            name="cma_clearance_days_from_filing",
            p50=_to_float(dur.get("p50")),
            p75=_to_float(dur.get("p75")),
            p90=_to_float(dur.get("p90")),
        ),
    }


async def compute_samr_stats(pool) -> Dict[str, Any]:
    """Compute SAMR activation rate (how often SAMR review was filed)."""
    rate_sql = """
        SELECT
            COUNT(DISTINCT rr.deal_pk)::float
            / NULLIF(
                (SELECT COUNT(*) FROM deals
                 WHERE deal_outcome = 'Closed'), 0
            ) AS samr_applicable_rate
        FROM regulatory_reviews rr
        JOIN deals d ON rr.deal_pk = d.deal_pk
        WHERE rr.jurisdiction_code = 'CN'
          AND rr.review_status != 'not_filed'
          AND d.deal_outcome = 'Closed'
    """
    applicable_rate = await _fetch_scalar(pool, rate_sql)

    return {
        "rates": RateSummary(
            name="samr_applicable_rate",
            observed=applicable_rate,
            model_base=None,
        ),
    }


async def compute_cfius_stats(pool) -> Dict[str, Any]:
    """Compute CFIUS review activation rate."""
    rate_sql = """
        SELECT
            COUNT(DISTINCT rr.deal_pk)::float
            / NULLIF(
                (SELECT COUNT(*) FROM deals
                 WHERE deal_outcome = 'Closed'), 0
            ) AS cfius_review_rate
        FROM regulatory_reviews rr
        JOIN deals d ON rr.deal_pk = d.deal_pk
        WHERE rr.jurisdiction_code = 'CFIUS'
          AND rr.review_status != 'not_filed'
          AND d.deal_outcome = 'Closed'
    """
    review_rate = await _fetch_scalar(pool, rate_sql)

    return {
        "rates": RateSummary(
            name="cfius_review_rate",
            observed=review_rate,
            model_base=None,
        ),
    }


def _to_float(val: Any) -> Optional[float]:
    return float(val) if val is not None else None


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
                "phase1_durations": _serialize(
                    ec["phase1_durations"]
                ),
                "phase2_durations": _serialize(
                    ec["phase2_durations"]
                ),
            },
            "cma": {
                "rates": _serialize(cma["rates"]),
                "durations": _serialize(
                    cma["durations"]
                ),
            },
            "samr": {
                "rates": _serialize(samr["rates"]),
            },
            "cfius": {
                "rates": _serialize(cfius["rates"]),
            },
        }
    finally:
        from db.connection import close_pool
        await close_pool()


def main() -> None:
    """Entry point for ``python -m scripts.calibration_report``."""
    parser = argparse.ArgumentParser(
        description="Generate calibration report from MARS v2",
    )
    parser.add_argument(
        "--output", "-o",
        type=str,
        default=None,
        help="Write JSON report to file instead of stdout",
    )
    args = parser.parse_args()

    report = asyncio.run(generate_report())
    report_json = json.dumps(report, indent=2, default=str)

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(report_json)
        print(f"Calibration report written to {out_path}")
    else:
        print(report_json)


if __name__ == "__main__":
    main()
