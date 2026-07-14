"""Unit tests for the Monte Carlo close-date distribution engine."""
import numpy as np
import pytest

from scoring.distribution import (
    build_close_distribution,
    fit_lognormal,
    sample_interval,
    sample_piecewise,
)


def _rng():
    return np.random.default_rng(7)


def test_fit_lognormal_recovers_percentiles():
    mu, sigma = fit_lognormal(100, p90=200)
    samples = _rng().lognormal(mu, sigma, 200_000)
    assert np.percentile(samples, 50) == pytest.approx(100, rel=0.05)
    assert np.percentile(samples, 90) == pytest.approx(200, rel=0.05)


def test_sample_interval_none_when_missing():
    assert sample_interval(None, 100, _rng()) is None
    assert sample_interval({"p50": None}, 100, _rng()) is None


def test_sample_piecewise_handles_negatives():
    q = {"p10": -95.0, "p25": -60.0, "p50": -30.0,
         "p75": 0.0, "p90": 45.0}
    s = sample_piecewise(q, 100_000, _rng())
    assert np.percentile(s, 50) == pytest.approx(-30, abs=4)
    assert np.percentile(s, 90) == pytest.approx(45, abs=8)
    assert (s < 0).mean() > 0.5


def test_distribution_percentiles_monotonic():
    ts = {
        "announcement_to_filing": {"p50": 20, "p75": 30, "p90": 45},
        "filing_to_clearance": {"p50": 30, "p75": 45, "p90": 90},
        "announcement_to_vote": {"p50": 80, "p75": 100, "p90": 130},
        "vote_to_close": {"p50": 5, "p75": 15, "p90": 40},
        "announcement_to_close": {"p50": 105, "p75": 150, "p90": 220},
    }
    dist = build_close_distribution(
        timeline_stats=ts, guidance_days=120.0, seed=1,
    )
    assert dist is not None
    assert dist["p50"] <= dist["p75"] <= dist["p90"]
    assert 30 < dist["p50"] < 400


def test_guidance_only_track():
    dist = build_close_distribution(
        guidance_days=180.0, seed=2,
    )
    assert dist is not None
    assert dist["tracks_used"] == ["guidance"]
    # bias is negative: P50 should sit at/below the anchor
    assert dist["p50"] < 185


def test_no_tracks_returns_none():
    assert build_close_distribution(seed=3) is None


def test_elapsed_conditioning_raises_floor():
    ts = {
        "announcement_to_close": {"p50": 100, "p75": 140, "p90": 200},
    }
    day0 = build_close_distribution(
        timeline_stats=ts, seed=4,
    )
    conditioned = build_close_distribution(
        timeline_stats=ts, elapsed_days=150.0, seed=4,
    )
    assert conditioned["conditioned"]
    assert conditioned["p50"] > 150
    assert conditioned["p50"] > day0["p50"]


def test_overdue_fallback_when_far_past_p90():
    ts = {
        "announcement_to_close": {"p50": 100, "p75": 120, "p90": 140},
    }
    dist = build_close_distribution(
        timeline_stats=ts, elapsed_days=400.0, seed=5,
    )
    assert dist["conditioned"]
    assert dist["p50"] > 400
    assert dist["p50"] < 500  # overdue: imminent, not +100d


def test_observed_clearance_collapses_regulatory():
    ts = {
        "announcement_to_filing": {"p50": 20, "p75": 30, "p90": 45},
        "filing_to_clearance": {"p50": 60, "p75": 90, "p90": 150},
        "announcement_to_vote": {"p50": 70, "p75": 85, "p90": 100},
        "vote_to_close": {"p50": 3, "p75": 8, "p90": 20},
    }
    open_reg = build_close_distribution(
        timeline_stats=ts, seed=6,
        weights={"mechanism": 1.0, "corpus": 0, "guidance": 0},
    )
    cleared = build_close_distribution(
        timeline_stats=ts, seed=6,
        weights={"mechanism": 1.0, "corpus": 0, "guidance": 0},
        observed={"antitrust_clearance": 65.0},
    )
    # Clearance known at day 65 → vote (~70-100d) governs; the
    # p90 must tighten vs the open regulatory tail (~200d+)
    assert cleared["p90"] < open_reg["p90"]


def test_deterministic_given_seed():
    ts = {
        "announcement_to_close": {"p50": 100, "p75": 140, "p90": 200},
    }
    a = build_close_distribution(timeline_stats=ts, seed=11)
    b = build_close_distribution(timeline_stats=ts, seed=11)
    assert a["p50"] == b["p50"]
    assert a["p90"] == b["p90"]
