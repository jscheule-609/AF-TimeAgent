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

    # Logged once so the mode is visible in `docker logs` after a deploy.
    from parsers.llm_extraction import llm_available, llm_mode
    logger.info(
        "TIMEAGENT_LLM_MODE=%s (llm_enabled=%s)", llm_mode(), llm_available()
    )

    # Start NOTIFY listener. AGENT_LISTEN_ENABLED=0 keeps the HTTP API and
    # /health up but never subscribes — a clean pause. (TimeAgent installs no
    # trigger; trg_new_deal is owned by AF-AJ migration 036.)
    listener_task = None
    if _listen_enabled():
        listener_task = asyncio.create_task(_notify_listener(dsn))
    else:
        logger.warning(
            "NOTIFY listener disabled by AGENT_LISTEN_ENABLED=0 — HTTP API only"
        )

    logger.info("TimeAgent API ready")
    yield

    if listener_task is not None:
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
    # engine version written to timing_predictions.model_version, and the
    # row's prediction_id — what a caller (the MCP trigger tool) cites.
    model_version: str | None = None
    prediction_id: str | None = None
    guidance_flag: str | None = None
    scenarios: list[dict] | None = None
    # DealTimingReport.risk_flags are RiskFlag objects
    # (flag/severity/jurisdiction/detail); serialised as dicts. list[str]
    # here made every report with >=1 flag fail validation -> HTTP 500
    # after the row was already written.
    risk_flags: list[dict] | None = None
    elapsed_seconds: float | None = None


class HealthResponse(BaseModel):
    status: str
    predictions_total: int
    listener_active: bool
    # False = paused on purpose (AGENT_LISTEN_ENABLED=0), distinct from broken.
    listener_enabled: bool = True
    # TIMEAGENT_LLM_MODE ("openrouter" | "off"); llm_enabled is False when
    # the mode is off or no OpenRouter key is set: the LLM-backed steps are
    # skipped and predictions run on MARS data + comparables.
    llm_mode: str = "openrouter"
    llm_enabled: bool = True


# ── Endpoints ─────────────────────────────────────────────

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
                _report_to_result(report, elapsed, row["deal_pk"])
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

    return _report_to_result(report, elapsed, deal_pk)


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

    from parsers.llm_extraction import llm_available, llm_mode

    return HealthResponse(
        status="healthy",
        predictions_total=total,
        listener_active=_listener_active,
        listener_enabled=_listen_enabled(),
        llm_mode=llm_mode(),
        llm_enabled=llm_available(),
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


def _report_to_result(report, elapsed, deal_pk: int) -> PredictionResult:
    """Convert DealTimingReport to API response."""
    from models.prediction import PredictionRecord

    def _iso(d):
        return str(d) if d else None

    gr = getattr(report, "guidance_reconciliation", None)
    flags = []
    for f in getattr(report, "risk_flags", None) or []:
        if hasattr(f, "model_dump"):
            flags.append(f.model_dump())
        else:
            flags.append({"flag": str(f)})

    return PredictionResult(
        deal_pk=deal_pk,
        target=getattr(report, "target", "") or "",
        acquirer=getattr(report, "acquirer", "") or "",
        p50_date=_iso(getattr(report, "p50_close_date", None)),
        p75_date=_iso(getattr(report, "p75_close_date", None)),
        p90_date=_iso(getattr(report, "p90_close_date", None)),
        critical_path=getattr(report, "critical_path_jurisdiction", None),
        model_version=PredictionRecord.model_fields["model_version"].default,
        prediction_id=getattr(report, "prediction_id", None),
        guidance_flag=getattr(gr, "flag", None) if gr else None,
        risk_flags=flags,
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

def _listen_enabled() -> bool:
    """AGENT_LISTEN_ENABLED=0 = pause the NOTIFY consumer, keep the API."""
    return os.environ.get("AGENT_LISTEN_ENABLED", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


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
