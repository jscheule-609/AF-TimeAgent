"""
Backtest the timing model against completed deals.

For completed deals in MARS, run the tool retrospectively and compare
predicted vs actual to calibrate.
"""
import asyncio
import logging
from datetime import date

from models.deal import DealInput
from pipeline.orchestrator import run_timing_estimation
from db.connection import get_pool, close_pool
from db.queries_prediction import update_prediction_actuals

logger = logging.getLogger(__name__)


async def run_backtest(max_deals: int = 50):
    """Run backtest on completed deals from MARS."""
    pool = await get_pool()

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT d.deal_pk, pa.ticker as acquirer_ticker, pt.ticker as target_ticker,
                   d.date_announced, d.actual_completion_date, d.timeline_days,
                   d.deal_value_usd, d.deal_outcome
            FROM deals d
            JOIN parties pa ON d.deal_pk = pa.deal_pk AND pa.role = 'acquirer'
            JOIN parties pt ON d.deal_pk = pt.deal_pk AND pt.role = 'target'
            WHERE d.deal_outcome = 'Closed'
              AND d.actual_completion_date IS NOT NULL
              AND d.timeline_days IS NOT NULL
              AND d.timeline_days > 30
              AND pa.ticker IS NOT NULL
              AND pt.ticker IS NOT NULL
            ORDER BY d.date_announced DESC
            LIMIT $1
            """,
            max_deals,
        )

    results = []
    for row in rows:
        row = dict(row)
        logger.info(f"Backtesting: {row['acquirer_ticker']} / {row['target_ticker']}")

        deal_input = DealInput(
            acquirer_ticker=row["acquirer_ticker"],
            target_ticker=row["target_ticker"],
            deal_value_usd=row.get("deal_value_usd"),
            announcement_date=row.get("date_announced"),
        )

        try:
            report = await run_timing_estimation(deal_input)

            actual_close = row["actual_completion_date"]
            actual_days = row["timeline_days"]

            # Update prediction with actuals
            if report.prediction_id:
                await update_prediction_actuals(
                    prediction_id=report.prediction_id,
                    actual_close_date=actual_close,
                    actual_timeline_days=actual_days,
                    actual_outcome="closed",
                    actual_critical_path="",
                )

            # Compute errors
            p50_error = None
            if report.p50_close_date:
                p50_error = (actual_close - report.p50_close_date).days

            results.append({
                "deal": f"{row['acquirer_ticker']}/{row['target_ticker']}",
                "actual_days": actual_days,
                "predicted_p50": (report.p50_close_date - row["date_announced"]).days if report.p50_close_date else None,
                "p50_error": p50_error,
                "within_p50": report.p50_close_date and actual_close <= report.p50_close_date,
                "within_p75": report.p75_close_date and actual_close <= report.p75_close_date,
                "within_p90": report.p90_close_date and actual_close <= report.p90_close_date,
            })

        except Exception as e:
            logger.error(f"Backtest failed for {row['acquirer_ticker']}/{row['target_ticker']}: {e}")
            results.append({"deal": f"{row['acquirer_ticker']}/{row['target_ticker']}", "error": str(e)})

    # Print summary
    valid = [r for r in results if "error" not in r]
    if valid:
        print(f"\nBacktest Results ({len(valid)} deals):")
        print(f"  Within P50: {sum(1 for r in valid if r.get('within_p50')) / len(valid):.0%}")
        print(f"  Within P75: {sum(1 for r in valid if r.get('within_p75')) / len(valid):.0%}")
        print(f"  Within P90: {sum(1 for r in valid if r.get('within_p90')) / len(valid):.0%}")

        errors = [r["p50_error"] for r in valid if r.get("p50_error") is not None]
        if errors:
            import numpy as np
            print(f"  Mean Absolute Error: {np.mean(np.abs(errors)):.0f} days")
            print(f"  Median Absolute Error: {np.median(np.abs(errors)):.0f} days")

    await close_pool()
    return results


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    asyncio.run(run_backtest())


if __name__ == "__main__":
    main()
