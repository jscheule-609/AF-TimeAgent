"""Regulatory detail queries against the MARS database."""
from typing import Optional
from db.connection import get_pool


async def get_deal_antitrust(deal_pk: int) -> Optional[dict]:
    """Get HSR antitrust data for a deal."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM deal_antitrust WHERE deal_pk = $1", deal_pk
        )
        return dict(row) if row else None


async def get_deal_ec_antitrust(deal_pk: int) -> Optional[dict]:
    """Get EC antitrust data for a deal."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM deal_ec_antitrust WHERE deal_pk = $1", deal_pk
        )
        return dict(row) if row else None


async def get_deal_cma_antitrust(deal_pk: int) -> Optional[dict]:
    """Get CMA antitrust data for a deal."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM deal_cma_antitrust WHERE deal_pk = $1", deal_pk
        )
        return dict(row) if row else None


async def get_deal_samr_antitrust(deal_pk: int) -> Optional[dict]:
    """Get SAMR antitrust data for a deal."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM deal_samr_antitrust WHERE deal_pk = $1", deal_pk
        )
        return dict(row) if row else None


async def get_deal_cfius(deal_pk: int) -> Optional[dict]:
    """Get CFIUS review data for a deal."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM deal_cfius WHERE deal_pk = $1", deal_pk
        )
        return dict(row) if row else None


async def get_deal_competitive_analysis(deal_pk: int) -> Optional[dict]:
    """Get competitive analysis data for a deal."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM deal_competitive_analysis WHERE deal_pk = $1", deal_pk
        )
        return dict(row) if row else None


async def get_deal_regulatory_efforts(deal_pk: int) -> Optional[dict]:
    """Get regulatory efforts provisions from merger agreement."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM deal_regulatory_efforts WHERE deal_pk = $1", deal_pk
        )
        return dict(row) if row else None
