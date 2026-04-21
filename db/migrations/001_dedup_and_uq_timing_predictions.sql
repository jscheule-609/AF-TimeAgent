-- FU-6: collapse duplicate timing_predictions rows to latest-per-deal,
-- then enforce uniqueness so future writes can UPSERT instead of INSERT-only.
--
-- Source: PIPELINE_REMEDIATION_FOLLOWUPS_2026-04-18.md FU-6.
-- Pre-state (2026-04-21): 3 deals with n=2 (deal_pk 93067, 93068, 93069).

BEGIN;

-- Keep the most recent row per deal_pk (by created_at, then prediction_id as
-- tiebreak). Rows with deal_pk IS NULL are untouched; Postgres allows multiple
-- NULLs in a UNIQUE column.
DELETE FROM timing_predictions tp
USING timing_predictions newer
WHERE tp.deal_pk = newer.deal_pk
  AND tp.deal_pk IS NOT NULL
  AND (newer.created_at, newer.prediction_id) > (tp.created_at, tp.prediction_id);

-- Enforce uniqueness going forward.
ALTER TABLE timing_predictions
  ADD CONSTRAINT uq_timing_predictions_deal UNIQUE (deal_pk);

COMMIT;
