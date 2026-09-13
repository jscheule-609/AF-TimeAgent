# Regulatory applicability

Population activation rates describe the probability that a jurisdiction applies.
They do not establish a required filing for an individual deal. Calibrated SAMR
and CFIUS entries above the existing activation floor are optional requirements:
`applicability = confidence = observed rate`. Required entries always have
applicability 1.0, regardless of confidence in their evidence. The sector keyword
CFIUS entry uses the same mechanism at 0.4 when document ingestion is enabled.

Step 5.5 simulates every mapped requirement. For applicability p below 1, it
prepends a clear-terminal `not applicable` path with empty states, zero duration
at every percentile, and probability 1-p. Retained machine paths are normalized
(enumeration can prune tiny branches) and weighted by p. The existing shared
probability-weighted duration calculation is then applied to the full path set.
Required state-machine paths and durations are unchanged. One INFO entry per
deal records every simulated applicability below 1.

The timing sampler draws zero for a zero-duration path; its existing conditioning
on eventual clearance still excludes blocked paths. Consequently sampled
applicability can be slightly below p when some applicable paths are blocked.
For 3.25% CFIUS, this creates a rare tail instead of an approximately 78-day floor.

Critical-path labels select the longest expected P50 among applicability >= 0.5.
If none qualify, HSR is preferred, then the longest modeled jurisdiction. The
existing P75/P90 bottleneck summaries still consider all simulations. Filing and
clearance milestone rows below 0.5 are omitted; `jurisdictions_modeled` continues
to include everything simulated. A not-applicable path can contribute to a joint
clean scenario, but never appears as a standalone named scenario. Risk-flag rules
are unchanged and operate on the reweighted paths.

Use `python -m scripts.backtest --no-persist ...` for experimental validation.
This skips both step 7 logging and actuals updates, including `--single`, while
still writing the local results JSON. The default retains existing persistence.
Fit only in a throwaway container; never refit the live container or commit a
changed calibration file for this applicability change.
