"""Comparable deal queries against the MARS v2 database.

All jurisdiction-specific antitrust tables are consolidated into
``regulatory_reviews`` with a ``jurisdiction_code`` column.  Column
aliases preserve the v1 names consumed by ``_row_to_comparable()``
in ``pipeline/step3_comparables.py`` to minimize downstream changes.
"""
from typing import Optional
from db.connection import get_pool


# ── v2 JOINs ────────────────────────────────────────────────
_REGULATORY_JOINS = """
LEFT JOIN regulatory_reviews rr_us
    ON d.deal_pk = rr_us.deal_pk
    AND rr_us.jurisdiction_code = 'US'
    AND rr_us.review_status != 'not_filed'
LEFT JOIN regulatory_reviews rr_eu
    ON d.deal_pk = rr_eu.deal_pk
    AND rr_eu.jurisdiction_code = 'EU'
    AND rr_eu.review_status != 'not_filed'
LEFT JOIN regulatory_reviews rr_cn
    ON d.deal_pk = rr_cn.deal_pk
    AND rr_cn.jurisdiction_code = 'CN'
    AND rr_cn.review_status != 'not_filed'
LEFT JOIN regulatory_reviews rr_gb
    ON d.deal_pk = rr_gb.deal_pk
    AND rr_gb.jurisdiction_code = 'GB'
    AND rr_gb.review_status != 'not_filed'
LEFT JOIN regulatory_reviews rr_cfius
    ON d.deal_pk = rr_cfius.deal_pk
    AND rr_cfius.jurisdiction_code = 'CFIUS'
    AND rr_cfius.review_status != 'not_filed'
LEFT JOIN deal_competitive_analysis dca ON d.deal_pk = dca.deal_pk
-- OQ-N20: v1 deal_dma_terms.long_stop_date/extended -> v2 odm.outside_date/extended
LEFT JOIN deal_outside_date_mechanics dma ON d.deal_pk = dma.deal_pk
LEFT JOIN deal_protections dp ON d.deal_pk = dp.deal_pk
"""

# Aliases map v2 columns → v1 column names so _row_to_comparable() works unchanged
_REGULATORY_COLUMNS = """
    (rr_us.review_id IS NOT NULL)               AS is_hsr_applicable,
    (rr_us.phase_2_start_date IS NOT NULL)       AS has_second_request,
    (rr_us.phase_1_outcome = 'cleared'
     AND rr_us.phase_2_start_date IS NULL)       AS has_early_termination,
    rr_us.filing_date                            AS hsr_filing_date,
    CASE WHEN rr_us.phase_1_outcome = 'cleared'
              AND rr_us.phase_2_start_date IS NULL
         THEN rr_us.clearance_date END           AS early_termination_date,
    rr_us.phase_2_start_date                     AS second_request_date,
    CASE WHEN rr_us.phase_2_start_date IS NOT NULL
         THEN rr_us.clearance_date END           AS second_request_clearance_date,

    (rr_eu.review_id IS NOT NULL)                AS is_ec_approval_required,
    rr_eu.filing_date                            AS ec_filing_date,
    CASE WHEN rr_eu.phase_1_outcome IS NOT NULL
              AND rr_eu.phase_2_start_date IS NULL
         THEN rr_eu.clearance_date END           AS phase_1_cleared_date,
    rr_eu.phase_2_start_date                     AS phase_2_date,
    rr_eu.clearance_date                         AS ec_final_clearance_date,
    rr_eu.phase_1_outcome,
    rr_eu.phase_2_outcome,

    (rr_cn.review_id IS NOT NULL)                AS is_samr_approval_required,
    rr_cn.filing_date                            AS samr_filing_date,
    rr_cn.clearance_date                         AS samr_clearance_date,
    rr_cn.review_status                          AS samr_clearance_phase,

    (rr_gb.review_id IS NOT NULL)                AS is_cma_approval_required,
    rr_gb.filing_date                            AS cma_filing_date,
    rr_gb.phase_1_outcome                        AS cma_phase_1_outcome,
    rr_gb.phase_2_outcome                        AS cma_phase_2_outcome,

    (rr_cfius.review_id IS NOT NULL)             AS is_cfius_review_required,

    dca.product_market_overlap,
    dca.geographic_market_overlap,
    dca.combined_market_share_pct,
    dca.hhi_delta,
    dca.antitrust_risk_rating,
    dca.remedy_feasibility,

    dma.outside_date                             AS outside_date,
    dma.extended_outside_date                    AS extended_outside_date,

    dp.efforts_standard,
    (dp.divestiture_cap IS NOT NULL)             AS divestiture_commitment,
    (dp.efforts_standard ILIKE '%hell%high%water%') AS litigation_commitment
"""

_BASE_DEAL_COLUMNS = """
    d.deal_pk, d.deal_id, d.deal_status, d.date_announced,
    d.deal_value_usd, d.industry, d.type_of_consideration,
    d.gics_sector, d.deal_attitude,
    pe_acq.domicile_country AS acquirer_country,
    pe_tgt.domicile_country AS target_country,
    d.timeline_days, d.actual_completion_date,
    d.date_expected_close_parsed, d.deal_outcome,
    pe_acq.ticker as acquirer_ticker,
    COALESCE(pe_acq.short_name, pe_acq.legal_name) as acquirer_name,
    pe_tgt.ticker as target_ticker,
    COALESCE(pe_tgt.short_name, pe_tgt.legal_name) as target_name,
    pe_acq.party_type as acquirer_party_type
"""

_PARTY_ENTITY_JOINS = """
LEFT JOIN deal_parties dp_acq
    ON d.deal_pk = dp_acq.deal_pk
    AND dp_acq.role_type = 'acquirer'
LEFT JOIN party_entities pe_acq
    ON dp_acq.party_id = pe_acq.party_id
LEFT JOIN deal_parties dp_tgt
    ON d.deal_pk = dp_tgt.deal_pk
    AND dp_tgt.role_type = 'target'
LEFT JOIN party_entities pe_tgt
    ON dp_tgt.party_id = pe_tgt.party_id
"""


async def find_deal_by_tickers(acquirer_ticker: str, target_ticker: str) -> Optional[dict]:
    """Find a deal in MARS by acquirer and target tickers."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            SELECT {_BASE_DEAL_COLUMNS}
            FROM deals d
            {_PARTY_ENTITY_JOINS}
            WHERE pe_acq.ticker = $1 AND pe_tgt.ticker = $2
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
            {_REGULATORY_JOINS}
            {_PARTY_ENTITY_JOINS}
            WHERE COALESCE(pe_acq.short_name, pe_acq.legal_name) ILIKE '%' || $1 || '%'
              AND d.deal_outcome IN ('Closed', 'Terminated')
            ORDER BY d.date_announced DESC
            LIMIT $2
            """,
            acquirer_name, limit,
        )
        return [dict(r) for r in rows]


async def get_target_prior_deals(
    target_name: str, limit: int = 15,
) -> list[dict]:
    """Get prior deals involving this target company.

    Matches on target company name across closed/terminated
    deals. Returns the same column shape as acquirer history
    for consistent scoring.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT {_BASE_DEAL_COLUMNS}, {_REGULATORY_COLUMNS}
            FROM deals d
            {_REGULATORY_JOINS}
            {_PARTY_ENTITY_JOINS}
            WHERE COALESCE(pe_tgt.short_name, pe_tgt.legal_name) ILIKE '%' || $1 || '%'
              AND d.deal_outcome IN ('Closed', 'Terminated')
            ORDER BY d.date_announced DESC
            LIMIT $2
            """,
            target_name, limit,
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
    """Get all regulatory timeline milestones for a specific deal.

    v2: regulatory_review_events is linked via review_id, not deal_pk.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT rre.*
            FROM regulatory_review_events rre
            JOIN regulatory_reviews rr ON rre.review_id = rr.review_id
            WHERE rr.deal_pk = $1
            ORDER BY rre.event_date
            """,
            deal_pk,
        )
        return [dict(r) for r in rows]


# get_proxy_timeline_comparables() removed 2026-09-12: it read
# deal_timeline_actuals, which AF-AJ migration 021 dropped (never populated),
# and had no callers. Comparable timeline data lives in deal_milestones;
# see get_comparable_regulatory_events() for the v2 pattern.

