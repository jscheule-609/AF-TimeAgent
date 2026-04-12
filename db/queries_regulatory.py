"""Regulatory detail queries against the MARS v2 database.

All jurisdiction-specific antitrust tables (deal_antitrust, deal_ec_antitrust,
deal_cma_antitrust, deal_samr_antitrust, deal_cfius) are consolidated into
a single ``regulatory_reviews`` table with a ``jurisdiction_code`` column.
"""
from typing import Optional
from db.connection import get_pool


async def _get_regulatory_review(
    deal_pk: int, jurisdiction_code: str,
) -> Optional[dict]:
    """Fetch a single regulatory review row by jurisdiction."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM regulatory_reviews "
            "WHERE deal_pk = $1 AND jurisdiction_code = $2",
            deal_pk, jurisdiction_code,
        )
        return dict(row) if row else None


async def get_deal_antitrust(deal_pk: int) -> Optional[dict]:
    """Get HSR antitrust data for a deal (v2: regulatory_reviews, US)."""
    return await _get_regulatory_review(deal_pk, "US")


async def get_deal_ec_antitrust(deal_pk: int) -> Optional[dict]:
    """Get EC antitrust data for a deal (v2: regulatory_reviews, EU)."""
    return await _get_regulatory_review(deal_pk, "EU")


async def get_deal_cma_antitrust(deal_pk: int) -> Optional[dict]:
    """Get CMA antitrust data for a deal (v2: regulatory_reviews, GB)."""
    return await _get_regulatory_review(deal_pk, "GB")


async def get_deal_samr_antitrust(deal_pk: int) -> Optional[dict]:
    """Get SAMR antitrust data for a deal (v2: regulatory_reviews, CN)."""
    return await _get_regulatory_review(deal_pk, "CN")


async def get_deal_cfius(deal_pk: int) -> Optional[dict]:
    """Get CFIUS review data for a deal (v2: regulatory_reviews, CFIUS)."""
    return await _get_regulatory_review(deal_pk, "CFIUS")


async def get_deal_competitive_analysis(deal_pk: int) -> Optional[dict]:
    """Get competitive analysis data for a deal (v2 redesigned table)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM deal_competitive_analysis WHERE deal_pk = $1",
            deal_pk,
        )
        return dict(row) if row else None


async def get_deal_regulatory_efforts(deal_pk: int) -> Optional[dict]:
    """Get regulatory efforts provisions (v2: deal_protections + deal_conditions).

    Returns a dict shaped like the old deal_regulatory_efforts row for
    backward compatibility with callers.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Efforts standard and divestiture from deal_protections
        prot = await conn.fetchrow(
            """SELECT efforts_standard, divestiture_cap,
                      hell_or_high_water
               FROM deal_protections WHERE deal_pk = $1""",
            deal_pk,
        )

        # Required approvals from deal_conditions
        cond_rows = await conn.fetch(
            """SELECT condition_name
               FROM deal_conditions
               WHERE deal_pk = $1
                 AND condition_family IN ('antitrust', 'foreign_investment')""",
            deal_pk,
        )

    if not prot and not cond_rows:
        return None

    efforts_standard = (prot["efforts_standard"] if prot else None) or "unknown"
    divestiture_cap = (prot["divestiture_cap"] if prot else None)
    hell_or_high_water = bool(prot["hell_or_high_water"]) if prot else False

    required_approvals = [r["condition_name"] for r in cond_rows] if cond_rows else []

    return {
        "deal_pk": deal_pk,
        "efforts_standard": efforts_standard,
        "required_approvals": required_approvals,
        "divestiture_commitment": divestiture_cap is not None,
        "divestiture_cap": divestiture_cap,
        "litigation_commitment": hell_or_high_water,
    }
