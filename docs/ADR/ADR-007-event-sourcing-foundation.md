# ADR-007: Event Sourcing Foundation for Data Pipeline

**日期**: 2026-06-11  
**状态**: Accepted  
**决策者**: cursor-agent + 人类 IC  
**关联**: FIX-20260611-020, FIX-20260611-021, FIX-20260611-022

---

## 背景

2026-06-11 全面系统审计发现 12 个异常，其中 5 个为 Sev 1。两个问题尤为致命：

1. **BTC 在降级模式下以 SL=0, TP=0 裸奔交易** — 无风控参数进入市场
2. **治理系统基于回测数据做大脑晋升/冻结决策** — `governance_state.performance_metrics` 来自 PnP 账本（反事实回测），而非实盘性能 (`brain_performance.json`)

根因分析揭示六层架构缺陷：并发写冲突、无单一事实来源、无数据出处标签、非原子读写、写入模式碎片化、静默数据丢失（被 `fail_open_guard` 吞噬）。

## 决策

### 1. Fail-Closed SL/TP 硬断言

在 `strategy_evaluator.py` 中添加 Cut 5：任何 `sl<=0` 或 `tp<=0` 的非 shadow 模式交易被物理阻断。此模式延伸自 DQAF-20260607-005 (FIX-140/141/142) 的 Fail-Closed dispatch 模式。

### 2. 治理手动白名单模式

三个治理注入点全部守卫：
- `scheduler_service.py`: `_GOVERNANCE_MANUAL_MODE = True`
- `live_startup.py`: `_GOVERNANCE_SKIP_INJECTION = True`
- `governance_scheduler.py`: `_GOVERNANCE_MANUAL_MODE = True`

在记录污染确认修复之前，所有自动晋升/冻结被禁用。

### 3. Append-Only Event Stream

核心架构变更：用不可变仅追加事件流替代可变 JSON 读-改-写。

**写入**: `EventWriter` (threading.Lock, line-buffered append) → `ledger_events.jsonl`  
**读取**: `project_governance_state()` (纯函数投影, source_filter 物理隔绝实盘/回测/影子)  
**恢复**: `load_from_stream()` (事件重放, 优先于旧 JSON)

每个事件携带 `source` (live/shadow/backtest/migration) 和 `generated_by` 标签。

### 4. 四级渐进降级模型

用渐进降级替代二元断路器：NORMAL(100%) → YELLOW(40%) → ORANGE(15%,禁止新开) → RED(0%,仅平仓)。集成到 `live_cycle` → `strategy_evaluator` 调用链。

### 5. Hypothesis 基于属性的测试

6 个数学不变量覆盖投影引擎：幂等性、PnL 守恒、交易计数、胜率边界、source filter 隔离、checkpoint 一致性。200 示例/测试。发现并修复 2 个 bug（UUID 排序、checkpoint 键不匹配）。

## 后果

**正面**:
- 裸奔交易被物理阻断
- 治理不再被回测数据欺骗
- 事件流提供完整审计线索
- 数据源物理隔离（live vs shadow vs backtest）
- 2829 测试全绿

**负面**:
- `brain_pnl_ledger.json` 仍存在作向后兼容（计划 6/18 退役）
- 旧数据中的记录污染未清理（架构已解决，存量未动）
- 65 个历史 ORPHAN FIX 条目（5/29-6/10 期间 Iron Law #7 执行不严）

## 替代方案

考虑过但未采用：
- **Kafka/消息队列**: 对单机精锐小队过重
- **Iceberg 分支 + WAP**: 基础设施就绪 (`core/data/wap.py`)，但治理已有 FileLock + tmp+replace，增益有限
- **数据网格**: 过度设计——仅 2 个品种、~10 个数据文件

---

*关联 ADR: ADR-001 (Parliament consensus), ADR-006 (live_launcher runtime)*
