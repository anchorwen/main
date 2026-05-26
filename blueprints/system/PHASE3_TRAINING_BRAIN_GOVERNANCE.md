# Phase 3: Training Pipeline & Brain Governance

> **Status**: PLANNING | **Priority**: 🛑 Third (deferred after Phase 1 & 2 closure)
> **Created**: 2026-05-25 | **Author**: cursor-agent

## Context

Phase 1 (Dynamic SL/TP) and Phase 2 (MetaLabel feature order) are code-complete but the MetaLabel brain (`Meta_Stage1_MetaLabel_Binary_V1`) is blocked at startup by `BrainConfigValidator` — the validator rejects 43 features (40 V9 + 3 OU) because it only knows about the 40-dim V9 schema. This is the **second layer** of the train-serve skew: the assembly order was fixed (FIX-20260525-026) but the brain factory validator prevents the brain from loading at all.

Beyond the immediate blocker, the broader Phase 3 scope covers:
1. Training→inference parity validation (are models actually receiving correct features?)
2. Brain roster health audit (zombie brains, frozen confidence, dead models)
3. Retraining pipeline readiness (can we retrain broken models quickly?)

## Immediate Blocker: BrainConfigValidator MetaLabel Rejection

### Problem
```
ERROR: features list length 43 != schema v9_institutional_40 expected 40
ERROR: feature[40]='ou_z_score' not in schema v9_institutional_40
ERROR: feature[41]='ou_half_life' not in schema v9_institutional_40
ERROR: feature[42]='ou_theta' not in schema v9_institutional_40
→ brain_build_skip → barrier_12bar_meta has 0 brains → silent
```

### Root Cause
`BrainConfigValidator` (in `core/brains/services/brain_factory.py`) validates brain config `features` against the canonical V9 40-dim schema. The MetaLabel brain legitimately has 43 features because it augments the 40 V9 features with 3 OU physics features (`ou_z_score`, `ou_half_life`, `ou_theta`). The validator has no provision for augmented schemas.

### Fix Direction
Option A: Add `feature_schema: "v9_40dim_ou3"` support to `BrainConfigValidator` — recognizes the augmented schema and validates 40 base features + 3 OU features.
Option B: Skip schema validation for brains with `feature_schema: "v9_40dim_ou3"` — trust the brain config as authoritative.
Option C: Remove the 3 OU features from brain config `features` list, keep them only in `_build_meta_feature_vector()` — brain config declares 40, runtime adds 3.

### Files to Modify
| File | Change |
|------|--------|
| `core/brains/services/brain_factory.py` | BrainConfigValidator: accept `v9_40dim_ou3` schema |
| `configs/brains/meta_stage1_metalabel_binary_v1.json` | (if Option C) trim features to 40 |
| `core/runtime/live_cycle.py` | (verification only) confirm feature assembly still works |

## Broader Phase 3 Scope

### Track A: Training-Inference Parity Audit

**Problem**: Multiple brains exhibit train-serve divergence:
| Brain | Symptom | Suspicion |
|-------|---------|-----------|
| `Meta_Stage1_Binary_Cls_V1` | OOF bimodal [0.0, 0.7-0.8], all-LONG bias | Feature resolution? Normalization? |
| `Meta_Stage1_Huber_V1` | -369.65R live (was regression collapse) | Frozen, already diagnosed |
| `LightGBM_H1_Swing` | Frozen confidence 0.612, all-LONG, -305 bps | Constant feature vector? |
| `XGBoost_H1_Swing` | All neutral, 0% directional | Dead model? |
| `Online_MLP_V1` | Frozen/removed from governance | Missing PnL records |

**Actions**:
1. Audit feature resolution pipeline for each brain type (LightGBM, XGBoost, MLP)
2. Verify normalization config path is correctly loaded
3. Check `adapter.infer()` receives non-constant feature vectors
4. Compare OOF predictions vs live predictions for a test day

### Track B: Brain Roster Cleanup

**Problem**: Governance state has 5 entries, but `brain_performance.json` tracks 43 brains. Many are dead/zombie.

**Current governance state (2026-05-25)**:
| Brain | Status | Vote Weight | Health |
|-------|--------|-------------|--------|
| `Meta_Stage1_Binary_Cls_V1` | shadow | 0.0 | ⚠️ Bimodal OOF |
| `Meta_Stage1_Huber_V1` | frozen | 0.0 | 🔴 -369.65R |
| `Meta_Stage1_MetaLabel_Binary_V1` | probation | 0.4 (penalized) | 🔴 Blocked by validator |
| `OU_Params_V6_Sniper` | probation | 1.0 | ✅ +119.91 bps, 49.7% WR |
| `OU_Params_V7_M15` | probation | 1.0 | ⚠️ New, unvalidated |
| `Online_MLP_V1` | frozen | 0.0 | 🔴 Removed from voting |

**Actions**:
1. Delete frozen brains without config files (SSOT cleanup)
2. Fix config path mismatches (5 brains have filename ≠ brain_id)
3. Resolve magic collision (90001: Meta_Stage1_Binary_Cls_V1 vs Meta_Stage1_Huber_V1)
4. Add transition_log to brain_states for Meta_Stage1_Binary_Cls_V1

### Track C: Retraining Pipeline Readiness

**Problem**: When models are confirmed broken, we need to retrain quickly.

**Current training assets**:
- `scripts/brain.py` — new brain training CLI (untracked)
- `scripts/daily_ops.py` — daily ops pipeline (feedback, retraining, governance)
- `docs/brain_model_architecture.md` — model architecture docs (untracked)
- `data/models/institutional/` — trained model artifacts

**Actions**:
1. Validate `scripts/brain.py` training pipeline end-to-end
2. Ensure retrained models produce correct feature schemas
3. Add `artifact_hash` to OU brain configs (currently missing — integrity unverifiable)
4. Test retrain→register→deploy cycle for one broken brain

### Track D: Meta Exit Model

**Problem**: Meta exit model is disabled:
```
meta_exit_model_rejected: n_wins=7, win_rate=0.1186, min_wins=15, min_win_rate=0.2
→ fallback to atr_trailing_stop_layer1
```

Only 7 winning meta-exit trades collected — need 15+ with 20%+ WR before the model activates. This is a data collection problem, not a code problem.

## Execution Order

```
🥇 P0: Fix BrainConfigValidator → unblock MetaLabel brain loading
🥈 P1: Track A audit → verify training-inference parity for all active brains
🥉 P2: Track B cleanup → governance SSOT, zombie removal, config fixes
🏅 P3: Track C readiness → training pipeline validation
🛑 P4: Track D → deferred until sufficient meta-exit data collected
```

## Verification

```bash
# After P0 fix: confirm MetaLabel brain loads
python scripts/verify.py --full

# After P1 audit: confirm train-serve parity
python -m pytest tests/ -k "feature_vector or meta_feature" -v

# After P2 cleanup: confirm governance health
python -m brain governance --check

# After P3 validation: confirm retraining pipeline
python scripts/brain.py --dry-run train --brain-id test_brain
```

## Related Fixes

| Fix ID | Description | Relation |
|--------|-------------|----------|
| FIX-20260525-026 | MetaLabel feature assembly order | Phase 2 — Layer 1 fix |
| FIX-20260524-006 | SSOT Dictator Governance Engine | Track B — precedent |
| FIX-20260524-036 | Brain SL/TP audit | Track A — same pattern |
| FIX-20260516-003 | Strategy Parameter Reference (data-backed) | Track A — diagnostic foundation |
| FIX-20260521-002 | live_intent_loop bypassed enabled:false | Track B — same class |
| FIX-20260515-014 | Brain config restoration | Track C — recovery precedent |
