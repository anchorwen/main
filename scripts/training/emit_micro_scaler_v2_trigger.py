#!/usr/bin/env python3
"""Emit Micro Scaler v2 Quantile Trigger 规格 (IC 部署令 2026-08-24, 免重训).

背景: IC 终局裁决 (FIX-20260824-002 续) — 豁免 [0.9,1.1] 校准斜率门禁
(slope=0.5048, 幅度排序器定性, qIC=0.5515 主闸门 + 净成本 D10 PASS),
Micro Scaler v2 晋升 SHADOW. 后续实盘执行引擎对本模型必须且只能采用
Quantile Trigger: |pred| 落入历史样本 Top-decile (D10) 才允许 Shadow Order,
绝不允许固定阈值 (Fixed Threshold) 触发.

为何免重训 (架构裁决): 训练后 feature store 持续追加新 M5 bar → 60/20/20
切分池漂移 → 重训会改变已记录的 OOS 战绩 (Iron Law #11: 唯一合法证据源 =
该次训练 stdout). 触发规格必须严格派生自已记录的 OOS 预测分布, 故从
reg_report.json 派生而非重算.

逻辑单一来源 (Iron Law #1.1): build_trigger_spec() 定义于
train_micro_scaler_v2.py (未来重训 run_regression 内联自动落档); 本脚本仅
复用同一函数对既有报告执行发射. 同一逻辑不多处散布.

用法:
  python scripts/training/emit_micro_scaler_v2_trigger.py --data-dir data
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# ── Repo-root bootstrap (scripts/training/ 直接运行) ──
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.training.train_micro_scaler_v2 import build_trigger_spec  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(
        description="Emit Micro Scaler V2 Quantile Trigger spec (from reg_report, no retrain)"
    )
    p.add_argument("--data-dir", default="data", help="Base data dir (XAU live)")
    p.add_argument("--model-id", default="micro_scaler_v2")
    args = p.parse_args()

    out_dir = Path(args.data_dir) / "training" / args.model_id
    report_path = out_dir / f"{args.model_id}_reg_report.json"
    if not report_path.exists():
        raise FileNotFoundError(f"reg report not found: {report_path}")
    report = json.loads(report_path.read_text(encoding="utf-8"))

    spec = build_trigger_spec(report)
    out_path = out_dir / f"{args.model_id}_trigger.json"
    out_path.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")

    # stdout = 唯一合法证据源 (Iron Law #11)
    print("=" * 78)
    print("  MICRO SCALER V2 — QUANTILE TRIGGER SPEC (IC 部署令 2026-08-24)")
    print("=" * 78)
    print(f"  trigger_mode        : {spec['trigger_mode']}")
    print(f"  threshold |pred| D10: {spec['threshold_abs_pred_pct']}%  (OOS 历史样本 p90)")
    print(f"  trigger_rate (OOS)  : {spec['trigger_rate_pct_oos']}%")
    print(f"  direction_semantics : {spec['direction_semantics']}")
    print(
        f"  D10 mean net (OOS)  : {spec['economics']['d10_mean_net_pct']:+.5f}%  "
        f"net_pos_share={spec['economics']['d10_net_positive_share_pct']}%"
    )
    print(f"  full-OOS mean net   : {spec['economics']['full_oos_mean_net_pct']:+.5f}%")
    print(
        f"  OOS rho (cal) / qIC : {spec['quality']['oos_rho_calibrated']} / "
        f"{spec['quality']['oos_quantile_ic']}"
    )
    print(
        f"  calib slope         : {spec['quality']['calib_slope']}  "
        f"status={spec['quality']['slope_gate_status']}"
    )
    print(f"  mandate             : {spec['mandate']}")
    print(f"  -> {out_path}")
    print("[DONE] Quantile Trigger 规格已落档 (派生自 reg_report, 免重训).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
