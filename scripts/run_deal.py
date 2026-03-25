"""
CLI entry point: python -m scripts.run_deal --acquirer AVGO --target VMW
"""
import asyncio
import argparse
import logging
from datetime import date

from models.deal import DealInput
from pipeline.orchestrator import run_timing_estimation
from output.markdown_renderer import render_full_report, render_compact_report
from db.connection import close_pool


def main():
    parser = argparse.ArgumentParser(description="Deal Timing Estimation Tool")
    parser.add_argument("--acquirer", required=True, help="Acquirer ticker symbol")
    parser.add_argument("--target", required=True, help="Target ticker symbol")
    parser.add_argument("--value", type=float, default=None, help="Deal value in USD")
    parser.add_argument("--date", type=str, default=None, help="Announcement date (YYYY-MM-DD)")
    parser.add_argument("--compact", action="store_true", help="Output compact format")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose logging")

    args = parser.parse_args()

    # Configure logging
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    announcement_date = None
    if args.date:
        announcement_date = date.fromisoformat(args.date)

    deal_input = DealInput(
        acquirer_ticker=args.acquirer.upper(),
        target_ticker=args.target.upper(),
        deal_value_usd=args.value,
        announcement_date=announcement_date,
    )

    try:
        report = asyncio.run(_run(deal_input))

        if args.compact:
            print(render_compact_report(report))
        else:
            print(render_full_report(report))

    except Exception as e:
        logging.error(f"Pipeline failed: {e}")
        raise SystemExit(1)


async def _run(deal_input: DealInput):
    try:
        return await run_timing_estimation(deal_input)
    finally:
        await close_pool()


if __name__ == "__main__":
    main()
