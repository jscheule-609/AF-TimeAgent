"""AF-TimeAgent health endpoints.

Run with: uvicorn api.health:app --port 8091
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse

app = FastAPI(title="AF-TimeAgent", version="0.1.0")


@app.get("/health")
async def health():
    """Basic liveness check."""
    return {"status": "healthy", "service": "AF-TimeAgent"}


@app.get("/health/db")
async def health_db():
    """Database connectivity and deal count."""
    try:
        from db.connection import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT COUNT(*) AS n FROM deals"
            )
            return {
                "status": "healthy",
                "deal_count": row["n"],
            }
    except Exception as e:
        return JSONResponse(
            status_code=503,
            content={
                "status": "unhealthy",
                "error": str(e),
            },
        )
