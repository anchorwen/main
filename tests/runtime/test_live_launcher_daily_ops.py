"""Tests for scripts.live_launcher._run_daily_ops_once — exit-code success predicate.

FIX-20260820-003 (DQAF-20260820-003, IC 裁决 Bulletproof Predicate): launcher 成功
谓词与 daily_ops 退出码契约对齐 — rc∈{0,1}=成功 (rc=1=完成且应用了动作), rc=2=错误,
崩溃 (rc=1 + stderr 含 Traceback) 仍判失败。本文件将该微妙契约物理锁死为回归锁。
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scripts.live_launcher import _run_daily_ops_once


def _run(
    capsys: pytest.CaptureFixture[str],
    *,
    returncode: int,
    stderr: str = "",
    stdout: str = "governance report ok",
) -> tuple[bool, bool, str]:
    """执行 _run_daily_ops_once (mock subprocess), 返回 (是否 stamp, 是否归档, stdout 文本)."""
    _result = MagicMock()
    _result.returncode = returncode
    _result.stderr = stderr
    _result.stdout = stdout

    with tempfile.TemporaryDirectory() as tmpdir:
        with (
            patch("subprocess.run", return_value=_result),
            patch("scripts.live_launcher._append_daily_run_log") as _archive,
            patch("core.runtime.daily_ops_state.save_daily_ops_completion") as _stamp,
        ):
            _run_daily_ops_once(
                python="python",
                project_root=Path(tmpdir),
                base_dir="data",
                log_fh=None,
                mt5_terminal_path=None,
                reason="test",
            )
            out = capsys.readouterr().out
            return _stamp.called, _archive.called, out


class TestRunDailyOpsOnceExitContract:
    def test_rc0_clean_stamps(self, capsys: pytest.CaptureFixture[str]) -> None:
        """rc=0 (无动作完成) → stamp + 归档 + Completed successfully."""
        stamped, archived, out = _run(capsys, returncode=0)
        assert stamped is True
        assert archived is True
        assert "[daily_ops] Completed successfully" in out

    def test_rc1_with_actions_stamps(self, capsys: pytest.CaptureFixture[str]) -> None:
        """rc=1 (完成且应用动作, errors==0, stderr 无 Traceback) → stamp + 归档.

        回归锚点: XAU daily_ops 常态含动作 → 恒 rc=1。FIX 前该运行被误判 FAILED
        → 永不 stamp → 4h age 兜底重跑循环 (DQAF-20260820-003)。
        """
        stamped, archived, out = _run(
            capsys,
            returncode=1,
            stderr="BrainFactory config warning: brain_id not found",
        )
        assert stamped is True
        assert archived is True
        assert "[daily_ops] Completed successfully" in out

    def test_rc2_error_fails_no_stamp(self, capsys: pytest.CaptureFixture[str]) -> None:
        """rc=2 (errors>0) → FAILED, 不 stamp 不归档."""
        stamped, archived, out = _run(capsys, returncode=2, stderr="integrity check failed")
        assert stamped is False
        assert archived is False
        assert "[daily_ops] FAILED (rc=2)" in out

    def test_rc1_crash_traceback_fails(self, capsys: pytest.CaptureFixture[str]) -> None:
        """rc=1 + stderr 含 Traceback (未捕获异常崩溃) → FAILED, 不 stamp.

        防弹背心: rc=1 同时是"动作完成"与"崩溃退出"的共用码 — 必须以 stderr
        Traceback 判别崩溃, 防止将崩溃误 stamp 为完成。
        """
        stamped, archived, out = _run(
            capsys,
            returncode=1,
            stderr=(
                "BrainFactory config warning: brain_id not found\n"
                "Traceback (most recent call last):\n"
                '  File "daily_ops.py", line 100, in main\n'
                "TypeError: 'NoneType' object is not subscriptable"
            ),
        )
        assert stamped is False
        assert archived is False
        assert "[daily_ops] FAILED (rc=1)" in out
