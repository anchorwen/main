# cycle_error 根因调查报告

**日期**: 2026-05-27
**错误**: `Python int too large to convert to C long`
**严重程度**: P1 — 每周期阻断，但系统自动恢复继续运行

---

## 1. 现象描述

### 1.1 错误频率

| 运行批次 | 日志文件 | cycle_error 次数 |
|----------|----------|------------------|
| 旧进程 (08:12 UTC 启动) | intent_20260527T081259Z.log | **0** |
| 第一次重启 (11:17 UTC) | intent_20260527T111758Z.log | 1 |
| 第二次重启 (11:19 UTC) | intent_20260527T111920Z.log | 3 |
| 第三次重启 (11:27 UTC) | intent_20260527T112724Z.log | 3 |

### 1.2 时序特征

```
cycle_start  →  11:27:26
cycle_error  →  11:27:28   (间隔 2s)
cycle_start  →  11:29:56
cycle_error  →  11:29:58   (间隔 2s)
cycle_start  →  11:34:56
cycle_error  →  11:34:57   (间隔 1s)
```

**每周期必定发生，在 cycle_start 后 1-3 秒内**。

### 1.3 错误含义

`OverflowError: Python int too large to convert to C long` 是 CPython C API 的 `PyLong_AsLong()` 在 Python int 超过 C `long` 最大值时抛出的异常。

Windows 平台上 MT5 是 32 位应用，C `long` 为 32 位有符号整数，最大值为 **2,147,483,647**。

---

## 2. Iron Law 流程执行

### Phase 1: 蓝图查询 ✅
- `blueprints/modules/runtime_live.md` — 无相关历史
- `blueprints/modules/features_service.md` — 无相关历史
- `blueprints/system/FIX_REGISTRY.md` — 无匹配记录
- `blueprints/system/FIX_REGISTRY_2026.md` — 无匹配记录
- **结论**: 此为全新错误类型，代码库无前例

### Phase 2: 实盘数据深潜 ✅
- 已读取 5 个 intent 日志文件
- 已读取 gate_audit / brain_votes 日志
- 确认 OFI 门禁无触发，信号流正常
- **关键发现**: 旧进程(commit 80597c1) 0 次错误，新进程每次重启必现

### Phase 3: 依赖分析 ✅
- `runtime-live`: 22 个依赖模块，1 个被依赖方
- `features-service`: 2 个依赖，4 个被依赖方
- `execution-orders`: 8 个依赖，3 个被依赖方

---

## 3. 根因分析

### 3.1 变更范围 (80597c1 → HEAD)

```
d5a7d87 feat(multi-module): [FIX-20260527-007] [FIX-20260527-008] P0双战役
036206d fix(execution): [FIX-20260527-006] COLD phase deadlock
b9f1d25 fix(multi-module): [FIX-20260527-004] [FIX-20260527-005] regime gate fusion + cold explore
faba0a3 fix(multi-module): [FIX-20260526-037..043] Full Pipeline Rebuild
8576635 feat(execution): [FIX-20260525-009] MT5 single-threaded worker  ← 关键嫌疑
ac0f32c fix(multi-module): [FIX-20260524-042..046] Tier 1-4 full-stack audit
... (更多 mypy/类型修复提交)
```

### 3.2 代码追踪结果

已对 `live_cycle.py` 中所有 `mt5_worker.*` 调用进行审计：

| 行号 | 调用 | try/except |
|------|------|-----------|
| 4341 | `_mid_and_prices()` → `symbol_info_tick()` | ✅ 包裹 |
| 4357 | `copy_rates_from_pos()` | ✅ 包裹 |
| 4641 | `_position_count()` → `positions_get()` | ✅ 包裹 |
| 4636 | `reconnect()` | ✅ 包裹 |
| 2212 | `positions_get(symbol=...)` | ✅ 包裹 |
| 2236/2254 | `history_deals_get(position=...)` | ✅ 包裹 |
| 648 | `positions_get(ticket=...)` | ✅ 包裹 |
| 873 | `positions_get(ticket=...)` | ✅ 包裹 |
| 4866 | `compute_all()` | ❌ **未包裹** |

### 3.3 最可能根因假设

**假设 A: MT5 Ticket Number 溢出 (概率: 中)**

MT5 仓位/订单 Ticket 号通常为 8-10 位数字（如 `5042181234`），超过 `LONG_MAX` (2,147,483,647)。当 `positions_get(ticket=5042181234)` 通过 Worker 线程调用 MT5 C++ API 时，C 代码的 `PyLong_AsLong()` 触发溢出。

但是：所有 `positions_get(ticket=...)` 调用点均在 try/except 中，Worker 的 `_run()` 方法也捕获所有异常并通过 Future 传播。理论上异常应该被正确处理。

**假设 B: 非 MT5 来源 (概率: 中高)**

错误发生在 `cycle_start` 后 ~2 秒。此时序匹配 `compute_all()` (line 4866) 的执行窗口，该调用**未被 try/except 包裹**。虽然内部方法 (`_fetch_m5_rates`, `_compute_tick_features`) 有内部异常处理，但如果 numpy C 扩展或 tick 数据处理中产生大整数操作，可能触发溢出。

**假设 C: Worker 线程异常传播间隙 (概率: 低)**

Worker `_run()` 的 `except Exception` 捕获所有异常，但 `future.set_exception()` → `future.result()` 的重新抛出链中，如果有代码路径未正确处理 OverflowError (它是 `ArithmeticError` 子类而非普通 `Exception`)，可能发生泄漏。

---

## 4. 已执行的修复

### 增强错误日志 (已完成)

`scripts/live_intent_loop.py` 的 cycle_error 处理器现在输出：
```json
{
  "event": "cycle_error",
  "time": "...",
  "error": "Python int too large to convert to C long",
  "error_type": "OverflowError",
  "traceback": "Traceback (most recent call last):\n  File ..."
}
```

**下次重启后，traceback 将精确指出溢出位置。**

---

## 5. 待定修复方案

| 方案 | 适用场景 | 改动 |
|------|---------|------|
| A: 在溢出位置加 try/except | traceback 指向已知调用 | 最小改动，1-3 行 |
| B: Worker 层统一防御 | 多处 ticket 溢出 | `mt5_worker.py` `_run()` 中对已知溢出命令做 int→str 转换 |
| C: ticket 参数类型转换 | `positions_get` / `history_deals_get` 传大 ticket | 包装方法中 `int(ticket)` 或预留 try/except |

**推荐**: 先等 traceback 确认位置，再选择最小改动方案。

---

## 6. 风险评估

| 风险项 | 评估 |
|--------|------|
| 系统稳定性 | 低风险 — 异常被外层 catch，系统继续运行 |
| 交易影响 | 中风险 — 若在关键路径(如开单前)触发，可能跳过该周期 |
| 数据完整性 | 低风险 — 异常不影响已持久化数据 |
| 修复风险 | 低 — traceback 确认后改动范围明确 |
