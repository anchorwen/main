"""Live Fire 敢死队熔断器 — 投委会方向 B 裁决的生死状执行器 (FIX-20260824-004).

语义: 敢死队策略 (Micro Scaler v2 live-fire, magic 专属) 累计**已实现** PnL
击穿 max_drawdown_usd (生死状, 默认 5000 美分 = $50) → 写熔断 flag,
引擎每 cycle 检查 flag 即停止所有敢死队派发 (fail-closed). 熔断为终态,
需人工裁决解除 (读取 flag 后决定删 flag / 改配置).

事件溯源: PnL 从 live_trade_journal.jsonl 实时重算 (magic 过滤 + action=close
+ pnl 非空), 绝不维护增量状态 → 重启 / 多进程天然一致
(Iron Law: ledger 是唯一真相, 不伪造 projection).

构造性隔离: 本模块仅 import 标准库 — 与 shadow_ops 包 Layer-1 import
denylist (zmq/mt5_bridge_worker/live_order_sender/communication_dispatcher/
execution_queue/live_execution_contract/dispatch_context) 兼容, 无派发能力.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

LIVE_FIRE_FLAG_FILENAME = "live_fire_breaker.flag"
BREAKER_EVENT = "live_fire_breaker"

_MAGIC_KEY = "magic"
_PNL_KEY = "pnl"
_ACTION_KEY = "action"
_CLOSE_ACTION = "close"


def live_fire_flag_path(base_dir: str | Path) -> Path:
    """熔断 flag 物理位置: data/shadow_ops/live_fire_breaker.flag."""
    return Path(base_dir) / "shadow_ops" / LIVE_FIRE_FLAG_FILENAME


def evaluate_drawdown(
    *,
    journal_path: str | Path,
    magic: int,
    max_drawdown_usd: float,
) -> dict[str, Any]:
    """从 journal 重算敢死队 magic 的累计已实现 PnL, 判定是否击穿生死状.

    journal 是 append-only ledger (事件溯源 SSOT), 逐行解析:
      action == "close" 且 magic == 目标 magic 且 pnl 非空 → 计入已实现 PnL.

    Returns:
        {
          "realized_pnl_usd": float,   # 累计已实现净 PnL (USD)
          "n_closed": int,             # 已计入的平仓单数
          "drawdown_usd": float,       # max(0, -realized_pnl) — 负向回撤
          "max_drawdown_usd": float,   # 生死状额度
          "breached": bool,            # realized_pnl <= -max_drawdown_usd
        }
    """
    path = Path(journal_path)
    realized = 0.0
    n_closed = 0
    if path.exists():
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # 坏行跳过 — 审计只读, 不阻断
                    if not isinstance(rec, dict):
                        continue
                    if rec.get(_ACTION_KEY) != _CLOSE_ACTION:
                        continue
                    if int(rec.get(_MAGIC_KEY, 0) or 0) != int(magic):
                        continue
                    pnl = rec.get(_PNL_KEY)
                    if pnl is None:
                        continue  # unknown_close / pending 确认 — 不计入已实现
                    try:
                        realized += float(pnl)
                        n_closed += 1
                    except (TypeError, ValueError):
                        continue
        except OSError:
            # journal 读失败 → 保守: 视为未击穿 (fail-open 语义, 熔断器自身
            # 故障不阻断敢死队开单 — 但下一 cycle 会重试, 且 watchdog 可审计)
            return {
                "realized_pnl_usd": 0.0,
                "n_closed": 0,
                "drawdown_usd": 0.0,
                "max_drawdown_usd": float(max_drawdown_usd),
                "breached": False,
                "error": "journal_read_failed",
            }
    drawdown = max(0.0, -realized)
    breached = realized <= -float(max_drawdown_usd)
    return {
        "realized_pnl_usd": round(realized, 6),
        "n_closed": n_closed,
        "drawdown_usd": round(drawdown, 6),
        "max_drawdown_usd": float(max_drawdown_usd),
        "breached": bool(breached),
    }


def is_breaker_open(base_dir: str | Path) -> bool:
    """熔断 flag 存在即 OPEN (fail-closed: 文件存在 = 敢死队停止派发)."""
    return live_fire_flag_path(base_dir).exists()


def write_breaker_flag(
    *,
    base_dir: str | Path,
    net_pnl_usd: float,
    n_closed: int,
    max_drawdown_usd: float,
    detail: str | None = None,
) -> Path:
    """写入熔断 flag (JSON, 含时间戳/原因/审计字段). 幂等: 已存在则覆盖保留首次时间.

    熔断 = 终态. 引擎每 cycle 读到此 flag 即 fail-closed 停止敢死队派发.
    人工解除: 删除 flag + 确认生死状参数后重启.
    """
    flag = live_fire_flag_path(base_dir)
    flag.parent.mkdir(parents=True, exist_ok=True)
    now_iso = datetime.now(UTC).replace(tzinfo=None).isoformat() + "Z"
    if flag.exists():
        # 幂等: 保留首次熔断时间, 仅追加最后触发时间
        try:
            prior = json.loads(flag.read_text(encoding="utf-8"))
            first_at = prior.get("breaker_at_utc")
        except (OSError, ValueError, json.JSONDecodeError):
            first_at = now_iso
    else:
        first_at = now_iso
    payload = {
        "event": BREAKER_EVENT,
        "breaker_at_utc": first_at,
        "last_evaluated_utc": now_iso,
        "net_pnl_usd": round(float(net_pnl_usd), 6),
        "n_closed": int(n_closed),
        "max_drawdown_usd": float(max_drawdown_usd),
        "state": "OPEN",
        "detail": detail or "live_fire max drawdown breached — 敢死队派发停止 (fail-closed)",
    }
    flag.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return flag
