"""
AF-TimeAgent API Server.

Endpoints:
  POST /predict/{deal_pk}    — run timing prediction for a deal
  POST /predict/batch        — predict N most recent active deals
  GET  /results/{deal_pk}    — get latest prediction from MARS
  GET  /results/active       — all active deals with predictions
  GET  /health               — service health check
  GET  /health/db            — database connectivity

Runs a NOTIFY listener that auto-predicts new deals as they're
inserted into the MARS deals table.
"""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime

import asyncpg
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("timeagent-api")

pool: asyncpg.Pool | None = None


# ── Lifespan ──────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool
    dsn = _build_dsn()
    logger.info(f"Connecting to {dsn.split('@')[1]}")
    pool = await asyncpg.create_pool(
        dsn, min_size=2, max_size=10, timeout=10,
    )
    logger.info("Connection pool created")

    # Inject pool into db.connection module so pipeline uses it
    import db.connection as db_conn
    db_conn._pool = pool

    # Set up environment for settings module
    _configure_env()

    # Start NOTIFY listener
    listener_task = asyncio.create_task(
        _notify_listener(dsn)
    )

    logger.info("TimeAgent API ready")
    yield

    listener_task.cancel()
    await pool.close()
    logger.info("Shutdown complete")


app = FastAPI(
    title="AF-TimeAgent",
    description="Deal timing prediction engine",
    version="1.0.0",
    lifespan=lifespan,
)


# ── Response Models ───────────────────────────────────────

class PredictionResult(BaseModel):
    deal_pk: int
    target: str
    acquirer: str
    p50_date: str | None
    p75_date: str | None
    p90_date: str | None
    critical_path: str | None
    scenarios: list[dict] | None = None
    risk_flags: list[str] | None = None
    elapsed_seconds: float | None = None


class HealthResponse(BaseModel):
    status: str
    predictions_total: int
    listener_active: bool


# ── Endpoints ─────────────────────────────────────────────

@app.post("/predict/{deal_pk}", response_model=PredictionResult)
async def predict_deal(deal_pk: int):
    """Run timing prediction for a single deal."""
    if not pool:
        raise HTTPException(503, "Not initialized")

    try:
        report, elapsed = await _run_pipeline(deal_pk)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        logger.error(f"Prediction failed for {deal_pk}: {e}")
        raise HTTPException(500, f"Prediction failed: {e}")

    return _report_to_result(report, elapsed)


@app.post("/predict/batch")
async def predict_batch(count: int = 20):
    """Predict N most recent active deals."""
    if not pool:
        raise HTTPException(503, "Not initialized")

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT deal_pk, target, acquirer
            FROM deals
            WHERE deal_status = 'Active'
            ORDER BY date_announced DESC
            LIMIT $1
            """,
            count,
        )

    results = []
    failed = 0
    for row in rows:
        try:
            report, elapsed = await _run_pipeline(
                row["deal_pk"]
            )
            results.append(
                _report_to_result(report, elapsed)
            )
        except Exception as e:
            logger.error(
                f"Batch failed on {row['deal_pk']}: {e}"
            )
            failed += 1

    return {
        "evaluated": len(results),
        "failed": failed,
        "results": [r.model_dump() for r in results],
    }


@app.get("/results/{deal_pk}")
async def get_results(deal_pk: int):
    """Get latest timing prediction from MARS."""
    if not pool:
        raise HTTPException(503, "Not initialized")

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT * FROM timing_predictions
            WHERE deal_pk = $1
            ORDER BY created_at DESC
            LIMIT 1
            """,
            deal_pk,
        )

    if not row:
        raise HTTPException(
            404, f"No prediction for deal_pk={deal_pk}"
        )

    return {
        k: (str(v) if hasattr(v, 'isoformat') else v)
        for k, v in dict(row).items()
    }


@app.get("/results/active")
async def get_active_results():
    """All active deals with timing predictions."""
    if not pool:
        raise HTTPException(503, "Not initialized")

    async with pool.acquire() as conn:
        # Check if timing_predictions table exists
        exists = await conn.fetchval(
            """
            SELECT EXISTS(
                SELECT 1 FROM information_schema.tables
                WHERE table_name = 'timing_predictions'
            )
            """
        )
        if not exists:
            return []

        rows = await conn.fetch(
            """
            SELECT DISTINCT ON (tp.deal_pk)
                tp.deal_pk,
                d.target,
                d.acquirer,
                tp.p50_close_date,
                tp.p75_close_date,
                tp.p90_close_date,
                tp.critical_path_jurisdiction,
                tp.created_at
            FROM timing_predictions tp
            JOIN deals d ON tp.deal_pk = d.deal_pk
            WHERE d.deal_status = 'Active'
            ORDER BY tp.deal_pk, tp.created_at DESC
            """,
        )

    return [
        {k: (str(v) if hasattr(v, 'isoformat') else v)
         for k, v in dict(r).items()}
        for r in rows
    ]


@app.get("/health", response_model=HealthResponse)
async def health():
    if not pool:
        return HealthResponse(
            status="unhealthy",
            predictions_total=0,
            listener_active=False,
        )

    try:
        async with pool.acquire() as conn:
            exists = await conn.fetchval(
                """
                SELECT EXISTS(
                    SELECT 1 FROM information_schema.tables
                    WHERE table_name = 'timing_predictions'
                )
                """
            )
            total = 0
            if exists:
                total = await conn.fetchval(
                    "SELECT COUNT(*) FROM timing_predictions"
                ) or 0
    except Exception:
        total = 0

    return HealthResponse(
        status="healthy",
        predictions_total=total,
        listener_active=_listener_active,
    )


@app.get("/health/db")
async def health_db():
    """Database connectivity check."""
    if not pool:
        return {"status": "unhealthy", "error": "No pool"}

    try:
        async with pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM deals"
            )
        return {"status": "healthy", "deal_count": count}
    except Exception as e:
        return {"status": "unhealthy", "error": str(e)}


# ── Pipeline Runner ───────────────────────────────────────

async def _run_pipeline(deal_pk: int):
    """Run the timing pipeline and return (report, elapsed)."""
    from models.deal import DealInput
    from pipeline.orchestrator import run_timing_estimation

    start = datetime.utcnow()
    deal_input = DealInput(deal_pk=deal_pk)
    report = await run_timing_estimation(deal_input)
    elapsed = (datetime.utcnow() - start).total_seconds()
    return report, elapsed


def _report_to_result(report, elapsed) -> PredictionResult:
    """Convert DealTimingReport to API response."""
    return PredictionResult(
        deal_pk=report.deal_pk if hasattr(report, 'deal_pk') else 0,
        target=getattr(report, 'target_name', ''),
        acquirer=getattr(report, 'acquirer_name', ''),
        p50_date=(
            str(report.p50_close_date)
            if hasattr(report, 'p50_close_date')
            and report.p50_close_date else None
        ),
        p75_date=(
            str(report.p75_close_date)
            if hasattr(report, 'p75_close_date')
            and report.p75_close_date else None
        ),
        p90_date=(
            str(report.p90_close_date)
            if hasattr(report, 'p90_close_date')
            and report.p90_close_date else None
        ),
        critical_path=(
            getattr(report, 'critical_path_jurisdiction', None)
        ),
        risk_flags=getattr(report, 'risk_flags', None),
        elapsed_seconds=round(elapsed, 1),
    )


# ── NOTIFY Listener ───────────────────────────────────────

_listener_active = False


async def _notify_listener(dsn: str):
    """Listen for new deal insertions."""
    global _listener_active

    while True:
        try:
            conn = await asyncpg.connect(dsn)
            logger.info("NOTIFY listener connected")
            _listener_active = True

            await conn.add_listener(
                "new_deal", _on_new_deal
            )

            while True:
                await asyncio.sleep(60)
                await conn.fetchval("SELECT 1")

        except asyncio.CancelledError:
            _listener_active = False
            return
        except Exception as e:
            _listener_active = False
            logger.error(
                f"Listener error: {e}. Reconnecting in 30s"
            )
            await asyncio.sleep(30)


def _on_new_deal(conn, pid, channel, payload):
    """Auto-predict timing for new deals."""
    logger.info(f"NOTIFY new_deal: {payload}")
    try:
        data = json.loads(payload)
        deal_pk = data.get("deal_pk")
        if deal_pk:
            asyncio.create_task(
                _auto_predict(deal_pk)
            )
    except Exception as e:
        logger.error(f"Failed to handle NOTIFY: {e}")


async def _auto_predict(deal_pk: int):
    """Background prediction for new deal."""
    # Wait 30s for the deal to be fully populated in MARS
    await asyncio.sleep(30)
    logger.info(f"Auto-predicting deal_pk={deal_pk}")
    try:
        report, elapsed = await _run_pipeline(deal_pk)
        logger.info(
            f"Auto-prediction complete: deal_pk={deal_pk} "
            f"elapsed={elapsed:.1f}s"
        )
    except Exception as e:
        logger.error(
            f"Auto-prediction failed for {deal_pk}: {e}"
        )


# ── Config ────────────────────────────────────────────────

def _build_dsn() -> str:
    host = os.environ.get("MARS_DB_HOST", "mars-db")
    port = os.environ.get("MARS_DB_PORT", "5432")
    db = os.environ.get("MARS_DB_NAME", "MARS")
    user = os.environ.get("MARS_DB_USER", "postgres")
    pw = os.environ.get("MARS_DB_PASSWORD", "postgres")
    return f"postgresql://{user}:{pw}@{host}:{port}/{db}"


def _configure_env():
    """Set env vars for Settings() to pick up with correct VPS defaults."""
    defaults = {
        "MARS_DB_HOST": "mars-db",
        "MARS_DB_PORT": "5432",
        "MARS_DB_NAME": "MARS",
    }
    for k, v in defaults.items():
        if k not in os.environ:
            os.environ[k] = v
