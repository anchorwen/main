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
| FIX-20260527-010 | 2026-05-27 | cursor-agent | — | RegimeGate.default_fail_closed() static factory: returns a RegimeGate with all strategies locked to "shadow" across all regimes (trending/mild_trend/ranging/high_vol/normal). Used by live_cycle.py when regime computation fails beyond stale tolerance. Blocks new position entries while allowing Exit Manager to continue managing existing positions (stop-loss, take-profit, trailing stops). Architect guardrail: entries fail-closed, exits remain fail-open. | RC-06 |
| FIX-20260527-005 | 2026-05-27 | cursor-agent | — | Cold exploration trailing bypass: Layer 1 Chandelier trail skipped when `pos.cold_explore=True`. Mean-reversion `trail_atr_mult_low` 1.2→1.8 — anti-intuitive: low vol = sticky noise, tight trail = decapitation by white noise. Breakeven ATR 0.5 defended (architect VETO on 0.3 — friction death). | RC-09, RC-12 |
| FIX-20260527-004 | 2026-05-27 | cursor-agent | — | P0: Regime modulation override fixed — `_evaluate_strategy_lines()` now uses minimum-privilege gate fusion via `get_stricter_mode(base_mode, global_mode)`. Continuous modulation can only tighten (full→reduced→shadow), never relax a discrete hardware lock. `RegimeGate()` now receives live.yaml `regime_map` (was hardcoded default). `classify()` strategy list auto-discovered from regime_map keys (was hardcoded 5 strategies, missing statarb_m15/barrier_12bar_meta). `get_strategy_mode()` handles YAML booleans (false→shadow, true→full). Hot-reload applies regime_map updates to running RegimeGate. | RC-06 |
| FIX-20260522-023 | 2026-05-22 | cursor-agent | 24ff517 | Batch mypy type safety: annotation fixes, None guards, type narrowing | type-confusion |

## Cross-Module Contracts
| Contract | Consumers | Stability |
|----------|-----------|----------|
| `RegimeDetector.update(atr_value)` → `str` (regime label) | live_cycle | Stable |
| `compute_continuous_regime_modulation(...)` → `RegimeModulation` | strategy_line | Stable |

## Verification
```bash
python -m pytest tests/ -k "regime" -q
```
