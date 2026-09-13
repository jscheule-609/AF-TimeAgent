"""Step 4: the MARS competitive-analysis row must carry an assessment
before it short-circuits the 10-K / LLM fallback (no DB, no network)."""
import pytest

from pipeline import step4_antitrust as s4


def _placeholder_row(**over) -> dict:
    """An all-NULL deal_competitive_analysis row (4,376 of 5,371 are)."""
    row = dict(
        analysis_id=1, deal_pk=27636,
        product_market_overlap=None, geographic_market_overlap=None,
        combined_market_share_pct=None, hhi_pre_merger=None,
        hhi_post_merger=None, hhi_delta=None, market_definition=None,
        geographic_market=None, vertical_relationship=None,
        complementarity_assessment=None, competitive_effects_theory=None,
        deal_narrative=None, remedy_feasibility=None,
        remedy_type_likely=None, antitrust_risk_rating=None,
    )
    row.update(over)
    return row


@pytest.mark.parametrize("row, expected", [
    (None, False),
    ({}, False),
    (_placeholder_row(), False),
    (_placeholder_row(combined_market_share_pct=12.0), False),
    (_placeholder_row(product_market_overlap="NONE"), True),
    (_placeholder_row(geographic_market_overlap="LIMITED"), True),
    (_placeholder_row(antitrust_risk_rating="High"), True),
    (_placeholder_row(antitrust_risk_rating="   "), False),
    (_placeholder_row(product_market_overlap="  "), False),
])
def test_mars_row_is_informative(row, expected):
    assert s4.mars_row_is_informative(row) is expected


@pytest.mark.asyncio
async def test_placeholder_row_does_not_short_circuit(monkeypatch):
    # Regression: `if mars_analysis:` was truthy for the all-NULL dict, so
    # ~4,400 deals got none/none from MARS and the 10-K path never ran.
    async def _fake(_pk):
        return _placeholder_row()
    monkeypatch.setattr(s4, "get_deal_competitive_analysis", _fake)

    result = await s4.assess_antitrust_overlap(None, None, mars_deal_pk=27636)
    assert "Insufficient data" in result.reasoning
    assert result.overlap_type == "none" and result.overlap_severity == "none"


@pytest.mark.asyncio
async def test_explicit_none_overlap_is_trusted_as_none(monkeypatch):
    # "NONE" is an assessment (analyst says no overlap). The old truthiness
    # test turned the string into overlap_type="horizontal".
    async def _fake(_pk):
        return _placeholder_row(product_market_overlap="NONE",
                                geographic_market_overlap="NONE")
    monkeypatch.setattr(s4, "get_deal_competitive_analysis", _fake)

    result = await s4.assess_antitrust_overlap(None, None, mars_deal_pk=1)
    assert result.reasoning == "Based on MARS v2 competitive analysis data"
    assert result.overlap_type == "none"
    assert result.overlap_severity == "none"
    assert result.second_request_probability_base == pytest.approx(0.03)


@pytest.mark.asyncio
async def test_significant_overlap_without_share_maps_to_high(monkeypatch):
    async def _fake(_pk):
        return _placeholder_row(product_market_overlap="SIGNIFICANT",
                                geographic_market_overlap="LIMITED")
    monkeypatch.setattr(s4, "get_deal_competitive_analysis", _fake)

    result = await s4.assess_antitrust_overlap(None, None, mars_deal_pk=1)
    assert result.overlap_type == "horizontal"
    assert result.overlap_severity == "high"
    assert result.second_request_probability_base == pytest.approx(0.35)


def test_share_and_rating_take_precedence_over_overlap_level():
    # rating > combined share > overlap level
    r = s4._build_from_mars(_placeholder_row(
        product_market_overlap="LIMITED", combined_market_share_pct=30.0))
    assert (r.overlap_type, r.overlap_severity) == ("horizontal", "medium")

    r = s4._build_from_mars(_placeholder_row(
        product_market_overlap="SIGNIFICANT", combined_market_share_pct=30.0,
        antitrust_risk_rating="Low"))
    assert (r.overlap_type, r.overlap_severity) == ("horizontal", "low")

    r = s4._build_from_mars(_placeholder_row(product_market_overlap="MODERATE"))
    assert (r.overlap_type, r.overlap_severity) == ("horizontal", "medium")


@pytest.mark.asyncio
async def test_no_row_falls_through_to_minimal_assessment(monkeypatch):
    async def _fake(_pk):
        return None
    monkeypatch.setattr(s4, "get_deal_competitive_analysis", _fake)
    result = await s4.assess_antitrust_overlap(None, None, mars_deal_pk=1)
    assert "Insufficient data" in result.reasoning
