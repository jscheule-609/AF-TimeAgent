"""Enforcement climate and trend queries against the MARS v2 database."""
from db.connection import get_pool


async def get_enforcement_stats(months: int = 24) -> dict:
    """Get aggregate enforcement statistics for the last N months.

    v2: uses regulatory_reviews with jurisdiction_code filters
    instead of deal_antitrust / deal_ec_antitrust / deal_cma_antitrust.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                COUNT(*) AS total_deals,
                COUNT(*) FILTER (
                    WHERE rr_us.phase_2_start_date IS NOT NULL
                ) AS second_requests,
                COUNT(*) FILTER (
                    WHERE rr_us.review_id IS NOT NULL
                    AND rr_us.phase_1_outcome = 'cleared'
                    AND rr_us.phase_2_start_date IS NULL
                ) AS early_terminations,
                AVG(d.timeline_days) AS avg_timeline,
                COUNT(*) FILTER (
                    WHERE rr_eu.phase_2_start_date IS NOT NULL
                ) AS ec_phase_2_count,
                COUNT(*) FILTER (
                    WHERE rr_eu.review_id IS NOT NULL
                ) AS ec_total,
                COUNT(*) FILTER (
                    WHERE rr_gb.phase_2_start_date IS NOT NULL
                ) AS cma_phase_2_count,
                COUNT(*) FILTER (
                    WHERE rr_gb.review_id IS NOT NULL
                ) AS cma_total,
                COUNT(*) FILTER (
                    WHERE dl.litigation_type = 'antitrust'
                ) AS litigation_count
            FROM deals d
            LEFT JOIN regulatory_reviews rr_us
                ON d.deal_pk = rr_us.deal_pk
                AND rr_us.jurisdiction_code = 'US'
                AND rr_us.review_status != 'not_filed'
            LEFT JOIN regulatory_reviews rr_eu
                ON d.deal_pk = rr_eu.deal_pk
                AND rr_eu.jurisdiction_code = 'EU'
                AND rr_eu.review_status != 'not_filed'
            LEFT JOIN regulatory_reviews rr_gb
                ON d.deal_pk = rr_gb.deal_pk
                AND rr_gb.jurisdiction_code = 'GB'
                AND rr_gb.review_status != 'not_filed'
            LEFT JOIN deal_litigation dl
                ON d.deal_pk = dl.deal_pk
            WHERE d.date_announced >= NOW()
                - ($1 || ' months')::interval
              AND d.deal_outcome IN ('Closed', 'Terminated')
            """,
            str(months),
        )
        return dict(row) if row else {}


async def get_sector_enforcement_intensity() -> list[dict]:
    """Get sector-level enforcement intensity over last 36 months."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                d.industry, d.gics_sector,
                COUNT(*) FILTER (
                    WHERE dl.litigation_type = 'antitrust'
                ) AS litigation_count,
                COUNT(*) FILTER (
                    WHERE d.deal_outcome = 'Terminated'
                    AND d.termination_reason
                        ILIKE '%regulat%'
                ) AS regulatory_breaks
            FROM deals d
            LEFT JOIN deal_litigation dl
                ON d.deal_pk = dl.deal_pk
            WHERE d.date_announced
                >= NOW() - INTERVAL '36 months'
            GROUP BY d.industry, d.gics_sector
            """
        )
        return [dict(r) for r in rows]
