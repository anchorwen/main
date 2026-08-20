"""Tests for scripts.live_launcher._run_daily_ops_once — JSON Payload Authentication.

FIX-20260820-004 (DQAF-20260820-004, IC 裁决 JSON Payload Authentication): 废弃 stderr
"Traceback" 启发式 (FIX-20260820-003 防弹背心被证伪 — daily_ops fail_open_guard 用
logging.exception 将被捕获异常 traceback 例行写入 stderr (last-resort handler 无 handler
配置时固定落 stderr); training_readiness 命中空/损坏 npz → EOFError 确定性命中每个 XAU
运行 → stderr Traceback 不具崩溃特异性, 会确定性误杀每条完成运行 → stamp 结构性无法落盘)。
成功判定 = rc<=1 AND stdout 尾部认证出完整 daily_ops report JSON (schema_version 标识,
daily_ops.py L3491 无条件键; L3589-3590 仅完整流水线正常返回后打印 — 崩溃走不到该行)。
本文件将契约物理锁死为回归锁 (7 分支)。
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scripts.live_launcher import _run_daily_ops_once

# 2026-08-20 12:50:44 solo 复现 (DQAF-20260820-004) 的 stderr 真实片段 —
# BrainFactory warnings + EOFError Traceback (被捕获异常, 非崩溃)。
_REPRO_STDERR = (
    "BrainFactory config warning: brain_id=Barrier_V9_12B_V2: auto-inferred "
    "objective='multi:softprob' from model artifact - consider adding to brain config explicitly\n"
    "BrainFactory config warning: brain_id=Brain_Rev_M30_V1: missing artifact_hash "
    "- model integrity cannot be verified. Re-train to generate hash.\n"
    "Traceback (most recent call last):\n"
    '  File "D:\\future\\scripts\\daily_ops.py", line 1992, in _step_training_readiness\n'
    "    report = evaluate_training_readiness(str(cp), str(resolved))\n"
    '  File "D:\\future\\scripts\\check_training_readiness.py", line 1082, in '
    "evaluate_training_readiness\n"
    "    validate_stage_3_dataset_builder(contract, data_dir),\n"
    '  File "D:\\future\\scripts\\check_training_readiness.py", line 722, in '
    "validate_stage_3_dataset_builder\n"
    "    data = np.load(_npz_path, allow_pickle=True)\n"
    '  File "...\\\\numpy\\\\lib\\\\_npyio_impl.py", line 468, in load\n'
    '    raise EOFError("No data left in file")\n'
    "EOFError: No data left in file"
)


def _report_stdout(**overrides: object) -> str:
    """模拟 daily_ops.py L3589-3590 打印形态的完成 report (stdout 尾块 JSON)."""
    report: dict[str, object] = {
        "schema_version": "daily_ops.v1",
        "generated_at": "2026-08-20T12:50:44",
        "base_dir": "data",
        "dry_run": False,
        "total_steps": 31,
        "errors": 0,
        "actions_total": 6,
        "steps": [],
    }
    report.update(overrides)
    return json.dumps(report, indent=2, ensure_ascii=False)


def _report_stdout_with_daily_recap() -> str:
    """真实结构复刻 (2026-08-20 12:50:44 solo 复现): daily_recap step 的 sections
    列表含字符串 "schema_version" — 曾使裸 `"schema_version"` 锚定定位到内部 step
    对象 → json.loads Extra data → 谓词误判 FAILED (FIX-20260820-004 实现缺陷实证).
    断言: 顶层 `{\n  "schema_version":` 标记认证必须忽略该 step 内同名内容."""
    return _report_stdout(
        steps=[
            {
                "step": "daily_recap",
                "status": "ok",
                "date_key": "",
                "sections": [
                    "schema_version",
                    "generated_at",
                    "date_key",
                    "total_steps",
                    "errors",
                ],
            },
            {"step": "param_optimization", "status": "ok", "actions_applied": 1},
        ]
    )


def _run(
    capsys: pytest.CaptureFixture[str],
    *,
    returncode: int,
    stderr: str = "",
    stdout: str | None = None,
) -> tuple[bool, bool, str]:
    """执行 _run_daily_ops_once (mock subprocess), 返回 (是否 stamp, 是否归档, 输出)."""
    if stdout is None:
        stdout = _report_stdout()
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


class TestRunDailyOpsOnceJsonAuthentication:
    def test_rc0_with_report_stamps(self, capsys: pytest.CaptureFixture[str]) -> None:
        """rc=0 (无动作完成) + stdout 尾 report JSON → stamp + 归档 + Completed."""
        stamped, archived, out = _run(capsys, returncode=0)
        assert stamped is True
        assert archived is True
        assert "[daily_ops] Completed successfully" in out

    def test_rc1_with_report_stamps(self, capsys: pytest.CaptureFixture[str]) -> None:
        """rc=1 (完成且应用动作) + report JSON → stamp + 归档.

        回归锚点: XAU daily_ops 常态含动作 → 恒 rc=1。FIX-20260820-003 前该运行
        被误判 FAILED → 永不 stamp → 4h age 兜底重跑循环 (DQAF-20260820-003)。
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

    def test_rc1_no_report_fails_no_stamp(self, capsys: pytest.CaptureFixture[str]) -> None:
        """rc=1 但 stdout 无完成 report (崩溃/中断走不到 L3589) → FAILED 不 stamp.

        取代 FIX-20260820-003 的 stderr Traceback 判别 — 完成标记缺失 = 未完成。
        stderr 是否含 Traceback 已与本判定无关。
        """
        stamped, archived, out = _run(
            capsys,
            returncode=1,
            stdout="governance report ok",
            stderr=(
                "Traceback (most recent call last):\n"
                '  File "daily_ops.py", line 100, in main\n'
                "TypeError: 'NoneType' object is not subscriptable"
            ),
        )
        assert stamped is False
        assert archived is False
        assert "[daily_ops] FAILED (rc=1)" in out

    def test_rc1_traceback_with_report_stamps(self, capsys: pytest.CaptureFixture[str]) -> None:
        """IC 雷霆裁决第 5 分支 — solo 复现桩: stderr 含被捕获 EOFError Traceback +
        stdout 尾完整 report JSON → SUCCESS (stamp + 归档).

        2026-08-20 12:50:44 solo 复现 (errors=0, actions_total=6) 实证: daily_ops
        fail_open_guard 将被捕获异常 traceback 例行写入 stderr, 该 Traceback 不具崩溃
        特异性 — stderr 全噪音必须被 JSON 认证免疫 (FIX-20260820-003 此处分死)。
        """
        stamped, archived, out = _run(capsys, returncode=1, stderr=_REPRO_STDERR)
        assert stamped is True
        assert archived is True
        assert "[daily_ops] Completed successfully" in out

    def test_rc1_real_report_step_schema_version_stamps(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """真实结构复刻 (2026-08-20 13:44 UTC launcher 实测): daily_recap step 的
        sections 列表含 "schema_version" → 顶层标记认证必须忽略 step 内同名内容.

        FIX-20260820-004 实现缺陷回归: 裸 `"schema_version"` rfind 锚定命中内部 step
        字符串 → 定位内部 step 对象 → json.loads Extra data → 谓词误判 FAILED →
        stamp 不落盘 (实盘 13:44 完成运行被误判). 顶层 `{\n  "schema_version":`
        (列0 `{` + 列2 键) 免疫该碰撞.
        """
        stamped, archived, out = _run(
            capsys,
            returncode=1,
            stdout=_report_stdout_with_daily_recap(),
            stderr="BrainFactory config warning: brain_id not found",
        )
        assert stamped is True
        assert archived is True
        assert "[daily_ops] Completed successfully" in out

    def test_truncated_report_tail_fails(self, capsys: pytest.CaptureFixture[str]) -> None:
        """stdout 尾部 report 截断 (崩溃写一半) → json.loads 失败 → FAILED 不 stamp."""
        truncated = _report_stdout_with_daily_recap().rstrip()[:-4]  # 砍 closing → 非完整 JSON
        stamped, archived, out = _run(capsys, returncode=1, stdout=truncated)
        assert stamped is False
        assert archived is False
        assert "[daily_ops] FAILED (rc=1)" in out
