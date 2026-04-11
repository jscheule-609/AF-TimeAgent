"""Comparable deal queries against the MARS database."""
from typing import Optional
from db.connection import get_pool


_REGULATORY_JOINS = """
LEFT JOIN deal_antitrust da ON d.deal_pk = da.deal_pk
LEFT JOIN deal_ec_antitrust ec ON d.deal_pk = ec.deal_pk
LEFT JOIN deal_samr_antitrust samr ON d.deal_pk = samr.deal_pk
LEFT JOIN deal_cma_antitrust cma ON d.deal_pk = cma.deal_pk
LEFT JOIN deal_cfius cfius ON d.deal_pk = cfius.deal_pk
LEFT JOIN deal_competitive_analysis dca ON d.deal_pk = dca.deal_pk
LEFT JOIN deal_dma_terms dma ON d.deal_pk = dma.deal_pk
LEFT JOIN deal_regulatory_efforts dre ON d.deal_pk = dre.deal_pk
"""

_REGULATORY_COLUMNS = """
    da.is_hsr_applicable, da.has_second_request, da.has_early_termination,
    da.hsr_filing_date, da.early_termination_date,
    da.second_request_date, da.second_request_clearance_date,
    ec.is_ec_approval_required, ec.ec_filing_date, ec.phase_1_cleared_date,
    ec.phase_2_date, ec.ec_final_clearance_date, ec.phase_1_outcome, ec.phase_2_outcome,
    samr.is_samr_approval_required, samr.samr_filing_date, samr.samr_clearance_date,
    samr.samr_clearance_phase,
    cma.is_cma_approval_required, cma.cma_filing_date,
    cma.cma_phase_1_outcome, cma.cma_phase_2_outcome,
    cfius.is_cfius_review_required,
    dca.product_market_overlap, dca.geographic_market_overlap,
    dca.combined_market_share_pct, dca.hhi_delta,
    dca.target_lists_acquirer_competitor, dca.acquirer_lists_target_competitor,
    dca.remedy_feasibility, dca.second_request_received,
    dma.long_stop_date AS outside_date,
    dma.extended_long_stop_date AS extended_outside_date,
    dre.efforts_standard, dre.divestiture_commitment, dre.litigation_commitment,
    dre.required_approvals
"""

_BASE_DEAL_COLUMNS = """
    d.deal_pk, d.deal_id, d.deal_status, d.date_announced,
    d.deal_value_usd, d.industry, d.type_of_consideration,
    d.gics_sector, d.deal_attitude,
    d.acquirer_country, d.target_country,
    d.timeline_days, d.actual_completion_date,
    d.date_expected_close_parsed, d.deal_outcome,
    pa.ticker as acquirer_ticker, pa.company_name as acquirer_name,
    pt.ticker as target_ticker, pt.company_name as target_name,
    pe_acq.party_type as acquirer_party_type
"""

_PARTY_ENTITY_JOINS = """
LEFT JOIN deal_parties dp_acq
    ON d.deal_pk = dp_acq.deal_pk AND dp_acq.role_type = 'acquirer'
LEFT JOIN party_entities pe_acq
    ON dp_acq.party_id = pe_acq.party_id
"""


async def find_deal_by_tickers(acquirer_ticker: str, target_ticker: str) -> Optional[dict]:
    """Find a deal in MARS by acquirer and target tickers."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            SELECT {_BASE_DEAL_COLUMNS}
            FROM deals d
            JOIN parties pa ON d.deal_pk = pa.deal_pk AND pa.role = 'acquirer'
            JOIN parties pt ON d.deal_pk = pt.deal_pk AND pt.role = 'target'
            {_PARTY_ENTITY_JOINS}
            WHERE pa.ticker = $1 AND pt.ticker = $2
            ORDER BY d.date_announced DESC LIMIT 1
            """,
            acquirer_ticker, target_ticker,
        )
        return dict(row) if row else None


async def get_acquirer_prior_deals(acquirer_name: str, limit: int = 15) -> list[dict]:
    """Get acquirer's prior completed deals with full regulatory enrichment."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT {_BASE_DEAL_COLUMNS}, {_REGULATORY_COLUMNS}
            FROM deals d
            JOIN parties pa ON d.deal_pk = pa.deal_pk AND pa.role = 'acquirer'
            JOIN parties pt ON d.deal_pk = pt.deal_pk AND pt.role = 'target'
            {_REGULATORY_JOINS}
            {_PARTY_ENTITY_JOINS}
            WHERE pa.company_name ILIKE '%' || $1 || '%'
              AND d.deal_outcome IN ('Closed', 'Terminated')
            ORDER BY d.date_announced DESC
            LIMIT $2
            """,
            acquirer_name, limit,
        )
        return [dict(r) for r in rows]


async def get_sector_comparable_deals(
    industry: str, lookback_years: int = 5, limit: int = 15
) -> list[dict]:
    """Get recent sector-matched deals with regulatory enrichment."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT {_BASE_DEAL_COLUMNS}, {_REGULATORY_COLUMNS}
            FROM deals d
            JOIN parties pa ON d.deal_pk = pa.deal_pk AND pa.role = 'acquirer'
            JOIN parties pt ON d.deal_pk = pt.deal_pk AND pt.role = 'target'
            {_REGULATORY_JOINS}
            {_PARTY_ENTITY_JOINS}
            WHERE d.industry = $1
              AND d.date_announced >= NOW() - ($2 || ' years')::interval
              AND d.deal_outcome IN ('Closed', 'Terminated')
            ORDER BY d.date_announced DESC
            LIMIT $3
            """,
            industry, str(lookback_years), limit,
        )
        return [dict(r) for r in rows]


async def get_size_matched_deals(
    deal_value_usd: float, lookback_years: int = 5, limit: int = 15
) -> list[dict]:
    """Get recent size-matched deals (0.33x to 3.0x deal value)."""
    pool = await get_pool()
    low = deal_value_usd * 0.33
    high = deal_value_usd * 3.0
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT {_BASE_DEAL_COLUMNS}, {_REGULATORY_COLUMNS}
            FROM deals d
            JOIN parties pa ON d.deal_pk = pa.deal_pk AND pa.role = 'acquirer'
            JOIN parties pt ON d.deal_pk = pt.deal_pk AND pt.role = 'target'
            {_REGULATORY_JOINS}
            {_PARTY_ENTITY_JOINS}
            WHERE d.deal_value_usd BETWEEN $1 AND $2
              AND d.date_announced >= NOW() - ($3 || ' years')::interval
              AND d.deal_outcome IN ('Closed', 'Terminated')
            ORDER BY d.date_announced DESC
            LIMIT $4
            """,
            low, high, str(lookback_years), limit,
        )
        return [dict(r) for r in rows]


async def get_regulatory_milestones(deal_pk: int) -> list[dict]:
    """Get all regulatory timeline milestones for a specific deal."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM regulatory_detail_events WHERE deal_pk = $1 ORDER BY event_date",
            deal_pk,
        )
        return [dict(r) for r in rows]


async def get_proxy_timeline_comparables(industry: str) -> list[dict]:
    """Get proxy/S-4 timeline data from comparable deals."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT dpt.*, d.timeline_days, d.date_announced, d.actual_completion_date
            FROM deal_proxy_timeline dpt
            JOIN deals d ON dpt.deal_pk = d.deal_pk
            WHERE d.industry = $1
              AND d.deal_outcome = 'Closed'
              AND d.date_announced >= NOW() - INTERVAL '3 years'
            ORDER BY d.date_announced DESC
            """,
            industry,
        )
        return [dict(r) for r in rows]
