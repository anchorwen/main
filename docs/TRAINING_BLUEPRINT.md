# Training Blueprint: Institutional-Grade Model Training Methodology

## Design Philosophy

Three core principles govern how we train models:

### Principle 1: Label Contract is the Mathematical Expression of a Strategy

> "Tell me how you label, and I'll tell you what your strategy earns." — Quant consensus

A Label Contract is not just a parameter file. It is the **formal specification of trading logic**:
- One contract = one falsifiable hypothesis ("If 2xATR SL + 3.5xATR TP on M5 cannot be profitable, this strategy is invalid")
- The model is merely a statistical approximator of this hypothesis
- Contract versioning → model reproducibility → strategy auditability

### Principle 2: Everything is Defined in Volatility Space

> Price is a function of time. Volatility is a function of price uncertainty. Models should learn the latter.

All distances and thresholds use ATR multiples, never absolute points:
- **Features**: ATR%, MACD/Price, normalized returns
- **Labels**: SL=2.0xATR, not SL=$15
- **Evaluation**: Per-ATR-regime metrics, not global accuracy

### Principle 3: Recipe is the Single Entry Point for Training

> Not `python train.py --lr 0.001 --epochs 200`, but a structured JSON that defines everything.

One Recipe = one complete, reproducible training run. The difference between two recipes = the difference between two models (ablation experiment).

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                    TRAINING RECIPE                           │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐   │
│  │Model Identity│  │Label Contract│  │   Data Config    │   │
│  │ lane, role,  │  │ barrier def, │  │ date range, norm │   │
│  │ generation   │  │ horizon, ATR │  │ augmentation     │   │
│  └──────┬───────┘  └──────┬───────┘  └────────┬─────────┘   │
│         │                 │                    │             │
│         ▼                 ▼                    ▼             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐   │
│  │  Training    │  │  Evaluation  │  │   Artifacts      │   │
│  │ arch, lr,    │  │ per-regime,  │  │ ONNX, manifest,  │   │
│  │ epochs, etc  │  │ stability    │  │ lineage record   │   │
│  └──────────────┘  └──────────────┘  └──────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

### Four-Layer Model

| Layer | Component | Responsibility |
|-------|-----------|----------------|
| 1. Contract | `LabelContract` | Define what "correct" means |
| 2. Recipe | `TrainingRecipe` | Define how to train |
| 3. Pipeline | `e2e_pipeline_validation.py` | 7-stage automated validation |
| 4. Governance | `BrainPromotionEvaluator` | Candidate → Probation → Active |

## Label Contract System

### Schema: `schemas/label_contract.v1.schema.json`

A Label Contract defines:
- **contract_id** — unique identifier
- **type** — `survival_barrier` | `regression` | `binary_class`
- **barriers** — SL/TP in ATR multiples
- **horizon** — forward window (bars + timeframe)
- **atr_config** — ATR period and timeframe
- **label_classes** — class mapping

### Core Algorithm: `build_barrier_labels()`

```
Given: OHLC arrays, entry_idx, side (long/short)
1. Compute ATR(14) at entry from preceding bars
2. Set SL = entry ± sl_atr_mult × ATR
3. Set TP = entry ∓ tp_atr_mult × ATR
4. Walk forward up to horizon_bars:
   - If bar low ≤ SL (long) or bar high ≥ SL (short): label = "sl_hit_first"
   - If bar high ≥ TP (long) or bar low ≤ TP (short): label = "tp_hit_first"
5. If neither hit: label = "timeout"
```

### Current Contracts

| Contract ID | Type | SL | TP | Horizon | Lane |
|------------|------|-----|-----|---------|------|
| `label-survival-barrier-1.0.0` | survival_barrier | 2.0xATR | 3.5xATR | 12×M5 | sur |

**Note**: These are BASE values. Live trading adjusts per volatility regime via `RegimeDetector`:
- Low vol: SL×1.00, TP×0.95
- Normal: SL×0.80, TP×0.85
- High vol: SL×0.55, TP×0.60

## Training Recipe System

### Schema: `schemas/training_recipe.v1.schema.json`

Four-section structure:

1. **model_identity** — lane, role, generation, feature_contract
2. **label_contract_ref** — reference to label contract
3. **data** — date range, normalization, augmentation
4. **training** — architecture, hyperparameters, optimizer
5. **evaluation** — per-regime metrics, stability checks

### Current Recipes

| Recipe ID | Lane | Role | Architecture | Features |
|-----------|------|------|-------------|----------|
| `CRT.sur.chlg.g2026.1` | sur | chlg | mlp_multihead | V9 Institutional 40 |

### Key Configuration

```
Architecture:    MLP Multi-Head (128→64→32)
Features:        40-dim V9 Institutional (M5/M15/M30/H1)
Loss:            direction(1.0) + risk(0.5) + volatility(0.3)
Optimizer:       Adam, lr=0.001
Augmentation:    Volatility scaling ×5 + Gaussian noise
Ensemble:        5 seeds for stability assessment
```

## End-to-End Training Pipeline

### 7-Stage Validation (`scripts/training/e2e_pipeline_validation.py`)

| Stage | Description | Gate |
|-------|-------------|------|
| 1 | Generate synthetic price data | Valid OHLC + ATR |
| 2 | Build barrier labels from contract | Label distribution |
| 3 | Populate feature store | Feature completeness |
| 4 | Build training dataset | Train/val split |
| 5 | Quality gate | 6 CI checks |
| 6 | Optuna hyperparameter search | Best params found |
| 7 | Train final model | ONNX export |

### Quality Gate Checks

1. **min_samples** — ≥100 per class
2. **label_balance** — no class <10%
3. **feature_validity** — no NaN/Inf columns
4. **feature_coverage** — ≥95% non-null
5. **temporal_integrity** — train < val dates
6. **contract_compliance** — labels match contract specification

## Per-Regime SL/TP Adjustment

### Regime Detection (`core/risk/regime_detector.py`)

EWMA-based online volatility regime classification:
- Tracks ATR(14) mean and variance with configurable halflife (default: 63 days)
- Classifies each bar via z-score: low (z<-0.43), normal (-0.43≤z≤0.43), high (z>0.43)
- Thresholds produce ~33/33/33 percentile split for normal distributions

### Calibrated Multipliers (2026-05-05)

Grid search on OU price paths with 42%-accuracy synthetic signal:

| Regime | SL Multiplier | TP Multiplier | RR Ratio |
|--------|--------------|---------------|----------|
| Low | 2.00 | 3.33 | 1.67 |
| Normal | 1.60 | 2.98 | 1.86 |
| High | 1.10 | 2.10 | 1.91 |

Calibration methodology and results: `data/reports/sl_tp_calibration.json`

## Live Training Governance

### Brain Lifecycle State Machine

```
candidate ──(WR≥40%, PF≥0.90, ≥20 signals)──▶ probation
    │                                              │
    │                                              ▼
    └──────────────────────────────▶ active ◀──(WR≥45%, PF≥1.10, ≥50 signals)
                                       │
                                       ▼
                                   throttled / retired
```

### Promotion Thresholds

| State | Min Signals | Min WR | Min PF | Max Cons Losses |
|-------|------------|--------|--------|-----------------|
| candidate→probation | 20 | 40% | 0.90 | — |
| probation→active | 50 | 45% | 1.10 | — |
| retire (any) | 20 | <30% | <0.60 | >8 |

### Evaluation Flow

```
live_intent_loop → dispatch → fill → BrainPerformanceTracker
                                         │
                                         ▼
                              daily_ops.py ──→ governance_scheduler.py
                              (feedback_loop)   (health_signal)
                                         │
                                         ▼
                              brain_promotion_runner.py
                              (state machine evaluation)
                                         │
                                         ▼
                              governance_state.json
                              (candidate→probation→active)
```

## Integration with Live Trading

### live_intent_loop.py

```
Each cycle:
1. RollingNormalizer.update(features)      ← online adaptive normalization
2. RegimeDetector.update(atr)              ← volatility regime classification
3. sl_mult, tp_mult = detector.get_adjusted_multipliers(regime_info)
4. compute_sl_tp_for_side(side, sl_mult, tp_mult, current_atr)
5. Dispatch order with regime-adjusted SL/TP
```

### Feature Pipeline

40 features across 4 timeframes:
- **M5**: Ret_1, Body_Ratio, ATR_14, RSI_14, MACD, Vol_ZScore, Macro1_Corr, Macro_Gold_Silver_Spread, OU_Theta, Hurst
- **M15**: Same 10 features
- **M30**: Same 10 features
- **H1**: Same 10 features

## File Index

| File | Purpose |
|------|---------|
| `schemas/label_contract.v1.schema.json` | Label contract JSON schema |
| `schemas/training_recipe.v1.schema.json` | Training recipe JSON schema |
| `core/contracts/training/label_contract.py` | LabelContract Pydantic model + barrier algorithm |
| `core/contracts/training/training_recipe.py` | TrainingRecipe Pydantic model + CLI generator |
| `configs/training/label_contracts/` | Instantiated label contracts |
| `configs/training/recipes/` | Instantiated training recipes |
| `scripts/training/label_builder.py` | Batch label generation from contracts |
| `scripts/training/e2e_pipeline_validation.py` | 7-stage pipeline validation |
| `scripts/training/calibrate_sl_tp.py` | SL/TP grid search calibration |
| `scripts/training/quality_gate.py` | 6-check CI quality gate |
| `scripts/training/brain_promotion_runner.py` | Brain lifecycle evaluation |
| `core/risk/regime_detector.py` | ATR-based volatility regime classifier |
| `core/features/rolling_normalizer.py` | EWMA online feature normalization |
| `core/brains/services/brain_promotion.py` | Brain state machine evaluator |

## References

- Two Sigma: Research Environment — alpha as falsifiable hypothesis
- WorldQuant: Alpha Factory — feature rank-normalization
- Citadel: Regime-conditional evaluation
- XTX Markets: Online adaptation with rolling normalization
- Man AHL: Barrier labels as strategy definition
- DeepMind (AlphaZero): Data augmentation as distributional robustness
