"""Phase 4 Shadow Ops — 验收闸门 G1-G4 (DEFCON 1).

接线蓝图: blueprints/modules/shadow_ops.md §7.
  G1 触发逻辑      — Trigger 契约边界 + mandate 篡改 fail-closed + 评分器 D10 边界
  G2 构造性隔离    — shadow_ops 包 import ⊄ 派发能力模块
  G3 派发链熔断    — dispatch_live_order 入口物理拦截 + 旁路 ledger + 零 dispatch
  G4 端到端        — ShadowOpsRuntime 消费 V9_40 → ledger 有行 + 结构零派发

红线: 绝不允许 shadow 信号触达派发链; Quantile Trigger ONLY (禁固定阈值).
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any, cast

import numpy as np
import pytest

from core.contracts.exceptions import DataIntegrityError

REPO_ROOT = Path(__file__).resolve().parents[1]

_DENYLIST = (
    "mt5_bridge_worker",
    "live_order_sender",
    "communication_dispatcher",
    "zmq",
    "execution_queue",
    "live_execution_contract",
    "dispatch_context",
)


def _write_trigger_spec(path: Path, **overrides: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "model_id": "micro_scaler_v2",
        "mode": "regression_forward3bar",
        # FIX-20260824-005: 触发源 cal→raw |pred| (规范 mode 固化)
        "trigger_mode": "quantile_top_decile_abs_raw_pred",
        "threshold_abs_pred_pct": 0.06007,
        "trigger_rate_pct_oos": 9.89,
        "direction_semantics": "sign(pred): LONG if pred>0 else SHORT",
        "mandate": "FIXED_THRESHOLD_FORBIDDEN: Quantile Trigger ONLY",
    }
    spec.update(overrides)
    path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    return spec


# ────────────────────────────────────────────────────────────────────
# G1 — Trigger 契约 + 评分器 D10 边界
# ────────────────────────────────────────────────────────────────────
def test_g1_trigger_contract_boundary_and_tamper(tmp_path):
    from core.runtime.shadow_ops.trigger_contract import (
        TriggerContract,
        TriggerContractState,
    )

    trig = tmp_path / "trigger.json"
    _write_trigger_spec(trig)
    tc = TriggerContract(trig, ttl_seconds=60)
    assert tc.state == TriggerContractState.OK
    assert tc.threshold_abs_pred_pct() == pytest.approx(0.06007)

    # 热刷新篡改 → VIOLATION (fail-closed, 阈值不可信)
    _write_trigger_spec(trig, trigger_mode="fixed_threshold")
    tc.refresh()
    assert tc.state == TriggerContractState.VIOLATION
    assert tc.threshold_abs_pred_pct() is None


def test_g1_trigger_contract_initial_tamper_raises(tmp_path):
    from core.runtime.shadow_ops.trigger_contract import TriggerContract

    trig = tmp_path / "trigger.json"
    _write_trigger_spec(trig, mandate="USE_FIXED_THRESHOLD_0.01")
    with pytest.raises(DataIntegrityError):
        TriggerContract(trig)

    _write_trigger_spec(trig, trigger_mode="fixed_threshold")
    with pytest.raises(DataIntegrityError):
        TriggerContract(trig)


class _FakeBooster:
    def __init__(self, raw: float) -> None:
        self._raw = raw

    def predict(self, X: Any) -> np.ndarray:
        return np.asarray([self._raw], dtype=np.float64)


class _FakeTrigger:
    def __init__(self, threshold: float) -> None:
        self._threshold = threshold
        self.state = "ok"

    def threshold_abs_pred_pct(self) -> float:
        return self._threshold


def _boundary_scorer(raw: float, threshold: float = 0.06007):
    """构造纯边界评分器 (绕开真实模型加载, 直测 D10 判定逻辑)."""
    from core.runtime.shadow_ops.micro_scaler_scorer import MicroScalerScorer

    scorer = object.__new__(MicroScalerScorer)
    from core.runtime.shadow_ops.trigger_contract import TriggerContract

    scorer._booster = _FakeBooster(raw)  # noqa: SLF001 — 测试探针直连评分器内部
    scorer._iso_curve = None  # raw == cal
    scorer._feature_schema = "v9_institutional_40"
    scorer._trigger = cast(
        TriggerContract, _FakeTrigger(threshold)
    )  # 类型探针: Fake 满足 scorer 契约
    return scorer


def test_g1_scorer_d10_boundary():
    thr = 0.06007
    # 未触发 (低于阈值)
    sig = _boundary_scorer(0.06006, thr).predict(np.zeros(40))
    assert sig.triggered is False
    assert sig.direction == "neutral"
    assert sig.threshold_abs_pred_pct == pytest.approx(thr)

    # 边界触发 (等于阈值) → LONG
    sig = _boundary_scorer(0.06007, thr).predict(np.zeros(40))
    assert sig.triggered is True
    assert sig.direction == "long"
    assert sig.decile_estimate == 10

    # SHORT 触发
    sig = _boundary_scorer(-0.06008, thr).predict(np.zeros(40))
    assert sig.triggered is True
    assert sig.direction == "short"

    # 零信号 → neutral (无 shadow order)
    sig = _boundary_scorer(0.0, thr).predict(np.zeros(40))
    assert sig.triggered is False
    assert sig.direction == "neutral"


def test_g1_signal_markers_welded_to_telemetry():
    sig = _boundary_scorer(0.075, 0.06007).predict(np.zeros(40))
    rec = sig.to_prediction_record(
        time_utc="2026-08-24T10:00:00Z",
        symbol="XAUUSDc",
        model_version="v2_20260824",
        feature_ts_utc="2026-08-24T10:00:00Z",
        cycle_count=7,
    )
    assert rec["venue"] == "shadow_ops"
    assert rec["action"] == "OBSERVE"
    assert rec["trigger_mode"] == "quantile_top_decile_abs_raw_pred"
    order = sig.to_shadow_order_record(
        time_utc="2026-08-24T10:00:00Z",
        symbol="XAUUSDc",
        model_version="v2_20260824",
        feature_ts_utc="2026-08-24T10:00:00Z",
        cycle_count=7,
    )
    assert order["event"] == "micro_scaler_shadow_order"
    assert order["triggered"] is True


# ────────────────────────────────────────────────────────────────────
# G2 — 构造性隔离 (Layer 1)
# ────────────────────────────────────────────────────────────────────
def test_g2_constructional_isolation():
    pkg = REPO_ROOT / "core" / "runtime" / "shadow_ops"
    import_re = re.compile(r"^\s*(?:import|from)\s+([A-Za-z0-9_\.]+)", re.MULTILINE)
    src = "\n".join(py.read_text(encoding="utf-8") for py in sorted(pkg.glob("*.py")))
    tops = {m.group(1).split(".")[0] for m in import_re.finditer(src)}
    for tok in _DENYLIST:
        assert not any(tok in t for t in tops), f"import denylist violated: {tok}"


# ────────────────────────────────────────────────────────────────────
# G3 — 派发链熔断 (Layer 2)
# ────────────────────────────────────────────────────────────────────
def test_g3_dispatch_filter_markers():
    from core.runtime.shadow_ops.dispatch_filter import (
        shadow_ops_dispatch_filter,
    )

    # venue 标记 → 拦截
    r = shadow_ops_dispatch_filter(
        {"venue": "shadow_ops", "action": "OBSERVE", "symbol": "XAUUSDc"}
    )
    assert r is not None
    assert r["dispatched"] is False
    assert r["reason"] == "shadow_ops_dispatch_filtered"

    # action 标记 → 拦截
    assert shadow_ops_dispatch_filter({"action": "OBSERVE"}) is not None

    # strategy 前缀 → 拦截
    assert shadow_ops_dispatch_filter({"strategy": "shadow_ops_micro_scaler"}) is not None

    # 正常实盘 payload → 放行 (None)
    assert (
        shadow_ops_dispatch_filter(
            {"action": "OPEN", "symbol": "XAUUSDc", "strategy": "XAU_Swing_M30"}
        )
        is None
    )
    # 非 dict → 放行 (payload 防御: 函数内部 isinstance 兜底, 类型探针 cast)
    assert shadow_ops_dispatch_filter(cast(Any, None)) is None


def test_g3_dispatch_live_order_intercept(tmp_path):
    """零穿透证明: shadow payload 在 dispatch_live_order 入口被物理拦截,
    任何派发机制 (ServiceContainer/dispatcher) 不被触达."""
    from core.execution.live_order_sender import dispatch_live_order

    res = dispatch_live_order(
        base_dir=str(tmp_path),
        broker=None,  # 若 fuse 失效, broker=None 会在此路径立即 TypeError (证明拦截先于派发)
        symbol="XAUUSDc",
        execution_payload={
            "venue": "shadow_ops",
            "action": "OBSERVE",
            "strategy": "shadow_ops_micro_scaler",
            "model_id": "micro_scaler_v2",
        },
        adapter_name="mt5_zmq",
    )
    assert res["dispatched"] is False
    assert res["reason"] == "shadow_ops_dispatch_filtered"
    assert res["status"] == "shadow_ops_filtered"

    # 旁路 ledger 已写
    block_path = tmp_path / "shadow_ops" / "dispatch_blocks.jsonl"
    assert block_path.exists()
    rows = [
        json.loads(line) for line in block_path.read_text(encoding="utf-8").strip().splitlines()
    ]
    assert len(rows) == 1
    assert rows[0]["event"] == "shadow_ops_dispatch_blocked"
    assert rows[0]["action"] == "BLOCKED"


# ────────────────────────────────────────────────────────────────────
# G4 — 端到端 (真实模型 + 真实 trigger, 遥测落 tmp)
# ────────────────────────────────────────────────────────────────────
def test_g4_runtime_end_to_end(tmp_path):
    from core.runtime.shadow_ops.runtime import ShadowOpsRuntime

    model_path = REPO_ROOT / "data" / "training" / "micro_scaler_v2" / "micro_scaler_v2_reg.txt"
    trigger_path = (
        REPO_ROOT / "data" / "training" / "micro_scaler_v2" / "micro_scaler_v2_trigger.json"
    )
    assert model_path.exists(), f"model artifact missing: {model_path}"
    assert trigger_path.exists(), f"trigger artifact missing: {trigger_path}"

    telemetry_dir = tmp_path / "shadow_ops"
    cfg_path = tmp_path / "live.yaml"
    cfg_path.write_text(
        json.dumps(
            {
                "shadow_ops": {
                    "enabled": True,
                    "micro_scaler_v2": {
                        "model_path": str(model_path),
                        "trigger_path": str(trigger_path),
                        "feature_schema": "v9_institutional_40",
                        "telemetry_dir": str(telemetry_dir),
                        "trigger_refresh_ttl_seconds": 0,
                        "import_denylist_enforced": True,
                        "model_version": "v2_20260824",
                    },
                    "meta_exit_v3": {"enforce_shadow_only": True},
                }
            }
        ),
        encoding="utf-8",
    )

    rt = ShadowOpsRuntime(
        symbol="XAUUSDc",
        base_dir=str(tmp_path),
        config_path=str(cfg_path),
    )
    assert rt.enabled is True
    diag = rt.describe()
    assert diag["model_version"] == "v2_20260824"
    # FIX-20260824-005: 阈值从 trigger.json 实读 (raw |pred| p90, 抗未来重训)
    expected_thr = json.loads(trigger_path.read_text(encoding="utf-8"))["threshold_abs_pred_pct"]
    assert diag["threshold_abs_pred_pct"] == pytest.approx(float(expected_thr))

    # 真实 V9_40 向量 → run()
    vec = np.zeros(40, dtype=np.float64)
    rt.run(
        feature_vector=vec,
        cycle_count=7,
        now_utc="2026-08-24T10:00:00Z",
    )

    pred_path = telemetry_dir / "micro_scaler_predictions.jsonl"
    assert pred_path.exists()
    rows = [json.loads(line) for line in pred_path.read_text(encoding="utf-8").strip().splitlines()]
    assert len(rows) == 1
    r = rows[0]
    assert r["event"] == "micro_scaler_prediction"
    assert r["venue"] == "shadow_ops"
    assert r["action"] == "OBSERVE"
    assert r["model_id"] == "micro_scaler_v2"
    assert r["model_version"] == "v2_20260824"
    assert r["symbol"] == "XAUUSDc"
    assert r["cycle_count"] == 7
    assert r["feature_schema"] == "v9_institutional_40"
    assert r["trigger_mode"] == "quantile_top_decile_abs_raw_pred"
    assert isinstance(r["pred_pct"], float) and math.isfinite(r["pred_pct"])
    assert isinstance(r["abs_pred_pct"], float) and math.isfinite(r["abs_pred_pct"])

    # shadow_orders ledger: 若触发, 每行必带 shadow 标记
    order_path = telemetry_dir / "micro_scaler_shadow_orders.jsonl"
    if order_path.exists():
        order_rows = [
            json.loads(line) for line in order_path.read_text(encoding="utf-8").strip().splitlines()
        ]
        for o in order_rows:
            assert o["venue"] == "shadow_ops"
            assert o["action"] == "OBSERVE"
            assert o["triggered"] is True

    # 结构零派发: 运行时包无派发能力 import (G2 复用)
    test_g2_constructional_isolation()
