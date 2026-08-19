"""Tests for core.features.computers.v9_micro_computer.

FIX-20260619-047: Tier 2 zero-coverage breakout #2.
"""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock

from core.features.computers.v9_micro_computer import V9MicroComputer


class TestV9MicroComputer:
    def _make(self) -> tuple[V9MicroComputer, Any, Any]:
        c = V9MicroComputer(MagicMock(), "XAUUSDc")
        # TECH_DEBT-009: 运行时替换私有计算机为 Mock (保持原测试语义, 零行为变化);
        # 类型层 Any 绕过 __init__ 固定属性类型; 返回 Any 句柄供探针注入 (A3)
        c._v9 = cast(Any, MagicMock())
        c._micro = cast(Any, MagicMock())
        return c, cast(Any, c._v9), cast(Any, c._micro)

    _MICRO = {  # actual MICROSTRUCTURE_9_FEATURES names
        "tick_return": 0.1,
        "hl_ratio": 0.5,
        "co_ratio": 0.3,
        "avg_spread": 0.02,
        "OIM": 0.0,
        "tick_velocity": 5.0,
        "XAGUSDc_return": 0.0,
        "EURUSDc_return": 0.0,
        "USDJPYc_return": 0.0,
    }

    def test_compute_all_v9_success_micro_ok(self) -> None:
        c, v9, micro = self._make()
        v9.compute_all.return_value = {"atr": 6.0, "regime": 1.0}
        micro.compute_all.return_value = dict(self._MICRO)
        result = c.compute_all()
        assert result["atr"] == 6.0
        assert result["avg_spread"] == 0.02
        assert c.last_micro_ok is True

    def test_micro_failure_fills_zeros(self) -> None:
        c, v9, micro = self._make()
        v9.compute_all.return_value = {"atr": 6.0}
        micro.compute_all.side_effect = RuntimeError("tick data unavailable")

        result = c.compute_all()
        assert c.last_micro_ok is False
        # Micro slots filled with 0.0
        assert result.get("avg_spread") == 0.0

    def test_v9_failure_still_returns_micro_fallback(self) -> None:
        c, v9, micro = self._make()
        v9.compute_all.side_effect = RuntimeError("v9 failed")
        micro.compute_all.return_value = dict(self._MICRO)
        result = c.compute_all()
        assert c.last_micro_ok is True
        assert result["avg_spread"] == 0.02

    def test_is_micro_available_reflects_last_state(self) -> None:
        c, _, _ = self._make()
        assert c.is_micro_available is False
        c.last_micro_ok = True
        assert c.is_micro_available is True

    def test_missing_micro_features_detected(self) -> None:
        c, v9, micro = self._make()
        v9.compute_all.return_value = {}
        # Only 7 of 9 features present
        partial = dict(self._MICRO)
        del partial["EURUSDc_return"]
        del partial["USDJPYc_return"]
        micro.compute_all.return_value = partial
        result = c.compute_all()
        assert c.last_micro_ok is False
