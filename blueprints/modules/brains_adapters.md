# Brains / Adapters

## Purpose
Model inference adapters that wrap diverse brain backends (XGBoost, LightGBM, Transformer, ONNX, OnlineMLP, SGD) behind a uniform `BaseBrainAdapter` interface. All inference paths now converge through `BrainRunService` — no other code path calls `adapter.infer()` directly.

## Architecture Overview

```
ALL consumers (live, shadow, backtest, verify)
  → BrainRunService.run_active_brains(feature_snapshot, control_snapshot, feature_blackboard)
    → BrainFactory.build(entry) → BrainConfigValidator.validate(entry) → adapter.load()
    → adapter.run(feature_snapshot, feature_dict)     # metadata-driven extraction
      → feature_names = brain_entry["features"]        # from config (single source of truth)
      → feature_vector = [feature_dict[name] for name in feature_names]
      → infer(feature_vector) → get_signal(raw_output) → BrainSignal
```

**Key principle**: Brain config JSON is the single source of truth for feature names, order, and dimensionality. No hardcoded schema imports in adapter code.

## Data Flow

```
BrainRegistryService → brain_entries → BrainFactory → adapters
                                                     ↓
                                             BrainRunService.run_active_brains()
                                                     ↓
                                             BrainSignal[]
                                                     ↓
                               Parliament / StrategyLine / Consensus
```

## Key Files

| File | Role |
|------|------|
| `core/brains/adapters/__init__.py` | Registry: `ADAPTER_REGISTRY`, `BRAIN_TYPE_MAP` (15 brain types → 6 adapters) |
| `core/brains/adapters/base_adapter.py` | Abstract interface: `load()`, `infer()`, `get_signal()`, `inference()`, `run()` (metadata-driven) |
| `core/brains/adapters/xgboost_brain_adapter.py` | XGBoost JSON booster, `_num_features` from feature_names, dimension guard + brain_alert |
| `core/brains/adapters/lightgbm_brain_adapter.py` | LightGBM .txt booster, three defense lines (name extraction → normalization → dim assertion) |
| `core/brains/adapters/v9_onnx_brain_adapter.py` | V9 institutional ONNX (classification + regression), `_num_features` from ONNX shape |
| `core/brains/adapters/transformer_brain_adapter.py` | QuantTransformer ONNX with rolling buffer, `_num_features` from ONNX shape |
| `core/brains/adapters/online_learner_adapter.py` | Dual-backend (SGD/MLP) with drift protection, dimension alert on truncation |
| `core/brains/adapters/params_brain_adapter.py` | OU process Z-Score from arb_params.json |
| `core/brains/services/brain_factory.py` | Builds adapters from config, integrates BrainConfigValidator |
| `core/brains/services/brain_run_service.py` | Unified inference entry point — single path for all consumers |
| `core/deployment/brain_config_validator.py` | Load-time config validation (7 checks) |
| `core/deployment/brain_alert.py` | Structured JSON alerts to stderr on any fallback/degradation |

## Feature Schema Reference

| Schema ID | Dim | Feature Names Source | Used By |
|-----------|-----|---------------------|---------|
| `v9_institutional_40` | 40 | `V9_INSTITUTIONAL_40_FEATURES` (M5/M15/M30/H1 technicals + OU/Hurst) | onnx_v9, deepresmlp, lightgbm_v1, xgboost_v9, online_sgd, crt_sur_chlg |
| `daily_swing_24` / `swing_24` | 24 | `DAILY_SWING_24_FEATURES` (D1 tech + H4 macro + cross-asset + derived) | lightgbm_h1_swing, xgboost_*_swing |
| `v4.3_microstructure_9` / `v4.5_microstructure_9` / `v2_microstructure_9` | 9 | `MICROSTRUCTURE_9_FEATURES` (tick/HL/CO/spread/OIM/velocity/3xFX) | transformer_v5, xgboost_v4.5 |
| `v2_microstructure_288` | 288 | `MICROSTRUCTURE_9_FEATURES × 32` (32-bar flat sequence) | xgboost_v4.5_h1/h4/m15 |
| `v6_price_series_1` | 1 | `["price_return"]` | ou_params_v6 |

**Schema aliases** (resolved automatically by `_resolve_schema_key()` in BrainRunService):
- `swing_24` → `daily_swing_24`
- `v2_microstructure_9` → `v4.3_microstructure_9`
- `v4.5_microstructure_9` → `v4.3_microstructure_9`

## Brain Type Reference

| brain_type | Adapter Class | Registry Key | Model Format | Schema |
|------------|--------------|-------------|-------------|--------|
| `onnx_v9` | V9OnnxBrainAdapter | onnx | ONNX | v9_institutional_40 (40) |
| `deepresmlp` | V9OnnxBrainAdapter | onnx | ONNX | v9_institutional_40 (40) |
| `xgboost_v4.5` | XGBoostBrainAdapter | xgboost_json | JSON booster | v4.3_microstructure_9 (9) |
| `xgboost_v4.5_m15` | XGBoostBrainAdapter | xgboost_json | JSON booster | v2_microstructure_288 (288) |
| `xgboost_v4.5_h1` | XGBoostBrainAdapter | xgboost_json | JSON booster | v2_microstructure_288 (288) |
| `xgboost_v4.5_h4` | XGBoostBrainAdapter | xgboost_json | JSON booster | v2_microstructure_288 (288) |
| `xgboost_v9` | XGBoostBrainAdapter | xgboost_json | JSON booster | v9_institutional_40 (40) |
| `lightgbm_v1` | LightGBMBrainAdapter | lightgbm_txt | .txt booster | v9_institutional_40 (40) |
| `ou_params_v6` | ParamsBrainAdapter | ou_params_json | JSON params | v6_price_series_1 (1) |
| `online_sgd` | OnlineLearnerAdapter | online_sgd | JSON weights | v9_institutional_40 (40) |
| `transformer_v4.3` | TransformerBrainAdapter | transformer_onnx | ONNX | v4.3_microstructure_9 (9) |
| `transformer_v5` | TransformerBrainAdapter | transformer_onnx | ONNX | v4.3_microstructure_9 (9) |
| `transformer_v5_m15` | TransformerBrainAdapter | transformer_onnx | ONNX | v2_microstructure_9 (9) |
| `transformer_v5_h1` | TransformerBrainAdapter | transformer_onnx | ONNX | v2_microstructure_9 (9) |
| `transformer_v5_h4` | TransformerBrainAdapter | transformer_onnx | ONNX | v2_microstructure_9 (9) |

## Brain Inference Pipeline (Unified)

All consumers use the same path:

```
BrainRunService.run_active_brains(feature_snapshot, control_snapshot, feature_blackboard)
  │
  ├─ ensure_loaded()  — BrainFactory.build() + BrainConfigValidator.validate()
  │
  └─ for each active brain entry:
       ├─ schema_id → _resolve_schema_key() → blackboard[schema_id] → feature_dict
       ├─ adapter.run(feature_snapshot, feature_dict)
       │   ├─ feature_names = brain_entry["features"]      (metadata-driven)
       │   ├─ feature_vector = [feature_dict[name] ...]     (ordered extraction)
       │   ├─ infer(feature_vector)                         (with dimension guard)
       │   └─ get_signal(raw_output)                        (BrainDecisionProposal)
       └─ on error → brain_alert (stderr) → continue
```

**Special methods**:
- `run_single_brain(brain_id, ...)` — OU exit re-evaluation, drift-lock
- `run_brain_type(brain_type, ...)` — first brain matching type
- `run_brains_for_contract_group(contract_group, ...)` — per-strategy filtering

## Diagnostic Manual

### Symptom: "Brain output constant / frozen confidence"

**Checklist**:
1. **Feature freshness**: Are features changing between cycles?
   - Check: `grep "FeatureService stale cache"` — fresh features being used?
   - Fix: If stale, ensure `check_feature_freshness()` is working (FIX-20260516-005)
2. **Feature names correct?**: Does `brain_entry["features"]` match the schema?
   - Check: `python scripts/repair_brain_configs.py --validate-only`
   - Common root cause: config had wrong feature names (e.g. D1_* instead of V9_*)
3. **Model loaded?**: Is `adapter._backend` showing a proper backend or `stub:*`?
   - Check: `grep "brain_alert.*model_load_failed"` — any load failures?
4. **Dimension match?**: Does `_num_features` match schema dimension?
   - Check: `grep "brain_alert.*feature_dimension_mismatch"`

### Symptom: "Brain in stub mode"

**Checklist**:
1. **Artifact path**: Does the file at `artifact_path` exist?
   - Check: `ls -la <artifact_path>`
2. **Model format**: Is the artifact the correct format for the brain_type?
3. **Dependencies**: Are xgboost/lightgbm/onnxruntime installed?

### Symptom: "Inference error: matmul dimension mismatch"

**Checklist**:
1. **Config `features` field length**: Does it match `feature_schema_id` dimension?
2. **Model `_num_features`**: Does booster/ONNX input match schema?
3. **Feature blackboard**: Is the right schema key present with correct feature count?

### Symptom: "Brain excluded from inference"

**Check**:
- `grep "brain_alert.*config_validation_error"` for specific validation errors
- Brain appears in `BrainRunService._failed_brain_ids` — will not be retried until `reload_adapters()`

## Brain Alert Types

| Alert Type | Meaning | Response |
|-----------|---------|----------|
| `config_validation_error` | Brain config failed load-time validation | Fix config JSON, reload |
| `feature_dimension_mismatch` | Feature vector dim != model expected dim | Check features field vs schema |
| `model_load_failed` | Artifact couldn't be loaded | Check artifact path/format |
| `feature_missing` | Feature key not in source dict | Check feature blackboard |
| `brain_stub_mode` | Brain running on deterministic stub | Check ONNX/model availability |

All alerts are printed as single-line JSON to stderr: `{"event":"brain_alert","time":"...","brain_id":"...","alert_type":"...","detail":{...}}`

## Inbound Dependencies

| Module | What is imported | Why |
|--------|-----------------|-----|
| contracts/domain | BrainDecisionProposal | Legacy output type, still used for backward compat |
| schemas/trading_contracts | BrainSignal | New immutable output type for all adapters (Layer 1) |
| contracts/ids | new_proposal_id | Proposal ID generation |
| deployment/brain_alert | emit_brain_alert | Structured alert on fallback |
| deployment/brain_config_validator | BrainConfigError, get_validator | Load-time validation |

## Outbound Dependents

| Module | What it imports | Why |
|--------|-----------------|-----|
| brains/services/brain_factory | ADAPTER_REGISTRY, BRAIN_TYPE_MAP | Builds adapters from config |
| brains/services/brain_run_service | BaseBrainAdapter | Unified inference entry point |
| runtime/live_cycle | BrainRunService | Management phase brain re-eval |
| execution/strategy_line | adapter.inference() | Per-strategy brain inference |
| parliament/contract_groups | BrainSignal | Consensus computation input |

## Known Issues

- **XGBoost_V9_Institutional & swing XGBoost models**: Previously reported as 9-dim due to adapter `load()` bug (FIX-20260517-005). Adapter read `num_feature` from `gradient_booster.model_param` (empty in XGBoost >=1.6) instead of `learner_model_param` (actual value: 40 for V9, 24 for swings). Models were ALWAYS trained on correct dimension — verified by counting unique feature indices in tree splits (0-39 / 0-23). Fixed with two-tier fallback: `learner_model_param.num_feature` → `gradient_booster.model_param.num_feature` → None.
- **Online_MLP_V1**: Model weights trained to always output neutral (up=4.5%, down=4.6%, confidence=0.909 frozen). NOT a dimension mismatch — model has n_features=40 matching config. Root cause: MLP converged to constant-neutral solution during training. Set vote_weight=0.0 on 2026-05-16 to prevent barrier_12bar consensus dilution. Needs retraining with better regularization.
- **DeepResMLP_V2_New**: ONNX model uses external data format — `.onnx.data` companion file is missing. `onnxruntime` cannot load the model; brain runs in deterministic stub mode (always neutral, 0.876 confidence). Need to re-export ONNX with embedded weights or recover the `.data` file from the training environment.

## Fix History

| Fix ID | Date | Author | Commit | Summary | Root Cause |
|--------|------|--------|--------|---------|------------|
| FIX-20260528-022 | 2026-05-28 | cursor-agent | — | swing_enhanced_35 brain loading fix: (1) `base_adapter.py` — `inference()` fallback dimension now uses `_num_features` (from model load) instead of hardcoded 40. (2) `xgboost_brain_adapter.py` — `load()` now sets `_feature_dimension` from actual model feature count, enabling non-40-dim brains to load correctly. Without this, Swing_V9 brains with 35-dim schema would produce zero-vector dimension mismatch at inference. | RC-06 |
| FIX-20260522-025 | 2026-05-22 | cursor-agent | 24ff517 | Complete BrainSignal.diagnostics passthrough for all 6 adapters (v9_onnx, transformer, online_learner) + shadow_recorder BrainSignal.diagnostics read path | contract-violation |
| FIX-20260521-007 | 2026-05-21 | cursor-agent | — | MetaFilter adapter: integrate track 3 47-dim LightGBM adapter (meta_filter_adapter.py) for dual-track Meta Pipeline bridging Huber BPS regression to Stage 2 LGB+MLP+Platt+Conformal filter chain | RC-06 |
| FIX-20260520-022 | 2026-05-20 | cursor-agent | — | OU z_entry revert 2.0→1.3: FIX-20260519-016 overcorrected — silenced OU brain for 16h. arb_params_v7.json restored to Optuna-validated z_entry=1.3 (all top-10 trials converged). Half-life discount retained. Data: May 19 AM (z_entry=1.3) 65 non-neutral signals; May 19 PM+20 (z_entry=2.0) 0 signals — yet Z-scores were correctly computed. | RC-05, RC-09 |
| FIX-20260519-016 | 2026-05-19 | cursor-agent | — | OU signal quality upgrade: (A) z_entry 1.3→2.0 in arb_params_v7.json — only 4.6% of normal samples exceed 2σ, filtering ~80% of weak mean-reversion signals; (B) half_life discount in _z_to_direction() — fast reversion (hl=18) gets 0.69× multiplier, slow reversion (hl=55) floor-capped at 0.3×, making confidence reflect reversion speed | RC-05, RC-06 |
| FIX-20260516-004 | 2026-05-16 | cursor-agent | — | LightGBM: metadata-driven run() with 3 defense lines replacing fragile dict.values() extraction | RC-06 (config drift) |
| FIX-20260516-006 | 2026-05-16 | cursor-agent | — | All adapters: added dimension guards + brain_alert on fallback paths. V9_ONNX + Transformer: _num_features extracted from ONNX input shape. OnlineLearner: alert on silent truncation. XGBoost: alert on dim mismatch. | RC-06 (silent failure) |
| FIX-20260516-007 | 2026-05-16 | cursor-agent | — | Base adapter run(): metadata-driven feature extraction from brain_entry["features"]. Replaced dict-order-dependent values() extraction. | RC-06 (config drift) |
| FIX-20260517-005 | 2026-05-17 | cursor-agent | — | XGBoost adapter load() fallback: read num_feature from learner_model_param instead of gradient_booster.model_param (empty in XGBoost>=1.6). Fixed 5 swing models (24-dim) + V9_Institutional (40-dim). Un-retired lightgbm_h1_swing. | RC-06 (contract-violation) |
| FIX-20260517-009 | 2026-05-17 | cursor-agent | — | Zero-vector guard added to LightGBM, XGBoost, V9_ONNX, OnlineLearner infer(): np.max(np.abs(vec))<1e-10 → brain_alert + neutral fallback with fallback_reason="zero_feature_vector". Prevents silent frozen confidence when FeatureService Tier 3 returns np.zeros(). | RC-06 (contract-violation) |
| FIX-20260518-029 | 2026-05-18 | cursor-agent | — | XGBoost adapter multi-class support: detect multi:softprob models via num_class in learner_model_param. Convert class probabilities → directional raw_score (P(LONG)−P(SHORT)). Previously float(pred[0]) failed when pred had shape (1,3) from 3-class classifier. Single-class regression path unchanged. | contract-violation |
| FIX-20260519-002 | 2026-05-19 | cursor-agent | — | Commit catch-up: XGBoost multi-class support (num_class detection). Previously registered as FIX-20260518-029. | process-violation |
| FIX-20260522-013 | 2026-05-22 | cursor-agent | — | Sign-flip bug: `_score_to_direction()` in all 5 adapters (LGB/XGBoost/ONNX/Transformer/Params) used `1-confidence` for the non-predicted direction, causing `up_prob > down_prob` for weak SHORT signals (confidence<0.5). Consensus layer only compared up/down probabilities, ignoring `direction_bias` field. Fixed with `0.5±confidence/2` anchoring. | RC-06 |
| FIX-20260522-015 | 2026-05-22 | cursor-agent | — | Layer 1 immutable contracts: All 5 adapters' `get_signal()` now returns frozen `BrainSignal` dataclass instead of `BrainDecisionProposal` with untyped dict `prediction`. `BrainSignal` carries `direction`/`confidence`/`raw_score`/`fallback`/`runtime_ms` — eliminates dict-key typos, missing-key silent failures, and the sign-flip class of bugs. Backward-compat via `getattr(p, "direction", None)` fallback in parliament. | RC-06 |
| FIX-20260524-024 | 2026-05-24 | cursor-agent | — | DRY _score_to_direction: extracted duplicated static method from 4 adapters (XGBoost/LightGBM/ONNX/Transformer) into BaseBrainAdapter as shared utility. Return type annotated as tuple[Direction, float, float] to satisfy Layer 1 immutable contract mypy checks. | RC-06 |
| FIX-20260525-018 | 2026-05-25 | cursor-agent | — | M15 parliament deadlock diagnostics: removed half_life + buffer_len from diagnostics exclusion filter in ParamsBrainAdapter.get_signal() so they flow into BrainSignal.diagnostics. Parliament gate_diag now includes brain_diag list (z_score, half_life, buffer_len, theta) per brain for root cause identification of statarb_m15 neutral_consensus. | RC-06 |
| FIX-20260529-027 | 2026-05-29 | cursor-agent | — | XGBoost feature name embedding + fail-fast validation: `load()` validates `booster.feature_names` against brain config `features` list at every index — `ValueError` on mismatch. For legacy models without embedded feature names, emits `brain_alert` with retraining guidance. Prevents silent column-order skew from corrupting tree-model predictions (positional indexing without name verification). | RC-06 |
| FIX-20260524-025 | 2026-05-24 | cursor-agent | — | MetaFilterAdapter discoverability: added to core/brains/adapters/__init__.py exports (NOT in ADAPTER_REGISTRY — standalone class with its own load/filter/predict_proba API, not a BaseBrainAdapter subclass). | RC-06 |
| FIX-20260528-017 | 2026-05-28 | cursor-agent | — | Schema Dimension & Feature Order SSOT: eliminated 3 silent `or 40` fallbacks in base_adapter.py, lightgbm_brain_adapter.py (2 sites), xgboost_brain_adapter.py — missing `_num_features` now raises RuntimeError instead of silently producing garbage predictions. | RC-06 |

## Cross-Module Contracts

| Contract | Consumers | Stability |
|----------|-----------|----------|
| `BaseBrainAdapter.load()` → sets `self._backend` + `self._num_features` | BrainFactory, BrainConfigValidator | Stable |
| `BaseBrainAdapter.run(snapshot, feature_dict)` → `BrainSignal` | BrainRunService | Stable (Layer 1) |
| `BaseBrainAdapter.inference(feature_vector)` → `BrainSignal` | Strategy files | Stable (Layer 1) |
| `BrainRunService.run_active_brains(snapshot, control, blackboard)` → `list[BrainSignal]` | live_cycle, shadow, verify | Stable (Layer 1) |
| `BrainConfigValidator.validate(entry)` → `ValidationResult` | BrainFactory | Stable |
| `ADAPTER_REGISTRY` dict format: `{registry_key: adapter_class}` | BrainFactory | Stable |

## Verification

```bash
# All brain tests
python -m pytest tests/ -k "brain" -q

# Validate all brain configs
python scripts/repair_brain_configs.py --validate-only

# Verify all brains produce varying signals
python scripts/verify_all_brains.py

# Check for brain alerts during shadow run
python -m apps.engine.main_v9_shadow --symbol XAUUSDc --cycles 50 2>&1 | grep brain_alert
```
