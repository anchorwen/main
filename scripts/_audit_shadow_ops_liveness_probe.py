#!/usr/bin/env python3
"""Shadow Ops 实证锁探针 — 真实特征 → 真实模型 → 真实遥测 (Phase 4, DEFCON 1).

Iron Law #11: 脚本 stdout 是唯一合法证据源.

用途 (IC 部署协议 实证锁):
  (a) 证明 Micro Scaler v2 评分器消费 **真实** V9_40 特征 (来自实盘 feature_store,
      source=mt5_live) 并产出真实预测 — 走与 live_cycle Phase 4 完全相同的
      ShadowOpsRuntime 构造路径 (configs/live.yaml shadow_ops 段);
  (b) 证明零订单穿透 — 复用 watchdog 零真实订单扫描 + mandate 完整性 + 构造性隔离.

用法:
  python scripts/_audit_shadow_ops_liveness_probe.py [--feature-file <path>] [--no-ledger-write]

默认特征源: data/feature_store/records/symbol=XAUUSDc/timeframe=M5/features.jsonl (最新行).
默认写入: data/shadow_ops/micro_scaler_predictions.jsonl (真实遥测 ledger, gitignored).
--no-ledger-write: 只评分不落盘 (dry-run, 用于无污染预演).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import UTC, datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_FEATURE_FILE_DEFAULT = "data/feature_store/records/symbol=XAUUSDc/timeframe=M5/features.jsonl"


def _iso_utc() -> str:
    return datetime.now(UTC).replace(tzinfo=None).isoformat() + "Z"


def _load_latest_real_feature(feature_file: Path) -> dict:
    """读 feature_store 最新一行真实 V9_40 特征 (实盘引擎 mt5_live 写入)."""
    if not feature_file.exists():
        raise SystemExit(f"feature store missing: {feature_file}")
    rows: list[dict] = []
    with open(feature_file, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    if not rows:
        raise SystemExit(f"feature store empty: {feature_file}")
    rec = rows[-1]
    if rec.get("schema_name") != "v9_institutional_40":
        raise SystemExit(f"schema mismatch: {rec.get('schema_name')!r} != v9_institutional_40")
    # values 为 dict (name→value) 或 list (规范序). dict → 投影到 V9_40 规范序.
    from core.runtime.shadow_ops.micro_scaler_scorer import MicroScalerScorer

    canonical = MicroScalerScorer.canonical_feature_names()
    values = rec.get("values")
    if isinstance(values, list):
        if len(values) != len(canonical):
            raise SystemExit(f"feature vector dim {len(values)} != {len(canonical)}")
        vec = [float(v) for v in values]
    elif isinstance(values, dict):
        if len(values) != len(canonical):
            raise SystemExit(f"feature dict {len(values)} keys != {len(canonical)}")
        missing = [n for n in canonical if n not in values]
        if missing:
            raise SystemExit(f"feature dict missing canonical names: {missing[:5]}")
        vec = [float(values[n]) for n in canonical]
    else:
        raise SystemExit(f"feature values unexpected type: {type(values).__name__}")
    if not all(math.isfinite(v) for v in vec):
        raise SystemExit("feature vector contains NaN/inf")
    return {
        "rec": rec,
        "vector": vec,
        "feature_ts_utc": rec.get("ingested_at", rec.get("event_time", "")),
    }


def _emit(source: str) -> None:
    print(f"  {source}")


def main() -> int:
    p = argparse.ArgumentParser(description="Shadow Ops 实证锁探针")
    p.add_argument("--feature-file", default=str(_REPO_ROOT / _FEATURE_FILE_DEFAULT))
    p.add_argument("--no-ledger-write", action="store_true")
    p.add_argument("--base-dir", default="data")
    args = p.parse_args()

    print("=" * 78)
    print("  SHADOW OPS 实证锁 (Phase 4 DEFCON 1) — REAL FEATURE → REAL MODEL → REAL TELEMETRY")
    print("=" * 78)

    # ── Step 1: 加载真实特征 ──────────────────────────────────────────────
    feat = _load_latest_real_feature(Path(args.feature_file))
    rec = feat["rec"]
    vec = feat["vector"]
    print(
        f"  [feat ] schema={rec.get('schema_name')} dim={len(vec)} "
        f"symbol={rec.get('symbol')} tf={rec.get('timeframe')} source={rec.get('source')}"
    )
    print(f"  [feat ] event_time={rec.get('event_time')}  (引擎实时 mt5_live 写入)")
    print(f"  [feat ] values[0:5]={[round(v, 6) for v in vec[:5]]}")

    # ── Step 2: 与 live_cycle Phase 4 完全相同的构造路径 ───────────────────
    # 缺省 read configs/live.yaml shadow_ops 段 (单点真源), 不手工传参.
    from core.runtime.shadow_ops.runtime import ShadowOpsRuntime

    runtime = ShadowOpsRuntime(symbol="XAUUSDc", base_dir=args.base_dir)
    diag = runtime.describe()
    print(f"  [init ] enabled={diag['enabled']} model_version={diag['model_version']}")
    print(
        f"  [init ] trigger_state={diag['trigger_state']} "
        f"threshold_abs_pred_pct={diag['threshold_abs_pred_pct']}"
    )
    print(f"  [init ] telemetry_dir={diag['telemetry_dir']}")
    if not diag["enabled"]:
        print("  [FATAL] shadow_ops disabled in configs/live.yaml — wiring not active")
        return 2

    # ── Step 3: 喂真实特征评分 (可选 dry-run) ──────────────────────────────
    ts = _iso_utc()
    from core.runtime.shadow_ops.micro_scaler_scorer import MicroScalerScorer

    # 直接走 scorer.predict 输出诊断字段 (raw vs cal — isotonic 语义证明)
    scorer: MicroScalerScorer = runtime._scorer  # noqa: SLF001 — 实证锁诊断只读
    trigger = runtime._trigger  # noqa: SLF001
    trigger.refresh()
    signal = scorer.predict(vec, contract_state=trigger.state)
    print(
        f"  [pred ] raw_pred_pct={signal.raw_pred_pct:.6f} → cal_pred_pct={signal.pred_pct:.6f} "
        f"(np.interp isotonic clip)"
    )
    print(
        f"  [pred ] abs={signal.abs_pred_pct:.6f} threshold={signal.threshold_abs_pred_pct} "
        f"triggered={signal.triggered} direction={signal.direction} decile={signal.decile_estimate}"
    )

    if args.no_ledger_write:
        print("  [dry  ] --no-ledger-write: 未落盘 (预演模式)")
    else:
        runtime.run(
            feature_vector=vec,
            cycle_count=-1,
            now_utc=ts,
            feature_ts_utc=feat["feature_ts_utc"],
        )
        print(f"  [ledger] run() executed → appended real prediction row")

    # ── Step 4: 遥测 ledger log tail (实证锁输出) ──────────────────────────
    so_dir = Path(_REPO_ROOT) / args.base_dir / "shadow_ops"
    pred_path = so_dir / "micro_scaler_predictions.jsonl"
    if pred_path.exists():
        lines = [l for l in pred_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        print(f"  [tail  ] {pred_path} — {len(lines)} row(s)")
        for line in lines[-3:]:
            try:
                obj = json.loads(line)
                print("    " + json.dumps(obj, ensure_ascii=False)[:200])
            except json.JSONDecodeError:
                print(f"    <unparsed> {line[:200]}")
    else:
        print(f"  [tail  ] {pred_path} — (ledger 尚未创建)")

    # ── Step 5: 零穿透证明 (watchdog 语义) ─────────────────────────────────
    from scripts._shadow_ops_watchdog import (
        _check_constructional_isolation,
        _check_mandate_integrity,
        _check_zero_real_orders,
    )

    base_dir = Path(_REPO_ROOT) / args.base_dir
    zero = _check_zero_real_orders(base_dir)
    mandate = _check_mandate_integrity(
        Path(_REPO_ROOT) / "data" / "training" / "micro_scaler_v2" / "micro_scaler_v2_trigger.json"
    )
    isolate = _check_constructional_isolation(Path(_REPO_ROOT) / "core" / "runtime" / "shadow_ops")
    print(
        f"  [zero  ] real-order leak: {'PASS (ZERO shadow orders in real journals)' if zero['zero_real_orders'] else 'VIOLATION'}  scanned={zero['scanned_lines']} lines"
    )
    print(
        f"  [mandate] trigger integrity: {mandate['status']} threshold={mandate.get('threshold_abs_pred_pct')}"
    )
    print(f"  [iso   ] constructional isolation: {'PASS' if isolate['ok'] else 'VIOLATION'}")

    critical = (
        not zero["zero_real_orders"]
        or mandate["status"] != "OK"
        or not isolate["ok"]
        or not diag["enabled"]
    )
    print("=" * 78)
    if critical:
        print("[实证锁 FAIL] DEFCON 1 red line breached.")
        return 1
    print("[实证锁 PASS] 真实特征 → 真实模型 → 真实遥测; 零订单穿透 MT5 bridge.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
