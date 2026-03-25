"""Multi-dimensional similarity scoring engine."""
import math
from datetime import date
from models.deal import DealParameters
from models.antitrust import OverlapAssessment
from models.comparables import ComparableDeal
from scoring.feature_weights import DEFAULT_FEATURE_WEIGHTS, compute_time_weight


# Sector adjacency map — sectors considered related
ADJACENT_SECTORS = {
    "Technology": {"Telecommunications", "Media"},
    "Telecommunications": {"Technology", "Media"},
    "Media": {"Technology", "Telecommunications"},
    "Healthcare": {"Pharmaceuticals", "Biotechnology"},
    "Pharmaceuticals": {"Healthcare", "Biotechnology"},
    "Biotechnology": {"Healthcare", "Pharmaceuticals"},
    "Energy": {"Utilities", "Mining"},
    "Utilities": {"Energy"},
    "Financial Services": {"Insurance", "Banking"},
    "Insurance": {"Financial Services", "Banking"},
    "Banking": {"Financial Services", "Insurance"},
}


def score_sector_match(deal_industry: str, deal_sector: str, comp: ComparableDeal) -> float:
    """Score sector/industry match: 1.0 exact industry, 0.7 same sector, 0.3 adjacent, 0.0 other."""
    if deal_industry and deal_industry == comp.industry:
        return 1.0
    if deal_sector and deal_sector == comp.sector:
        return 0.7
    if deal_sector in ADJACENT_SECTORS and comp.sector in ADJACENT_SECTORS.get(deal_sector, set()):
        return 0.3
    return 0.0


def score_size_match(deal_value: float, comp_value: float) -> float:
    """Score deal size proximity on log scale. 1.0 = same, 0.5 = 10x diff, 0.0 = 100x+."""
    if not deal_value or not comp_value or deal_value <= 0 or comp_value <= 0:
        return 0.5  # Unknown — assume moderate match
    log_diff = abs(math.log10(deal_value) - math.log10(comp_value))
    return max(0.0, 1.0 - log_diff / 2.0)


def score_deal_structure_match(deal_structure: str, comp_structure: str) -> float:
    """Score deal structure match."""
    if deal_structure == comp_structure:
        return 1.0
    # Partial matches
    mixed_types = {"cash", "stock", "mixed"}
    if deal_structure in mixed_types and comp_structure in mixed_types:
        return 0.5
    return 0.0


def score_buyer_type_match(deal_type: str, comp_type: str) -> float:
    """Score buyer type match."""
    if deal_type == comp_type:
        return 1.0
    # PE and financial are similar
    financial_types = {"financial", "pe_sponsor"}
    if deal_type in financial_types and comp_type in financial_types:
        return 0.7
    return 0.0


def score_overlap_type_match(deal_overlap: str, comp_has_horizontal: bool) -> float:
    """Score overlap type similarity."""
    deal_is_horizontal = deal_overlap in ("horizontal", "mixed")
    if deal_is_horizontal and comp_has_horizontal:
        return 1.0
    if not deal_is_horizontal and not comp_has_horizontal:
        return 1.0
    if deal_is_horizontal or comp_has_horizontal:
        return 0.5
    return 0.0


def score_jurisdiction_count_match(deal_count: int, comp_count: int) -> float:
    """Score jurisdiction count proximity."""
    return max(0.0, 1.0 - abs(deal_count - comp_count) / 4.0)


def score_jurisdiction_overlap(deal_jurisdictions: set[str], comp_jurisdictions: list[str]) -> float:
    """Jaccard similarity of required jurisdiction sets."""
    comp_set = set(comp_jurisdictions)
    if not deal_jurisdictions and not comp_set:
        return 1.0
    if not deal_jurisdictions or not comp_set:
        return 0.0
    intersection = deal_jurisdictions & comp_set
    union = deal_jurisdictions | comp_set
    return len(intersection) / len(union)


def score_hostility_match(deal_attitude: str, comp_outcome: str) -> float:
    """Score deal attitude match."""
    # Simplified — friendly vs hostile
    deal_friendly = deal_attitude.lower() in ("friendly", "")
    comp_friendly = "hostile" not in comp_outcome.lower() if comp_outcome else True
    return 1.0 if deal_friendly == comp_friendly else 0.0


def score_regulatory_climate_proximity(
    deal_date: date, comp_date: date, half_life_months: float = 24.0
) -> float:
    """Score based on time proximity — captures similar enforcement regime."""
    months_diff = abs((deal_date - comp_date).days) / 30.44
    return compute_time_weight(months_diff, half_life_months)


def compute_similarity_score(
    deal_params: DealParameters,
    overlap: OverlapAssessment,
    comp: ComparableDeal,
    deal_jurisdictions: set[str],
    weights: dict[str, float] | None = None,
    half_life_months: float = 24.0,
) -> tuple[float, dict[str, float]]:
    """
    Compute weighted similarity score between current deal and a comparable.
    Returns (total_score, per_feature_scores).
    """
    w = weights or DEFAULT_FEATURE_WEIGHTS
    features = {}

    features["sector_match"] = score_sector_match(
        deal_params.industry, deal_params.sector, comp
    )
    features["size_match"] = score_size_match(
        deal_params.deal_value_usd, comp.deal_value_usd or 0
    )
    features["deal_structure_match"] = score_deal_structure_match(
        deal_params.deal_structure.value, comp.deal_structure
    )
    features["buyer_type_match"] = score_buyer_type_match(
        deal_params.buyer_type.value, comp.buyer_type
    )
    features["overlap_type_match"] = score_overlap_type_match(
        overlap.overlap_type, comp.horizontal_overlap
    )
    features["jurisdiction_count_match"] = score_jurisdiction_count_match(
        len(deal_jurisdictions), len(comp.jurisdictions_required)
    )
    features["jurisdiction_overlap"] = score_jurisdiction_overlap(
        deal_jurisdictions, comp.jurisdictions_required
    )
    features["deal_hostility_match"] = score_hostility_match(
        deal_params.deal_attitude, comp.deal_outcome
    )
    features["regulatory_climate_proximity"] = score_regulatory_climate_proximity(
        deal_params.announcement_date, comp.announcement_date, half_life_months
    )
    features["cross_border_match"] = 1.0  # Default — enriched when cross_border data available

    # Weighted sum
    total = sum(features.get(k, 0.0) * w.get(k, 0.0) for k in w)
    return total, features
