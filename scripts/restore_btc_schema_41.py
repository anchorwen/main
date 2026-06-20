#!/usr/bin/env python
"""一键恢复 BTC btc_macro_enhanced_37 schema 至 41 维.

前置条件: V4, V9, V12 三个大脑必须已用 41 维特征重训练完毕.
验证: 每个模型的 num_feature() == 41，任一大脑不满足则拒绝执行.

用法:
    python scripts/restore_btc_schema_41.py          # 检查 + 执行
    python scripts/restore_btc_schema_41.py --check  # 仅检查，不执行

关联:
    DQAF-20260615-009 战术回滚 → TECH_DEBT-004
    重训练完成后运行此脚本以切换 schema 至 41 维.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from core.runtime.fault_handler import fail_open_guard

PROJECT_ROOT = Path(__file__).resolve().parent.parent

REGISTRY_PATH = PROJECT_ROOT / "core" / "features" / "schemas" / "registry.py"
BRAINS_DIR = PROJECT_ROOT / "configs" / "brains_btc"

BRAIN_CONFIGS = [
    "BTC_Swing_V4.json",
    "BTC_Swing_V9_H1_Survival.json",
    "BTC_Swing_V12_H1_Survival.json",
]

# 4 regime derivative features added by FIX-B3-feat (positions 37-40 in the 41-dim schema)
REGIME_DERIVATIVES_4 = [
    "TF_OU_x_Hurst",
    "TF_OU_div_ADX",
    "Cross_BTC_Gold_Ratio",
    "Cross_BTC_Gold_Ratio_ROC",
]


def check_model_dim(brain_cfg: dict) -> tuple[bool, int]:
    """Verify model num_feature() == 41. Returns (ok, num_features)."""
    brain_type = brain_cfg.get("brain_type", "")
    artifact_path = brain_cfg.get("artifact_path", "")
    if not artifact_path:
        return False, 0

    try:
        if "lightgbm" in brain_type:
            import lightgbm as lgb
            booster = lgb.Booster(model_file=artifact_path)
            nf = booster.num_feature()
        elif "xgboost" in brain_type:
            model_data = json.loads(open(artifact_path).read())
            nf = int(
                model_data.get("learner", {})
                .get("learner_model_param", {})
                .get("num_feature", "0")
            )
        else:
            return False, 0
        return nf == 41, nf
    except Exception as exc:  # BLE001:FOG (Sev 4, Phase 3b)
        with fail_open_guard("restore_btc_schema_41:check_model_dim"):
            print(f"  [ERROR] Failed to read model {artifact_path}: {exc}")
            return False, 0
def update_registry() -> None:
    """Update SCHEMA_DIMENSIONS: btc_macro_enhanced_37 37→41."""
    content = REGISTRY_PATH.read_text(encoding="utf-8")
    old = '"btc_macro_enhanced_37": 37,  # FIX-20260615-009: ROLLED BACK'
    new = '"btc_macro_enhanced_37": 41,  # FIX-B3-feat: 41-dim (models retrained)'
    if old in content:
        content = content.replace(old, new)
        REGISTRY_PATH.write_text(content, encoding="utf-8")
        print("  [OK] Registry: btc_macro_enhanced_37 37 → 41")
    else:
        print("  [SKIP] Registry already at 41 or unexpected format — check manually")


def update_brain_config(cfg_path: Path) -> None:
    """Update n_features 37→41 and expand features list."""
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))

    brain_id = cfg.get("brain_id", "?")
    current_nf = cfg.get("n_features", 0)
    current_features = cfg.get("features", [])

    if current_nf == 41 and len(current_features) == 41:
        print(f"  [SKIP] {brain_id}: already at 41")
        return

    # Update n_features
    cfg["n_features"] = 41

    # Expand features list: append 4 regime derivatives if not already present
    existing = set(current_features)
    for feat in REGIME_DERIVATIVES_4:
        if feat not in existing:
            current_features.append(feat)

    cfg["features"] = current_features

    cfg_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"  [OK] {brain_id}: n_features {current_nf}→41, features {len(current_features)} items")


def main() -> None:
    check_only = "--check" in sys.argv

    print("=" * 60)
    print("BTC Schema 41-Dim Recovery — One-Click Restore")
    print(f"Mode: {'CHECK ONLY' if check_only else 'EXECUTE'}")
    print("=" * 60)

    # ── Phase 1: Pre-flight check — all models must be 41-dim ──
    print("\n[Phase 1] Model dimension check (all must be 41)...")
    all_ok = True
    for cfg_name in BRAIN_CONFIGS:
        cfg_path = BRAINS_DIR / cfg_name
        if not cfg_path.exists():
            print(f"  [FAIL] Config not found: {cfg_path}")
            all_ok = False
            continue
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        brain_id = cfg.get("brain_id", "?")
        ok, nf = check_model_dim(cfg)
        if ok:
            print(f"  [PASS] {brain_id}: num_feature={nf} == 41")
        else:
            print(f"  [FAIL] {brain_id}: num_feature={nf} != 41 — model NOT retrained!")
            all_ok = False

    if not all_ok:
        print("\n[ABORT] Pre-flight checks failed.")
        print("All 3 BTC brains must be retrained with 41-dim features first.")
        print("See: TECH_DEBT-004 in blueprints/system/FIX_REGISTRY.md")
        sys.exit(1)

    print("\n[Phase 1] All models confirmed at 41-dim. Ready to restore.")

    if check_only:
        print("\n[CHECK ONLY] No changes made. Run without --check to execute.")
        return

    # ── Phase 2: Update registry ──
    print("\n[Phase 2] Updating registry...")
    update_registry()

    # ── Phase 3: Update brain configs ──
    print("\n[Phase 3] Updating brain configs...")
    for cfg_name in BRAIN_CONFIGS:
        update_brain_config(BRAINS_DIR / cfg_name)

    # ── Phase 4: Verify ──
    print("\n[Phase 4] Final verification...")
    from core.features.schemas.registry import SCHEMA_DIMENSIONS
    reg = SCHEMA_DIMENSIONS.get("btc_macro_enhanced_37", "MISSING")
    print(f"  Registry: btc_macro_enhanced_37 = {reg}")

    for cfg_name in BRAIN_CONFIGS:
        cfg = json.loads((BRAINS_DIR / cfg_name).read_text(encoding="utf-8"))
        nf = cfg["n_features"]
        fl = len(cfg["features"])
        brain_id = cfg["brain_id"]
        status = "OK" if (nf == 41 and fl == 41) else "MISMATCH"
        print(f"  {brain_id}: n_features={nf}, features_len={fl} [{status}]")

    print("\n" + "=" * 60)
    print("DONE. Restart BTC process to activate 41-dim brains.")
    print("Verify: brain_count=3, brain_ids=[V4, V9, V12]")
    print("=" * 60)


if __name__ == "__main__":
    main()
