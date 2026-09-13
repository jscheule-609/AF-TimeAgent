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

## Validation on 2026-09-13

The scratch image built from revision `dd706bd` completed the same 120 deal PKs
as both references with the LLM leg off, cutoff `2024-07-01`, and `--no-persist`.
Only the throwaway container was fitted. Its CFIUS rate was 0.032407407407407406;
the committed calibration remains unchanged. No weights or durations were tuned.

| Metric | July | LLM-off | cfius_fix |
| --- | ---: | ---: | ---: |
| Successful / attempted | 120/120 | 120/120 | 120/120 |
| MAE (days) | 41.55 | 39.98 | 38.92 |
| Median absolute error (days) | 37.5 | 35.0 | 33.5 |
| Bias, actual minus predicted (days) | -12.68 | -11.26 | -9.38 |
| P50 coverage | 81/120 (67.5%) | 81/120 (67.5%) | 80/120 (66.7%) |
| P75 coverage | 111/120 (92.5%) | 109/120 (90.8%) | 109/120 (90.8%) |
| P90 coverage | 118/120 (98.3%) | 119/120 (99.2%) | 119/120 (99.2%) |
| Guidance MAE (days; n=120) | 80.88 | 80.88 | 80.88 |
| Critical paths | CFIUS 112; HSR 8 | CFIUS 114; HSR 6 | HSR 115; GENERIC 5; CFIUS 0 |
| Overlap counts | none/none 117; horizontal/high 3 | none/none 120 | none/none 120 |

All acceptance thresholds pass. Results are in
`backtest_results/backtest_2026-09-13_cfius_fix.json`; every prediction ID is null.
The count and MD5 fingerprint of complete `timing_predictions` rows for these
120 PKs were identical before/after: `120|95d5f78a414a199063e6ded3f61d9bde`.
The experiment log contains 120 applicability entries and 120 distributions,
with zero failed deals. DB-free tests increased from 70 to 91 passing.

The five existing GENERIC labels occur on NBBK/PBCP, COLB/PPBI, FBK/SSBK,
BPE/BPSO, and BMPS/MB. Generic-machine label naming is unchanged by this fix.

After merging and deployment, Justen should relabel the active deals with
`docker exec timeagent python -m scripts.batch_run`, then run
`docker exec timeagent python -m scripts.score_predictions --apply --rescore`.
These live batch commands were not run during validation.
