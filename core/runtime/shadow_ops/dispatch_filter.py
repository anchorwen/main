"""Layer-2 派发链熔断 (Dispatcher 物理拦截, IC 点名要素).

在 canonical choke point (``dispatch_live_order`` 入口) 插入 shadow 过滤器:
  若 payload 携带 shadow 标记 (venue="shadow_ops" / action="OBSERVE" /
  strategy 前缀 "shadow_ops_") → 物理拦截, 旁路写入 shadow 遥测 ledger,
  返回失败结果 (永不触达 MT5).
  否则 → 放行 (零开销, 正常路径零影响).

本模块**依赖零** (仅标准库) — 允许被 core/execution 函数内 import,
不构成跨层重型依赖链 (Decoupling 铁律).
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DISPATCH_FAILURE_REASON_SHADOW_OPS = "shadow_ops_dispatch_filtered"

# shadow 标记双字段 + strategy 前缀 (与 blueprint §2.3 一致)
_VENUE_MARKER = "shadow_ops"
_ACTION_MARKER = "observe"
_STRATEGY_PREFIX = "shadow_ops_"


def shadow_ops_dispatch_filter(payload: dict[str, Any]) -> dict[str, Any] | None:
    """返回拦截结果 dict (未派发) 若 payload 是 shadow 信号; 否则 None (放行)."""
    p = payload if isinstance(payload, dict) else {}
    venue = str(p.get("venue") or "").lower()
    action = str(p.get("action") or "").lower()
    strategy = str(p.get("strategy") or "").lower()

    is_shadow = (
        venue == _VENUE_MARKER or action == _ACTION_MARKER or strategy.startswith(_STRATEGY_PREFIX)
    )
    if not is_shadow:
        return None

    return {
        "status": "shadow_ops_filtered",
        "dispatched": False,
        "reason": DISPATCH_FAILURE_REASON_SHADOW_OPS,
        "detail": {
            "venue": p.get("venue"),
            "action": p.get("action"),
            "strategy": p.get("strategy"),
            "model_id": p.get("model_id"),
        },
    }


def record_dispatch_block(
    *,
    base_dir: str | Path,
    payload: dict[str, Any],
    result: dict[str, Any],
) -> None:
    """旁路写入 shadow 遥测 ledger (dispatch_blocks.jsonl). 调用方捕获 BLE001.

    即使 ledger 写入失败, 拦截已经生效 (订单未派发) — 只丢失一条审计行.
    """
    block_dir = Path(base_dir) / "shadow_ops"
    block_dir.mkdir(parents=True, exist_ok=True)
    rec = {
        "event": "shadow_ops_dispatch_blocked",
        "time_utc": datetime.now(UTC).replace(tzinfo=None).isoformat() + "Z",
        "venue": "shadow_ops",
        "action": "BLOCKED",
        "reason": result.get("reason"),
        "intent_id": result.get("intent_id"),
        "detail": result.get("detail"),
        "payload_keys": sorted(payload.keys()),
        "uuid": uuid.uuid4().hex,
    }
    with open(block_dir / "dispatch_blocks.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
