#!/usr/bin/env python3
"""Emit Micro Scaler V2 RAW Quantile Trigger spec (FIX-20260824-005, IC 裁决 2026-08-24).

背景: Isotonic 校准平坦区将宽范围 raw 预测吸附到阈值台阶 (实测触发率 75.6% vs
设计 9.89%, 57% 触发行 pred_pct 恰为阈值) → 投委会裁决「改 raw |pred| 触发」.

方法 (Iron Law #11, 零 MT5 触碰 — 实盘终端单客户端, 不初始化):
  1. 加载已落档 booster (micro_scaler_v2_reg.txt).
  2. 复现训练池: current-gen 特征行, event_time <= 训练 built_at (切分池漂移豁免,
     p90 对 ±3 行鲁棒).
  3. raw = booster.predict(X_canonical40); threshold = 训练池 |raw| p90.
  4. Isotonic 单调 → raw 排序 == cal 排序 → D10 人口不变, OOS 经济 (59.5%/+0.0176%)
     原样有效; 仅阈值值域 cal→raw, 触发语义去平坦区吸附.
  5. build_trigger_spec(report, raw_p90=...) 单一来源重导 (逻辑不散布), 落档 trigger.json.

用法:
  python scripts/training/emit_micro_scaler_v2_raw_trigger.py --data-dir data
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import TypedDict

# ── Repo-root bootstrap (scripts/training/ 直接运行) ──
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import numpy as np  # noqa: E402

from scripts.build_micro_cost_model import load_current_gen_rows  # noqa: E402
from scripts.training.train_micro_scaler_v2 import build_trigger_spec  # noqa: E402
from core.features.schemas.v9_institutional_schema import V9_INSTITUTIONAL_40_FEATURES  # noqa: E402


def _parse_iso(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


class _RawP90Stats(TypedDict):
    """训练池 raw |pred| 分位数统计 (类型诚实标注, 禁 dict[str, float] 误标 str)."""

    n_pool: int
    raw_p90: float
    raw_p75: float
    raw_p95: float
    pool_trigger_rate_pct: float
    built_at: str


def derive_raw_p90(data_dir: str) -> _RawP90Stats:
    """复现训练池 → 预测 raw → |raw| p90/p75/p95 + 池内触发率."""
    import lightgbm as lgb

    base = Path(data_dir)
    model_dir = base / "training" / "micro_scaler_v2"
    model_path = model_dir / "micro_scaler_v2_reg.txt"
    report_path = model_dir / "micro_scaler_v2_reg_report.json"
    fs_path = (
        base / "feature_store" / "records" / "symbol=XAUUSDc" / "timeframe=M5" / "features.jsonl"
    )
    if not model_path.exists():
        raise FileNotFoundError(f"model not found: {model_path}")
    if not fs_path.exists():
        raise FileNotFoundError(f"feature store not found: {fs_path}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    built_at: str = str(report["built_at"])  # 训练落档时间 = 池切分上界
    built_at_ts = _parse_iso(built_at)

    booster = lgb.Booster(model_file=str(model_path))
    n_feat = int(booster.num_feature())
    if n_feat != 40:
        raise RuntimeError(f"model feature-count {n_feat} != 40")

    rows = load_current_gen_rows(fs_path)
    names = list(V9_INSTITUTIONAL_40_FEATURES)
    canon = set(names)
    X_rows: list[list[float]] = []
    n_pool = 0
    for row in rows:
        if row.event_dt.timestamp() > built_at_ts:
            continue  # 切分池上界: 训练后追加的 bar 不计入
        keys = set(row.values)
        if not canon.issubset(keys):
            continue
        vec = [float(row.values[nm]) for nm in names]
        if any(v != v for v in vec):
            continue
        X_rows.append(vec)
        n_pool += 1
    if n_pool < 100:
        raise RuntimeError(f"pool too small: {n_pool}")
    X = np.asarray(X_rows, dtype=np.float64)
    raw = booster.predict(X).astype(np.float64)
    abs_raw = np.abs(raw)
    p90 = float(np.percentile(abs_raw, 90))
    p75 = float(np.percentile(abs_raw, 75))
    p95 = float(np.percentile(abs_raw, 95))
    trig_rate = float(np.mean(abs_raw >= p90) * 100.0)
    return {
        "n_pool": n_pool,
        "raw_p90": p90,
        "raw_p75": p75,
        "raw_p95": p95,
        "pool_trigger_rate_pct": trig_rate,
        "built_at": built_at,
    }


def main() -> int:
    # GBK 控制台 → UTF-8
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    p = argparse.ArgumentParser(description="Emit Micro Scaler V2 RAW Quantile Trigger")
    p.add_argument("--data-dir", default="data")
    args = p.parse_args()

    der = derive_raw_p90(args.data_dir)
    base = Path(args.data_dir) / "training" / "micro_scaler_v2"
    report_path = base / "micro_scaler_v2_reg_report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    spec = build_trigger_spec(report, raw_p90=der["raw_p90"])
    out_path = base / "micro_scaler_v2_trigger.json"
    out_path.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")

    # stdout = 唯一合法证据源 (Iron Law #11)
    print("=" * 78)
    print("  MICRO SCALER V2 — RAW QUANTILE TRIGGER (FIX-20260824-005, IC 裁决)")
    print("=" * 78)
    print(
        f"  pool             : {der['n_pool']} current-gen rows (event_time <= {der['built_at'][:19]}Z)"
    )
    print(
        f"  raw |pred| p90   : {der['raw_p90']:.5f}%  (p75={der['raw_p75']:.5f}%, p95={der['raw_p95']:.5f}%)"
    )
    print(f"  池内触发率@p90   : {der['pool_trigger_rate_pct']:.2f}%  (应为 ~10%)")
    print(f"  trigger_mode     : {spec['trigger_mode']}")
    print(f"  threshold        : {spec['threshold_abs_pred_pct']}%  (原 cal 阈值 0.06007% 弃用)")
    print(f"  trigger_rate(OOS): {spec['trigger_rate_pct_oos']}%  (D10 人口不变, Isotonic 单调)")
    print(f"  direction_semantics: {spec['direction_semantics']}")
    print(f"  mandate          : {spec['mandate']}")
    print(f"  -> {out_path}")
    print("[DONE] RAW Quantile Trigger 已落档 (触发源 cal→raw, 阈值重导).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
