"""Quantile Trigger 契约 — SSOT 读取 micro_scaler_v2_trigger.json.

IC 终局裁决 (2026-08-24): Micro Scaler v2 必须且只能采用 Quantile Trigger —
|pred| 落入历史样本 Top-decile (D10) 才允许 Shadow Order; 绝不允许固定阈值触发.

契约单一来源 = trigger json (随训练报告落档, emit 脚本派生自已记录 OOS 分布).
本模块提供:
  1. 启动加载 (fail-closed: 文件缺失 / 字段缺失 / mandate 篡改 → 构造即抛
     DataIntegrityError, 严禁 dict.get 抹平缺失字段)
  2. TTL 热刷新 (重读文件 mtime, 未来重训重发射的 trigger 免重启生效 —
     这是 "动态" 读取的落点)
  3. fail-closed 校验 (trigger_mode / mandate 被篡改 → 状态 VIOLATION,
     评分器拒出 shadow order, 保留预测遥测)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.contracts.exceptions import DataIntegrityError

TRIGGER_MODE_QUANTILE = "quantile_top_decile_abs_pred"
MANDATE_SENTINEL = "FIXED_THRESHOLD_FORBIDDEN"

_REQUIRED_FIELDS = (
    "trigger_mode",
    "threshold_abs_pred_pct",
    "direction_semantics",
    "mandate",
)


class TriggerContractState:
    """契约双态: OK → VIOLATION. VIOLATION 是终态 (需人工裁决后重启).

    语义 (IC 红线): VIOLATION 期间评分器仍可产出预测遥测, 但绝不允许产出
    shadow order — 不信任被篡改的阈值.
    """

    OK = "ok"
    VIOLATION = "violation"


class TriggerContract:
    """Quantile Trigger 规格的 SSOT 读取器 + 校验器 (每进程单例使用)."""

    def __init__(self, trigger_path: str | Path, ttl_seconds: float = 60.0) -> None:
        self._path = Path(trigger_path)
        self._ttl_seconds = float(ttl_seconds)
        self._spec: dict[str, Any] | None = None
        self._mtime_ns: int | None = None
        self._state: str = TriggerContractState.OK
        self._load(initial=True)

    @property
    def state(self) -> str:
        return self._state

    def spec(self) -> dict[str, Any]:
        if self._spec is None:
            raise DataIntegrityError("trigger contract spec unavailable (VIOLATION)")
        return self._spec

    def threshold_abs_pred_pct(self) -> float | None:
        """返回 D10 阈值 (%); VIOLATION 时返回 None (拒出 shadow order 语义)."""
        return float(self._spec["threshold_abs_pred_pct"]) if self._spec is not None else None

    def refresh(self) -> None:
        """TTL 热刷新: 仅当文件 mtime 变化才重读 (免重启生效)."""
        if self._ttl_seconds <= 0:
            return
        try:
            mtime_ns = self._path.stat().st_mtime_ns
        except OSError:
            self._state = TriggerContractState.VIOLATION
            return
        if mtime_ns != self._mtime_ns:
            self._load(initial=False)

    def _load(self, *, initial: bool) -> None:
        try:
            spec = json.loads(self._path.read_text(encoding="utf-8"))
            self._validate(spec)
            self._spec = spec
            self._mtime_ns = self._path.stat().st_mtime_ns
            self._state = TriggerContractState.OK
        except (OSError, ValueError, KeyError, TypeError, DataIntegrityError) as exc:
            if initial:
                raise DataIntegrityError(
                    f"trigger contract initial load failed: {self._path} — {exc!r}"
                ) from exc
            # 热刷新篡改判定: fail-closed — 进入 VIOLATION, 清空 spec (阈值不可信),
            # 评分器拒出 shadow order (保留预测遥测).
            self._state = TriggerContractState.VIOLATION
            self._spec = None

    def _validate(self, spec: dict[str, Any]) -> None:
        # 数据防幻觉 / DataIntegrityError 语义: 缺失字段是数据病, 严禁 dict.get 抹平.
        for field in _REQUIRED_FIELDS:
            if field not in spec:
                raise DataIntegrityError(f"trigger contract missing required field: {field!r}")
        if spec["trigger_mode"] != TRIGGER_MODE_QUANTILE:
            raise DataIntegrityError(
                f"trigger_mode tampered: {spec['trigger_mode']!r} != " f"{TRIGGER_MODE_QUANTILE!r}"
            )
        if MANDATE_SENTINEL not in str(spec["mandate"]):
            raise DataIntegrityError(
                f"trigger mandate tampered — {MANDATE_SENTINEL} sentinel missing"
            )
        thr = spec["threshold_abs_pred_pct"]
        if not isinstance(thr, int | float) or float(thr) <= 0.0:
            raise DataIntegrityError(f"threshold_abs_pred_pct must be positive, got {thr!r}")
