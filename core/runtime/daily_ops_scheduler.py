"""Daily operations scheduler — signal-only gate (FIX-20260820-002, 方案 A).

原职责 (同步执行 daily_ops 管线) 已移交 launcher 子进程 (唯一执行者 / SSOT):

- intent 进程不再执行任何重负载同步计算 —— 只写瞬时触发信标
  (`core.runtime.daily_ops_state.save_daily_ops_trigger`)，由 launcher 消费。
- 完成时间戳仅由 launcher 子进程成功回调回写 (stamp-at-completion)，彻底废除
  stamp-at-start (旧实现导致逐日 +5min 无界漂移 + `_already_ran_today` 永久
  抑制 22:00 主窗口)。
- 管线副作用全部由 `scripts/daily_ops.py` 子进程承担：
  feature store maintenance (`_step_feature_store_maintenance`) / governance
  (`_step_governance`) / label prune (`_step_label_prune`) / tracker 落盘。
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.runtime.live_cycle import LiveCycleConfig, LiveCycleState

from core.runtime.time_utils import _utc_iso


def run_scheduled_daily_ops(config: LiveCycleConfig, state: LiveCycleState) -> None:
    """请求 launcher 执行 daily_ops (信号触发, 零重负载)。

    Single Executor (方案 A): intent 进程只负责"判定该跑"并写触发信标，
    真正的 daily_ops 管线由 launcher 子进程执行，成功后在 completion 时
    stamp-at-completion。intent 内不再存在 >300s 同步长任务 —— watchdog
    300s 硬杀的结构性根因被移除。
    """
    _requested = time.time()
    print(
        json.dumps({"event": "daily_ops_scheduled", "time": _utc_iso()}, ensure_ascii=False),
        flush=True,
    )
    from core.runtime.daily_ops_state import save_daily_ops_trigger

    # best-effort 信号: 触发信标写失败绝不冒泡 crash intent 循环 (watchdog 300s 内
    # 零容忍异常路径)。launcher age 兜底 (6h) 会在下次 tick 补跑, 信号只是加速。
    try:
        save_daily_ops_trigger(config.base_dir, _requested)
    except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
        print(
            json.dumps(
                {
                    "event": "daily_ops_trigger_signal_failed",
                    "time": _utc_iso(),
                    "reason": "best_effort_signal_write_failed",
                    "fallback": "launcher_age_check",
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return
    print(
        json.dumps(
            {
                "event": "daily_ops_trigger_signal",
                "time": _utc_iso(),
                "requested_utc": _requested,
                "executor": "launcher_subprocess",
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
