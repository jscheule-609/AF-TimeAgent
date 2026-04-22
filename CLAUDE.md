# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

AF-TimeAgent is a deal-timing prediction engine for M&A. It takes a deal (by `deal_pk` or by acquirer/target tickers) and produces P50/P75/P90 close-date estimates, a critical-path jurisdiction, scenario probabilities, and risk flags. Predictions are stored in MARS (`timing_predictions` table, UNIQUE on `deal_pk`).

It is the second half of a two-system pipeline:
- **AF-ARB_AUTORESEARCH** (sister project, separate repo) — fetches SEC filings, runs LLM extraction, writes facts into MARS.
- **AF-TimeAgent** (this repo) — reads from MARS, models regulatory timing, predicts close dates.

TimeAgent **never writes** to autoresearch-owned MARS tables (`deals`, `parties`, `deal_dma_terms`, `deal_break_fees`, `regulatory_reviews`, etc.) — only reads. It writes only to `timing_predictions`.

## Common commands

```bash
# Run a single deal
python -m scripts.run_deal --deal-pk 93067              # MARS-first (preferred)
python -m scripts.run_deal --acquirer AVGO --target VMW # ticker fallback (no autoresearch data)
python -m scripts.run_deal --acquirer AVGO --target VMW --compact --verbose

# Backtest against historical closed deals (excludes the deal_pk to prevent leakage)
python -m scripts.backtest --years 2 --max-deals 50
python -m scripts.backtest --single AVGO/VMW --verbose

# Re-run prediction-vs-actuals calibration report
python -m scripts.calibrate
python -m scripts.calibration_report --output config/calibration.json   # regenerates calibration overrides

# Batch-run all active deals (writes JSON to batch40_results.json)
python -m scripts.batch_run

# Health check
python -m scripts.health_check

# Tests
pytest                                     # all
pytest -m "not integration"                # skip integration / network tests
pytest tests/test_state_machines.py        # one file
pytest tests/test_state_machines.py::test_hsr_clean_path -v
pytest -m "integration and requires_network"

# API server (FastAPI, port 8004)
uvicorn api.server:app --host 0.0.0.0 --port 8004

# Deploy to house-mars VPS (packages, scps, runs scripts/deploy_local.sh on the box)
bash deploy.sh
```

Install: `pip install -e ".[dev]"`. Requires Python ≥3.11. `AF-SECAPI` is installed from a private GitHub repo using `GITHUB_PAT` (see Dockerfile); it must be available before running anything that touches EDGAR.

## Architecture

### Pipeline orchestration

`pipeline/orchestrator.py::run_timing_estimation()` is the canonical entry point. It runs four stages with explicit parallelism:

1. **Stage 0** — `step0_validation` resolves the deal. Two paths:
   - `deal_pk` provided → load from MARS via `db/read_autoresearch.py` (preferred path).
   - tickers only → SEC API CIK resolution + MARS lookup-by-tickers.
2. **Stage 1 (parallel)** — `step1_press_release` + `step2_document_ingestion` (10-Ks + merger agreement). Step 2 prefers MARS-cached merger terms; only re-parses EDGAR when MARS is empty.
3. **Stage 2 (parallel)** — `step3_comparables` (3-group comparable engine: acquirer history / sector / size) + `step4_antitrust` (overlap assessment from MARS `competitive_analysis`, falling back to LLM on 10-Ks).
4. **Stage 3** — `step5_regulatory_map` determines required jurisdictions, then `step5_5_state_machine` simulates each one.
5. **Stage 3b/3c** — `step3b_timeline_calibration` (per-deal duration calibration from comps) and `step2b_guidance_anchor` (loads ArbJournal/company guidance).
6. **Stage 4** — `step6_timeline` assembles the final report; `step8_guidance_reconciliation` compares to guidance; `step7_prediction_log` writes to MARS.

`pipeline/backtest_runner.py` mirrors the orchestrator but **strips the test deal** from MARS enrichment (Stage 0), comparables (Stage 2), and antitrust lookup (Stage 2) to prevent leakage. Use it — never the orchestrator — for backtests.

### State machines

Each jurisdiction is a subclass of `state_machines/base.py::BaseRegulatoryStateMachine` (HSR, EC, CMA, SAMR, CFIUS, ACCC, plus a `generic` fallback). A subclass defines:

- `define_states()` — the regulatory stages (e.g. `not_filed → filed → waiting_period → ...`).
- `define_transitions(overlap, climate, comparable_stats)` — directed edges with probabilities, adjusted by overlap severity, enforcement climate, and historical base rates.
- `compute_duration_distributions(...)` — `{state_id: {p50, p75, p90}}`.
- `terminal_states()` / `_clear_terminal_states()` — which paths count as "cleared" vs blocked/withdrawn.

`base.simulate()` enumerates all paths via BFS (pruning any cumulative probability < 1%, with cycle detection except for HSR's pull-and-refile), then probability-weights durations to produce expected `p50/p75/p90`.

### Configuration

- `config/constants.py` — **statutory deadlines treated as legal facts** (e.g. `HSR_INITIAL_WAITING_PERIOD_DAYS = 30`). Don't change these without a regulatory citation.
- `config/calibration.json` — empirical overrides generated by `scripts/calibration_report.py`. Loaded by `config/calibration.py::load_calibration()`. Missing keys mean "fall back to constants".
- `config/settings.py` — pydantic `Settings` (env-var-driven). Defaults assume a local MARS at `localhost:5434/mars`; the API container overrides to `mars-db:5432/MARS` via `api/server.py::_configure_env()`.
- `.env` (gitignored, see `.env.example`) — `MARS_DB_*`, `OPENROUTER_API_KEY`, `SEC_USER_AGENT`, `BRAVE_API_KEY`.

### Database (MARS)

- `db/connection.py` — single async `asyncpg` pool, lazily created. The API server **injects its own pool** into `db.connection._pool` at startup (see `api/server.py::lifespan`); standalone scripts call `get_pool()` themselves and must `await close_pool()` at exit.
- `db/queries_*.py` — per-domain read queries. `read_autoresearch.py` is the canonical reader for autoresearch-populated tables and includes the v2 schema mapping (e.g. `break_fees → deal_break_fees`, 5 antitrust tables → `regulatory_reviews` filtered by `jurisdiction_code`).
- `db/migrations/` — manual SQL migrations (no migration framework). Run by hand.
- The API server runs a Postgres `LISTEN new_deal` listener and auto-predicts new deals 30s after insertion (see `_notify_listener`).

### Models

`models/` are all pydantic. Key hierarchy: `DealInput` → `DealParameters` (after Stage 0) → various per-step outputs (`PressReleaseData`, `ParsedMergerAgreement`, `ComparableGroup`, `OverlapAssessment`, `JurisdictionSimulation`) → `DealTimingReport` (final). `mars_deal_pk` on `DealParameters` is the link back to MARS — `None` means the deal isn't in MARS yet.

### LLM usage

`parsers/llm_extraction.py::call_llm()` is the single entry point — calls OpenRouter, defaults to `google/gemini-2.5-flash`, temperature 0, JSON-only system prompt. `parse_json_response()` handles markdown fences and extracts JSON from noisy output. Per-document parsers (`press_release_parser`, `tenk_parser`, `merger_agreement_parser`) hold the prompts.

## Conventions worth knowing

- **Async everywhere.** Every pipeline step, every DB query, every HTTP call. Never call sync DB or `httpx` from pipeline code.
- **`AsyncIO.gather` parallelism is intentional** in stages 1 and 2 of the orchestrator. Don't serialize them.
- **Connection pool lifecycle:** in CLI scripts always wrap the run in `try/finally` with `await close_pool()` (see `scripts/run_deal.py::_run`). The API server manages this via `lifespan`.
- **`EdgarClient` from `sec_api_tools` must be used as `async with`** — it raises if not opened as a context manager. Each function that needs SEC access opens its own client.
- **Prediction logging is non-fatal** — `step7_prediction_log` failures log a warning but don't fail the pipeline.
- **MARS is read-only for everything except `timing_predictions`.** If you need a new field from autoresearch, add it to the autoresearch FIELD_REGISTRY in that repo, not here. See `docs/# AF-TimeAgent Refactor Plan — Eliminati.txt` for the formal handoff contract.
- **Backtests must use `backtest_runner`**, not `orchestrator`, or you'll leak the deal under test into its own comparables/MARS lookup.
- **Calibration data flows one way:** backtest → `update_prediction_actuals` → `scripts/calibration_report.py` → `config/calibration.json` → state machines via `config/calibration.py`. Don't bypass.

## Deployment

`deploy.sh` (run from a dev machine) tars the repo (excluding `__pycache__`, `.env`, `.git`, `results`), scps to `house-mars`, then SSHes in to run `scripts/deploy_local.sh`, which `docker build`s and `docker run`s the `timeagent` container on `mars-net` exposing port 8004. The VPS also has a cron poller (`/root/af-deploy/poll.sh`) that runs `deploy_local.sh` from a freshly pulled tree. Secrets live in `/root/af-deploy/secrets.env` on the VPS — `GITHUB_PAT`, `OPENROUTER_API_KEY`, `SEC_USER_AGENT` are required.

Health checks once deployed:
```
curl http://localhost:8004/health
curl http://localhost:8004/health/db
```

## Outstanding work

`docs/next-steps.md` tracks open issues (most P0/P1 items resolved as of 2026-04-01). Read it before starting work that touches document fetching, comparables queries, prediction logging, or `sec_api_tools` env handling — it documents the actual failure modes those areas have hit.
