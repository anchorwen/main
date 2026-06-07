# ReB Pattern Index — 修复知识库

> **标准参考**: SRE "Postmortem Culture" (Google SRE Book Chapter 15), ISO 30401:2018 "Knowledge Management Systems"
> **用途**: 记录可复用的 Bug 模式签名，使历史模式可被程序化搜索，防止同类 Bug 重复修复（FIX-022 型问题）。
> **格式约定**: **强制使用三级标题块格式**（禁止 Markdown 表格，模式描述和预防策略文本较长会被水平拉爆不可读）。

## 格式模板

```markdown
### ReB-YYYYMMDD-NNN
- **Pattern Signature**: 简短机器可读标识（如 `hardcoded_feature_list_in_assembler`）
- **描述**: 该模式的本质特征（2-3 句）
- **关联 FIX IDs**: FIX-YYYYMMDD-NNN, ...
- **关联 Docket IDs**: DQAF-YYYYMMDD-NNN, ...
- **预防策略**: 如何从类型系统/架构层面防止复发
- **检测方法**: 自动化检测手段（mypy rule / ruff rule / 专项测试）
```

## 模式索引

---

### ReB-20260606-001
- **Pattern Signature**: `neutral_deadlock_misinterpreted_as_total_flip`
- **描述**: 当多脑策略的群组投票出现 neutral 平票时，调用方将 `current_supporting` 设为空列表 `[]`，导致下游 flip 计算将空集误解为"100% 入场 brain 已翻转"，触发假阳性 brain_flip_extreme 紧急出场。本质是 neutral 状态与 flip 判定之间的语义契约断裂。
- **关联 FIX IDs**: FIX-20260606-137
- **关联 Docket IDs**: DQAF-20260606-002
- **预防策略**: 
  1. 在 `evaluate_brain_exit()` 中添加防御性检查：若 `current_supporting` 为空但 `entry_ids` 非空，应记录 WARNING 而非执行 flip 判定
  2. 类型系统层面：`current_supporting` 参数应有明确的 None vs `[]` 语义区分（None="未计算"，[]="确实无支持 brain"）
- **检测方法**:
  1. 单元测试：模拟双脑 neutral 平票场景，验证 `evaluate_brain_exit()` 不产生 brain_flip
  2. 运行时监控：若 `brain_flip_extreme_100pct` 在 1h 内触发超过 2 次，触发 DQAF 诊断流程

---

### ReB-20260607-003
- **Pattern Signature**: `dispatch_crash_fail_open_orphan_spiral`
- **描述**: 执行队列 (ExecutionQueue) 内部发生未预期异常时，通用 `except Exception` 仅打印日志而不触发 circuit_breaker。系统主循环继续运行，大脑持续开新仓但派发管道已断，持仓沦为孤儿。孤儿收养逻辑缺乏完整 MT5 数据富化，exit watchdog 无法接管管理。本质是 **三层 Fail-Open**: (1) dispatch 内部异常未熔断, (2) 调用方未区分 fatal vs transient 异常, (3) 孤儿收养缺乏强制看门狗接管回调。
- **关联 FIX IDs**: FIX-20260607-140, FIX-20260607-141, FIX-20260607-142
- **关联 Docket IDs**: DQAF-20260607-005
- **预防策略**:
  1. 所有执行层方法必须 Fail-Closed: 内部异常 → 抛出特定 FatalError → 调用方 trip circuit_breaker
  2. 孤儿收养必须从 MT5 读取完整 position 数据 (SL/TP/entry/方向/手数)
  3. circuit_breaker 触发 → 直接市场市价清仓 (绕过大脑和队列)
- **检测方法**:
  1. 预提交 hook 检查: 任何 `except Exception` 在 dispatch 路径中必须包含 circuit_breaker trip 逻辑
  2. 运行时监控: `cycle_error` 后 3 个周期内若无 management_phase 事件 → 触发 DQAF Sev 1 告警

### ReB-20260606-002
- **Pattern Signature**: `bootstrap_silent_fail_to_open`
- **描述**: 重启状态恢复（restart_state bootstrap）中的异常被静默吞噬（`except Exception: return`），导致 `_reentry_states` 保持空字典。下游 reentry guard 的 `check_and_record_entry()` 遇到 `last_exit = None` 时按"首次入场"放行（`return True, "first_entry"`），使所有重入防护被绕过。本质是 **Fail-Open 反模式**：恢复失败时系统不应放行，而应进入保守状态（Fail-Closed）阻塞所有交易直到人工确认。这是 RC-03（state_leak_across_restart）的最致命子类。
- **关联 FIX IDs**: FIX-20260606-138
- **关联 Docket IDs**: DQAF-20260606-003
- **预防策略**:
  1. **严禁空 except 捕获**: 所有 error-handling 路径必须使用结构化日志（WARNING/ERROR 级别）并打印完整 traceback
  2. **引导失败必须 Fail-Closed**: 状态恢复失败时设置 `_bootstrap_degraded` 标志，下游 gate evaluator 检查此标志并阻塞所有交易
  3. **代码审查规则**: CI 中禁止 `except Exception: return` 和 `except Exception: pass` 模式（ruff 自定义规则或 pre-commit grep 检查）
- **检测方法**:
  1. 单元测试：模拟 journal 解析异常，验证 `_bootstrap_degraded = True` 且所有策略被阻塞
  2. 静态检查：grep `except Exception:\s*(return|pass)` 并标记为阻断
  3. 运行时告警：若 `system_online` 后 60s 内出现 `open` 记录，触发 DQAF 诊断流程

---

### ReB-20260606-003
- **Pattern Signature**: `metric_pollution_via_rejected_retries`
- **描述**: Append-only event log（记录所有尝试）被消费者错误地解释为 trade ledger（只记录最终结果）。当 MT5 断连导致 exit_watchdog 的重试在 journal 中产生大量 `ack_status="rejected"` 的重复条目时，告警聚合器无条件求和所有 `action=="close"` 的 `pnl` 字段，将同一仓位的 N 次重试计算为 N 笔独立亏损。本质是 **ontology-violation (RC-10)**：event log 与 trade ledger 是不同本体论范畴，消费者混淆了二者。
- **关联 FIX IDs**: FIX-20260606-138-Phase0, FIX-20260606-138-Phase2
- **关联 Docket IDs**: DQAF-20260606-005
- **预防策略**:
  1. **消费端幂等性聚合**: 告警聚合器必须按 `ack_status IN ("accepted","closed")` 过滤 + 按 `position_ticket` 去重（反向扫描取首条 = 终态）
  2. **Schema 语义标注**: journal 条目应区分 "attempt"（尝试）与 "settlement"（结算），可选 `is_durable: bool` 字段
  3. **跨周期冷却**: 连续被拒 ≥3 次的仓位进入 10 周期冷却池，从源头掐断重试风暴
- **检测方法**:
  1. 告警系统自检：对比 `COUNT(*)` vs `COUNT(DISTINCT position_ticket) WHERE ack_status IN ('accepted','closed')` — 差异 >20% 触发指标污染告警
  2. 单元测试：注入 5 条同仓位 rejected + 1 条 accepted 的 journal → 验证聚合结果仅计 1 笔
  3. 运行时监控：`exit_cooldown_activated` 事件计数，>0 时触发 bridge health 检查

---

### ReB-20260606-004
- **Pattern Signature**: `missing_pnl_in_trade_notification`
- **描述**: Dispatch 返回值契约不包含估算 PnL，导致下游通知服务无法获取盈亏数据。`_net_out_close_dispatch_fn` 内部已计算理论 PnL，但返回的 dict 未携带 → `execution_queue.flush()` 构造 `DispatchResult` 时无 PnL 来源 → `notify_trade(pnl=None)` → 钉钉永远显示 "N/A"。本质是数据契约在调用链中的逐层断裂。
- **关联 FIX IDs**: FIX-20260606-138-Phase3
- **关联 Docket IDs**: DQAF-20260606-006
- **预防策略**:
  1. `DispatchResult` 应作为通用 dispatch 结果承载所有通知所需字段（pnl, volume, price）
  2. 回调函数返回值契约应显式声明可选字段，避免"隐式丢弃"
- **检测方法**: 单元测试：构造带 PnL 的 close dispatch → 验证 DispatchResult.pnl 非空 → 验证 notify_trade 收到 pnl

---

---

### ReB-20260606-005
- **Pattern Signature**: `p_win_statistical_freeze_dead_zone`
- **描述**: 当历史 bug 导致的真实亏损将 rolling WR 压低至盈亏平衡地板附近（如 0.44 vs 0.45）时，Fail-Closed 兜底因触发线太低（0.40）无法介入，而 p_win 闸门硬阻断所有交易。无新交易 → 无新数据 → rolling WR 不更新 → 永久冰封。本质是边界值死锁：p_win 在 0.40 和 breakeven 之间的"死锁带"无逃生机制。
- **关联 FIX IDs**: FIX-20260606-139
- **关联 Docket IDs**: DQAF-20260606-004
- **预防策略**: UCB 弹性地板——当 p_win 落入死锁带（0.40 < p_win < min_p_win）且置信度高时，用置信度推导弹性 p_win 解锁。Kelly 自动将仓位缩减至微仓级别，风险可控。
- **检测方法**: 监控 `p_win_source == "ucb_elastic_floor"` 触发频率——若连续 >10 周期触发，说明弹性地板在持续兜底，需人工检查脑健康。若连续 >50 周期触发，触发 DQAF 诊断。

---

以下模式来自 FIX_REGISTRY.md 中反复出现的 Bug 类型，作为初始化参考：

### PATTERN-PLACEHOLDER-001
- **Pattern Signature**: `hardcoded_feature_dimension_mismatch`
- **描述**: 特征装配点硬编码了特定品种/周期的维度，导致训练-推理特征错位。8+ 历史 FIX 条目（FIX-022, FIX-025, FIX-026, FIX-028, FIX-076, FIX-080, FIX-081, FIX-133）
- **关联 FIX IDs**: FIX-20260525-026, FIX-20260526-028, FIX-20260526-037, FIX-20260528-017, FIX-20260529-028, FIX-20260531-022, FIX-20260601-039
- **关联 Docket IDs**: 待回填
- **预防策略**: 集中式 Schema Registry（`core/features/schemas/registry.py`）作为 SSOT，FeatureAssembler 严格按 Schema 名动态组装，禁止硬编码维度
- **检测方法**: `BrainConfigValidator` 启动时校验训练维度=推理维度；`verify_all_brains.py` 全量脑加载测试

### PATTERN-PLACEHOLDER-002
- **Pattern Signature**: `cross_symbol_parameter_leak`
- **描述**: 一个品种的参数/配置/硬编码路径静默泄漏到另一品种（如 BTC 使用 XAU 的 contract_size / MetaFilter 路径 / MT5 worker symbol_select）
- **关联 FIX IDs**: FIX-20260530-088, FIX-20260531-014, FIX-20260601-031, FIX-20260601-037, FIX-20260601-038
- **关联 Docket IDs**: 待回填
- **预防策略**: `validate_artifacts.py` 跨文件跨品种参数漂移检测；双品种 Golden Master 重放对比
- **检测方法**: `audit_btc_cross_validate.py` 跨品种交叉验证；启动时验证所有 config 路径同时存在于 XAU 和 BTC 数据目录

### PATTERN-PLACEHOLDER-003
- **Pattern Signature**: `state_leak_across_restart`
- **描述**: 系统重启后内存状态（冷却/预算/跟踪器）被重置为默认值而非从持久化存储恢复，导致"重启即开单"的反复出现
- **关联 FIX IDs**: FIX-20260602-050, FIX-20260603-072, FIX-20260603-073, FIX-20260603-074, FIX-20260604-077
- **关联 Docket IDs**: DQAF-20260606-003
- **预防策略**: `execution_state.json` 作为 SSOT 持久化所有门禁状态，启动时强制水合（hydration），不可跳过
- **检测方法**: `state_hydration_test.py` 启动水合完整性检查；`reentry_guard.py` TTL 持久化验证

### ReB-20260607-007
- **Pattern Signature**: `signal_wiring_unconsumed_computed_output`
- **描述**: 信号源已通过 O(1) 算法计算完成，包含在下游函数的返回 dict 中，但决策层从未消费。表现为：数据存在（regime_gate_result["m5_hurst"]），下游函数（evaluate/exit）的参数签名中缺失对应字段。本质是数据路径的最后一公里未接通——信号发射器与信号消费器之间的 glue code 缺失。
- **关联 FIX IDs**: FIX-20260607-143
- **关联 Docket IDs**: DQAF-20260607-007
- **预防策略**: 对任何新增的 RegimeGate 特征字段，在 classify() 返回 dict 中添加后，应同步检查两个消费点：(1) evaluate() 入口是否需要该信号，(2) exit management 是否需要。可选: 在架构审计 checklist 中增加"信号消费审计"专项。
- **检测方法**: 用 grep 搜索 `regime_gate_result.get("` 找出所有被提取的字段，对比下游函数签名中被实际使用的字段。gap = extracted - consumed。自动化脚本 `check_unconsumed_regime_signals.py` 考虑加入 pre-commit。
