"""Tests for the scoring engine."""
import pytest
from datetime import date
from models.deal import DealParameters, DealStructure, BuyerType, classify_buyer_type
from models.antitrust import OverlapAssessment
from models.comparables import ComparableDeal, ComparableSource
from scoring.similarity import compute_similarity_score
from scoring.feature_weights import compute_time_weight


def _make_deal_params(**overrides) -> DealParameters:
    defaults = dict(
        acquirer_ticker="AVGO",
        acquirer_name="Broadcom",
        acquirer_cik="0001649338",
        target_ticker="VMW",
        target_name="VMware",
        target_cik="0001124610",
        deal_value_usd=69_000_000_000,
        deal_structure=DealStructure.MIXED,
        buyer_type=BuyerType.STRATEGIC,
        announcement_date=date(2024, 3, 1),
        sector="Technology",
        industry="Semiconductors",
    )
    defaults.update(overrides)
    return DealParameters(**defaults)


def _make_comparable(**overrides) -> ComparableDeal:
    defaults = dict(
        deal_pk=1, deal_id="D001", acquirer="Intel", target="Altera",
        sector="Technology", industry="Semiconductors",
        deal_value_usd=16_700_000_000, deal_structure="cash",
        buyer_type="strategic", announcement_date=date(2023, 6, 1),
        deal_outcome="Closed", source=ComparableSource.SECTOR_MATCH,
    )
    defaults.update(overrides)
    return ComparableDeal(**defaults)


class TestSimilarityScoring:
    def test_exact_sector_match_scores_higher(self):
        """Same industry should score higher than different industry."""
        deal = _make_deal_params()
        overlap = OverlapAssessment()

        comp_same = _make_comparable(industry="Semiconductors", sector="Technology")
        comp_diff = _make_comparable(industry="Software", sector="Technology")

        score_same, _ = compute_similarity_score(deal, overlap, comp_same, set())
        score_diff, _ = compute_similarity_score(deal, overlap, comp_diff, set())

        assert score_same > score_diff

    def test_recent_deals_weighted_higher(self):
        """Deals from 6 months ago should have higher time weight than 3 years ago."""
        recent = compute_time_weight(6.0, 24.0)
        old = compute_time_weight(36.0, 24.0)
        assert recent > old
        assert recent > 0.8  # 6 months → ~0.84
        assert old < 0.4     # 36 months → ~0.35

    def test_jurisdiction_overlap_scoring(self):
        """Comp with same jurisdictions should score higher."""
        deal = _make_deal_params()
        overlap = OverlapAssessment()
        jurisdictions = {"HSR", "EC", "SAMR"}

        comp_match = _make_comparable(jurisdictions_required=["HSR", "EC", "SAMR"])
        comp_nomatch = _make_comparable(jurisdictions_required=["CFIUS"])

        score_match, _ = compute_similarity_score(deal, overlap, comp_match, jurisdictions)
        score_nomatch, _ = compute_similarity_score(deal, overlap, comp_nomatch, jurisdictions)

        assert score_match > score_nomatch

    def test_size_match_log_scale(self):
        """Size scoring should use log scale — 10x diff = 0.5."""
        deal = _make_deal_params(deal_value_usd=10_000_000_000)
        overlap = OverlapAssessment()

        comp_same = _make_comparable(deal_value_usd=10_000_000_000)
        comp_10x = _make_comparable(deal_value_usd=1_000_000_000)
        comp_100x = _make_comparable(deal_value_usd=100_000_000)

        score_same, feat_same = compute_similarity_score(deal, overlap, comp_same, set())
        score_10x, feat_10x = compute_similarity_score(deal, overlap, comp_10x, set())
        score_100x, feat_100x = compute_similarity_score(deal, overlap, comp_100x, set())

        assert feat_same["size_match"] > feat_10x["size_match"]
        assert feat_10x["size_match"] > feat_100x["size_match"]


class TestTimeWeighting:
    def test_time_weight_at_zero(self):
        assert compute_time_weight(0, 24) == 1.0

    def test_time_weight_at_half_life(self):
        w = compute_time_weight(24, 24)
        assert abs(w - 0.5) < 0.01

    def test_time_weight_at_double_half_life(self):
        w = compute_time_weight(48, 24)
        assert abs(w - 0.25) < 0.01

    def test_time_weight_negative_months(self):
        assert compute_time_weight(-5, 24) == 1.0


class TestBuyerTypeClassification:
    def test_pe_keywords(self):
        assert classify_buyer_type("Apollo Global Management") == BuyerType.PE_SPONSOR
        assert classify_buyer_type("Thoma Bravo Capital Partners") == BuyerType.PE_SPONSOR
        assert classify_buyer_type("Silver Lake Partners") == BuyerType.PE_SPONSOR
        assert classify_buyer_type("KKR & Co Investment Holdings") == BuyerType.PE_SPONSOR

    def test_strategic(self):
        assert classify_buyer_type("Microsoft Corporation") == BuyerType.STRATEGIC
        assert classify_buyer_type("Broadcom Inc.") == BuyerType.STRATEGIC

    def test_none_defaults_strategic(self):
        assert classify_buyer_type(None) == BuyerType.STRATEGIC
        assert classify_buyer_type("") == BuyerType.STRATEGIC


class TestCrossBorderScoring:
    def test_both_cross_border(self):
        deal = _make_deal_params()
        overlap = OverlapAssessment()
        comp = _make_comparable(jurisdictions_required=["HSR", "EC"])
        _, feats = compute_similarity_score(deal, overlap, comp, {"HSR", "EC"})
        assert feats["cross_border_match"] == 1.0

    def test_both_domestic(self):
        deal = _make_deal_params()
        overlap = OverlapAssessment()
        comp = _make_comparable(jurisdictions_required=["HSR"])
        _, feats = compute_similarity_score(deal, overlap, comp, {"HSR"})
        assert feats["cross_border_match"] == 1.0

    def test_mismatch(self):
        deal = _make_deal_params()
        overlap = OverlapAssessment()
        comp = _make_comparable(jurisdictions_required=["HSR", "EC"])
        _, feats = compute_similarity_score(deal, overlap, comp, {"HSR"})
        assert feats["cross_border_match"] == 0.5
