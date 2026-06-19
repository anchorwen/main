"""Reentry block streak alert — Strangler Fig #28 from live_cycle.py.

Extracted from live_cycle.py:execute_live_cycle() (~51 lines).
Scans strategy evaluation results for persistently blocked strategies
and fires alerts when a strategy has been reentry-blocked for >=5
consecutive cycles.
"""

from __future__ import annotations

import contextlib
import time
from typing import Any

from core.runtime.time_utils import _utc_iso


def check_reentry_block_streaks(
    eval_summary: dict[str, Any],
    state: Any,
) -> None:
    """Scan strategy results for reentry-blocked strategies and fire alerts.

    Called at cycle end after strategy evaluation.  Tracks consecutive
    blocks per strategy via dynamic state attributes and fires a warning
    via the alert hub every 5 cycles while a strategy remains blocked.

    Args:
        eval_summary: Results dict from strategy evaluator with
            ``strategy_results`` list.
        state: LiveCycleState, mutated to track streak counters via
            ``_reentry_block_streak_<strategy>`` attributes.
    """
    _strat_results = eval_summary.get("strategy_results", [])
    _ah_reentry = getattr(state, "alert_hub", None)

    for _sr in _strat_results:
        _sname = _sr.get("strategy", "")
        _reason = _sr.get("reason", "")
        if not _sr.get("should_trade") and (
            "brain_flip" in _reason
            or "meta_exit" in _reason
            or "sl_" in _reason
            or "ou_revert" in _reason
            or "unknown" in _reason
            or "bleed" in _reason
            or "momentum" in _reason
            or "hesitation" in _reason
        ):
            _streak_key = f"_reentry_block_streak_{_sname}"
            _streak = getattr(state, _streak_key, 0) + 1
            setattr(state, _streak_key, _streak)
            if _streak >= 5 and _streak % 5 == 0 and _ah_reentry is not None:
                _alert = {
                    "rule_name": "reentry_persistent_block",
                    "rule_id": f"reentry_block_{_sname}_{int(time.time())}",
                    "severity": "warning",
                    "title": f"Reentry Block: {_sname} ({_streak} cycles)",
                    "text": (
                        f"## {_sname} 重入守卫持续拦截\n\n"
                        f"- 连续拦截: **{_streak}** 个周期\n"
                        f"- 拦截原因: {_reason}\n"
                        f"- 时间: {_utc_iso()}\n\n"
                        f"> 请检查退出类型和历史置信度。"
                    ),
                    "timestamp_utc": _utc_iso(),
                    "context": {
                        "strategy": _sname,
                        "consecutive_blocks": _streak,
                        "reason": _reason,
                    },
                }
                with contextlib.suppress(Exception):
                    _ah_reentry._alert_queue.put_nowait(_alert)
        else:
            # Reset streak when strategy passes or isn't reentry-blocked
            _streak_key = f"_reentry_block_streak_{_sname}"
            if hasattr(state, _streak_key):
                setattr(state, _streak_key, 0)
