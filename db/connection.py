"""asyncpg connection pool to MARS database."""
import asyncpg
from typing import Optional

_pool: Optional[asyncpg.Pool] = None


async def get_pool() -> asyncpg.Pool:
    """Get or create the connection pool."""
    global _pool
    if _pool is None:
        from config.settings import Settings
        settings = Settings()
        _pool = await asyncpg.create_pool(
            host=settings.mars_db_host,
            port=settings.mars_db_port,
            database=settings.mars_db_name,
            user=settings.mars_db_user,
            password=settings.mars_db_password,
            min_size=2,
            max_size=10,
        )
    return _pool


async def close_pool():
    """Close the connection pool."""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
