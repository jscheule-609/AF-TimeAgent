"""Milestone interval queries for empirical timeline calibration.

Given a set of comparable deal PKs, returns raw milestone-to-milestone
intervals from ``deal_milestones``.  The caller (step3b) handles
similarity-weighted percentile computation.
"""
from typing import Optional
from db.connection import get_pool


# Each pair is (from_milestone, to_milestone, interval_name)
_INTERVAL_PAIRS = [
    ("announcement", "antitrust_filing", "announcement_to_filing"),
    ("antitrust_filing", "antitrust_clearance", "filing_to_clearance"),
    ("announcement", "shareholder_vote", "announcement_to_vote"),
    ("shareholder_vote", "closing", "vote_to_close"),
    ("antitrust_clearance", "closing", "clearance_to_close"),
    ("announcement", "closing", "announcement_to_close"),
    ("announcement", "proxy_filed", "announcement_to_proxy"),
    ("announcement", "s4_filing", "announcement_to_s4"),
]


async def get_milestone_intervals(
    deal_pks: list[int],
) -> list[dict]:
    """Return raw milestone intervals for the given deals.

    Each row: {"deal_pk": int, "interval": str, "days": int}

    Only returns intervals where both milestones have actual
    dates and the gap is 0-365 days (filters outliers).
    """
    if not deal_pks:
        return []

    pool = await get_pool()

    # Build UNION ALL query for all interval pairs
    parts = []
    for from_ms, to_ms, name in _INTERVAL_PAIRS:
        parts.append(f"""
            SELECT m1.deal_pk,
                   '{name}' AS interval,
                   (m2.actual_date - m1.actual_date) AS days
            FROM deal_milestones m1
            JOIN deal_milestones m2
                ON m1.deal_pk = m2.deal_pk
            WHERE m1.milestone_type = '{from_ms}'
              AND m2.milestone_type = '{to_ms}'
              AND m1.actual_date IS NOT NULL
              AND m2.actual_date IS NOT NULL
              AND m1.deal_pk = ANY($1::bigint[])
              AND (m2.actual_date - m1.actual_date)
                  BETWEEN 0 AND 365
        """)

    sql = "\nUNION ALL\n".join(parts)

    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, deal_pks)

    return [
        {
            "deal_pk": r["deal_pk"],
            "interval": r["interval"],
            "days": int(r["days"]),
        }
        for r in rows
    ]


async def get_deal_observed_milestones(
    deal_pk: int,
) -> dict:
    """Realized milestone dates for ONE deal (mid-deal updates).

    Returns {milestone_type: date} for the milestone types the
    close-distribution conditions on.  First filing starts the
    regulatory clock; the LAST clearance/vote governs closing,
    hence min/max respectively.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT milestone_type,
                   CASE WHEN milestone_type = 'antitrust_filing'
                        THEN MIN(actual_date)
                        ELSE MAX(actual_date)
                   END AS actual_date
            FROM deal_milestones
            WHERE deal_pk = $1
              AND actual_date IS NOT NULL
              AND milestone_type IN (
                  'antitrust_filing',
                  'antitrust_clearance',
                  'shareholder_vote'
              )
            GROUP BY milestone_type
            """,
            deal_pk,
        )
    return {
        r["milestone_type"]: r["actual_date"] for r in rows
    }


async def get_global_baseline_intervals() -> list[dict]:
    """Return milestone intervals across ALL closed deals.

    Used as fallback when comparable sample is too small.
    Returns pre-aggregated percentiles (not raw rows) for
    efficiency.
    """
    pool = await get_pool()
    results = []

    async with pool.acquire() as conn:
        for from_ms, to_ms, name in _INTERVAL_PAIRS:
            row = await conn.fetchrow(
                f"""
                SELECT
                    percentile_cont(0.50) WITHIN GROUP (
                        ORDER BY (m2.actual_date - m1.actual_date)
                    ) AS p50,
                    percentile_cont(0.75) WITHIN GROUP (
                        ORDER BY (m2.actual_date - m1.actual_date)
                    ) AS p75,
                    percentile_cont(0.90) WITHIN GROUP (
                        ORDER BY (m2.actual_date - m1.actual_date)
                    ) AS p90,
                    COUNT(*) AS n
                FROM deal_milestones m1
                JOIN deal_milestones m2
                    ON m1.deal_pk = m2.deal_pk
                JOIN deals d ON m1.deal_pk = d.deal_pk
                WHERE m1.milestone_type = '{from_ms}'
                  AND m2.milestone_type = '{to_ms}'
                  AND m1.actual_date IS NOT NULL
                  AND m2.actual_date IS NOT NULL
                  AND d.deal_outcome = 'Closed'
                  AND (m2.actual_date - m1.actual_date)
                      BETWEEN 0 AND 365
                """,
            )
            if row and row["n"] and row["n"] > 0:
                results.append({
                    "interval": name,
                    "p50": float(row["p50"]) if row["p50"] else None,
                    "p75": float(row["p75"]) if row["p75"] else None,
                    "p90": float(row["p90"]) if row["p90"] else None,
                    "n": int(row["n"]),
                })

    return results
