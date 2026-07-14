"""Fit the data-driven components of the timing model.

Produces ``config/calibration.json`` with everything the runtime
consumes:

  1. Regulatory rates + cleaned duration percentiles
     (reuses scripts/calibration_report.py, whose queries now filter
     negative/absurd durations)
  2. Guidance residual quantiles — (actual close − guided midpoint)
     over closed deals, parsed with the SAME parser used at predict
     time (pipeline.step2b_guidance_anchor.parse_guidance)
  3. Lognormal AFT model — log-OLS of timeline_days on deal
     covariates (feature contract shared with scoring.aft)
  4. Default track mixture weights (if not already present)

Usage:
    python -m scripts.fit_model                      # fit on all closed deals
    python -m scripts.fit_model --cutoff 2024-01-01  # train-only-before (backtest hygiene)
"""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import date
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

import numpy as np

from db.connection import get_pool, close_pool
from pipeline.step2b_guidance_anchor import parse_guidance
from scoring.aft import build_features

_CAL_PATH = Path(__file__).resolve().parent.parent / "config" / "calibration.json"

_QUANTS = {"p10": 0.10, "p25": 0.25, "p50": 0.50, "p75": 0.75, "p90": 0.90}

DEFAULT_TRACK_WEIGHTS = {
    "mechanism": 0.35, "corpus": 0.25, "guidance": 0.40,
}


async def fetch_closed_deals(pool, cutoff: date | None) -> list[dict]:
    sql = """
        SELECT d.deal_pk, d.date_announced, d.actual_completion_date,
               d.timeline_days, d.deal_value_usd, d.gics_sector,
               d.type_of_consideration, d.deal_attitude,
               d.acquirer_country, d.target_country,
               d.closing_guidance_arbjournal,
               d.closing_guidance_companies
        FROM deals d
        WHERE d.deal_outcome = 'Closed'
          AND d.date_announced IS NOT NULL
          AND d.timeline_days BETWEEN 5 AND 1000
    """
    params = []
    if cutoff:
        sql += " AND d.date_announced < $1"
        params.append(cutoff)
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return [dict(r) for r in rows]


def fit_guidance_residuals(deals: list[dict]) -> dict | None:
    """Quantiles of (actual close − guided midpoint), in days."""
    residuals = []
    for d in deals:
        actual = d["actual_completion_date"]
        ann = d["date_announced"]
        if not actual or not ann:
            continue
        anchors = []
        for col in ("closing_guidance_arbjournal",
                    "closing_guidance_companies"):
            lo, hi = parse_guidance(d.get(col), ann)
            if lo and hi:
                anchors.append(lo + (hi - lo) / 2)
        if not anchors:
            continue
        # Same rule as step2b.load_guidance_anchor: later wins
        anchor = max(anchors)
        resid = (actual - anchor).days
        if -400 <= resid <= 400:
            residuals.append(resid)

    if len(residuals) < 100:
        return None
    arr = np.array(residuals, dtype=float)
    return {
        "residual_quantiles": {
            k: round(float(np.quantile(arr, q)), 1)
            for k, q in _QUANTS.items()
        },
        "n": len(residuals),
        "mean": round(float(arr.mean()), 1),
        "mae": round(float(np.abs(arr).mean()), 1),
    }


def fit_aft(deals: list[dict], cutoff: date | None) -> dict | None:
    """Log-OLS AFT fit of timeline_days on deal covariates.

    Terminated deals are censored observations we currently drop
    (they never produce a close duration); competing-risk handling
    is BreakAgent's domain.
    """
    rows_x, rows_y = [], []
    feature_names: list[str] = []
    raw = []

    for d in deals:
        days = d.get("timeline_days")
        if not days:
            continue
        cons = (d.get("type_of_consideration") or "").lower()
        stock_flag = "stock" in cons or "share" in cons
        hostile_flag = (
            (d.get("deal_attitude") or "Friendly").lower()
            != "friendly"
        )
        cross_border = bool(
            d.get("acquirer_country") and d.get("target_country")
            and d["acquirer_country"] != d["target_country"]
        )
        x = build_features(
            float(d["deal_value_usd"]) if d.get("deal_value_usd") else None,
            d.get("gics_sector"),
            stock_flag, hostile_flag, cross_border,
        )
        raw.append((x, float(days)))

    if len(raw) < 500:
        return None

    # Stable feature ordering: union of all keys
    keys = set()
    for x, _ in raw:
        keys.update(x)
    feature_names = sorted(keys)

    for x, days in raw:
        rows_x.append([x.get(k, 0.0) for k in feature_names])
        rows_y.append(np.log(days))

    X = np.array(rows_x)
    y = np.array(rows_y)
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ beta

    return {
        "coefficients": {
            k: round(float(b), 5)
            for k, b in zip(feature_names, beta)
        },
        "residual_quantiles": {
            k: round(float(np.quantile(resid, q)), 4)
            for k, q in _QUANTS.items()
        },
        "residual_sigma": round(float(resid.std()), 4),
        "n": len(raw),
        "train_cutoff": cutoff.isoformat() if cutoff else None,
        "fit_date": date.today().isoformat(),
    }


async def run(cutoff: date | None) -> dict:
    pool = await get_pool()
    deals = await fetch_closed_deals(pool, cutoff)
    print(f"Closed-deal corpus: {len(deals)} deals"
          f"{f' (announced before {cutoff})' if cutoff else ''}")

    guidance = fit_guidance_residuals(deals)
    if guidance:
        print(f"Guidance residuals: n={guidance['n']} "
              f"mae={guidance['mae']}d "
              f"quantiles={guidance['residual_quantiles']}")
    else:
        print("Guidance residuals: insufficient data, skipped")

    aft = fit_aft(deals, cutoff)
    if aft:
        print(f"AFT model: n={aft['n']} "
              f"sigma={aft['residual_sigma']} "
              f"({len(aft['coefficients'])} coefficients)")
    else:
        print("AFT model: insufficient data, skipped")

    # Regulatory rates + cleaned durations (report closes the pool)
    from scripts.calibration_report import generate_report
    reg = await generate_report()

    cal = dict(reg)
    if guidance:
        cal["guidance"] = guidance
    if aft:
        cal["aft_model"] = aft
    cal.setdefault("track_weights", DEFAULT_TRACK_WEIGHTS)
    return cal


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fit timing-model calibration from MARS",
    )
    parser.add_argument(
        "--cutoff", type=str, default=None,
        help="Train only on deals announced before this date "
             "(YYYY-MM-DD) — for leakage-free backtests",
    )
    parser.add_argument(
        "--output", "-o", type=str, default=str(_CAL_PATH),
        help=f"Output path (default: {_CAL_PATH})",
    )
    args = parser.parse_args()

    cutoff = date.fromisoformat(args.cutoff) if args.cutoff else None
    cal = asyncio.run(run(cutoff))

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(cal, indent=2, default=str))
    print(f"Calibration written to {out}")


if __name__ == "__main__":
    main()
