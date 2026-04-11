"""Simple health check for AF-TimeAgent.

Verifies MARS DB connectivity via asyncpg using the standard Settings
config (config/settings.py) and prints a small JSON status document.
"""

from __future__ import annotations

import asyncio
import json

from db.connection import get_pool, close_pool


async def _check_db() -> dict:
    status = {"ok": False, "error": None}
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.fetchrow("SELECT 1")
        status["ok"] = True
    except Exception as exc:  # pragma: no cover - defensive
        status["error"] = str(exc)
    finally:
        await close_pool()
    return status


def main() -> None:
    status = {
        "service": "AF-TimeAgent",
        "db": asyncio.run(_check_db()),
    }
    print(json.dumps(status, indent=2))


if __name__ == "__main__":
    main()

