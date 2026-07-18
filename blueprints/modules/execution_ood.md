# Execution / OOD Gateway

## Purpose
Feature-Space Out-of-Distribution (OOD) detection using Mahalanobis distance. When the live feature vector drifts beyond the training distribution, the model automatically falls silent — confidence=0, direction=neutral, reason="regime_ood_blocked".

**Institutional mandate (DQAF-20260705-064 P2)**: The system must possess immunity against trading in unknown feature manifolds. P0 (kill switch) is emergency amputation; P1 (netting) is physical isolation; P2 (OOD) is the immune system.

## Key Files
| File | Role |
|------|------|
| `core/execution/ood_gateway.py` | OODGateway, OODConfig, OODVerdict — Mahalanobis distance computation |
| `scripts/export_ood_params.py` | Offline calibration — feature store → centroid + covariance + thresholds |
| `core/runtime/strategy_evaluator.py` | Integration point — Cut 2 pre-inference gates |
| `data_btc/models/ood_{schema}.json` | Calibrated OOD parameters per feature schema |

## Data Flow
```
Offline (one-time):
  feature_store → export_ood_params.py → centroid + inv_cov + thresholds
                                              ↓
                                    data_btc/models/ood_{schema}.json

Online (every cycle):
  feature_vector → repair_feature_vector() → check_feature_vector()
                                              ↓
                                  ★ OOD GATEWAY ★
                                  Mahalanobis(fv, centroid, inv_cov)
                                              ↓
                      d < 2σ → NORMAL     → inference proceeds
                      2σ ≤ d < 3σ → CAUTIOUS → inference with dampened confidence
                      d ≥ 3σ → BLOCKED    → model silent (neutral, confidence=0)
```

## Algorithm
Mahalanobis distance: d = sqrt((x - μ)^T · Σ^{-1} · (x - μ))

Under multivariate normality, d² ~ χ²(k) where k = feature dimension.

Thresholds:
- BLOCK: χ²_{0.99}(k) — P99 of training distribution
- CAUTIOUS: χ²_{0.95}(k) — P95 of training distribution

For the diagonal covariance fallback (insufficient samples for full matrix):
d² = Σ_i ((x_i - μ_i) / σ_i)²  (normalized Euclidean distance)

## Calibrated Schemas
| Schema | Features | Samples | Block (P99) | Cautious (P95) | Covariance | Window |
|--------|----------|---------|-------------|----------------|------------|--------|
| v9_institutional_40 | 40 | 870 | 15.07 | 9.63 | Full (40×40) | 30-day rolling |
| v4.3_microstructure_9 | 9 | 8,052 | 8.20 | 4.47 | Full (9×9) | 30-day rolling |

*Threshold method: empirical P95/P99 (DQAF-20260716-001). Chi2 theoretical values replaced due to fat-tail violation in financial data. FIX-20260720-001: rolling-window recalibration replaces static full-history calibration — gate adapts to secular regime shifts while still catching sudden anomalies.*

## Inbound Dependencies
| Module | What is imported | Why |
|--------|-----------------|-----|
| — | — | Self-contained — operates on numpy arrays |

## Outbound Dependents
| Module | What it imports | Why |
|--------|-----------------|-----|
| runtime/strategy_evaluator | OODGateway (via _get_ood_gateway) | Cut 2 pre-inference OOD check |

## Cross-Module Contracts
| Contract | Consumers | Stability |
|----------|-----------|----------|
| `OODGateway.check(fv, schema_name)` → `OODVerdict` | strategy_evaluator | Stable |
| `export_ood_params.py --data-dir --min-samples` | CLI / CI pipeline | Stable |

## Relationship to Other Defenses
| Layer | Component | What it catches |
|-------|-----------|----------------|
| Cut 1 | repair_feature_vector() | NaN/Inf in feature vector |
| Cut 2 | check_feature_vector() | Extreme outliers (Z > 50) |
| Cut 2 | Extreme value gate | Float overflow (abs > 1e6) |
| **Cut 2.5** | **OOD Gateway** | **Regime shift / distribution drift** |
| Adapter | Zero-vector guard | Silent FeatureService fallback |
| Adapter | Dimension guard | Schema mismatch |

OOD Gateway fills the gap between per-value sanity checks and model inference — it detects when the ENTIRE feature vector represents a market regime the model has never seen.

## Verification
```bash
python -m pytest tests/test_ood_gateway.py -q
python scripts/export_ood_params.py --data-dir data_btc --dry-run
```

## Fix History
| Fix ID | Date | Author | Commit | Summary | Root Cause |
|--------|------|--------|--------|---------|------------|
| FIX-20260720-001 | 2026-07-20 | cursor-agent | bd8c3244 | L2: Rolling-window OOD recalibration. export_ood_params.py: added --max-age-days for rolling-window calibration. OODGateway: added invalidate_cache() + mtime-based auto-invalidation. Recalibrated v9_institutional_40 with 30-day window (870 samples, block=15.07). Static full-history calibration caused permanent block when BTC entered low-vol regime (ATR 45 vs centroid 145). | config-drift |
| FIX-20260716-001 | 2026-07-16 | cursor-agent | — | **L3: OOD threshold tuning — chi2→empirical percentile**. `calibrate()`: added `threshold_method` parameter (default "empirical"). Uses P95/P99 of calibration distances instead of chi2 theoretical values. Chi2 assumes multivariate normality violated by financial fat tails (54.6% false-positive block). v9_institutional_40: 7.47/7.98→8.82/12.64, recent-1000 block 11.3%→1.2%. DQAF-20260716-001. | L3 — chi2 theoretical thresholds incorrect for fat-tailed financial data |
| FIX-20260705-065 | 2026-07-05 | cursor-agent | — | **P2: Feature-Space OOD Gateway — Mahalanobis distance regime-shift immunity**. Offline: export_ood_params.py reads 11,384 BTC feature store records, computes centroid + full 40×40 covariance per schema. Online: OODGateway.check() integrated into strategy_evaluator.py Cut 2. 3σ chi2 threshold → REGIME_OOD_BLOCKED. 12/12 TDD tests. See DQAF-20260705-064 for the 7-day directional lock that motivated this defense. | RC-12 — no automated OOD detection |
