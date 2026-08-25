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
_OPEN_ACTION = "open"
_TICKET_KEY = "position_ticket"


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

    journal 是 append-only ledger (事件溯源 SSOT). 归集规则 (FIX-20260826-001,
    IC Sev-1 热修复 · Magic Nullification):
      - MT5 平仓 (Deal IN/OUT) 的 close 记录 top-level magic 可能为 0/None,
        但 position 的真实 magic 永远挂在它的 OPEN 记录上. 因此以
        position_ticket 反查 open 记录继承真实 magic 作为唯一权威来源;
        仅当无匹配 open 时回退 close 自身 magic (兼容旧构造/外部 close).
      - action == "close" 且 (继承/回退) magic == 目标 magic 且 pnl 非空
        → 计入已实现 PnL.

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
            # ── PASS 1: 索引 OPEN 记录 → position_ticket → 真实 magic.
            # append-only ledger 中 open 恒早于同 position 的 close.
            open_magic: dict[str, int] = {}
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
                    if rec.get(_ACTION_KEY) != _OPEN_ACTION:
                        continue
                    _ticket = rec.get(_TICKET_KEY)
                    if _ticket is None:
                        continue  # 无 position_ticket → 反查能力缺失, 忽略
                    open_magic[str(_ticket)] = int(rec.get(_MAGIC_KEY, 0) or 0)

            # ── PASS 2: 平仓归集. 真实 magic 优先取 open 继承 (position_ticket),
            # 仅当无匹配 open 时回退 close 自身 magic.
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
                    _ticket = rec.get(_TICKET_KEY)
                    _eff_magic = open_magic.get(str(_ticket)) if _ticket is not None else None
                    if _eff_magic is None:
                        _eff_magic = int(rec.get(_MAGIC_KEY, 0) or 0)
                    if _eff_magic != int(magic):
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


# ────────────────────────────────────────────────────────────────────────
# 全局生死状 (IC 2026-08-26 Sev-1 裁决: 敢死队 = 一个计划, 非每品种各 $50)
#
# 投委会定性: XAU 的 -$31.10 与 BTC 的 -$15.96 必须并入同一个 $50 血槽,
# 不是"每个品种送 $50 去亏". 因此熔断评估是跨树全局共享的.
# 单点定义: 涉及"哪些树共享一个生死状", 只改此处 (Decoupling/Iterability).
# ────────────────────────────────────────────────────────────────────────
LIVE_FIRE_BASE_DIRS: tuple[str, ...] = ("data", "data_btc")


def live_fire_tree_base_dirs() -> list[Path]:
    """全局生死状覆盖的树集 (XAU=data/, BTC=data_btc/) — 单点扩展点."""
    return [Path(d) for d in LIVE_FIRE_BASE_DIRS]


def live_fire_pool_for(base_dir: str | Path) -> list[Path]:
    """当前 cycle 所属的生死状池.

    全局共享语义: config.base_dir 属于全局树集 → 返回完整的跨树池 (XAU+BTC
    共享一个 $50); 反之 (隔离/测试用临时目录) → 仅返回自身单树池, 避免测试
    被全局树旗误伤 (isolation 保障).
    """
    _bd = Path(base_dir)
    _resolved = _bd.resolve()
    if any(_resolved == _d.resolve() for _d in live_fire_tree_base_dirs()):
        return live_fire_tree_base_dirs()
    return [_bd]


def aggregate_live_fire_drawdown(
    *,
    magic: int,
    max_drawdown_usd: float,
    base_dirs: list[Path] | tuple[Path, ...] | None = None,
) -> dict[str, Any]:
    """跨树动态汇总敢死队 magic 的累计已实现 PnL (全局共享生死状).

    每棵树独立 evaluate_drawdown 后求和; 基于 append-only ledger 实时重算,
    多进程对同一全局值天然一致 (Iron Law: ledger 是唯一真相). base_dirs 缺省
    为全球生死状树集; 调用方可传入单树池做隔离评估 (如测试/单一进程).

    Returns:
        {
          "realized_pnl_usd": float,   # 全局累计已实现净 PnL (USD)
          "n_closed": int,             # 全局已计入平仓单数
          "drawdown_usd": float,       # max(0, -realized) — 负向回撤
          "max_drawdown_usd": float,   # 生死状额度
          "breached": bool,            # realized <= -max_drawdown_usd
          "per_tree": dict[str, dict], # 各树明细 (仅诊断用)
        }
    """
    _dirs = list(base_dirs) if base_dirs is not None else live_fire_tree_base_dirs()
    total = 0.0
    n_closed = 0
    per_tree: dict[str, dict[str, Any]] = {}
    for _d in _dirs:
        _jp = Path(_d) / "live_trade_journal.jsonl"
        _r = evaluate_drawdown(
            journal_path=_jp, magic=magic, max_drawdown_usd=float(max_drawdown_usd)
        )
        per_tree[str(Path(_d))] = _r
        total += _r["realized_pnl_usd"]
        n_closed += _r["n_closed"]
    return {
        "realized_pnl_usd": round(total, 6),
        "n_closed": n_closed,
        "drawdown_usd": round(max(0.0, -total), 6),
        "max_drawdown_usd": float(max_drawdown_usd),
        "breached": total <= -float(max_drawdown_usd),
        "per_tree": per_tree,
    }
