"""Tests for core.runtime.daily_ops_scheduler — daily operations scheduling.

FIX-20260619-035: Tier 1 zero-coverage breakout #6.
FIX-20260820-002 (方案 A): run_scheduled_daily_ops 降级为纯信号触发器 —
intent 进程零重负载同步计算, 只写触发信标 (由 launcher 子进程消费执行)。
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.runtime.daily_ops_scheduler import run_scheduled_daily_ops


class TestRunScheduledDailyOps:
    def _make_config(self, base_dir: str) -> MagicMock:
        cfg = MagicMock()
        cfg.base_dir = base_dir
        cfg.mt5_terminal_path = "/fake/mt5"
        return cfg

    def _make_state(self) -> MagicMock:
        state = MagicMock()
        state._last_daily_ops_utc = 0.0
        state._tracker_reload_pending = False
        return state

    def test_writes_trigger_signal_not_state(self) -> None:
        """方案 A: intent 只写触发信标, 绝不 stamp-at-start (state 由 launcher 回写)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._make_config(tmpdir)
            state = self._make_state()

            run_scheduled_daily_ops(cfg, state)

            # trigger 信标存在
            tpath = Path(tmpdir) / "state" / "daily_ops_trigger.json"
            assert tpath.exists()
            data = json.loads(tpath.read_text(encoding="utf-8"))
            assert data["requested_utc"] > 0
            # state 文件绝不写 (stamp-at-completion 唯一写者 = launcher)
            spath = Path(tmpdir) / "state" / "daily_ops_state.json"
            assert not spath.exists()

    def test_does_not_run_pipeline_synchronously(self) -> None:
        """intent 内不再有重负载同步计算 — run_daily_ops 不得被调用."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._make_config(tmpdir)
            state = self._make_state()

            with patch("scripts.daily_ops.run_daily_ops") as mock_run:
                run_scheduled_daily_ops(cfg, state)
            mock_run.assert_not_called()

    def test_does_not_set_tracker_reload_pending(self) -> None:
        """reload 标志由 live_cycle 跨进程完成检测设置 (非调度器)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._make_config(tmpdir)
            state = self._make_state()

            run_scheduled_daily_ops(cfg, state)

            assert state._tracker_reload_pending is False

    def test_does_not_run_resource_cleanup(self) -> None:
        """gc/compact/label prune 全部移交 launcher 子进程管线 (daily_ops.py)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._make_config(tmpdir)
            state = self._make_state()

            with patch("gc.collect") as mock_gc:
                run_scheduled_daily_ops(cfg, state)
            mock_gc.assert_not_called()

    def test_trigger_write_is_idempotent_and_never_raises(self) -> None:
        """信号触发必须无异常 (子进程不可用时也仅写信标)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._make_config(tmpdir)
            state = self._make_state()

            run_scheduled_daily_ops(cfg, state)
            run_scheduled_daily_ops(cfg, state)  # 幂等覆盖写, 无异常

            tpath = Path(tmpdir) / "state" / "daily_ops_trigger.json"
            data = json.loads(tpath.read_text(encoding="utf-8"))
            assert data["requested_utc"] > 0

    def test_trigger_write_failure_never_raises(self) -> None:
        """best-effort: 信标写失败 (OSError) 绝不 crash intent 循环 — 仅告警降级."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._make_config(tmpdir)
            state = self._make_state()

            with patch("core.runtime.daily_ops_state.save_daily_ops_trigger") as mock_save:
                mock_save.side_effect = OSError("disk full")
                run_scheduled_daily_ops(cfg, state)  # 不得抛异常
            mock_save.assert_called_once()
