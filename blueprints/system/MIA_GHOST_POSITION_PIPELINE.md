# MIA/幽灵仓位对账管道 — 架构白皮书

> **文档类型**: 架构知识资产 (Architectural Knowledge Asset)  
> **创建日期**: 2026-06-15  
> **Docket ID**: DQAF-20260615-006 — 全链路审计  
> **状态**: 稳定运行  
> **关联模块**: `core/runtime/live_cycle.py`, `core/runtime/reconciliation.py`, `core/runtime/position_close_adapter.py`

---

## 概述

### 什么是 MIA

MIA = **Missing In Action**。当 live 系统内部追踪了一个持仓（`known_open_tickets`），但下一轮去 MT5 `positions_get()` 查询时，该 ticket 已经不在终端持仓列表里了——仓位在两次对账周期间被 SL/TP/手动平仓关闭了。

### 为什么 MIA 管道是"物理免疫系统"

在基于 MT5 这种异步、高延迟终端的量化交易系统中，"幽灵仓位（Ghost Positions）"和"账本状态撕裂（Ledger State Tearing）"是摧毁策略胜率、导致实盘爆仓的两大隐形元凶：

| 威胁 | 症状 | 无 MIA 管道的后果 |
|------|------|-------------------|
| **幽灵仓位** | ActivePositionManager 持有已平仓 ticket | 新开仓被永久阻塞，策略静默停摆 |
| **账本撕裂** | journal 有 open 事件无 close 事件 | PnL 永远无法结算，reentry guard 永久封禁 |
| **PnL 黑洞** | 平仓价格缺失 | 盈亏记录丢失 → 大脑收到虚假胜率信号 |
| **重启 stale** | 停机期间平仓的仓位重启后不可见 | 误读远古 mid price → 重启后立即误开仓 |

---

## 管道 6 阶段全链路

```
┌──────────┐   ┌──────────┐   ┌──────────┐   ┌────────┐   ┌──────────┐   ┌──────────┐
│ 1. 检测  │ → │ 2. 构建  │ → │ 3. 富化  │ → │4. 入队 │ → │ 5. 刷盘  │ → │6. 启动   │
│ Detection│   │ Build    │   │ Enrich   │   │ Queue  │   │ Flush    │   │ 对账     │
└──────────┘   └──────────┘   └──────────┘   └────────┘   └──────────┘   └──────────┘
 管理阶段         保守估算        deal 还原      内存队列       五路写出      进程重启
 每轮巡检         假设 SL 命中    3×重试×1s     立即清理       三层去重      停机补偿
```

---

### 阶段 1: 检测 (Detection)

**位置**: `core/runtime/live_cycle.py` — `_execute_management_phase()` ~line 670

```
管理阶段每轮遍历 known_open_tickets:
  for ticket in known_open_tickets:
      pos = position_manager.get_position(ticket)
      mt5_pos = mt5_worker.positions_get(ticket=pos.ticket)

      分支:
      ├─ mt5_pos 有效 → 仓位正常, 跳过, 继续下一个
      ├─ mt5_pos == _MT5_TIMEOUT_SENTINEL → 超时, 跳过 (下轮重试)
      └─ mt5_pos 为空 → MIA! → 进入阶段 2
```

**关键设计决策**: 超时不判 MIA。网络抖动 ≠ 仓位消失。超时返回 sentinel 值，下个 cycle 重新查询。这防止了 MT5 IPC 瞬时故障导致误关仓。

---

### 阶段 2: 构建 (Build)

**位置**: `_build_mia_close_entry()` — line 2047

```
输入: pos (ActivePosition), known_entry (dict)
输出: mia_entry (dict — 符合 journal close schema)

构建逻辑:
  close_price = current_sl          ← 保守假设: SL 触发 (最坏情况)
  volume      = pos.volume          ← 优先引擎侧, 回退到 known_entry
                → known_entry.volume
                → known_entry.effective_volume_hint
                → 0 (允许为 0, 供阶段 3 deal 恢复)
  entry_price = pos.entry_price     ← 引擎侧优先
                → known_entry.entry_price
  side        = pos.side → known_entry.side
  PnL         = (close_price - entry_price) × volume  ← 初步估算

  FIX-20260610-004: volume=0 时仍保留数值, 不跳过
  (之前 truthiness `if close_volume:` 在 volume=0 时
   falsy → 跳过 PnL → pnl=None → label="breakeven")
```

**关键设计决策**: 保守估算。假设仓位以 SL 价平仓（最差结果），等阶段 3 用 MT5 真实 deal 数据覆写。即使阶段 3 失败了，journal 里也有一个合理的保守记录，而不是空值。

---

### 阶段 3: 富化 (Enrich)

**位置**: `_enrich_mia_from_deals()` — line 2141

```
MT5 history_deals_get(position=ticket) → 3 次重试 × 1s 间隔

因为: MT5 deal 落盘比 position 消失滞后 1-3s
不加 retry → 23% MIA PnL 为空 (10/43 BTC, FIX-20260612-004)

deal 优先级:
  1. reason=4 (SL) 或 reason=5 (TP) 的 deal → 最优先
  2. entry=1 (exit deal) → 取最大的 time
  3. 最后一个 deal (兜底)

富化动作:
  mia_entry["detail"]["close_price"] = 真实平仓价
  mia_entry["detail"]["reason"]      = "sl_hit" | "tp_hit" | "unknown_close"
  mia_entry["pnl"]                   = 重算: (close_price - entry_price) × volume
  mia_entry["label"]                 = "win" | "loss" | "breakeven"

Volume 恢复 (FIX-20260610-004):
  如果 close_volume <= 0 → 从 exit deals (entry=1) 累加 volume
  → 覆写 mia_entry["volume"] → 重算 PnL

  之前 truthiness bug: `if close_volume:` 对 0/0.0 为 falsy
  → 跳过 PnL → pnl=None → label="breakeven"
  修复: `if close_volume <= 0.0:` + deal volume 恢复
```

**关键设计决策**: 3 次重试 + 1s 间隔。MT5 的异步特性意味着 deal history API 可能在 position 消失后的 1-3 秒内返回空结果。重试窗口覆盖了这个竞态窗口。

---

### 阶段 4: 入队 (Queue)

**位置**: `_execute_management_phase()` — line 704-712

```
state._pending_mia_closes.append(mia_entry)   ← 入队 (延迟批量刷盘)
pm.clear_position(ticket=pos.ticket)           ← 立即清理 (防止新开仓被阻塞)
state.known_open_tickets.pop(pos.ticket, None) ← 清理追踪
pm.save_state(config.position_state_path)      ← 立即持久化 (不等周期保存)
```

**关键设计决策**: 检测到 MIA 后**立即**从 ActivePositionManager 清除，不等阶段 5 批量刷盘。这是 FIX-20260610-002 的核心修复——之前 `clear_position()` 从未被调用，幽灵仓位卡在 position_manager 里，新开仓被永久阻塞。

---

### 阶段 5: 刷盘 (Flush)

**位置**: `_execute_management_phase()` — line 3764-3940

这是管道最复杂的阶段。在一个周期结束时批量处理 `_pending_mia_closes`。

#### 三层去重

```
Dedup 1 — 内存 Session 级:
  _mia_processed_tickets (set[int])
  来源: FIX-20260610-002 (F2)
  目的: clear_position() 失败 → 同一 ticket 被重复检测 → 跳过

Dedup 2 — Journal 扫描:
  每次刷盘前全量读 journal → 提取已有 close ticket → 跳过
  目的: bridge worker 可能已先一步写入 close entry
  风险: O(N) 全量扫描 — 见 TECH_DEBT-001

Dedup 3 — Bridge 已写检测:
  journal 中已有此 ticket 的 "action": "close" 行 → 跳过
  目的: 防止 JOURNAL_SLA_VIOLATION (重复 close entry)
```

#### 五路写出

```
1. position_close_adapter.record_mia_closes()
   → journal JSONL 写入 close entry
   → FIX-20260611-005 Phase 2: Strangler Fig #12

2. reentry_guard.record_exit(ExitRecord)
   → 防止 unknown_exit 永久封禁
   → 写入 exit_reason/timestamp/price → reentry_states

3. _emit_close_notification() → DingTalk
   → 告警通知 (fire-and-forget, 不阻塞主循环)
   → FIX-20260608-002: 统一通知出口

4. position_manager.clear_position(ticket)
   → 清除幽灵仓位
   → FIX-20260610-002 (F1): 之前从未调用!

5. PnL → budget tracker → execution_state 持久化
   → 写入 _pending_budget_records
   → FIX-20260610-002 (F3): 关闭 PnL 幻觉缺口
```

**数据流顺序**: journal 先写 → reentry guard → DingTalk → clear_position → budget → state save。这个顺序保证了即使中途崩溃，journal 中有记录（幂等可恢复），reentry guard 已更新（不会被封禁）。

---

### 阶段 6: 启动对账 (Startup Reconciliation)

**位置**: `_reconcile_closed_positions()` — line 2028  
**实现**: `core/runtime/reconciliation.py` (Strangler Fig #3, FIX-20260530-062)

```
进程重启后, 在任何交易之前:

reconcile_closed_positions()
  → 比较 known_open_tickets vs MT5 positions_get()
  → 发现停机期间被平掉的仓位:
      写入 close journal entry
      写入 PnL ledger (SignalSettled, DQAF-20260614-005)
      写入 live_labels.jsonl (FIX-20260614-013)

FIX-20260603-074: reconciliation 必须在 bootstrap 之前运行
  原因: bootstrap 读取 _active_open_mids 时, 如果 reconciliation
  还没清理已知平仓 → _active_open_mids 包含已平仓 ticket
  → 跳过最近平仓 → 回退到远古 stale mid price
  → 重启后立即误开仓 ("重启即开仓" bug)
```

---

## 历史修复谱系 (Fix Genealogy)

这条管道经历了 **12+ 轮修复**，每一次都堵死了一个确诊的漏洞：

| Fix ID | 日期 | 修复内容 | 堵死的漏洞 |
|--------|------|---------|-----------|
| FIX-20260525-017 | 05-25 | 启动 reconciliation: 停机期间平仓检测 | 重启后 journal 缺口 |
| FIX-20260525-024 | 05-25 | MIA 入队机制: 不再直接丢弃 | PnL 黑洞 + reentry 封禁 |
| FIX-20260531-006 | 05-31 | symbol 参数化: 不再硬编码 XAUUSDc | BTC MIA 写错 symbol |
| FIX-20260602-058 | 06-02 | entry_price 存储: PnL 不再为 0 | 100% PnL 丢失 (实际 +$83 记录为 $0) |
| FIX-20260603-074 | 06-03 | reconciliation 前置: bootstrap 之前运行 | 重启后 stale_exit 误开仓 |
| FIX-20260604-089 | 06-04 | 吞异常修复: MIA 写入异常不再静默丢弃 | 静默 journal 写入失败 |
| FIX-20260606-131 | 06-06 | reentry guard 前置 (P2.6) | 幽灵信号 |
| FIX-20260608-002 | 06-08 | 统一通知出口: _emit_close_notification() | MIA 关闭无告警 |
| FIX-20260610-002 | 06-10 | 幽灵仓位清理 + session 去重 + budget PnL | 新开仓被永久阻塞 |
| FIX-20260610-004 | 06-10 | volume=0 PnL 恢复 + deal volume 累加 | truthiness bug→PnL=NULL |
| FIX-20260610-006 | 06-10 | trail_advances 计数器 + MIA trail 标签 | trail PnL 盲区 |
| FIX-20260612-004 | 06-12 | 3 次 deal history 重试 + 1s 间隔 | 23% MIA PnL 为空 (BTC 10/43) |
| FIX-20260614-005 | 06-14 | reconciliation 写 SignalSettled PnL | reconciliation PnL 不被计入大脑 |
| DQAF-20260615-006 | 06-15 | `_build_mia_close_entry` symbol 默认值识别 | 架构残留 (当前调用方已传参) |

---

## 已知残余风险 (Residual Risks)

> 以下风险已定性但当前不触发。作为未来清债路线图登记在 FIX_REGISTRY [TECH_DEBT] 中。

### TECH_DEBT-001: `symbol="XAUUSDc"` 幽灵默认值

- **定性**: L3 架构残留 — 与 DQAF-20260615-006 同一模式
- **位置**: `_build_mia_close_entry(symbol="XAUUSDc")` — [live_cycle.py:2048](core/runtime/live_cycle.py#L2048)
- **风险**: 调用方 line 684 目前显式传了 `symbol=config.symbol`，BTC 不受影响。但如果有新调用方忘记传参，BTC MIA entry 会写 `"symbol": "XAUUSDc"`
- **触发条件**: 新增调用方未传 symbol
- **计划**: 下次修改 live_cycle.py 时删除默认值

### TECH_DEBT-002: Journal 全量扫描 O(N) 性能炸弹

- **定性**: L2/L3 性能债
- **位置**: `_execute_management_phase()` Dedup 2 — [live_cycle.py:3792](core/runtime/live_cycle.py#L3792)
- **风险**: 每次 MIA 刷盘都全量读取 `live_trade_journal.jsonl` 做 ticket 去重。系统运行半年后 journal 膨胀到几万行 → 单次扫描 100-500ms → 主循环卡顿 → MIA 超时雪崩
- **触发条件**: journal 行数 > 10,000
- **计划**: 引入 Journal Index — 启动时在内存中维护 `set(closed_tickets)`，O(1) 查询替代 O(N) 扫描

### TECH_DEBT-003: 三层去重的过度设计

- **定性**: 历史补丁堆叠产物 (Patch Accumulation per Iron Law #12)
- **位置**: `_execute_management_phase()` Dedup 1+2+3 — [live_cycle.py:3767-3813](core/runtime/live_cycle.py#L3767)
- **风险**: 三层去重（内存 set + journal 扫描 + bridge 已写检测）说明历史上出过多次重复写入事故。代码极其难以维护，后人不敢动
- **计划**: 下一大版本建立 SSOT 仓位状态机引擎，统一去重入口

---

## 运维指标

| 指标 | 当前状态 | 来源 |
|------|---------|------|
| MIA PnL 空值率 | ~0% | FIX-004 3-retry + deal volume recovery |
| MIA 重复写 journal | ~0% | FIX-002 三层去重 |
| MIA 幽灵仓位残留 | 已消灭 | FIX-002 clear_position |
| 重启后 stale_exit 误开仓 | 已消灭 | FIX-074 reconciliation 前置 |
| MIA journal 与 bridge 竞态 | 已防护 | journal 扫描去重 (Dedup 2) |
| MIA 关闭无告警 | 已消灭 | FIX-002 DingTalk notify |

---

## 关联文档

- [FIX_REGISTRY.md](FIX_REGISTRY.md) — 修复登记
- [runtime_live.md](../modules/runtime_live.md) — 运行时蓝图
- [CROSS_ASSET_CONTAMINATION_AUDIT.md](CROSS_ASSET_CONTAMINATION_AUDIT.md) — XAU/BTC 交叉感染审计
- [ROOT_CAUSE_DIAGNOSIS_PROTOCOL.md](ROOT_CAUSE_DIAGNOSIS_PROTOCOL.md) — 根因诊断协议
