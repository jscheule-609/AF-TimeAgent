"""asyncpg connection pool to MARS, plus writer attribution.

Writer attribution (MARS point-in-time design, section 5.4): every
connection this service opens carries ``application_name`` =
``MARS_APPLICATION_NAME`` (default ``timeagent``), and every write path
tags its session with

    SELECT set_config('mars.writer', 'timeagent:<trigger>', false)
    SELECT set_config('mars.run_id',  '<run id>',            false)

so the ``mars_change_log`` trigger (AF-AJ migration 048) can attribute
each row.  ``<trigger>`` is one of :data:`TRIGGERS`; ``<run id>`` is the
same identifier stored in ``timing_predictions.run_id``.

Entry points call :func:`set_run_context` once (a batch, an API call, a
NOTIFY, a CLI run, a scoring run); the writers then use
:func:`acquire_for_write` / :func:`attribute_writer`, which read the
context.  asyncpg resets session settings when a connection goes back
to the pool, so the tags never leak between runs.
"""
from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import AsyncIterator, Optional

import asyncpg

SERVICE_NAME = "timeagent"

# Who asked for the write.  Design 5.4 lists batch / notify / api / mcp /
# run_deal / score; ``backtest`` is scripts.backtest, which persists both a
# prediction row and its actuals.
TRIGGERS = frozenset({
    "batch", "notify", "api", "mcp", "run_deal", "score", "backtest",
})

_pool: Optional[asyncpg.Pool] = None

_run_context: ContextVar[tuple[str, str] | None] = ContextVar(
    "mars_run_context", default=None,
)


# -- application_name ------------------------------------------------------

def application_name() -> str:
    """``MARS_APPLICATION_NAME`` or the service default."""
    return os.environ.get("MARS_APPLICATION_NAME", "").strip() or SERVICE_NAME


def server_settings() -> dict[str, str]:
    """Startup parameters for every asyncpg connect / create_pool call."""
    return {"application_name": application_name()}


# -- run context -----------------------------------------------------------

def new_run_id(trigger: str) -> str:
    """``<trigger>-<UTC stamp>-<6 hex>``, e.g. ``batch-20261031T032500Z-5180ef``."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{trigger}-{stamp}-{uuid.uuid4().hex[:6]}"


def writer_name(trigger: str) -> str:
    """The ``mars.writer`` value for a trigger: ``timeagent:<trigger>``."""
    if trigger not in TRIGGERS:
        raise ValueError(
            f"unknown MARS writer trigger {trigger!r}; "
            f"expected one of {sorted(TRIGGERS)}"
        )
    return f"{SERVICE_NAME}:{trigger}"


def set_run_context(trigger: str, run_id: str | None = None) -> str:
    """Declare the current run (trigger + run id) for this task.

    Returns the run id (generated with :func:`new_run_id` when not given).
    """
    writer_name(trigger)  # validates
    run_id = run_id or new_run_id(trigger)
    _run_context.set((trigger, run_id))
    return run_id


def clear_run_context() -> None:
    _run_context.set(None)


def current_run_context() -> tuple[str, str] | None:
    """``(trigger, run_id)`` of the current task, or None."""
    return _run_context.get()


def current_run_id() -> str | None:
    ctx = _run_context.get()
    return ctx[1] if ctx else None


# -- writer attribution ----------------------------------------------------

async def attribute_writer(
    conn,
    trigger: str | None = None,
    run_id: str | None = None,
) -> tuple[str, str]:
    """Tag ``conn``'s session with ``mars.writer`` / ``mars.run_id``.

    Explicit arguments win; otherwise the values come from the run
    context.  Raises ``RuntimeError`` when neither is available: an
    unattributed write is a bug, not a fallback.
    """
    ctx = _run_context.get()
    if trigger is None:
        trigger = ctx[0] if ctx else None
    if run_id is None:
        run_id = ctx[1] if ctx else None
    if not trigger or not run_id:
        raise RuntimeError(
            "MARS writer attribution missing: call "
            "db.connection.set_run_context(<trigger>) before writing"
        )
    writer = writer_name(trigger)
    await conn.execute(
        "SELECT set_config('mars.writer', $1, false)", writer,
    )
    await conn.execute(
        "SELECT set_config('mars.run_id', $1, false)", run_id,
    )
    return trigger, run_id


@asynccontextmanager
async def acquire_for_write(
    trigger: str | None = None,
    run_id: str | None = None,
) -> AsyncIterator[asyncpg.Connection]:
    """``pool.acquire()`` followed by :func:`attribute_writer`."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await attribute_writer(conn, trigger, run_id)
        yield conn


# -- pool ------------------------------------------------------------------

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
            server_settings=server_settings(),
        )
    return _pool


async def close_pool():
    """Close the connection pool."""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
