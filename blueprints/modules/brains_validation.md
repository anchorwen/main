# Brains / Validation

## Purpose
Load-time brain config validation and runtime alerting. Catches configuration errors before they become silent inference failures. Every fallback or degradation emits a structured JSON alert to stderr.

## Key Files

| File | Role |
|------|------|
| `core/deployment/brain_config_validator.py` | `BrainConfigValidator` — 7-check validation at BrainFactory.build() time |
| `core/deployment/brain_alert.py` | `emit_brain_alert()` — structured JSON alerts to stderr |
| `scripts/repair_brain_configs.py` | Batch repair tool for missing `features` fields |

## Validation Rules (BrainConfigValidator)

### Rule 1: Required Fields
**Check**: `brain_id`, `brain_type`, `feature_schema_id`, `artifact_path`, `status` must be present and non-empty.
**Level**: ERROR — brain excluded from inference.

### Rule 2: Brain Type
**Check**: `brain_type` must be a key in `BRAIN_TYPE_MAP` (15 known types).
**Level**: ERROR.

### Rule 3: Feature Schema
**Check**: `feature_schema_id` must be in `SCHEMA_DIMENSIONS` (9 known schemas + aliases).
**Level**: ERROR.

### Rule 4: Artifact Path
**Check**: `artifact_path` must point to an existing file.
**Level**: WARNING — non-blocking (some models resolve paths at runtime).

### Rule 5: Features List Length
**Check**: If `features` field is present, its length must match `SCHEMA_DIMENSIONS[feature_schema_id]`.
**Level**: ERROR.

### Rule 6: Feature Name Validity
**Check**: Each name in `features` must exist in the canonical schema feature list.
**Level**: WARNING.

### Rule 7: Model Dimension (Post-Load)
**Check**: Adapter's `_num_features` (from model file) must match `SCHEMA_DIMENSIONS[feature_schema_id]`.
**Level**: ERROR — `BrainConfigError` raised, brain excluded.

## Schema Dimensions

| Schema ID | Canonical | Dimension |
|-----------|-----------|-----------|
| `v9_institutional_40` | v9_institutional_40 | 40 |
| `daily_swing_24` | daily_swing_24 | 24 |
| `swing_24` | daily_swing_24 (alias) | 24 |
| `v4.5_microstructure_9` | v4.3_microstructure_9 (alias) | 9 |
| `v2_microstructure_9` | v4.3_microstructure_9 (alias) | 9 |
| `v4.3_microstructure_9` | v4.3_microstructure_9 | 9 |
| `v2_microstructure_288` | v2_microstructure_288 | 288 |
| `meta_stage2_runtime_47` | meta_stage2_runtime_47 | 47 |
| `meta_stage2_runtime_56` | meta_stage2_runtime_56 | 56 |
| `meta_stage2_runtime_59` | meta_stage2_runtime_59 | 59 |
| `v6_price_series_1` | v6_price_series_1 | 1 |
| `v9_40dim_ou3` | v9_40dim_ou3 | 43 |

## Alert Types

| Alert Type | Trigger | Fallback Behavior |
|-----------|---------|-------------------|
| `config_validation_error` | BrainConfigValidator fails (ERROR level) | Brain excluded, added to `_failed_brain_ids` |
| `feature_dimension_mismatch` | `len(feature_vector) != adapter._num_features` | Zero vector → neutral prediction | **RESOLVED in FIX-20260610-009** — root cause was `SwingStrategy._run_inference()` resolving schema from adapter object (which lacks `feature_schema`) instead of brain config `feature_schema_id`. Silent fallback to `"v9_institutional"` (40-dim) corrupted input for non-V9 brains. Fix: schema anchored to `b_info["feature_schema_id"]` + fatal error on missing schema. |
| `model_load_failed` | ONNX/XGBoost/LightGBM/JSON load exception | `stub:<ExceptionName>` backend |
| `feature_missing` | No `features` in config AND booster unavailable | Zero vector |
| `brain_stub_mode` | ONNX session is None, deterministic stub active | Heuristic-based fallback |

All alerts follow the format: `{"event":"brain_alert","time":"<ISO8601>","brain_id":"...","alert_type":"...","detail":{...}}`

Printed to **stderr** to avoid corrupting stdout JSON output (shadow CLI, feature export, etc.).

## Brain Registration Checklist

Before registering a new brain in `configs/brains/`:

1. [ ] `brain_id` is unique and descriptive
2. [ ] `brain_type` is in `BRAIN_TYPE_MAP`
3. [ ] `feature_schema_id` matches the training data schema
4. [ ] `artifact_path` exists and is the correct format for the brain_type
5. [ ] `features` field is populated (run `python scripts/repair_brain_configs.py --write`)
6. [ ] Run `python scripts/repair_brain_configs.py --validate-only` — no errors
7. [ ] Run `python -m apps.engine.main_v9_shadow --symbol XAUUSDc --cycles 50` — brain produces varying signals
8. [ ] Check for brain_alerts: `grep brain_alert` in shadow output

## Data Flow

```
BrainConfigValidator.validate(brain_entry) → ValidationResult
  └─ on ERROR  → BrainConfigError → brain excluded from inference
  └─ on WARNING → emit_brain_alert() → brain runs with alert

Adapter fallback paths
  └─ emit_brain_alert(brain_id, alert_type, detail) → stderr JSON
```

## Inbound Dependencies

| Module | What is imported | Why |
|--------|-----------------|-----|
| brains/adapters | BaseBrainAdapter | Validation of model file dimensions |
| deployment/brain_alert | emit_brain_alert | Structured alert on validation failure |

## Outbound Dependents

| Module | What it imports | Why |
|--------|-----------------|-----|
| brains/services/brain_factory | BrainConfigValidator, BrainConfigError | Load-time brain config validation |
| brains/services/brain_run_service | (indirect via BrainFactory) | Failed brains tracked in _failed_brain_ids |

## Known Issues

## Fix History

| Fix ID | Date | Author | Commit | Summary | Root Cause |
|--------|------|--------|--------|---------|------------|
| FIX-20260528-017 | 2026-05-28 | cursor-agent | — | Schema Dimension & Feature Order SSOT: replaced local SCHEMA_DIMENSIONS + _get_schema_feature_names() with imports from core.features.schemas.registry. Eliminated ~130 lines of duplicate schema definitions. Backwards-compat alias preserved. | RC-09 |
| FIX-20260620-070 | 2026-06-20 | cursor-agent | — | **inference_guard tests**: 19 new tests (191 lines) for brains/inference_guard validation. | RC-12 |
| FIX-20260526-028 | 2026-05-26 | cursor-agent | — | Binary_Cls_V1 brain config feature order corrected: `features` list changed from V9 canonical order (M5→H1, 8 core/TF + OU + Hurst blocks) to model training order (H1→M15→M30→M5, 10 metrics/TF inline). Training meta.json `feature_names` is the authoritative ground truth. Without this, `_reorder_for_brain()` would have no correct target order. | RC-06 |
| FIX-20260525-027 | 2026-05-25 | cursor-agent | — | `v9_40dim_ou3` schema registration: MetaLabel brain (`Meta_Stage1_MetaLabel_Binary_V1`) legitimately requires 43 features (40 V9 institutional + 3 OU physics: `ou_z_score`, `ou_half_life`, `ou_theta`) but `BrainConfigValidator` only recognized the 40-dim `v9_institutional_40` schema. Result: `brain_build_skip` at startup → `barrier_12bar_meta` strategy had 0 brains → completely silent. Fix: (1) added `"v9_40dim_ou3": 43` to `SCHEMA_DIMENSIONS`; (2) added `_get_schema_feature_names()` branch returning V9 40 + 3 OU names (following the `meta_stage2_runtime_47` pattern); (3) changed brain config `feature_schema_id` from `"v9_institutional_40"` to `"v9_40dim_ou3"`. 11 unit tests validate schema acceptance, name validity, dimension mismatch rejection, and backward compat for existing v9_institutional_40 brains. | RC-06 (contract-violation — schema dimension mismatch blocked valid augmented config) |
| FIX-20260518-025 | 2026-05-18 | cursor-agent | — | Phase 1a: Per-brain schema startup validator — validates each brain's feature_schema_id against registered schemas (Tier 1 cache) and implemented schemas (Tier 2 live compute). Drops individual mismatched brains instead of killing all strategy lines. | config-drift, contract-violation |
| FIX-20260518-042 | 2026-05-18 | cursor-agent | — | Brain deployment quality gate: `validate_brain_before_deploy.py` catches direction bias (>90% one direction), NEUTRAL death (>80%), signal redundancy (>0.85 correlation), and output validity gaps BEFORE deployment. Fixed `_get_direction_and_confidence()` to read from `proposal.prediction` dict (direction_bias/confidence) instead of non-existent top-level attributes. Tested on all registered brains. | RC-09 |
| FIX-20260516-009 | 2026-05-16 | cursor-agent | — | Added enable_onnxruntime:true to DeepResMLP_V2_New config (was missing, would cause stub mode). Deleted 4 stale configs for permanently retired brains. Registered 5 new shadow brains in governance_state. | RC-09 |
| FIX-20260516-008 | 2026-05-16 | cursor-agent | — | BrainConfigValidator (7 checks at load time) + BrainAlert (structured JSON to stderr) + metadata completion + blueprint diagnostic manual | RC-09 |
| FIX-20260517-012 | 2026-05-17 | cursor-agent | — | Magic uniqueness 放宽为 per-contract_group：同一策略线（barrier_12bar）的大脑共享同一 magic（如 90001），不再被 brain_registration_gate 拒绝。 | RC-06 |
| FIX-20260519-002 | 2026-05-19 | cursor-agent | — | Commit catch-up: brain_registration_gate.py magic uniqueness per contract_group. Previously registered as FIX-20260517-012. | process-violation |
| FIX-20260519-003 | 2026-05-19 | cursor-agent | — | New file: startup_validator.py — per-brain schema startup validator (Tier 1 registered schemas + Tier 2 live compute). Previously registered as FIX-20260518-025. | missing-feature |
| FIX-20260524-029 | 2026-05-24 | cursor-agent | — | Perf: _check_magic_unique() O(n²)→O(n) — replaced per-entry re-read of all configs/brains/*.json files with lazy-built magic→[brain_id] reverse index. Single O(n) pre-pass builds index; O(1) lookup per validation. Eliminates n×n file reads on multi-brain validation. | RC-06 |

## Cross-Module Contracts

| Contract | Consumers | Stability |
|----------|-----------|----------|
| `BrainConfigValidator.validate(brain_entry)` → `ValidationResult` | BrainFactory | Stable |
| `emit_brain_alert(brain_id, alert_type, detail)` → stderr JSON | All adapters | Stable |
| `repair_brain_configs.py --validate-only` → 0 errors gate | CI/CD, pre-registration | Stable |

## Verification

```bash
# Validate all configs
python scripts/repair_brain_configs.py --validate-only

# Check for brain_alerts in stderr during shadow run
python -m apps.engine.main_v9_shadow --symbol XAUUSDc --cycles 50 2>&1 >/dev/null | grep brain_alert

# Brain-specific tests
python -m pytest tests/ -k "brain" -q
```
