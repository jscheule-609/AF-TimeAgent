"""Score stored timing predictions against realized outcomes.

Idempotent: only rows with ``actual_close_date IS NULL`` are touched, so
it is safe as a weekly cron on house-mars:

    docker exec timeagent python -m scripts.score_predictions            # dry-run (default)
    docker exec timeagent python -m scripts.score_predictions --apply    # write
    docker exec timeagent python -m scripts.score_predictions --apply --rescore  # + fix stale error cols
    docker exec timeagent python -m scripts.score_predictions --report   # calibration by model_version
    docker exec timeagent python -m scripts.score_predictions --report --json /tmp/cal.json
    docker exec timeagent python -m scripts.score_predictions --unscore-posthoc --apply  # one-time cleanup

Rules
  Closed      deals.deal_status = 'Completed' AND actual_completion_date IS NOT NULL
              AND prediction_date <= actual_completion_date
              -> actual_close_date, actual_timeline_days (close - announce),
                 actual_outcome = 'Completed', actual_critical_path NULL,
                 p50/p75/p90 error days and close_within_* flags (same
                 formulas as db.queries_prediction.update_prediction_actuals)
  Post-hoc    same, but prediction_date > actual_completion_date (a backtest
              or re-predict written after the deal closed is not a forecast)
              -> actuals only; error and close_within_* columns stay NULL
  Terminated  deals.deal_status = 'Terminated'
              -> actual_outcome = 'Terminated' only (no timing score)

Deals whose actual_completion_date precedes date_announced are skipped and
listed (data quality, not a scoring question).  Completed deals with no
actual_completion_date stay unscored until MARS learns the date.

--unscore-posthoc NULLs the six derived columns of rows already scored
although predicted after the close (the 09-13 backtest batch and the 09-30
backfill re-predicts); actuals and percentiles stay.  Idempotent.

The calibration report (--report) prints, per model_version and overall:
n, MAE, MedAE, mean bias, % within P50/P75/P90, and the guidance-only
baseline (later-of AJ/company guidance midpoint, parsed with the same
step2b.parse_guidance the engine uses) on the same deals.  Post-hoc rows
are left out of every metric and counted as posthoc_n.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import date
from statistics import mean, median

from db.connection import get_pool, close_pool

logger = logging.getLogger("score_predictions")

# -- candidate selection (shared by dry-run and apply) ---------------------

_CLOSED_BASE = """
    SELECT tp.prediction_id, tp.deal_pk, tp.model_version,
           tp.p50_close_date, d.actual_completion_date AS acd,
           d.date_announced AS ann
    FROM timing_predictions tp
    JOIN deals d USING (deal_pk)
    WHERE tp.actual_close_date IS NULL
      AND d.deal_status = 'Completed'
      AND d.actual_completion_date IS NOT NULL
      AND d.date_announced IS NOT NULL
      AND d.actual_completion_date >= d.date_announced
"""

_CLOSED_CANDIDATES = _CLOSED_BASE + """
      AND tp.prediction_date <= d.actual_completion_date
"""

_CLOSED_POSTHOC = _CLOSED_BASE + """
      AND tp.prediction_date > d.actual_completion_date
"""

_CLOSED_ANOMALIES = """
    SELECT tp.deal_pk, d.date_announced, d.actual_completion_date
    FROM timing_predictions tp
    JOIN deals d USING (deal_pk)
    WHERE tp.actual_close_date IS NULL
      AND d.deal_status = 'Completed'
      AND d.actual_completion_date IS NOT NULL
      AND (d.date_announced IS NULL
           OR d.actual_completion_date < d.date_announced)
"""

_CLOSED_UPDATE = """
    WITH cand AS (""" + _CLOSED_CANDIDATES + """)
    UPDATE timing_predictions tp SET
        actual_close_date    = c.acd,
        actual_timeline_days = c.acd - c.ann,
        actual_outcome       = 'Completed',
        actual_critical_path = NULL,
        p50_error_days       = c.acd - tp.p50_close_date,
        p75_error_days       = c.acd - tp.p75_close_date,
        p90_error_days       = c.acd - tp.p90_close_date,
        close_within_p50     = (c.acd <= tp.p50_close_date),
        close_within_p75     = (c.acd <= tp.p75_close_date),
        close_within_p90     = (c.acd <= tp.p90_close_date),
        updated_at           = NOW()
    FROM cand c
    WHERE c.prediction_id = tp.prediction_id
    RETURNING tp.deal_pk
"""

_POSTHOC_UPDATE = """
    WITH cand AS (""" + _CLOSED_POSTHOC + """)
    UPDATE timing_predictions tp SET
        actual_close_date    = c.acd,
        actual_timeline_days = c.acd - c.ann,
        actual_outcome       = 'Completed',
        actual_critical_path = NULL,
        p50_error_days       = NULL,
        p75_error_days       = NULL,
        p90_error_days       = NULL,
        close_within_p50     = NULL,
        close_within_p75     = NULL,
        close_within_p90     = NULL,
        updated_at           = NOW()
    FROM cand c
    WHERE c.prediction_id = tp.prediction_id
    RETURNING tp.deal_pk
"""

_TERMINATED_CANDIDATES = """
    SELECT tp.prediction_id, tp.deal_pk, tp.model_version, tp.actual_outcome
    FROM timing_predictions tp
    JOIN deals d USING (deal_pk)
    WHERE tp.actual_close_date IS NULL
      AND d.deal_status = 'Terminated'
      AND tp.actual_outcome IS DISTINCT FROM 'Terminated'
"""

_TERMINATED_UPDATE = """
    WITH cand AS (""" + _TERMINATED_CANDIDATES + """)
    UPDATE timing_predictions tp SET
        actual_outcome = 'Terminated',
        updated_at     = NOW()
    FROM cand c
    WHERE c.prediction_id = tp.prediction_id
    RETURNING tp.deal_pk
"""

# Scored rows whose derived columns no longer match p50/p75/p90 -- happens
# when a scored deal is re-predicted (the backtest UPSERT replaces the
# percentiles; before the step7 prediction_id fix its actuals update then
# matched nothing).  --rescore recomputes them; idempotent.  Post-hoc rows
# are excluded: their NULL errors are deliberate, and the weekly
# --apply --rescore cron would otherwise score them all again.
_STALE_WHERE = """
    WHERE tp.actual_close_date IS NOT NULL
      AND tp.prediction_date <= tp.actual_close_date
      AND (tp.p50_error_days IS DISTINCT FROM tp.actual_close_date - tp.p50_close_date
        OR tp.p75_error_days IS DISTINCT FROM tp.actual_close_date - tp.p75_close_date
        OR tp.p90_error_days IS DISTINCT FROM tp.actual_close_date - tp.p90_close_date
        OR tp.close_within_p50 IS DISTINCT FROM (tp.actual_close_date <= tp.p50_close_date)
        OR tp.close_within_p75 IS DISTINCT FROM (tp.actual_close_date <= tp.p75_close_date)
        OR tp.close_within_p90 IS DISTINCT FROM (tp.actual_close_date <= tp.p90_close_date))
"""

_STALE_COUNT = (
    "SELECT tp.model_version, count(*) AS n FROM timing_predictions tp"
    + _STALE_WHERE + " GROUP BY 1"
)

_RESCORE_UPDATE = """
    UPDATE timing_predictions tp SET
        p50_error_days   = tp.actual_close_date - tp.p50_close_date,
        p75_error_days   = tp.actual_close_date - tp.p75_close_date,
        p90_error_days   = tp.actual_close_date - tp.p90_close_date,
        close_within_p50 = (tp.actual_close_date <= tp.p50_close_date),
        close_within_p75 = (tp.actual_close_date <= tp.p75_close_date),
        close_within_p90 = (tp.actual_close_date <= tp.p90_close_date),
        updated_at       = NOW()
""" + _STALE_WHERE + """
    RETURNING tp.deal_pk
"""

_UNSCORABLE = """
    SELECT count(*) AS n
    FROM timing_predictions tp
    JOIN deals d USING (deal_pk)
    WHERE tp.actual_close_date IS NULL
      AND d.deal_status = 'Completed'
      AND d.actual_completion_date IS NULL
"""

# Rows scored although predicted after the close: the 09-13 backtest batch,
# the 09-30 backfill re-predicts, or a later manual re-predict of a closed
# deal (the UPSERT moves prediction_date but keeps the actuals).
_POSTHOC_SCORED_WHERE = """
    WHERE tp.actual_close_date IS NOT NULL
      AND tp.prediction_date > tp.actual_close_date
      AND (tp.p50_error_days IS NOT NULL OR tp.p75_error_days IS NOT NULL
        OR tp.p90_error_days IS NOT NULL OR tp.close_within_p50 IS NOT NULL
        OR tp.close_within_p75 IS NOT NULL OR tp.close_within_p90 IS NOT NULL)
"""

_POSTHOC_SCORED = """
    SELECT tp.deal_pk, tp.model_version, tp.prediction_date,
           tp.actual_close_date, tp.p50_error_days
    FROM timing_predictions tp
""" + _POSTHOC_SCORED_WHERE + """
    ORDER BY tp.prediction_date, tp.deal_pk
"""

_UNSCORE_POSTHOC = """
    UPDATE timing_predictions tp SET
        p50_error_days   = NULL,
        p75_error_days   = NULL,
        p90_error_days   = NULL,
        close_within_p50 = NULL,
        close_within_p75 = NULL,
        close_within_p90 = NULL,
        updated_at       = NOW()
""" + _POSTHOC_SCORED_WHERE + """
    RETURNING tp.deal_pk
"""


async def score(apply: bool, rescore: bool = False) -> dict:
    """Dry-run (default) or apply the closed/terminated scoring updates.

    ``rescore`` also recomputes the derived error/coverage columns of
    already-scored rows whose percentiles changed since they were scored.
    """
    pool = await get_pool()
    out: dict = {"apply": apply, "rescore": rescore}
    async with pool.acquire() as conn:
        closed = await conn.fetch(_CLOSED_CANDIDATES)
        posthoc = await conn.fetch(_CLOSED_POSTHOC)
        term = await conn.fetch(_TERMINATED_CANDIDATES)
        anomalies = await conn.fetch(_CLOSED_ANOMALIES)
        unscorable = await conn.fetchval(_UNSCORABLE)
        stale = await conn.fetch(_STALE_COUNT)
        stale_by_ver = {r["model_version"]: r["n"] for r in stale}
        posthoc_scored = await conn.fetch(_POSTHOC_SCORED)
        out["stale_scored_rows"] = stale_by_ver

        by_ver: dict[str, int] = {}
        for r in closed:
            by_ver[r["model_version"]] = by_ver.get(r["model_version"], 0) + 1
        out.update(
            closed_candidates=len(closed),
            closed_by_model_version=by_ver,
            closed_posthoc=len(posthoc),
            posthoc_scored_rows=len(posthoc_scored),
            terminated_candidates=len(term),
            anomalies=[dict(r) for r in anomalies],
            completed_without_close_date=unscorable,
        )

        print(f"Closed, scoreable now : {len(closed)}  {by_ver}")
        print(f"Closed, predicted post-hoc: {len(posthoc)} "
              f"(actuals set, not scored)")
        print(f"Terminated, to flag   : {len(term)}")
        print(f"Completed, no close dt: {unscorable} (left unscored)")
        print(f"Scored rows w/ stale errors: {sum(stale_by_ver.values())}  "
              f"{stale_by_ver}" + ("" if rescore else "  (pass --rescore to fix)"))
        if posthoc_scored:
            print(f"Scored rows predicted post-hoc: {len(posthoc_scored)} "
                  f"(pass --unscore-posthoc --apply to clear)")
        if anomalies:
            pks = [r["deal_pk"] for r in anomalies][:20]
            print(f"Skipped (close < announce or no announce): "
                  f"{len(anomalies)} -> {pks}")
        for r in closed[:10]:
            print(f"  sample deal_pk={r['deal_pk']} v{r['model_version']} "
                  f"ann={r['ann']} p50={r['p50_close_date']} actual={r['acd']}")

        if not apply:
            print("\nDry run - nothing written (pass --apply).")
            return out

        async with conn.transaction():
            closed_done = await conn.fetch(_CLOSED_UPDATE)
            posthoc_done = await conn.fetch(_POSTHOC_UPDATE)
            term_done = await conn.fetch(_TERMINATED_UPDATE)
            rescored = await conn.fetch(_RESCORE_UPDATE) if rescore else []
        out.update(closed_written=len(closed_done),
                   posthoc_written=len(posthoc_done),
                   terminated_written=len(term_done),
                   rescored=len(rescored))
        print(f"\nWritten: {len(closed_done)} closed scored, "
              f"{len(posthoc_done)} post-hoc actuals, "
              f"{len(term_done)} terminated flagged, "
              f"{len(rescored)} rescored.")
    return out


async def unscore_posthoc(apply: bool) -> dict:
    """Dry-run (default) or NULL the derived columns of post-hoc scored rows.

    Actuals and percentiles are kept, so the errors stay recomputable.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(_POSTHOC_SCORED)
        out: dict = {"apply": apply, "posthoc_scored": len(rows),
                     "deal_pks": [r["deal_pk"] for r in rows]}
        by_ver: dict[str, int] = {}
        for r in rows:
            by_ver[r["model_version"]] = by_ver.get(r["model_version"], 0) + 1
        print(f"Scored rows predicted post-hoc: {len(rows)}  {by_ver}")
        for r in rows:
            print(f"  deal_pk={r['deal_pk']} v{r['model_version']} "
                  f"predicted={r['prediction_date']:%Y-%m-%d} "
                  f"actual={r['actual_close_date']} "
                  f"p50_err={r['p50_error_days']}")
        if not apply:
            print("\nDry run - nothing written (pass --apply).")
            return out
        async with conn.transaction():
            done = await conn.fetch(_UNSCORE_POSTHOC)
        out["unscored"] = len(done)
        print(f"\nWritten: {len(done)} post-hoc rows unscored "
              f"(actuals kept).")
    return out


# -- calibration report -----------------------------------------------------

_SCORED_ROWS = """
    SELECT tp.model_version, tp.deal_pk,
           tp.p50_close_date, tp.p75_close_date, tp.p90_close_date,
           tp.actual_close_date, tp.p50_error_days,
           tp.close_within_p50, tp.close_within_p75, tp.close_within_p90,
           (tp.prediction_date > tp.actual_close_date) AS posthoc,
           d.date_announced,
           d.closing_guidance_arbjournal, d.closing_guidance_companies
    FROM timing_predictions tp
    JOIN deals d USING (deal_pk)
    WHERE tp.actual_close_date IS NOT NULL
"""


def guidance_anchor(row: dict) -> date | None:
    """Later-of AJ/company guidance midpoint.

    Same rule as step2b.load_guidance_anchor and
    scripts.backtest._guidance_anchor_for_row.
    """
    from pipeline.step2b_guidance_anchor import parse_guidance
    ann = row.get("date_announced")
    if not ann:
        return None
    anchors = []
    for col in ("closing_guidance_arbjournal", "closing_guidance_companies"):
        lo, hi = parse_guidance(row.get(col), ann)
        if lo and hi:
            anchors.append(lo + (hi - lo) / 2)
    return max(anchors) if anchors else None


def _within(row: dict, flag_key: str, date_key: str) -> bool:
    stored = row.get(flag_key)
    if stored is not None:
        return bool(stored)
    pred = row.get(date_key)
    return pred is not None and row["actual_close_date"] <= pred


def _bucket(rows: list[dict]) -> dict:
    errs: list[int] = []
    w50 = w75 = w90 = 0
    guided_g: list[int] = []
    guided_m: list[int] = []
    for r in rows:
        actual = r["actual_close_date"]
        err = r.get("p50_error_days")
        if err is None and r.get("p50_close_date") is not None:
            err = (actual - r["p50_close_date"]).days
        if err is None:
            continue
        errs.append(err)
        w50 += _within(r, "close_within_p50", "p50_close_date")
        w75 += _within(r, "close_within_p75", "p75_close_date")
        w90 += _within(r, "close_within_p90", "p90_close_date")
        g = r.get("_guidance_anchor")
        if g is not None:
            guided_g.append((actual - g).days)
            guided_m.append(err)
    n = len(errs)
    if not n:
        return {"n": 0}
    out = {
        "n": n,
        "mae": round(mean(abs(e) for e in errs), 1),
        "medae": round(median(abs(e) for e in errs), 1),
        "bias": round(mean(errs), 1),
        "within_p50": round(100 * w50 / n, 1),
        "within_p75": round(100 * w75 / n, 1),
        "within_p90": round(100 * w90 / n, 1),
        "guided_n": len(guided_g),
    }
    if guided_g:
        beats = sum(1 for g, m in zip(guided_g, guided_m) if abs(m) < abs(g))
        out["guidance_mae"] = round(mean(abs(e) for e in guided_g), 1)
        out["guidance_medae"] = round(median(abs(e) for e in guided_g), 1)
        out["model_mae_on_guided"] = round(mean(abs(e) for e in guided_m), 1)
        out["model_beats_guidance_pct"] = round(100 * beats / len(guided_g), 1)
    return out


def _scored_bucket(rows: list[dict]) -> dict:
    """_bucket over forecasts only; post-hoc rows are counted, not scored.

    _bucket recomputes a NULL p50 error from the dates, so excluding
    post-hoc rows by their NULL errors alone would not keep them out.
    """
    out = _bucket([r for r in rows if not r.get("posthoc")])
    out["posthoc_n"] = sum(1 for r in rows if r.get("posthoc"))
    return out


def summarize(rows: list[dict], anchor_fn=guidance_anchor) -> dict:
    """Calibration metrics per model_version plus 'all'.

    ``rows`` are dicts shaped like _SCORED_ROWS; ``anchor_fn`` maps a row
    to its guidance midpoint (injectable for tests).
    """
    for r in rows:
        r["_guidance_anchor"] = anchor_fn(r)
    versions = sorted({r["model_version"] for r in rows})
    rep = {v: _scored_bucket([r for r in rows if r["model_version"] == v])
           for v in versions}
    rep["all"] = _scored_bucket(rows)
    return rep


_REPORT_COLS = [
    "n", "posthoc_n", "mae", "medae", "bias",
    "within_p50", "within_p75", "within_p90",
    "guided_n", "guidance_mae", "model_mae_on_guided",
    "model_beats_guidance_pct",
]


def _print_report(rep: dict) -> None:
    print(f"{'version':10}" + "".join(f"{c:>14}" for c in _REPORT_COLS))
    for v, b in rep.items():
        print(f"{v:10}" + "".join(
            f"{str(b.get(c, '-')):>14}" for c in _REPORT_COLS
        ))


async def report(json_path: str | None) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = [dict(r) for r in await conn.fetch(_SCORED_ROWS)]
    rep = summarize(rows)
    _print_report(rep)
    if json_path:
        with open(json_path, "w") as f:
            json.dump(rep, f, indent=2, default=str)
        print(f"Report written to {json_path}")
    return rep


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Score timing_predictions against closed/terminated deals",
    )
    parser.add_argument("--apply", action="store_true",
                        help="Write the updates (default: dry-run)")
    parser.add_argument("--rescore", action="store_true",
                        help="Also recompute error/coverage columns of scored "
                             "rows whose percentiles changed (with --apply)")
    parser.add_argument("--report", action="store_true",
                        help="Print calibration by model_version")
    parser.add_argument("--json", type=str, default=None,
                        help="With --report: also write the metrics as JSON")
    parser.add_argument("--unscore-posthoc", action="store_true",
                        help="NULL error/coverage columns of rows predicted "
                             "after the close (with --apply; idempotent)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)

    async def _run():
        try:
            if args.report:
                await report(args.json)
            elif args.unscore_posthoc:
                await unscore_posthoc(apply=args.apply)
            else:
                await score(apply=args.apply, rescore=args.rescore)
        finally:
            await close_pool()

    asyncio.run(_run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
