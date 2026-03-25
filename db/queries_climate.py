"""Enforcement climate and trend queries against the MARS database."""
from db.connection import get_pool


async def get_enforcement_stats(months: int = 24) -> dict:
    """Get aggregate enforcement statistics for the last N months."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                COUNT(*) as total_deals,
                COUNT(*) FILTER (WHERE da.has_second_request = TRUE) as second_requests,
                COUNT(*) FILTER (WHERE da.has_early_termination = TRUE) as early_terminations,
                AVG(d.timeline_days) as avg_timeline,
                COUNT(*) FILTER (WHERE ec.phase_2_date IS NOT NULL) as ec_phase_2_count,
                COUNT(*) FILTER (WHERE ec.is_ec_approval_required = TRUE) as ec_total,
                COUNT(*) FILTER (WHERE cma.cma_phase_2_start_date IS NOT NULL) as cma_phase_2_count,
                COUNT(*) FILTER (WHERE cma.is_cma_approval_required = TRUE) as cma_total,
                COUNT(*) FILTER (WHERE dl.antitrust_litigation = TRUE) as litigation_count
            FROM deals d
            LEFT JOIN deal_antitrust da ON d.deal_pk = da.deal_pk
            LEFT JOIN deal_ec_antitrust ec ON d.deal_pk = ec.deal_pk
            LEFT JOIN deal_cma_antitrust cma ON d.deal_pk = cma.deal_pk
            LEFT JOIN deal_litigation dl ON d.deal_pk = dl.deal_pk
            WHERE d.date_announced >= NOW() - ($1 || ' months')::interval
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
            SELECT d.industry, d.gics_sector,
                COUNT(*) FILTER (WHERE dl.antitrust_litigation = TRUE) as litigation_count,
                COUNT(*) FILTER (
                    WHERE d.deal_outcome = 'Terminated'
                    AND d.termination_reason ILIKE '%regulat%'
                ) as regulatory_breaks
            FROM deals d
            LEFT JOIN deal_litigation dl ON d.deal_pk = dl.deal_pk
            WHERE d.date_announced >= NOW() - INTERVAL '36 months'
            GROUP BY d.industry, d.gics_sector
            """
        )
        return [dict(r) for r in rows]
