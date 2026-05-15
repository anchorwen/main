# Features / Rolling

## Purpose
Feature normalization and computation: rolling EWMA normalizer, warmup period handling, and data augmentation utilities.

## Key Files
| File | Role |
|------|------|
| `core/features/rolling_normalizer.py` | `RollingNormalizer` — EWMA-based feature normalization |
| `core/features/data_augmentation.py` | Data augmentation utilities |
| `core/constants.py` | `WARMUP_BARS` (100), `EWMA_HALFLIFE_BARS` (18144) |

## Data Flow
```
Raw feature values → RollingNormalizer.update() → normalized features
                         ↓
                  warmup period (100 bars, full-sample stats)
                         ↓
                  EWMA period (halflife=18144 bars ≈ 63 trading days)
```

## Inbound Dependencies
| Module | What is imported | Why |
|--------|-----------------|-----|
| — | — | Self-contained; numpy only |

## Outbound Dependents
| Module | What it imports | Why |
|--------|-----------------|-----|
| features/service | RollingNormalizer | Feature normalization pipeline |
| features/adapters | (via normalization configs) | Per-adapter normalization |

## Known Issues

## Fix History
| Fix ID | Date | Author | Commit | Summary | Root Cause |

## Cross-Module Contracts
| Contract | Consumers | Stability |
|----------|-----------|----------|
| `RollingNormalizer.update(feature_vector)` → `np.ndarray` | FeatureService | Stable |
| `RollingNormalizer.warmup_complete` → `bool` | FeatureService | Stable |

## Verification
```bash
python -m pytest tests/ -k "normaliz" -q
```
