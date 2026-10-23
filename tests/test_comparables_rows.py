"""Comparable rows from MARS must build even when sector / industry are NULL.

Regression for the 2026-10-22 nightly batch: three AUTORESEARCH-only deals
closed by hand (data-gaps plan D6b) carry NULL gics_sector / industry and
entered the size-matched comparables; pydantic rejected them and 12 Active
deals failed at stage2_analysis.
"""
from datetime import date
import inspect

from models.comparables import ComparableSource
from pipeline.step3_comparables import _row_to_comparable
from db import queries_comparables


def _row(**overrides) -> dict:
    row = {
        "deal_pk": 93336,
        "deal_id": "DEAL-20260420-D4E80B",
        "acquirer_name": "USA Rare Earth, Inc.",
        "target_name": "Serra Verde Group",
        "acquirer_country": None,
        "target_country": None,
        "gics_sector": None,
        "industry": None,
        "deal_value_usd": 2.8e9,
        "type_of_consideration": None,
        "acquirer_party_type": None,
        "date_announced": date(2026, 4, 20),
        "actual_completion_date": None,
        "timeline_days": None,
        "deal_outcome": "Closed",
    }
    row.update(overrides)
    return row


def test_null_sector_and_industry_build_as_empty_strings():
    comp = _row_to_comparable(_row(), ComparableSource.SIZE_MATCH)
    assert comp.sector == ""
    assert comp.industry == ""
    assert comp.deal_structure == "cash"


def test_null_names_build_as_empty_strings():
    comp = _row_to_comparable(
        _row(acquirer_name=None, target_name=None, deal_outcome=None),
        ComparableSource.ACQUIRER_HISTORY,
    )
    assert comp.acquirer == ""
    assert comp.target == ""
    assert comp.deal_outcome == ""


def test_populated_row_keeps_its_values():
    comp = _row_to_comparable(
        _row(gics_sector="Materials", industry="Mining"), ComparableSource.SECTOR_MATCH,
    )
    assert comp.sector == "Materials"
    assert comp.industry == "Mining"


def test_comparable_queries_exclude_non_ma_rows():
    """Every comparables query filters is_ma_deal (NULL counts as M&A)."""
    src = inspect.getsource(queries_comparables)
    outcome_filters = src.count("AND d.deal_outcome IN ('Closed', 'Terminated')")
    assert outcome_filters == 4
    assert src.count("AND COALESCE(d.is_ma_deal, TRUE)") == outcome_filters
