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
      - consumer-side 幂等去重 (FIX-20260826-003/DQAF-20260826-003): 同一
        position_ticket 的双写重复平仓行 (桥接器重试/回调重复) 只计一次 —
        以首条有效 pnl 为准, 防止同一笔已实现盈亏被重复累加 (XAU twin-write
        曾把 -31.10 双记为 -62.20 → 全局假 -76.58 → 假熔断).

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
            # 仅当无匹配 open 时回退 close 自身 magic. consumer-side 幂等去重
            # (FIX-20260826-003): 同一 position_ticket 只计一次平仓盈亏.
            ticket_seen: set[str] = set()
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
                        _pnl = float(pnl)
                    except (TypeError, ValueError):
                        continue
                    _key = str(_ticket) if _ticket is not None else None
                    if _key is not None:
                        if _key in ticket_seen:
                            continue  # 双写重复行 → 幂等跳过
                        ticket_seen.add(_key)
                    realized += _pnl
                    n_closed += 1
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

# ── DQAF-20260826-005 / FIX-20260826-005 (IC 2026-08-26 裁决 Q2) ──
# 敢死队家族 = 一个血槽, 非单 magic. 90601 = XAU Micro Scaler v2 敢死队;
# 90452 = BTC V4_SHORT 破局特区 (btc_expected_r_m15 strategy_line magic).
# 两 magic 同池聚合 → 一荣俱荣一损俱损. 单点扩展点: 新增家族成员在此登记.
LIVE_FIRE_TRACKED_MAGICS: tuple[int, ...] = (90601, 90452)

# ── DQAF-20260826-006 / FIX-20260826-006 (IC 最高阻断令) ────────────────
# 敢死队生死状 $50 硬阈值. 本模块是生死状单点定义处; 拦截侧自探测击穿时
# 用此常量为聚合基准 (与 micro_scaler config 默认 50.0 一致, 不随 yaml 漂移).
# 单点扩展点: 改生死状额度只改此处.
LIVE_FIRE_MAX_DRAWDOWN_USD: float = 50.0


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
    magic: int | None = None,
    magics: tuple[int, ...] | None = None,
    max_drawdown_usd: float,
    base_dirs: list[Path] | tuple[Path, ...] | None = None,
) -> dict[str, Any]:
    """跨树动态汇总敢死队 magic 集累计已实现 PnL (全局共享生死状).

    FIX-20260826-005 (DQAF-20260826-005, IC 2026-08-26 裁决 Q2): 敢死队家族 =
    一个血槽, 非单 magic. 缺省聚合 `LIVE_FIRE_TRACKED_MAGICS` (90601 XAU Micro
    Scaler + 90452 BTC V4_SHORT 特区) 同池 → 一荣俱荣一损俱损. 传 `magic` 单值
    (向后兼容旧调用/审计脚本, 如 magic=90601) 等价于单元素集; 传 `magics` 显式
    覆盖. 单点注册新家族成员 → 只加 LIVE_FIRE_TRACKED_MAGICS.

    每棵树 × 每个 magic 独立 evaluate_drawdown 后求和; 基于 append-only ledger
    实时重算, 多进程对同一全局值天然一致. base_dirs 缺省为全球生死状树集;
    调用方可传入单树池做隔离评估 (如测试/单一进程).

    Returns:
        {
          "realized_pnl_usd": float,   # 全局累计已实现净 PnL (USD)
          "n_closed": int,             # 全局已计入平仓单数
          "drawdown_usd": float,       # max(0, -realized) — 负向回撤
          "max_drawdown_usd": float,   # 生死状额度
          "breached": bool,            # realized <= -max_drawdown_usd
          "per_tree": dict[str, dict], # 各树 {realized_pnl_usd, n_closed, breached, by_magic}
        }
    """
    if magic is not None:
        _magics: tuple[int, ...] = (int(magic),)
    elif magics is not None:
        _magics = tuple(int(m) for m in magics)
    else:
        _magics = LIVE_FIRE_TRACKED_MAGICS
    _dirs = list(base_dirs) if base_dirs is not None else live_fire_tree_base_dirs()
    total = 0.0
    n_closed = 0
    per_tree: dict[str, dict[str, Any]] = {}
    for _d in _dirs:
        _jp = Path(_d) / "live_trade_journal.jsonl"
        _tree_total = 0.0
        _tree_n = 0
        _by_magic: dict[str, Any] = {}
        for _m in _magics:
            _r = evaluate_drawdown(
                journal_path=_jp, magic=_m, max_drawdown_usd=float(max_drawdown_usd)
            )
            _by_magic[str(_m)] = _r
            _tree_total += _r["realized_pnl_usd"]
            _tree_n += _r["n_closed"]
        # 向后兼容: per_tree[dir] 保 realized_pnl_usd/n_closed/breached (单 magic
        # 审计直接读), 另含 by_magic 以明细分 magic 贡献 (多 magic 判读用).
        per_tree[str(Path(_d))] = {
            "realized_pnl_usd": round(_tree_total, 6),
            "n_closed": _tree_n,
            "breached": _tree_total <= -float(max_drawdown_usd),
            "by_magic": _by_magic,
        }
        total += _tree_total
        n_closed += _tree_n
    return {
        "realized_pnl_usd": round(total, 6),
        "n_closed": n_closed,
        "drawdown_usd": round(max(0.0, -total), 6),
        "max_drawdown_usd": float(max_drawdown_usd),
        "breached": total <= -float(max_drawdown_usd),
        "per_tree": per_tree,
    }


def check_vanguard_breaker(execution_zone: str, base_dir: str | Path) -> bool:
    """敢死队特区熔断拦截判定 (IC 2026-08-26 最高阻断令, DQAF-20260826-006).

    **边界控制 (绝不误杀)**: 仅当 `execution_zone == "live_fire_vanguard"` 时强制
    校验 `is_breaker_open`; 非特区 (execution_zone 空/其他值) → 恒 False — 熔断器
    只针对敢死队 (live_fire / vanguard), 严禁拦正常 live 策略.

    **闭环语义 (STOP 半侧)**: 此前敢死队 PnL 只并入生死状**记账侧** (aggregate),
    常规派发 `_evaluate_strategy_lines` 不经 `is_breaker_open` → 击穿仍会下单.
    本函数把熔断真正挂载到枪管: 派发真实订单前判定,
      (1) 生死状 flag 已存在 (熔断已触发) → True (fail-closed);
      (2) flag 未写但全局聚合已击穿 → 自探测 + 幂等双写 flag (保留首次熔断时间),
          再 True — 确保 XAU 静默时 BTC 特区击穿也被拦 (STOP 半侧彻底闭环).
    base_dir 单树隔离: 非全局树 → 仅查自身树 flag/账本 (防测试误读全局生产账本).

    Returns: True = 应物理 Bypass (跳过派发); False = 放行.
    """
    if execution_zone != "live_fire_vanguard":
        return False
    _pool = live_fire_pool_for(base_dir)
    # (1) flag 已存在 (熔断已触发) → 直接停.
    if any(is_breaker_open(_d) for _d in _pool):
        return True
    # (2) 自探测: 聚合未击穿则放行, 击穿则写 flag 后停 (自感知闭环).
    _agg = aggregate_live_fire_drawdown(
        magics=LIVE_FIRE_TRACKED_MAGICS,
        max_drawdown_usd=LIVE_FIRE_MAX_DRAWDOWN_USD,
        base_dirs=_pool,
    )
    if _agg.get("breached"):
        for _d in _pool:
            write_breaker_flag(
                base_dir=_d,
                net_pnl_usd=_agg["realized_pnl_usd"],
                n_closed=_agg["n_closed"],
                max_drawdown_usd=LIVE_FIRE_MAX_DRAWDOWN_USD,
                detail="vanguard interceptor — live_fire global drawdown breached (fail-closed)",
            )
        return True
    return False
