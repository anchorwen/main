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
| FIX-20260604-082 | 2026-06-04 | cursor-agent | — | **OU mean-reversion revival**: re-enabled statarb_dynamic + statarb_m15. Added Gate 1b in strategy_line.py: OU strategies blocked when detected_regime=="high" — prevents catching-falling-knife in trending crashes. | RC-05 |
| FIX-20260531-002 | 2026-05-31 | cursor-agent | — | BTC intent loop cycle_error: `'numpy.void' object has no attribute 'get'` in regime_gate.py:454 `feed_m5_bars_batch()`. MT5 `copy_rates_from_pos()` returns numpy structured arrays, not dicts. The `.get("high", b["close"])` pattern fails because numpy.void has no `.get()`. Added `_get_field()` static helper with try/except for both dict and numpy.void access, ported `feed_h1_bars_batch()` to same safe pattern. | RC-06 (type-confusion: numpy-vs-dict) |
| FIX-20260527-010 | 2026-05-27 | cursor-agent | — | RegimeGate.default_fail_closed() static factory: returns a RegimeGate with all strategies locked to "shadow" across all regimes | RC-06 |
| FIX-20260527-005 | 2026-05-27 | cursor-agent | — | Cold exploration trailing bypass: Layer 1 Chandelier trail skipped when `pos.cold_explore=True`. Mean-reversion `trail_atr_mult_low` 1.2→1.8 — anti-intuitive: low vol = sticky noise, tight trail = decapitation by white noise. Breakeven ATR 0.5 defended (architect VETO on 0.3 — friction death). | RC-09, RC-12 |
| FIX-20260527-004 | 2026-05-27 | cursor-agent | — | P0: Regime modulation override fixed — `_evaluate_strategy_lines()` now uses minimum-privilege gate fusion via `get_stricter_mode(base_mode, global_mode)`. Continuous modulation can only tighten (full→reduced→shadow), never relax a discrete hardware lock. `RegimeGate()` now receives live.yaml `regime_map` (was hardcoded default). `classify()` strategy list auto-discovered from regime_map keys (was hardcoded 5 strategies, missing statarb_m15/barrier_12bar_meta). `get_strategy_mode()` handles YAML booleans (false→shadow, true→full). Hot-reload applies regime_map updates to running RegimeGate. | RC-06 |
| FIX-20260528-020 | 2026-05-28 | cursor-agent | — | Direction-blind regime gate for statarb strategies: `live.yaml` trending regime map changed `statarb_dynamic`/`statarb_m15` from `false` (hard shadow lock) to `"reduced"`. `_OU_REGIME_MATRIX` trending cells changed from `(0.0, "off")` to `(0.35/0.25, "reduced")`. Default regime_map in RegimeGate.__init__ aligned. The existing direction-aware counter_trend check in strategy_line.py:945-980 already correctly distinguishes with-trend SHORT (allow) vs counter-trend LONG (block). SHORT was previously killed by the direction-blind shadow lock before reaching the counter_trend check. | direction-blind gate |
| FIX-20260522-023 | 2026-05-22 | cursor-agent | 24ff517 | Batch mypy type safety: annotation fixes, None guards, type narrowing | type-confusion |
| FIX-20260529-026 | 2026-05-29 | cursor-agent | — | FIFO buffer eviction bias: replaced `bisect.insort()` sorted list with `collections.deque(maxlen=window)` + `numpy` vectorized percentile scan. The old approach used `pop(0)` on a sorted buffer, which removes the smallest ATR value instead of the oldest — causing systematic upward drift in volatility percentile estimates and "low vol false-positive" regime gating. For a 500-element window, the C-level scan costs ≈3 µs (negligible at M5 cadence). | RC-06 |

## Cross-Module Contracts
| Contract | Consumers | Stability |
|----------|-----------|----------|
| `RegimeDetector.update(atr_value)` → `str` (regime label) | live_cycle | Stable |
| `compute_continuous_regime_modulation(...)` → `RegimeModulation` | strategy_line | Stable |

## Verification
```bash
python -m pytest tests/ -k "regime" -q
```
