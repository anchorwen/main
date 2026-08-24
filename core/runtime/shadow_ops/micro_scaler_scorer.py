"""Micro Scaler v2 评分器 — LightGBM Booster + Isotonic 曲线 + Quantile Trigger.

推理链路 (与训练语义严格对齐, 防 train/serve skew):
  raw = booster.predict(X_40)        # 原始 3-bar 前向收益 (%)
  cal = isotonic_interp(raw)         # reg_report calibration_curve (np.interp, clip)
  triggered = |cal| >= D10 threshold # 阈值派生自已校准 OOS 预测分布
  direction = sign(cal): LONG if >0 else SHORT (幅度排序器); 未触发 → neutral

输出 ``ShadowOpsSignal`` (venue="shadow_ops" + action="OBSERVE") — 双字段标记,
任何下游见该标记必须旁路到遥测, 永不进入派发链 (DEFCON 1).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from core.contracts.exceptions import DataIntegrityError
from core.features.schemas.v9_institutional_schema import V9_INSTITUTIONAL_40_FEATURES
from core.runtime.shadow_ops.trigger_contract import TriggerContract, TriggerContractState

FEATURE_SCHEMA_V9_40 = "v9_institutional_40"
_EXPECTED_DIM = 40
# 报告文件名 (与模型同目录, 训练落档) — 校准曲线单一来源
_REPORT_FILENAME = "micro_scaler_v2_reg_report.json"


@dataclass(frozen=True)
class ShadowOpsSignal:
    """暗影评分信号 — 结构化双字段标记 (venue + action), 永不开单."""

    model_id: str = "micro_scaler_v2"
    pred_pct: float = 0.0
    raw_pred_pct: float = 0.0
    abs_pred_pct: float = 0.0
    threshold_abs_pred_pct: float | None = None
    triggered: bool = False
    direction: str = "neutral"
    decile_estimate: int = 0
    feature_schema: str = FEATURE_SCHEMA_V9_40
    contract_state: str = TriggerContractState.OK
    extra: dict[str, Any] = field(default_factory=dict)

    def to_prediction_record(
        self,
        *,
        time_utc: str,
        symbol: str,
        model_version: str,
        feature_ts_utc: str,
        cycle_count: int,
    ) -> dict[str, Any]:
        """全量预测流行 (每 cycle, 供 OOS ρ 实测 + 分布漂移)."""
        return {
            "event": "micro_scaler_prediction",
            "time_utc": time_utc,
            "symbol": symbol,
            "model_id": self.model_id,
            "model_version": model_version,
            "pred_pct": round(self.pred_pct, 6),
            "raw_pred_pct": round(self.raw_pred_pct, 6),
            "abs_pred_pct": round(self.abs_pred_pct, 6),
            "trigger_threshold_pct": self.threshold_abs_pred_pct,
            "trigger_mode": "quantile_top_decile_abs_pred",
            "triggered": self.triggered,
            "direction": self.direction,
            "feature_schema": self.feature_schema,
            "feature_ts_utc": feature_ts_utc,
            "cycle_count": cycle_count,
            "venue": "shadow_ops",
            "action": "OBSERVE",
        }

    def to_shadow_order_record(
        self,
        *,
        time_utc: str,
        symbol: str,
        model_version: str,
        feature_ts_utc: str,
        cycle_count: int,
    ) -> dict[str, Any]:
        """D10 触发的 shadow order intent 行 (供未来回测 / 晋级评审)."""
        rec = self.to_prediction_record(
            time_utc=time_utc,
            symbol=symbol,
            model_version=model_version,
            feature_ts_utc=feature_ts_utc,
            cycle_count=cycle_count,
        )
        rec["event"] = "micro_scaler_shadow_order"
        rec["decile_estimate"] = self.decile_estimate
        return rec


class MicroScalerScorer:
    """每进程单例评分器: 模型加载 + 维度断言 + Isotonic 校准 + 触发判定.

    Layer-1 构造性隔离: 本模块仅 import 特征 schema / trigger contract /
    标准库 / numpy / lightgbm. 没有任何函数能构造或发送订单.
    """

    def __init__(
        self,
        *,
        model_path: str | Path,
        trigger: TriggerContract,
        calibration_report_path: str | Path | None = None,
        feature_schema: str = FEATURE_SCHEMA_V9_40,
    ) -> None:
        import lightgbm as lgb

        self._model_path = Path(model_path)
        self._booster = lgb.Booster(model_file=str(self._model_path))
        n_feat = int(self._booster.num_feature())
        if n_feat != _EXPECTED_DIM:
            raise DataIntegrityError(
                f"micro_scaler_v2 model feature-count mismatch: model={n_feat} "
                f"expected={_EXPECTED_DIM}"
            )
        if feature_schema != FEATURE_SCHEMA_V9_40:
            raise DataIntegrityError(
                f"feature_schema mismatch: {feature_schema!r} != {FEATURE_SCHEMA_V9_40!r}"
            )
        self._feature_schema = feature_schema
        self._iso_curve: tuple[np.ndarray, np.ndarray] | None = None
        if calibration_report_path is not None:
            self._iso_curve = self._load_iso_curve(Path(calibration_report_path))
        self._trigger = trigger

    # ── Isotonic 校准曲线 (训练落档 → 运行时复刻, 防触发语义错位) ──
    def _load_iso_curve(self, report_path: Path) -> tuple[np.ndarray, np.ndarray] | None:
        if not report_path.exists():
            return None
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
            curve = report.get("calibration_curve")
            if not isinstance(curve, dict):
                return None
            x = np.asarray(curve.get("x_grid"), dtype=np.float64)
            y = np.asarray(curve.get("y_grid"), dtype=np.float64)
            if x.shape != y.shape or x.ndim != 1 or x.size < 2:
                raise DataIntegrityError(f"calibration_curve malformed: x={x.shape} y={y.shape}")
            return (x, y)
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            # 校准曲线缺失/损坏 → 退化到 raw pred (触发语义降级), 不阻断遥测
            return None

    def _apply_iso(self, raw: float) -> float:
        """np.interp 端点恒定外推 = isotonic out_of_bounds="clip" 语义."""
        if self._iso_curve is None:
            return raw
        x, y = self._iso_curve
        return float(np.interp(raw, x, y))

    def predict(
        self,
        feature_vector: Any,
        *,
        contract_state: str | None = None,
    ) -> ShadowOpsSignal:
        """喂入 V9_40 特征向量 → 校准 pred → Quantile Trigger 判定.

        contract_state 由 runtime 传入 (TTL 刷新后最新状态); 缺省读 trigger 自身.
        """
        vec = np.asarray(feature_vector, dtype=np.float64)
        if vec.ndim != 1 or vec.shape[0] != _EXPECTED_DIM:
            raise DataIntegrityError(
                f"micro_scaler_v2 feature vector must be 1D-{_EXPECTED_DIM}, "
                f"got shape {vec.shape}"
            )
        if not bool(np.isfinite(vec).all()):
            raise DataIntegrityError("micro_scaler_v2 feature vector contains NaN/inf")

        raw = float(self._booster.predict(vec.reshape(1, -1))[0])
        cal = self._apply_iso(raw)

        state = contract_state if contract_state is not None else self._trigger.state
        contract_ok = state == TriggerContractState.OK
        threshold = self._trigger.threshold_abs_pred_pct() if contract_ok else None
        triggered = threshold is not None and abs(cal) >= threshold

        if triggered and cal > 0:
            direction = "long"
        elif triggered and cal < 0:
            direction = "short"
        else:
            direction = "neutral"

        # decile_estimate (仅诊断): 触发 → 10; 未触发 → 按 |cal|/threshold 粗估 1..9
        if triggered:
            decile_est = 10
        elif threshold is not None and threshold > 0.0:
            decile_est = max(1, min(9, int(abs(cal) / threshold * 10.0)))
        else:
            decile_est = 0

        return ShadowOpsSignal(
            model_id="micro_scaler_v2",
            pred_pct=cal,
            raw_pred_pct=raw,
            abs_pred_pct=abs(cal),
            threshold_abs_pred_pct=threshold,
            triggered=triggered,
            direction=direction,
            decile_estimate=decile_est,
            feature_schema=self._feature_schema,
            contract_state=state,
        )

    @staticmethod
    def canonical_feature_names() -> list[str]:
        return list(V9_INSTITUTIONAL_40_FEATURES)
