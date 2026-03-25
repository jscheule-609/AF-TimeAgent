"""Feature weighting for comparable deal similarity scoring."""
import math

DEFAULT_FEATURE_WEIGHTS = {
    # Structural similarity
    "sector_match": 0.15,
    "size_match": 0.10,
    "deal_structure_match": 0.05,
    "buyer_type_match": 0.08,
    # Regulatory similarity (most important for timing)
    "overlap_type_match": 0.18,
    "jurisdiction_count_match": 0.12,
    "jurisdiction_overlap": 0.10,
    # Context similarity
    "deal_hostility_match": 0.05,
    "regulatory_climate_proximity": 0.12,
    "cross_border_match": 0.05,
}


def compute_time_weight(months_since_close: float, half_life_months: float = 24.0) -> float:
    """
    Exponential decay time weighting.
    weight = exp(-ln(2) * months / half_life)
    24 months ago → 0.5, 48 months → 0.25, etc.
    """
    if months_since_close <= 0:
        return 1.0
    return math.exp(-math.log(2) * months_since_close / half_life_months)
