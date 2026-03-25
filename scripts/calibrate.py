"""
Analyze prediction accuracy and suggest model parameter adjustments.
"""
import asyncio
import logging
import numpy as np

from models.prediction import CalibrationMetrics
from db.queries_prediction import get_calibration_data
from db.connection import close_pool

logger = logging.getLogger(__name__)


async def run_calibration() -> CalibrationMetrics:
    """Analyze prediction accuracy from stored predictions with actuals."""
    data = await get_calibration_data()

    if not data:
        print("No calibration data available (no predictions with actuals).")
        return CalibrationMetrics()

    total = len(data)
    within_p50 = sum(1 for d in data if d.get("close_within_p50"))
    within_p75 = sum(1 for d in data if d.get("close_within_p75"))
    within_p90 = sum(1 for d in data if d.get("close_within_p90"))

    p50_errors = [d["p50_error_days"] for d in data if d.get("p50_error_days") is not None]
    mae = float(np.mean(np.abs(p50_errors))) if p50_errors else 0.0
    medae = float(np.median(np.abs(p50_errors))) if p50_errors else 0.0

    metrics = CalibrationMetrics(
        total_predictions=total,
        predictions_with_actuals=total,
        pct_within_p50=within_p50 / total * 100 if total else 0,
        pct_within_p75=within_p75 / total * 100 if total else 0,
        pct_within_p90=within_p90 / total * 100 if total else 0,
        mean_absolute_error_days=mae,
        median_absolute_error_days=medae,
    )

    # Print report
    print(f"\n{'='*60}")
    print(f" CALIBRATION REPORT ({total} predictions)")
    print(f"{'='*60}")
    print(f" Within P50: {metrics.pct_within_p50:.1f}% (target: 50%)")
    print(f" Within P75: {metrics.pct_within_p75:.1f}% (target: 75%)")
    print(f" Within P90: {metrics.pct_within_p90:.1f}% (target: 90%)")
    print(f" MAE: {metrics.mean_absolute_error_days:.0f} days")
    print(f" Median AE: {metrics.median_absolute_error_days:.0f} days")
    print(f"{'='*60}")

    # Suggest adjustments
    if metrics.pct_within_p50 < 40:
        print("\n SUGGESTION: P50 predictions are too aggressive.")
        print(" Consider widening duration distributions or increasing base probabilities")
        print(" for extended review paths.")
    elif metrics.pct_within_p50 > 60:
        print("\n SUGGESTION: P50 predictions are too conservative.")
        print(" Consider tightening duration distributions.")

    if metrics.pct_within_p90 < 80:
        print("\n SUGGESTION: P90 tail is not capturing enough variance.")
        print(" Consider increasing p90 duration multipliers.")

    await close_pool()
    return metrics


def main():
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_calibration())


if __name__ == "__main__":
    main()
