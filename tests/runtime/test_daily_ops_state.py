"""Tests for core.runtime.daily_ops_state — cross-process state (FIX-20260820-002).

回归锁 (IC 裁决: 铺设回归锁):
- stamp-at-completion 幂等 + 原子写 (唯一写者 = launcher 子进程)
- 触发信号 save/consume 契约 (intent → launcher IPC)
- BTC 24/7 对照: state 缺失视为从未运行 (age 兜底立即触发)
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from core.runtime.daily_ops_state import (
    consume_daily_ops_trigger,
    load_daily_ops_trigger,
    load_last_daily_ops_utc,
    save_daily_ops_completion,
    save_daily_ops_trigger,
)


class TestSaveLoadCompletion:
    def test_save_then_load_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            save_daily_ops_completion(tmpdir, 1781834400.0)
            assert load_last_daily_ops_utc(tmpdir) == 1781834400.0

    def test_load_missing_returns_zero(self) -> None:
        """BTC 24/7 对照: state 缺失 = 从未运行 (0.0 → launcher age 兜底立即触发)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            assert load_last_daily_ops_utc(tmpdir) == 0.0

    def test_atomic_write_no_tmp_leftover(self) -> None:
        """原子写 (tmp + os.replace) — tmp 文件不残留."""
        with tempfile.TemporaryDirectory() as tmpdir:
            save_daily_ops_completion(tmpdir, 100.0)
            save_daily_ops_completion(tmpdir, 200.0)
            files = [p.name for p in Path(tmpdir).rglob("*")]
            assert "daily_ops_state.tmp" not in files
            assert "daily_ops_state.json" in files

    def test_stamp_overwrites_last_value(self) -> None:
        """唯一写者覆盖契约: 新 stamp 覆盖旧 (完成时间戳单调最新)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            save_daily_ops_completion(tmpdir, 111.0)
            save_daily_ops_completion(tmpdir, 222.0)
            assert load_last_daily_ops_utc(tmpdir) == 222.0

    def test_writes_under_expected_state_path(self) -> None:
        """路径与 catalog DAILY_OPS_STATE (state/daily_ops_state.json) 一致."""
        with tempfile.TemporaryDirectory() as tmpdir:
            save_daily_ops_completion(tmpdir, 123.0)
            assert Path(tmpdir) / "state" / "daily_ops_state.json" is not None
            data = json.loads(
                (Path(tmpdir) / "state" / "daily_ops_state.json").read_text(encoding="utf-8")
            )
            assert data["last_daily_ops_utc"] == 123.0


class TestTriggerContract:
    def test_save_load_trigger(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            save_daily_ops_trigger(tmpdir, 999.0)
            assert load_daily_ops_trigger(tmpdir) == 999.0

    def test_load_missing_trigger_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            assert load_daily_ops_trigger(tmpdir) is None

    def test_consume_reads_and_removes(self) -> None:
        """consume = 读 + 删 (launcher 消费后 intent 可再判定, 不重复触发)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            save_daily_ops_trigger(tmpdir, 777.0)
            assert consume_daily_ops_trigger(tmpdir) == 777.0
            assert consume_daily_ops_trigger(tmpdir) is None  # 已删 → 幂等 None

    def test_consume_no_file_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            assert consume_daily_ops_trigger(tmpdir) is None

    def test_trigger_overwrite_is_idempotent(self) -> None:
        """intent 主窗口多次判定 → 覆盖写同一信标 (幂等, 消费方去重)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            save_daily_ops_trigger(tmpdir, 1.0)
            save_daily_ops_trigger(tmpdir, 2.0)
            assert consume_daily_ops_trigger(tmpdir) == 2.0
