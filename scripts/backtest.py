"""
Backtest the timing model against closed deals from the past N years.

For each deal:
  1. Query MARS for deal metadata (tickers, dates, value)
  2. Run the full pipeline with that deal EXCLUDED from comparables,
     antitrust lookups, and MARS enrichment — simulating a forward
     prediction made on announcement day
  3. Compare predicted P50/P75/P90 against actual close date
  4. Store results and print calibration summary

Usage:
    python -m scripts.backtest [--years 2] [--max-deals 50] [--verbose]
    python -m scripts.backtest --single AVGO/VMW --verbose
"""
import asyncio
import argparse
import json
import logging
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()  # must run before sec_api_tools is imported

import numpy as np

from db.connection import get_pool, close_pool
from db.queries_prediction import update_prediction_actuals
from pipeline.backtest_runner import run_backtest_deal
from models.deal import DealInput

logger = logging.getLogger(__name__)

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "backtest_results"


async def fetch_backtest_universe(
    lookback_years: int = 2, max_deals: int = 50,
) -> list[dict]:
    """Pull closed deals from MARS within the lookback window."""
    pool = await get_pool()
    cutoff = date.today() - timedelta(days=lookback_years * 365)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                d.deal_pk,
                pa.ticker  AS acquirer_ticker,
                pa.company_name AS acquirer_name,
                pt.ticker  AS target_ticker,
                pt.company_name AS target_name,
                d.date_announced,
                d.actual_completion_date,
                d.timeline_days,
                d.deal_value_usd,
                d.deal_outcome,
                d.industry,
                d.gics_sector
            FROM deals d
            JOIN parties pa ON d.deal_pk = pa.deal_pk
                AND pa.role = 'acquirer'
            JOIN parties pt ON d.deal_pk = pt.deal_pk
                AND pt.role = 'target'
            WHERE d.deal_outcome = 'Closed'
              AND d.actual_completion_date IS NOT NULL
              AND d.timeline_days IS NOT NULL
              AND d.timeline_days > 30
              AND d.date_announced >= $1
              AND pa.ticker IS NOT NULL
              AND pt.ticker IS NOT NULL
            ORDER BY d.date_announced DESC
            LIMIT $2
            """,
            cutoff, max_deals,
        )
    return [dict(r) for r in rows]


async def run_backtest(
    lookback_years: int = 2,
    max_deals: int = 50,
    save_results: bool = True,
):
    """Main backtest loop."""
    universe = await fetch_backtest_universe(lookback_years, max_deals)
    logger.info(f"Backtest universe: {len(universe)} closed deals "
                f"(past {lookback_years} years)")

    if not universe:
        print("No deals found in the lookback window.")
        return []

    results = []
    for i, row in enumerate(universe, 1):
        label = f"{row['acquirer_ticker']}/{row['target_ticker']}"
        logger.info(f"[{i}/{len(universe)}] {label}")

        deal_input = DealInput(
            acquirer_ticker=row["acquirer_ticker"],
            target_ticker=row["target_ticker"],
            deal_value_usd=row.get("deal_value_usd"),
            announcement_date=row.get("date_announced"),
        )

        try:
            report = await run_backtest_deal(
                deal_input,
                exclude_deal_pk=row["deal_pk"],
            )

            actual_close = row["actual_completion_date"]
            actual_days = row["timeline_days"]
            ann_date = row["date_announced"]

            # Compute errors
            p50_err = (
                (actual_close - report.p50_close_date).days
                if report.p50_close_date else None
            )
            p75_err = (
                (actual_close - report.p75_close_date).days
                if report.p75_close_date else None
            )
            p90_err = (
                (actual_close - report.p90_close_date).days
                if report.p90_close_date else None
            )

            result = {
                "deal": label,
                "deal_pk": row["deal_pk"],
                "industry": row.get("industry", ""),
                "deal_value_usd": row.get("deal_value_usd"),
                "announcement_date": str(ann_date),
                "actual_close_date": str(actual_close),
                "actual_days": actual_days,
                "predicted_p50_date": (
                    str(report.p50_close_date)
                    if report.p50_close_date else None
                ),
                "predicted_p75_date": (
                    str(report.p75_close_date)
                    if report.p75_close_date else None
                ),
                "predicted_p90_date": (
                    str(report.p90_close_date)
                    if report.p90_close_date else None
                ),
                "predicted_p50_days": (
                    (report.p50_close_date - ann_date).days
                    if report.p50_close_date else None
                ),
                "p50_error_days": p50_err,
                "p75_error_days": p75_err,
                "p90_error_days": p90_err,
                "within_p50": (
                    report.p50_close_date is not None
                    and actual_close <= report.p50_close_date
                ),
                "within_p75": (
                    report.p75_close_date is not None
                    and actual_close <= report.p75_close_date
                ),
                "within_p90": (
                    report.p90_close_date is not None
                    and actual_close <= report.p90_close_date
                ),
                "critical_path": report.critical_path_jurisdiction,
                "overlap_type": report.overlap_type,
                "overlap_severity": report.overlap_severity,
                "comparable_deals_used": report.comparable_deals_used,
                "prediction_id": report.prediction_id,
            }
            results.append(result)

            # Persist actuals alongside the prediction
            if report.prediction_id:
                try:
                    await update_prediction_actuals(
                        prediction_id=report.prediction_id,
                        actual_close_date=actual_close,
                        actual_timeline_days=actual_days,
                        actual_outcome="closed",
                        actual_critical_path="",
                    )
                except Exception as e:
                    logger.warning(f"Failed to store actuals: {e}")

            _print_deal_result(result)

        except Exception as e:
            logger.error(f"Backtest failed for {label}: {e}")
            results.append({"deal": label, "error": str(e)})

    # Aggregate summary
    _print_summary(results)

    # Save to disk
    if save_results:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        out_file = OUTPUT_DIR / f"backtest_{date.today().isoformat()}.json"
        with open(out_file, "w") as f:
            json.dump(results, f, indent=2, default=str)
        print(f"\nResults saved to {out_file}")

    return results


def _print_deal_result(r: dict):
    """Print one-line summary for a deal."""
    err = r.get("p50_error_days")
    err_str = f"{err:+d}d" if err is not None else "N/A"
    flags = []
    if r.get("within_p50"):
        flags.append("<=P50")
    elif r.get("within_p75"):
        flags.append("<=P75")
    elif r.get("within_p90"):
        flags.append("<=P90")
    else:
        flags.append("MISS")
    print(
        f"  {r['deal']:<20s}  actual={r['actual_days']:>4d}d  "
        f"p50_err={err_str:>6s}  {' '.join(flags)}"
    )


def _print_summary(results: list[dict]):
    """Print aggregate calibration metrics."""
    valid = [r for r in results if "error" not in r]
    failed = len(results) - len(valid)

    if not valid:
        print("\nNo successful backtest runs.")
        return

    n = len(valid)
    within_p50 = sum(1 for r in valid if r.get("within_p50"))
    within_p75 = sum(1 for r in valid if r.get("within_p75"))
    within_p90 = sum(1 for r in valid if r.get("within_p90"))

    p50_errs = [
        r["p50_error_days"] for r in valid
        if r.get("p50_error_days") is not None
    ]
    abs_errs = np.abs(p50_errs) if p50_errs else []

    print(f"\n{'='*60}")
    print(f" BACKTEST SUMMARY  ({n} deals, {failed} failures)")
    print(f"{'='*60}")
    print(f" Within P50:  {within_p50}/{n}  ({within_p50/n:.0%})"
          f"   target: 50%")
    print(f" Within P75:  {within_p75}/{n}  ({within_p75/n:.0%})"
          f"   target: 75%")
    print(f" Within P90:  {within_p90}/{n}  ({within_p90/n:.0%})"
          f"   target: 90%")

    if len(abs_errs):
        print(f" MAE (P50):   {np.mean(abs_errs):.0f} days")
        print(f" MedAE (P50): {np.median(abs_errs):.0f} days")

        # Directional bias: positive = actual later than predicted
        print(f" Mean bias:   {np.mean(p50_errs):+.0f} days "
              f"({'late' if np.mean(p50_errs) > 0 else 'early'})")

    # Breakdown by overlap severity
    for sev in ("high", "medium", "low", "none"):
        subset = [r for r in valid if r.get("overlap_severity") == sev]
        if not subset:
            continue
        s_errs = [
            r["p50_error_days"] for r in subset
            if r.get("p50_error_days") is not None
        ]
        if s_errs:
            print(f"\n Overlap={sev} ({len(subset)} deals):"
                  f"  MAE={np.mean(np.abs(s_errs)):.0f}d"
                  f"  bias={np.mean(s_errs):+.0f}d")

    print(f"{'='*60}")


async def run_single_deal(
    acquirer_ticker: str,
    target_ticker: str,
) -> dict | None:
    """Run backtest for one specific deal. Prints detailed output."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                d.deal_pk,
                pa.ticker  AS acquirer_ticker,
                pa.company_name AS acquirer_name,
                pt.ticker  AS target_ticker,
                pt.company_name AS target_name,
                d.date_announced,
                d.actual_completion_date,
                d.timeline_days,
                d.deal_value_usd,
                d.deal_outcome,
                d.industry,
                d.gics_sector
            FROM deals d
            JOIN parties pa ON d.deal_pk = pa.deal_pk
                AND pa.role = 'acquirer'
            JOIN parties pt ON d.deal_pk = pt.deal_pk
                AND pt.role = 'target'
            WHERE pa.ticker = $1 AND pt.ticker = $2
              AND d.deal_outcome = 'Closed'
              AND d.actual_completion_date IS NOT NULL
            ORDER BY d.date_announced DESC
            LIMIT 1
            """,
            acquirer_ticker.upper(),
            target_ticker.upper(),
        )

    if not row:
        print(f"No closed deal found for "
              f"{acquirer_ticker}/{target_ticker} in MARS.")
        return None

    row = dict(row)
    label = f"{row['acquirer_ticker']}/{row['target_ticker']}"
    print(f"\n{'='*60}")
    print(f" SINGLE DEAL BACKTEST: {label}")
    print(f"{'='*60}")
    print(f" Announced:    {row['date_announced']}")
    print(f" Actual close: {row['actual_completion_date']}")
    print(f" Actual days:  {row['timeline_days']}")
    print(f" Value:        ${row['deal_value_usd']:,.0f}"
          if row.get('deal_value_usd') else " Value: N/A")
    print(f" Industry:     {row.get('industry', 'N/A')}")
    print(f" Excluding deal_pk={row['deal_pk']} from "
          f"comparables + MARS lookups")
    print(f"{'='*60}\n")

    deal_input = DealInput(
        acquirer_ticker=row["acquirer_ticker"],
        target_ticker=row["target_ticker"],
        deal_value_usd=row.get("deal_value_usd"),
        announcement_date=row.get("date_announced"),
    )

    report = await run_backtest_deal(
        deal_input,
        exclude_deal_pk=row["deal_pk"],
    )

    actual_close = row["actual_completion_date"]
    ann_date = row["date_announced"]

    print(f"\n{'='*60}")
    print(f" RESULTS: {label}")
    print(f"{'='*60}")
    print(f" Predicted P50: {report.p50_close_date}")
    print(f" Predicted P75: {report.p75_close_date}")
    print(f" Predicted P90: {report.p90_close_date}")
    print(f" Actual close:  {actual_close}")
    print()

    if report.p50_close_date:
        p50_err = (actual_close - report.p50_close_date).days
        print(f" P50 error: {p50_err:+d} days "
              f"({'late' if p50_err > 0 else 'early'})")
    if report.p75_close_date:
        p75_err = (actual_close - report.p75_close_date).days
        print(f" P75 error: {p75_err:+d} days")
    if report.p90_close_date:
        p90_err = (actual_close - report.p90_close_date).days
        print(f" P90 error: {p90_err:+d} days")

    within = "MISS"
    if report.p50_close_date and actual_close <= report.p50_close_date:
        within = "Within P50"
    elif report.p75_close_date and actual_close <= report.p75_close_date:
        within = "Within P75"
    elif report.p90_close_date and actual_close <= report.p90_close_date:
        within = "Within P90"
    print(f" Result: {within}")

    print(f"\n Critical path:  {report.critical_path_jurisdiction}")
    print(f" Overlap:        {report.overlap_type} / "
          f"{report.overlap_severity}")
    print(f" Comparables:    {report.comparable_deals_used}")
    print(f" Outside date:   {report.outside_date}")
    print(f" P(close by OD): "
          f"{report.probability_close_by_outside_date}")

    if report.scenarios:
        print(f"\n Scenarios:")
        for s in report.scenarios:
            print(f"   {s.scenario_name:<20s}  "
                  f"{s.probability_pct:5.1f}%  "
                  f"{s.expected_close_date}  "
                  f"({s.duration_days}d)")

    if report.risk_flags:
        print(f"\n Risk flags:")
        for f in report.risk_flags:
            print(f"   [{f.severity}] {f.flag}: {f.detail}")

    print(f"{'='*60}")
    return row


def main():
    parser = argparse.ArgumentParser(
        description="Backtest timing model against closed deals"
    )
    parser.add_argument(
        "--years", type=int, default=2,
        help="Lookback window in years (default: 2)",
    )
    parser.add_argument(
        "--max-deals", type=int, default=50,
        help="Maximum deals to test (default: 50)",
    )
    parser.add_argument(
        "--single", type=str, default=None,
        metavar="ACQ/TGT",
        help="Run one deal only, e.g. --single AVGO/VMW",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if args.single:
        parts = args.single.split("/")
        if len(parts) != 2:
            print("--single expects ACQ/TGT format, "
                  "e.g. --single AVGO/VMW")
            raise SystemExit(1)
        asyncio.run(_run_single(parts[0], parts[1]))
    else:
        asyncio.run(_run(args.years, args.max_deals))


async def _run_single(acquirer: str, target: str):
    try:
        await run_single_deal(acquirer, target)
    finally:
        await close_pool()


async def _run(years: int, max_deals: int):
    try:
        await run_backtest(lookback_years=years, max_deals=max_deals)
    finally:
        await close_pool()


if __name__ == "__main__":
    main()
