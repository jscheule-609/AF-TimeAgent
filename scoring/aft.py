"""Lognormal AFT (accelerated failure time) duration model.

Coefficients are fitted offline by ``scripts/fit_model.py`` (log-OLS
on closed deals in MARS) and stored in ``config/calibration.json``
under ``"aft_model"``.  At prediction time this module builds the
same feature vector from ``DealParameters`` and returns per-deal
duration quantiles (days from announcement), which feed the corpus
track of ``scoring.distribution``.

Feature contract (must match fit_model.py exactly):
    intercept        1.0
    log_value        log10(deal_value_usd) − 9  (centered at $1B, clamped ±3)
    sector:<gics>    one-hot on GICS sector
    stock_flag       1 if consideration includes stock
    hostile_flag     1 if attitude is not friendly
    cross_border     1 if acquirer and target countries differ
"""
from __future__ import annotations

import math
import logging
from typing import Optional

from config.calibration import load_calibration

logger = logging.getLogger(__name__)


def build_features(
    deal_value_usd: Optional[float],
    gics_sector: Optional[str],
    stock_flag: bool,
    hostile_flag: bool,
    cross_border: bool,
) -> dict[str, float]:
    """Build the AFT feature vector. Shared contract with fit_model."""
    x: dict[str, float] = {"intercept": 1.0}
    if deal_value_usd and deal_value_usd > 0:
        lv = math.log10(deal_value_usd) - 9.0
        x["log_value"] = max(-3.0, min(3.0, lv))
    else:
        x["log_value"] = 0.0
    if gics_sector:
        x[f"sector:{gics_sector.strip()}"] = 1.0
    x["stock_flag"] = 1.0 if stock_flag else 0.0
    x["hostile_flag"] = 1.0 if hostile_flag else 0.0
    x["cross_border"] = 1.0 if cross_border else 0.0
    return x


def predict_quantiles(deal_params) -> Optional[dict]:
    """Per-deal duration quantiles from the fitted AFT model.

    Returns {"p10","p25","p50","p75","p90"} in days, or None if no
    fitted model is available in calibration.json.
    """
    model = load_calibration().get("aft_model")
    if not model or not model.get("coefficients"):
        return None

    coef = model["coefficients"]
    resid_q = model.get("residual_quantiles") or {}
    if "p50" not in resid_q:
        return None

    stock_flag = str(
        getattr(deal_params, "deal_structure", "")
    ).lower() in ("stock", "mixed", "dealstructure.stock",
                  "dealstructure.mixed")
    hostile_flag = (
        (deal_params.deal_attitude or "Friendly").lower()
        != "friendly"
    )
    cross_border = bool(
        deal_params.acquirer_country
        and deal_params.target_country
        and deal_params.acquirer_country
        != deal_params.target_country
    )

    x = build_features(
        deal_params.deal_value_usd,
        deal_params.gics_sector or deal_params.sector,
        stock_flag,
        hostile_flag,
        cross_border,
    )

    mu = sum(coef.get(k, 0.0) * v for k, v in x.items())
    if not (0.5 < mu < 8.0):  # e^mu in (1.6, 2981) days sanity band
        logger.warning(f"AFT mu={mu:.2f} out of sanity band, skipping")
        return None

    out = {}
    for key in ("p10", "p25", "p50", "p75", "p90"):
        r = resid_q.get(key)
        if r is None:
            continue
        out[key] = math.exp(mu + float(r))
    return out if "p50" in out else None
