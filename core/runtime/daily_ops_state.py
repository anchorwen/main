"""Daily-ops cross-process state — Single Writer contract (FIX-20260820-002).

方案 A (Single Executor / SSOT): daily_ops 管线唯一执行者 = launcher 子进程.

- **stamp-at-completion**: `daily_ops_state.json` 的唯一写者是 launcher 子进程
  成功回调 (`save_daily_ops_completion`)。彻底废除 intent 侧 stamp-at-start
  (旧实现导致逐日 +5min 无界漂移 + `_already_ran_today` 永久抑制 22:00 主窗口)。
- **触发信号**: intent 进程只写瞬时 trigger 信标 (`save_daily_ops_trigger`),
  launcher 消费 (`consume_daily_ops_trigger`) 后执行子进程。零重负载、零同步阻塞
  —— intent 内不再有 >300s 同步长任务 (消除 watchdog 硬杀结构根因)。
- **统一读取**: live_cycle (触发判定) / launcher (age 兜底) / 健康检查三方
  共用 `load_last_daily_ops_utc` 单点。

纯 stdlib + 原子写 (tmp + os.replace)，无 core 依赖 —— 保证 live_launcher
(顶层脚本，仅 stdlib) 与 core.runtime 可安全共同消费。文件路径与 catalog
DAILY_OPS_STATE (`path_template="state/daily_ops_state.json"`) 一致。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# 触发信号文件名 (瞬时 IPC 信标，消费即删，不注册 catalog TTL)
TRIGGER_FILE = "daily_ops_trigger.json"


def _state_path(base_dir: str | Path) -> Path:
    return Path(base_dir) / "state" / "daily_ops_state.json"


def _trigger_path(base_dir: str | Path) -> Path:
    return Path(base_dir) / "state" / TRIGGER_FILE


def _atomic_write_json(path: Path, payload: dict) -> None:
    """原子写 — tmp + os.replace，避免读侧撕裂。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    _tmp = path.with_suffix(path.suffix + ".tmp")
    _tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(_tmp, path)


def save_daily_ops_completion(base_dir: str | Path, ts: float) -> None:
    """stamp-at-completion — 唯一写者 (launcher 子进程 rc==0 回调)。"""
    _atomic_write_json(_state_path(base_dir), {"last_daily_ops_utc": ts})


def load_last_daily_ops_utc(base_dir: str | Path) -> float:
    """读取最近一次成功完成时间戳。文件缺失/损坏 → 0.0 (视为从未运行)。"""
    try:
        _data = json.loads(_state_path(base_dir).read_text(encoding="utf-8"))
        return float(_data.get("last_daily_ops_utc", 0.0))
    except (OSError, ValueError, KeyError, TypeError):
        return 0.0


def save_daily_ops_trigger(base_dir: str | Path, requested_utc: float) -> None:
    """intent → launcher 触发信标 (覆盖写；幂等性由调用方判定控制)。"""
    _atomic_write_json(_trigger_path(base_dir), {"requested_utc": requested_utc})


def load_daily_ops_trigger(base_dir: str | Path) -> float | None:
    """是否有未消费的触发请求。无 → None。"""
    try:
        _data = json.loads(_trigger_path(base_dir).read_text(encoding="utf-8"))
        return float(_data.get("requested_utc", 0.0))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def consume_daily_ops_trigger(base_dir: str | Path) -> float | None:
    """消费触发请求 (读 + 删)。launcher 每 60s tick 调用。"""
    _ts = load_daily_ops_trigger(base_dir)
    if _ts is not None:
        try:
            _trigger_path(base_dir).unlink()
        except OSError:
            pass  # 删除失败不致命 — 幂等读取已拿到 ts
    return _ts
