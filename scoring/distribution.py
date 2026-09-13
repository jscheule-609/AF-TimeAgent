"""Monte Carlo close-date distribution engine.

Replaces the additive-percentile / max-of-medians arithmetic in
step6 with a coherent sampled distribution:

  1. Each timing component (empirical interval, state-machine path,
     AFT quantiles, guidance residuals) is sampled independently.
  2. Structural constraints combine by elementwise ``max`` — closing
     requires BOTH regulatory clearance AND the shareholder vote.
  3. Alternative estimators of the same quantity (mechanism chain,
     corpus/AFT duration model, guidance anchor) combine as a
     weighted MIXTURE — model averaging, never max().
  4. P50/P75/P90 are read off the empirical quantiles of the final
     sample array, so the percentile labels are actually coherent.

Also supports mid-deal conditioning: observed milestones collapse
the sampled component to its realized value, and elapsed time
truncates the distribution (close date must be after "today").
"""
from __future__ import annotations

import logging
import math
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# Standard normal quantiles for percentile → lognormal fitting
_Z75 = 0.6745
_Z90 = 1.2816

# Default number of Monte Carlo samples
N_SAMPLES = 4000

# Mixture weights over alternative close-date estimators.
# Overridable via calibration.json "track_weights".
DEFAULT_WEIGHTS = {
    "mechanism": 0.35,   # max(regulatory, proxy) constraint chain
    "corpus": 0.25,      # AFT covariate model / empirical total
    "guidance": 0.40,    # company/AJ guidance + residual distribution
}

# Fallback guidance residual quantiles (actual − guided midpoint, days).
# Overwritten by scripts/fit_model.py from the closed-deal corpus;
# these approximations come from the 2026-07 corpus audit
# (MAE≈60d, bias≈−32d, 78% close by guidance).
DEFAULT_GUIDANCE_RESIDUALS = {
    "p10": -95.0, "p25": -60.0, "p50": -30.0, "p75": 0.0, "p90": 45.0,
}

# "Overdue" distribution: used when conditioning eliminates nearly
# all samples (deal has outlived the model) — close is imminent but
# uncertain.
_OVERDUE_STATS = {"p50": 21.0, "p75": 45.0, "p90": 90.0}


def fit_lognormal(
    p50: float,
    p90: Optional[float] = None,
    p75: Optional[float] = None,
) -> tuple[float, float]:
    """Fit (mu, sigma) of a lognormal from duration percentiles."""
    p50 = max(float(p50), 1.0)
    mu = math.log(p50)
    sigma = 0.0
    if p90 and float(p90) > p50:
        sigma = (math.log(float(p90)) - mu) / _Z90
    elif p75 and float(p75) > p50:
        sigma = (math.log(float(p75)) - mu) / _Z75
    if sigma <= 0:
        sigma = 0.35  # mild default spread when tails are missing
    return mu, min(sigma, 1.5)


def sample_interval(
    stats: Optional[dict], n: int, rng: np.random.Generator,
) -> Optional[np.ndarray]:
    """Sample day-counts from an interval's {"p50","p75","p90"} dict."""
    if not stats:
        return None
    p50 = stats.get("p50")
    if not p50:
        return None
    mu, sigma = fit_lognormal(p50, stats.get("p90"), stats.get("p75"))
    return rng.lognormal(mu, sigma, n)


def sample_piecewise(
    quantiles: dict, n: int, rng: np.random.Generator,
) -> Optional[np.ndarray]:
    """Sample from a piecewise-linear inverse CDF given quantiles.

    Accepts {"p10","p25","p50","p75","p90"} (missing knots skipped;
    values may be negative — used for guidance residuals).  Tails
    beyond the outer knots are linearly extrapolated using the
    adjacent segment slope, sampling u ∈ (0.02, 0.98).
    """
    probs, vals = [], []
    for key in ("p10", "p25", "p50", "p75", "p90"):
        v = quantiles.get(key)
        if v is not None:
            probs.append(int(key[1:]) / 100.0)
            vals.append(float(v))
    if len(vals) < 2:
        return np.full(n, vals[0]) if vals else None

    p = np.array(probs)
    v = np.array(vals)
    u = rng.uniform(0.02, 0.98, n)
    out = np.interp(u, p, v)

    lo = u < p[0]
    if lo.any() and p[1] > p[0]:
        slope = (v[1] - v[0]) / (p[1] - p[0])
        out[lo] = v[0] + (u[lo] - p[0]) * slope
    hi = u > p[-1]
    if hi.any() and p[-1] > p[-2]:
        slope = (v[-1] - v[-2]) / (p[-1] - p[-2])
        out[hi] = v[-1] + (u[hi] - p[-1]) * slope
    return out


def sample_jurisdiction(
    sim, n: int, rng: np.random.Generator,
) -> Optional[np.ndarray]:
    """Sample clearance durations for one JurisdictionSimulation.

    Samples a path (by normalized path probability, conditional on
    eventual clearance — timing is meaningless for blocked paths),
    then a duration from that path's p50/p90 lognormal fit.
    """
    paths = [p for p in sim.possible_paths if p.is_terminal_clear and p.path_probability > 0]
    if not paths:
        paths = [p for p in sim.possible_paths if p.path_probability > 0]
    if not paths:
        return None

    probs = np.array(
        [max(p.path_probability, 1e-9) for p in paths]
    )
    probs = probs / probs.sum()
    idx = rng.choice(len(paths), size=n, p=probs)

    out = np.empty(n)
    for i, path in enumerate(paths):
        mask = idx == i
        cnt = int(mask.sum())
        if not cnt:
            continue
        if (path.total_duration_days_p50 == 0
                and path.total_duration_days_p75 == 0
                and path.total_duration_days_p90 == 0):
            out[mask] = 0.0
            continue
        mu, sigma = fit_lognormal(
            max(path.total_duration_days_p50, 1),
            path.total_duration_days_p90 or None,
            path.total_duration_days_p75 or None,
        )
        out[mask] = rng.lognormal(mu, sigma, cnt)
    return out


def _mixture(
    tracks: dict[str, np.ndarray],
    weights: dict[str, float],
    n: int,
    rng: np.random.Generator,
) -> np.ndarray:
    """Weighted mixture over alternative estimators (model averaging)."""
    names = [t for t in tracks if tracks[t] is not None]
    w = np.array([max(weights.get(t, 0.0), 0.0) for t in names])
    if w.sum() <= 0:
        w = np.ones(len(names))
    w = w / w.sum()

    counts = (w * n).astype(int)
    counts[-1] = n - counts[:-1].sum()  # exact total

    parts = []
    for name, cnt in zip(names, counts):
        if cnt <= 0:
            continue
        samples = tracks[name]
        pick = rng.choice(len(samples), size=cnt, replace=True)
        parts.append(samples[pick])
    return np.concatenate(parts)


def build_close_distribution(
    *,
    simulation=None,
    timeline_stats: Optional[dict] = None,
    aft_quantiles: Optional[dict] = None,
    guidance_days: Optional[float] = None,
    guidance_residuals: Optional[dict] = None,
    is_tender: bool = False,
    dma_close_gap: int = 3,
    weights: Optional[dict] = None,
    elapsed_days: Optional[float] = None,
    observed: Optional[dict] = None,
    n: int = N_SAMPLES,
    seed: int = 42,
) -> Optional[dict]:
    """Build the close-date distribution (days from announcement).

    Parameters
    ----------
    simulation : FullSimulationResult, state-machine output
    timeline_stats : step3b empirical intervals
    aft_quantiles : per-deal duration quantiles from scoring.aft
    guidance_days : days from announcement to guidance midpoint
    guidance_residuals : quantiles of (actual − guided) from calibration
    elapsed_days : days since announcement as-of prediction time;
        None means day-0 prediction (backtest semantics)
    observed : realized milestone day-offsets for THIS deal, e.g.
        {"antitrust_filing": 42, "antitrust_clearance": 130,
         "shareholder_vote": 95}

    Returns dict with p50/p75/p90 (float days), diagnostics, or None
    if no track could be built.
    """
    rng = np.random.default_rng(seed)
    ts = timeline_stats or {}
    obs = observed or {}
    wts = dict(DEFAULT_WEIGHTS)
    if weights:
        wts.update({k: v for k, v in weights.items() if v is not None})

    # ── Regulatory constraint ────────────────────────────
    reg_candidates: list[np.ndarray] = []

    if obs.get("antitrust_clearance") is not None:
        reg = np.full(n, float(obs["antitrust_clearance"]))
        reg_candidates.append(reg)
    else:
        clear_s = sample_interval(ts.get("filing_to_clearance"), n, rng)
        if obs.get("antitrust_filing") is not None and clear_s is not None:
            reg_candidates.append(
                float(obs["antitrust_filing"]) + clear_s
            )
        else:
            file_s = sample_interval(
                ts.get("announcement_to_filing"), n, rng
            )
            if file_s is not None and clear_s is not None:
                reg_candidates.append(file_s + clear_s)

        # State machine: max across jurisdictions (parallel reviews)
        if simulation is not None and simulation.jurisdictions:
            jur_samples = [
                s for s in (
                    sample_jurisdiction(js, n, rng)
                    for js in simulation.jurisdictions
                )
                if s is not None
            ]
            if jur_samples:
                reg_candidates.append(
                    np.maximum.reduce(jur_samples)
                )

    if len(reg_candidates) == 2:
        # Two alternative estimates of the same clearance date →
        # 50/50 per-sample mixture
        choose = rng.random(n) < 0.5
        reg_track = np.where(
            choose, reg_candidates[0], reg_candidates[1]
        )
    elif reg_candidates:
        reg_track = reg_candidates[0]
    else:
        reg_track = None

    # ── Proxy / vote constraint ──────────────────────────
    proxy_track = None
    if not is_tender:
        post_vote = sample_interval(ts.get("vote_to_close"), n, rng)
        if obs.get("shareholder_vote") is not None:
            if post_vote is None:
                post_vote = sample_interval(
                    {"p50": 5, "p75": 12, "p90": 30}, n, rng
                )
            proxy_track = float(obs["shareholder_vote"]) + post_vote
        else:
            vote_s = sample_interval(
                ts.get("announcement_to_vote"), n, rng
            )
            if vote_s is not None and post_vote is not None:
                proxy_track = vote_s + post_vote

    # ── Mechanism track: max of structural constraints ───
    constraints = [
        t for t in (reg_track, proxy_track) if t is not None
    ]
    mechanism = (
        np.maximum.reduce(constraints) + dma_close_gap
        if constraints else None
    )

    # ── Corpus track: AFT covariate model, else empirical ─
    if aft_quantiles:
        corpus = sample_piecewise(aft_quantiles, n, rng)
    else:
        corpus = sample_interval(
            ts.get("announcement_to_close"), n, rng
        )

    # ── Guidance track ───────────────────────────────────
    guidance = None
    if guidance_days is not None:
        resid = sample_piecewise(
            guidance_residuals or DEFAULT_GUIDANCE_RESIDUALS, n, rng
        )
        if resid is not None:
            guidance = np.maximum(
                float(guidance_days) + resid, 5.0
            )

    tracks = {
        "mechanism": mechanism,
        "corpus": corpus,
        "guidance": guidance,
    }
    if all(t is None for t in tracks.values()):
        return None

    final = _mixture(tracks, wts, n, rng)

    # ── Mid-deal conditioning: close must be in the future ─
    conditioned = False
    if elapsed_days is not None and elapsed_days > 0:
        surviving = final[final > elapsed_days]
        if len(surviving) >= max(50, n // 50):
            final = surviving
            conditioned = True
        else:
            # Deal has outlived the model — overdue distribution
            overdue = sample_interval(_OVERDUE_STATS, n, rng)
            final = float(elapsed_days) + overdue
            conditioned = True

    p50, p75, p90 = np.percentile(final, [50, 75, 90])

    def _track_p50(t):
        return (
            round(float(np.median(tracks[t])), 1)
            if tracks[t] is not None else None
        )

    return {
        "p50": float(p50),
        "p75": float(p75),
        "p90": float(p90),
        "n_effective": int(len(final)),
        "conditioned": conditioned,
        "tracks_used": [
            t for t in tracks if tracks[t] is not None
        ],
        "track_p50s": {
            t: _track_p50(t) for t in tracks
        },
        "weights": wts,
    }
