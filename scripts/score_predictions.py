"""Score stored timing predictions against realized outcomes.

Idempotent: only rows with ``actual_close_date IS NULL`` are touched, so
it is safe as a weekly cron on house-mars:

    docker exec timeagent python -m scripts.score_predictions            # dry-run (default)
    docker exec timeagent python -m scripts.score_predictions --apply    # write
    docker exec timeagent python -m scripts.score_predictions --report   # calibration by model_version
    docker exec timeagent python -m scripts.score_predictions --report --json /tmp/cal.json

Rules
  Closed      deals.deal_status = 'Completed' AND actual_completion_date IS NOT NULL
              -> actual_close_date, actual_timeline_days (close - announce),
                 actual_outcome = 'Completed', actual_critical_path NULL,
                 p50/p75/p90 error days and close_within_* flags (same
                 formulas as db.queries_prediction.update_prediction_actuals)
  Terminated  deals.deal_status = 'Terminated'
              -> actual_outcome = 'Terminated' only (no timing score)

Deals whose actual_completion_date precedes date_announced are skipped and
listed (data quality, not a scoring question).  Completed deals with no
actual_completion_date stay unscored until MARS learns the date.

The calibration report (--report) prints, per model_version and overall:
n, MAE, MedAE, mean bias, % within P50/P75/P90, and the guidance-only
baseline (later-of AJ/company guidance midpoint, parsed with the same
step2b.parse_guidance the engine uses) on the same deals.
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

_CLOSED_CANDIDATES = """
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

_UNSCORABLE = """
    SELECT count(*) AS n
    FROM timing_predictions tp
    JOIN deals d USING (deal_pk)
    WHERE tp.actual_close_date IS NULL
      AND d.deal_status = 'Completed'
      AND d.actual_completion_date IS NULL
"""


async def score(apply: bool) -> dict:
    """Dry-run (default) or apply the closed/terminated scoring updates."""
    pool = await get_pool()
    out: dict = {"apply": apply}
    async with pool.acquire() as conn:
        closed = await conn.fetch(_CLOSED_CANDIDATES)
        term = await conn.fetch(_TERMINATED_CANDIDATES)
        anomalies = await conn.fetch(_CLOSED_ANOMALIES)
        unscorable = await conn.fetchval(_UNSCORABLE)

        by_ver: dict[str, int] = {}
        for r in closed:
            by_ver[r["model_version"]] = by_ver.get(r["model_version"], 0) + 1
        out.update(
            closed_candidates=len(closed),
            closed_by_model_version=by_ver,
            terminated_candidates=len(term),
            anomalies=[dict(r) for r in anomalies],
            completed_without_close_date=unscorable,
        )

        print(f"Closed, scoreable now : {len(closed)}  {by_ver}")
        print(f"Terminated, to flag   : {len(term)}")
        print(f"Completed, no close dt: {unscorable} (left unscored)")
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
            term_done = await conn.fetch(_TERMINATED_UPDATE)
        out.update(closed_written=len(closed_done),
                   terminated_written=len(term_done))
        print(f"\nWritten: {len(closed_done)} closed scored, "
              f"{len(term_done)} terminated flagged.")
    return out


# -- calibration report -----------------------------------------------------

_SCORED_ROWS = """
    SELECT tp.model_version, tp.deal_pk,
           tp.p50_close_date, tp.p75_close_date, tp.p90_close_date,
           tp.actual_close_date, tp.p50_error_days,
           tp.close_within_p50, tp.close_within_p75, tp.close_within_p90,
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


def summarize(rows: list[dict], anchor_fn=guidance_anchor) -> dict:
    """Calibration metrics per model_version plus 'all'.

    ``rows`` are dicts shaped like _SCORED_ROWS; ``anchor_fn`` maps a row
    to its guidance midpoint (injectable for tests).
    """
    for r in rows:
        r["_guidance_anchor"] = anchor_fn(r)
    versions = sorted({r["model_version"] for r in rows})
    rep = {v: _bucket([r for r in rows if r["model_version"] == v])
           for v in versions}
    rep["all"] = _bucket(rows)
    return rep


_REPORT_COLS = [
    "n", "mae", "medae", "bias", "within_p50", "within_p75", "within_p90",
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
    parser.add_argument("--report", action="store_true",
                        help="Print calibration by model_version")
    parser.add_argument("--json", type=str, default=None,
                        help="With --report: also write the metrics as JSON")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)

    async def _run():
        try:
            if args.report:
                await report(args.json)
            else:
                await score(apply=args.apply)
        finally:
            await close_pool()

    asyncio.run(_run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
