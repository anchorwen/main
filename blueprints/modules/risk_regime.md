# Risk / Regime

## Purpose
Online volatility regime classification using ATR-based rolling percentile with confirmation hysteresis. Also provides continuous regime modulation (trend, vol, hurst, variance ratio) and Kalman trend filtering.

## Key Files
| File | Role |
|------|------|
| `core/risk/regime_detector.py` | `RegimeDetector` — ATR volatility regime with EWMA + rolling percentile |
| `core/execution/regime_gate.py` | `compute_continuous_regime_modulation()` — 4D regime modulation |
| `core/execution/trend_detector.py` | `KalmanTrendFilter`, `Hurst`, `TrendDetector` |
| `core/constants.py` | `REGIME_LOOKBACK_BARS`, `REGIME_CONFIRM_BARS`, `REGIME_EXIT_BARS`, `REGIME_RATE_LIMIT_CYCLES` |

## Data Flow
```
Market bars → RegimeDetector.update(bar) → regime_label (low/normal/high vol)
                 ↓
          RegimeGate.modulate() → continuous 4D modulation factors
```

## Inbound Dependencies
| Module | What is imported | Why |
|--------|-----------------|-----|
| — | — | Self-contained; uses only numpy/stdlib |

## Outbound Dependents
| Module | What it imports | Why |
|--------|-----------------|-----|
| execution/strategy_line | RegimeGate | Strategy-level gating |
| runtime/live_cycle | RegimeDetector, RegimeGate | Live trading cycle |

## Known Issues

## Fix History
| Fix ID | Date | Author | Commit | Summary | Root Cause |

## Cross-Module Contracts
| Contract | Consumers | Stability |
|----------|-----------|----------|
| `RegimeDetector.update(atr_value)` → `str` (regime label) | live_cycle | Stable |
| `compute_continuous_regime_modulation(...)` → `RegimeModulation` | strategy_line | Stable |

## Verification
```bash
python -m pytest tests/ -k "regime" -q
```
