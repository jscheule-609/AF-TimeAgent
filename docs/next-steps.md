# AF-TimeAgent: Next Steps

Status as of 2026-04-01 (updated from 2026-03-26 after audit fix pass).

---

## P0 — Blocking: Pipeline produces garbage without these

### 1. [RESOLVED] Fix document fetching (`step1_press_release.py`, `step2_document_ingestion.py`)

**Problem:** `get_filing_document()` from `sec_api_tools` requires an `EdgarClient` passed as the `client` parameter, but internally it validates the client was opened as an async context manager. The sub-functions (`_find_press_release`, `_find_merger_in_filing`, `_find_merger_in_8k`) receive a `client` from the parent scope but `get_filing_document` and `get_filing_index` fail with:

```
EdgarClient must be used as an async context manager: async with EdgarClient() as client: ...
```

**Root cause:** Each sub-function calls `sec_api_tools` functions that check `client._client is not None` (set in `__aenter__`). The parent opens the client, but the child functions receive the outer `client` object — the issue is likely that `get_filing_document` or `get_filing_index` internally constructs a *new* `EdgarClient` rather than using the one passed in.

**Fix options:**
- Option A: Open a fresh `async with get_client() as client:` inside each sub-function (simplest, slight overhead from extra connections)
- Option B: Audit `sec_api_tools` functions to confirm they properly accept and use the passed `client` parameter — may be a bug in the library itself
- Option C: Create a shared module-level client pool in AF-TimeAgent that all steps draw from

**Impact:** Without this fix, the pipeline gets no press release, no 10-K, no merger agreement. It defaults to HSR-only clean close (~35 days) regardless of deal complexity.

**Files:**
- `pipeline/step1_press_release.py` — `_find_press_release()`
- `pipeline/step2_document_ingestion.py` — `_ingest_tenk()`, `_find_merger_in_filing()`, `_find_merger_in_8k()`

---

### 2. [RESOLVED] Fix MARS comparables queries (`db/queries_comparables.py`)

**Problem:** The comparables queries reference columns that don't exist in the MARS `deals` table:
- `d.acquirer_type` — does not exist
- `d.cross_border_flag` — does not exist

All three comparable groups (acquirer history, sector match, size match) fail with:
```
column d.acquirer_type does not exist
```

**Fix:** `_BASE_DEAL_COLUMNS` was already patched to remove these two columns. But there may be additional references in:
- `WHERE` clauses or `ORDER BY` in the query functions
- The `scoring/similarity.py` feature weights that expect `acquirer_type` and `cross_border_flag` fields
- The `ComparableDeal` model constructor that maps these fields

**Action items:**
1. Grep for `acquirer_type` and `cross_border_flag` across the codebase
2. Remove or remap references — `acquirer_type` is not in MARS; buyer type classification must come from other signals or be dropped from scoring
3. Re-run the backtest query to confirm all three comparable groups return data

**Files:**
- `db/queries_comparables.py` — query definitions
- `scoring/similarity.py` — feature weight calculation
- `pipeline/step3_comparables.py` — comparable construction

---

## P1 — Important: Affects accuracy and logging

### 3. Fix prediction logging serialization (`pipeline/step7_prediction_log.py`)

**Problem:** `date` objects are not JSON-serializable when storing the prediction record:
```
Failed to store prediction: Object of type date is not JSON serializable
```

**Fix:** Add a JSON encoder that handles `date` and `datetime`, or convert dates to ISO strings before passing to the storage function.

**Files:**
- `pipeline/step7_prediction_log.py`
- `db/queries_prediction.py` (check what format the INSERT expects)

---

### 4. Add `date_to` filtering for 10-K lookups in backtesting

**Problem:** When backtesting a deal announced on 2025-07-30, the 10-K lookup should only find filings filed *before* that date (simulating what was available at announcement time). The `_ingest_tenk()` function now accepts `filed_before` and passes it as `date_to`, but this needs to be verified end-to-end once document fetching (item 1) is fixed.

**Files:**
- `pipeline/step2_document_ingestion.py` — `_ingest_tenk()` already has the `filed_before` param
- `pipeline/backtest_runner.py` — verify it passes `announcement_date` as the cutoff

---

## P2 — Enhancement: Improve prediction quality

### 5. Validate `sec_api_tools` env var naming

**Problem:** `sec_api_tools` uses `env_prefix="SEC_"` in its pydantic settings, so the field `sec_user_agent` maps to env var `SEC_SEC_USER_AGENT` (double-prefixed). Current workaround: both `SEC_USER_AGENT` and `SEC_SEC_USER_AGENT` are in `.env`.

**Fix:** Either rename the field in `sec_api_tools` from `sec_user_agent` to `user_agent` (so it maps to `SEC_USER_AGENT`), or accept the double prefix and document it. This should be fixed in the AF-SECAPI repo.

**Files:**
- `AF-SECAPI/src/sec_api_tools/config.py` — Settings class
- `AF-TimeAgent/.env` — current workaround

---

### 6. [RESOLVED] Wire up the external antitrust tool interface

**Resolved 2026-04-01:** `_build_from_external()` was already implemented.
`orchestrator.run_timing_estimation()` now accepts `external_signals` and
`external_overlap` kwargs and passes them through to `assess_antitrust_overlap()`.
Backtest runner left as-is (backtests don't use external data).

**Files:**
- `pipeline/step4_antitrust.py` — `_build_from_external()` already implemented
- `pipeline/orchestrator.py` — external signals now threaded through
- `pipeline/backtest_runner.py` — unchanged (external=None for backtests)

---

### 7. Full backtest run on 2-year closed deal universe

**Depends on:** Items 1-4 being resolved.

**Plan:**
1. Query MARS for all deals with `deal_outcome = 'Closed'` and `date_announced >= 2024-03-26`
2. Run each through the pipeline with that deal excluded from comparables/MARS lookups
3. Compare predicted P50/P75/P90 against `actual_completion_date`
4. Compute calibration metrics: MAE, median error, % within P75, % within P90
5. Identify systematic biases (e.g., consistently underestimating multi-jurisdiction deals)
6. Feed results back into state machine duration distributions and transition probabilities

**Scripts:**
- `scripts/backtest.py` — already supports `--years 2 --max-deals N`
- `scripts/backtest.py` — calibration summary printing is implemented

---

## Execution order

```
1 → Fix document fetching (unblocks everything)
2 → Fix comparables queries (unblocks scoring)
3 → Fix prediction logging (minor)
4 → Verify date filtering (quick check after 1)
──── re-run PANW/CYBR single deal test ────
5 → Fix sec_api_tools env naming (AF-SECAPI repo)
6 → Implement external antitrust interface
7 → Full 2-year backtest run
```
