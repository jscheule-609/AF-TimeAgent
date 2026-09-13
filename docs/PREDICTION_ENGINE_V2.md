# Prediction Engine v2 (model_version 0.2.0)

> 2026-07-13 rework following the TimeAgent-vs-MARS review.
> Replaces the additive-percentile / max-of-medians heuristic with a
> Monte Carlo mixture model, anchored on management guidance and
> conditioned on observed milestones.

## Why v1 was replaced

The 2026-07-13 review found, empirically (mars-db):

- 153 predictions (April 2026 batch), **zero ever scored** — the
  outcome columns in `timing_predictions` were never backfilled.
- 28 of the 153 deals were already closed before the batch ran;
  some P50s were up to **407 days in the past** at prediction time
  (no floor at "today", stale deal statuses).
- Per-deal P50→P90 spread (~167–205d) was **wider than the
  unconditional corpus spread** (109→247d = 138d): the model added
  ~zero information over the base rate.
- Guidance-only baseline on 4,157 closed deals: **MAE 60d**, median
  50d, 77.9% close by guidance — and guidance never touched the
  headline numbers.
- `calibration.json` was inert: durations null (generated before
  regulatory dates were loaded), observed HSR rate never wired,
  activation rates below threshold.

Structurally, v1 percentiles were incoherent: component percentiles
were summed along paths (assumes perfect rank correlation → inflated
tails) and four alternative estimators were combined with `max()`
(the max of medians is not the median of the max).

## v2 architecture

`scoring/distribution.py` builds a sampled close-date distribution
(N=4000, seeded by deal_pk for reproducibility):

```
regulatory constraint   = empirical(announce→filing + filing→clear)
                          ⊕50/50 state-machine max-across-jurisdictions
proxy constraint        = empirical(announce→vote + vote→close)
mechanism track         = max(regulatory, proxy) + DMA close gap
corpus track            = AFT covariate model (fallback: empirical
                          announcement→close comps)
guidance track          = guidance midpoint + fitted residual dist
final distribution      = weighted mixture(mechanism, corpus, guidance)
P50/P75/P90             = empirical quantiles of the final samples
```

Key rules:

- **Constraints combine by per-sample `max`** (closing requires
  clearance AND vote) — structurally correct.
- **Alternative estimators combine as a mixture** (model averaging),
  never `max` — weights in `calibration.json → track_weights`
  (default 0.35 / 0.25 / 0.40).
- **Guidance finally matters**: the guidance track samples
  `anchor + residual`, where residuals are fitted quantiles of
  (actual − guided midpoint) over ~1,900 closed deals with guidance.
- **Mid-deal conditioning** (`as_of`, `observed_milestones`):
  observed filing/clearance/vote dates collapse their components;
  elapsed time truncates the distribution (a live prediction can
  never be in the past). `as_of=None` = day-0 semantics (backtests).
- **Overdue fallback**: when a deal has outlived nearly all samples,
  switch to an "imminent but uncertain" distribution instead of
  degenerate conditioning.

## Fitting (scripts/fit_model.py)

```
python -m scripts.fit_model                      # production fit
python -m scripts.fit_model --cutoff 2024-07-01  # backtest hygiene
```

Writes `config/calibration.json`:

- regulatory rates + duration percentiles (via calibration_report,
  whose queries now exclude negative/absurd durations — ~104 rows
  had clearance before filing)
- `guidance.residual_quantiles` — parsed with the SAME parser used
  at predict time (step2b.parse_guidance, later-of-AJ/company)
- `aft_model` — log-OLS AFT on closed deals (features: log value,
  GICS sector, stock/hostile/cross-border flags; feature contract
  shared with `scoring/aft.py`); terminated deals are treated as
  censored and dropped (competing-risk modeling is BreakAgent's
  domain)
- `track_weights` — mixture weights (tunable via backtest)

## Backtesting (scripts/backtest.py)

Now exercises the PRODUCTION path (timeline_stats + guidance +
mixture), validates by deal_pk (closed-deal targets are delisted, so
SEC ticker resolution fails), supports `--start/--end` time windows
to pair with `fit_model --cutoff`, and prints the guidance-only
baseline the model must beat.

## Known limitations / next steps

- AFT ignores censoring (pending deals) — a proper survival
  likelihood (lognormal AFT with right-censoring) is the next step.
- PUC and most non-antitrust regulators still route to
  GenericStateMachine (45–90d); the mixture bounds the damage but
  jurisdiction-specific machines for PUC/banking remain to do.
- Track weights are hand-set defaults; grid-search them against the
  backtest once enough scored predictions accumulate.
- `update_prediction_actuals` still needs a scheduled job that joins
  freshly-closed deals to open predictions (see AF-AJ weekly update).
