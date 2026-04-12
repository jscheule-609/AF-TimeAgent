"""
Step 3b: Per-Deal Empirical Timeline Calibration

Uses the comparable deals already scored in Step 3, pulls their
actual milestone intervals from deal_milestones, and computes
similarity-weighted percentiles for each timing component.

No hardcoded durations — every interval is derived from the
specific comp set for this deal.
"""
import logging
from collections import defaultdict
from typing import Optional

from models.comparables import ComparableGroup
from db.queries_timeline_stats import (
    get_milestone_intervals,
    get_global_baseline_intervals,
)

logger = logging.getLogger(__name__)

# Minimum comps with data for an interval before we trust
# the weighted percentile (else fall back to global baseline).
_MIN_COMPS = 5

# Intervals we calibrate
_INTERVALS = [
    "announcement_to_filing",
    "filing_to_clearance",
    "announcement_to_vote",
    "vote_to_close",
    "clearance_to_close",
    "announcement_to_close",
    "announcement_to_proxy",
    "announcement_to_s4",
]


def _weighted_percentile(
    values: list[float],
    weights: list[float],
    percentile: float,
) -> float:
    """Compute a weighted percentile.

    Sorts by value, accumulates weights, returns the value
    where cumulative weight crosses the percentile threshold.
    """
    if not values:
        return 0.0
    pairs = sorted(zip(values, weights))
    total = sum(w for _, w in pairs)
    if total == 0:
        return pairs[len(pairs) // 2][0]
    cutoff = percentile * total
    cumulative = 0.0
    for val, w in pairs:
        cumulative += w
        if cumulative >= cutoff:
            return val
    return pairs[-1][0]


async def calibrate_deal_timeline(
    comparable_groups: list[ComparableGroup],
    target_prior_deals: list[dict],
) -> dict:
    """Compute empirical timeline distributions from this deal's
    comparable set.

    Returns dict of interval → {"p50", "p75", "p90"} plus
    metadata about comp coverage.
    """
    # 1. Collect all comp deals with their similarity weights.
    #    Dedup by deal_pk, keeping the highest weight.
    comp_weights: dict[int, float] = {}

    for group in comparable_groups:
        for deal in group.deals:
            pk = deal.deal_pk
            w = deal.weighted_score or 0.1
            if pk not in comp_weights or w > comp_weights[pk]:
                comp_weights[pk] = w

    # Target history gets a 2x boost — party-specific history
    # is highly predictive of timeline pace.
    for row in target_prior_deals:
        pk = row.get("deal_pk")
        if pk:
            existing = comp_weights.get(pk, 0.0)
            comp_weights[pk] = max(existing, 1.0) * 2.0

    all_pks = list(comp_weights.keys())
    if not all_pks:
        logger.warning(
            "No comparable deals found for timeline calibration"
        )
        return await _baseline_as_stats()

    logger.info(
        f"Timeline calibration: {len(all_pks)} comp deals"
    )

    # 2. Pull milestone intervals for all comp deals.
    raw_intervals = await get_milestone_intervals(all_pks)

    # 3. Group by interval name, pair with weights.
    by_interval: dict[str, list[tuple[float, float]]] = (
        defaultdict(list)
    )
    for row in raw_intervals:
        pk = row["deal_pk"]
        w = comp_weights.get(pk, 0.1)
        by_interval[row["interval"]].append(
            (float(row["days"]), w)
        )

    # 4. Compute weighted percentiles per interval.
    global_baseline = await _load_baseline()
    result: dict[str, dict] = {}
    comps_with_milestones = set()

    for interval_name in _INTERVALS:
        data = by_interval.get(interval_name, [])
        if len(data) >= _MIN_COMPS:
            values = [d for d, _ in data]
            weights = [w for _, w in data]
            result[interval_name] = {
                "p50": _weighted_percentile(
                    values, weights, 0.50
                ),
                "p75": _weighted_percentile(
                    values, weights, 0.75
                ),
                "p90": _weighted_percentile(
                    values, weights, 0.90
                ),
                "n": len(data),
                "source": "comparable",
            }
            for d, _ in data:
                comps_with_milestones.update(
                    r["deal_pk"]
                    for r in raw_intervals
                    if r["interval"] == interval_name
                )
        elif interval_name in global_baseline:
            result[interval_name] = {
                **global_baseline[interval_name],
                "source": "global_baseline",
            }
        else:
            result[interval_name] = {
                "p50": None,
                "p75": None,
                "p90": None,
                "n": 0,
                "source": "none",
            }

    result["comp_count"] = len(all_pks)
    result["comp_with_milestones"] = len(comps_with_milestones)

    return result


# ── Baseline cache ───────────────────────────────────────
_baseline_cache: Optional[dict] = None


async def _load_baseline() -> dict[str, dict]:
    global _baseline_cache
    if _baseline_cache is not None:
        return _baseline_cache

    rows = await get_global_baseline_intervals()
    _baseline_cache = {}
    for row in rows:
        _baseline_cache[row["interval"]] = {
            "p50": row["p50"],
            "p75": row["p75"],
            "p90": row["p90"],
            "n": row["n"],
        }
    return _baseline_cache


async def _baseline_as_stats() -> dict:
    """Return global baseline formatted as timeline_stats."""
    baseline = await _load_baseline()
    result = {}
    for name in _INTERVALS:
        if name in baseline:
            result[name] = {
                **baseline[name],
                "source": "global_baseline",
            }
        else:
            result[name] = {
                "p50": None, "p75": None, "p90": None,
                "n": 0, "source": "none",
            }
    result["comp_count"] = 0
    result["comp_with_milestones"] = 0
    return result
