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

### ReB-20260615-012
- **Pattern Signature**: `ORPHAN_ENTRY_ALERT_POLLUTION`
- **描述**: 启动管道生成的合成 orphan close 条目 (label=`auto_orphan_*`, pnl=0, position_ticket=None) 涌入告警上下文的滚动窗口计算。由于无 ticket 绕过去重、pnl=0 计为亏损，真实胜率被稀释至灾难级 (0.91%)。修复: 告警上下文构建器中按 label 过滤 `auto_orphan_` 前缀——纯展示层修复，0 行动及实盘逻辑。
- **关联 FIX IDs**: FIX-20260615-012
- **关联 Docket IDs**: DQAF-20260615-012
- **预防策略**:
  1. 任何向 journal 写入 synthetic 条目的函数必须使用可识别的 label 前缀 (如 `auto_orphan_`)
  2. 告警/统计模块在遍历 journal 时应显式定义包含/排除的 label 集合
  3. CI 检查: 新 synthetic label 出现时自动注册到排除列表
- **检测方法**: `grep auto_orphan_ data/live_trade_journal.jsonl | wc -l` > 100 → 触发本 Pattern

### ReB-20260615-011
- **Pattern Signature**: `ARCHIVED_BRAIN_ALERT_POLLUTION`
- **描述**: 退役/归档大脑的历史 counterfactual PnL 数据永久占据告警"最差大脑"评比位置。因为 `get_all_metrics()` 返回所有大脑（含已禁用/归档），而累积 PnL 随历史长度单调增长 → 退役大脑(最长的历史、最极端的累积值)永远"赢"过活跃大脑。告警面板对活跃大脑的实时退化完全失聪——这是"幸存者偏差"的逆向版本：尸体统治排行榜。
- **关联 FIX IDs**: FIX-20260615-011
- **关联 Docket IDs**: DQAF-20260615-011
- **预防策略**:
  1. 任何跨大脑排名/评比必须先过滤治理活性状态 → 仅评估 operational (非 terminal) 大脑
  2. `get_all_metrics()` 应接受可选的 `active_brain_ids: set[str] | None` 参数
  3. 每次大脑退役时，CI 检查是否有告警/排行榜仍然引用退役大脑
- **检测方法**: 对比 `governance_state.json` 活跃大脑列表与告警"最差大脑"输出 → 出现非活跃大脑 → 触发本 Pattern

### ReB-20260608-001
- **Pattern Signature**: `CIRCUIT_BREAKER_RESET_ASYM`
- **描述**: 熔断器有 N 个触发路径（bridge_silence / cycle_stall×3 / ExecutionQueueFatalError / staleness），但自愈逻辑仅覆盖其中一部分（只检查 `consecutive_degraded_cycles > 0`）。未被自愈逻辑覆盖的触发路径导致熔断器永久卡死。本质是状态机转换表不完备——触发边与自愈边不是 N:N 映射。
- **关联 FIX IDs**: FIX-20260608-003
- **关联 Docket IDs**: DQAF-20260608-001
- **预防策略**: 
  1. 熔断器必须有统一的状态转换表文档（触发边 × 自愈边矩阵），代码审查时对照检查
  2. 自愈逻辑应基于超时冷却（cooldown）而非依赖特定计数器——冷却制是通用自愈路径，覆盖所有触发源
  3. 每个 `circuit_breaker_tripped = True` 赋值点必须同步记录 `tripped_at` 时间戳
- **检测方法**: 搜索 `_circuit_breaker_tripped = True` 的所有赋值点 → 逐一检查是否存在对应的自愈路径 → 缺失则告警

### ReB-20260608-002
- **Pattern Signature**: `ORPHAN_SUBSYSTEM_DETECTION`
- **描述**: 子系统代码完整存在（core/alpha/, MetaFilterGate），状态文件存在但永远处于初始/空值。根因是子系统从未被主循环接线（Alpha）或接线因路径断裂静默失败（MetaFilter）。表面看状态文件"正常"（schema 正确、无损坏），但数据量为零暴露了"未接线"的事实。
- **关联 FIX IDs**: FIX-20260608-003
- **关联 Docket IDs**: DQAF-20260608-001
- **预防策略**:
  1. 每个子系统在模块蓝图中标记为 {wired | standalone | deferred} 三态
  2. 每周定时扫描：状态文件大小 < 阈值 → 告警
  3. 新子系统集成必须在 live_cycle.py 中有显式调用点，CI 检查调用链完整性
- **检测方法**: `python scripts/audit_data_health.py` 已包含状态文件 size=0 检测 → 扩展为检测初始值模式（如 `alpha_count: 0`, `pred_history: []`）

### ReB-20260608-003
- **Pattern Signature**: `WEAKLY_TYPED_DICT_KEY_MISMATCH`
- **描述**: 使用弱类型字典 `.get(key, fallback)` 时，上游返回的字典不包含预期的 key，导致代码静默回退到错误的 fallback 值。本质是 Dict[str, Any] 类型在调用处和返回处之间的契约断裂——没有编译期检查保证键名一致。
- **关联 FIX IDs**: FIX-20260608-003
- **关联 Docket IDs**: DQAF-20260608-001
- **预防策略**:
  1. 状态描述方法（如 `describe()`）应返回 dataclass 而非 `Dict[str, Any]`
  2. 如果必须用 dict，调用处应显式检查期望的 key 是否存在，不存在时记录 WARNING
  3. 字段名应自文档化——`updated_utc` 应被命名为 `updated_utc_iso` 以明确其格式
- **检测方法**: mypy 的 `TypedDict` 可以捕获键名拼写错误 → 将状态文件 schema 声明为 TypedDict 而非 plain dict

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

### ReB-20260608-003
- **Pattern Signature**: `MISSING_NOTIFY_IN_MANAGED_CLOSE`
- **描述**: 受管平仓的统一入口函数 (`dispatch_managed_close`) 完成了所有业务逻辑（重入守卫、Budget、SL 追踪、仓位清理）但遗漏了 横切关注点——钉钉通知。每个新的退出路径 (meta_exit/SL/TP/hesitation 等) 都通过此函数, 却全部静默。本质是"事件总线缺失综合征" (Missing Event Bus Syndrome): 通信 (通知) 通过手动调用耦合到每个 action site, 而非通过发布/订阅机制自动覆盖。任何新增退出路径都可能遗漏相同关注点。
- **关联 FIX IDs**: FIX-20260608-005, FIX-20260608-002 (MIA 路径的先发修复, 同根同源)
- **关联 Docket IDs**: DQAF-20260608-002
- **预防策略**:
  1. **架构北极星**: Event Bus (Pub-Sub) 模式 — 每笔平仓成功后发布 `TRADE_CLOSED_EVENT`, 钉钉通知器订阅该事件。底层订单模块无需知道通知的存在。
  2. **当前务实的闸门**: `dispatch_managed_close()` 现在在函数尾部 (所有业务逻辑完成后的统一收口处) 调用 `notify_trade`。任何新增的退出路径通过此函数自动获得通知覆盖。
  3. **代码审查清单**: 任何新增"平仓"代码路径 (action="close") 必须包含 `notify_trade` 调用或复用 `dispatch_managed_close`。
- **检测方法**:
  1. `verify.py --quick` 中的蓝图合规检查 — 若 managed_close.py 有修改但 FIX_REGISTRY 无对应条目 → 阻断
  2. 运行时审计: 对比 `live_trade_journal.jsonl` 中的 close action 数量与 `alert_audit.jsonl` 中的 trade_close 数量, 差距 > 0 → 触发 DQAF

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

---

### ReB-20260607-008
- **Pattern Signature**: `stale_data_fail_open_blind_trading`
- **描述**: 数据源（MT5 Bridge）在断连或数据停滞时返回过期 tick 而非抛出异常，数据获取层（market_ingress）未提取并传播 tick 时间戳，决策层（live_cycle）无 staleness 检查，系统在数据管道冰封时继续用过期价格做特征计算、开仓、平仓决策。同时平仓派发路径缺少 pending 状态锁，watchdog batch 被管理循环反复重新触发形成百次级重试拒绝雪崩。本质是**两道 Fail-Open**：(1) 数据层——过期数据被当作实时数据处理，(2) 执行层——已派发的平仓指令可被后续周期无脑重建。
- **关联 FIX IDs**: FIX-20260613-052: resolved placeholder (Staleness Contract + Pending Close Lock)
- **关联 Docket IDs**: DQAF-20260607-006
- **预防策略**:
  1. **Staleness Contract (数据新鲜度契约)**: 所有价格获取函数必须返回时间戳，调用方在每次决策前验证 `time.time() - tick_time < max_age`。连续超限 → circuit_breaker 熔断
  2. **Pending Close Lock (派发锁)**: 对已派发平仓的 ticket，管理循环在 N 周期内禁止重建新的 watchdog batch。锁在 `clear_position()` 时自动释放，超时后自动过期
  3. **价格年龄守卫**: 在平仓派发前验证用于构建订单的价格不超过 60 秒。过期价格必然导致 deviation 拒绝，不如让 MT5 服务端 SL/TP 执行
  4. **Circuit Breaker**: 连续 3 周期 staleness → `circuit_breaker_tripped = True` → 下一周期绕过所有决策层，直接 `mt5_worker.order_send()` 平掉所有持仓
- **检测方法**:
  1. 启动时健康检查：验证最近一次 tick 的年龄 < 30s
  2. 运行时监控：`data_stale` 事件计数 > 5/小时 → DQAF 诊断
  3. `analyze_live_journal.py` 脚本检测：ticket 的 close_attempts > 10 → 告警
  4. 单元测试：模拟 stale tick → 验证 circuit_breaker 触发 + close dispatch 被拒

---

### ReB-20260607-009
- **Pattern Signature**: `frankenstein_metric_independent_min`
- **描述**: 当需要报告"最差策略"的性能指标时，对多个子组件的 PnL 和 WinRate **独立取 min()**，导致最终报告的两个指标可能来自**不同的大脑/策略**。告警描述的"策略"在物理世界中不存在——是多个实体的碎片拼接（缝合怪）。本质是聚合语义错误：`min()` 应该作用于**整个实体**（选择最差的那个），而非作用于**各个字段**（拼接各字段的最差值）。
- **关联 FIX IDs**: FIX-20260613-052: resolved placeholder
- **关联 Docket IDs**: DQAF-20260607-007
- **预防策略**:
  1. 对多实体聚合场景，始终使用 `min(items, key=lambda x: x.field)` 选择单一实体，而非对各字段独立 `min()`
  2. 告警标签必须匹配数据的物理量纲——`per-unit R-multiple` ≠ `USD`
  3. 告警上下文中的"策略级"指标应标注来源实体 ID（如 `worst_brain_id`），使运维可溯源
- **检测方法**:
  1. Code review 规则：搜索 `min(acc, x.field1)` + `min(acc2, x.field2)` 在同一循环中的模式
  2. 告警审计：若 `strategy_pnl` 和 `strategy_win_rate` 在同一告警中出现，验证它们来自同一实体

---

### ReB-20260608-003: `FRAGMENTED_BREAKER_TRIP_PATHS_WITH_STALE_COUNTER_LEAK`

- **发现日期**: 2026-06-08
- **发现环境**: BTCUSDc 实盘 — 熔断器反复触发，系统 110 次/日重启 (May 31)
- **模式描述**: 断路器有多条独立 trip 路径（bridge_silence, cycle_stall, data_staleness, feature_staleness, degraded_wakeup），各自使用独立的连续计数器。Auto-reset 仅重置其中一种计数器（`_consecutive_degraded_cycles`），其余计数器（`_consecutive_stale_cycles`, `_consecutive_stale_features`）在 reset 后仍然存活。若 breaker 由未重置的计数器触发，auto-reset 后的同一 cycle 内立即被重新 trip → 形成"reset → same-cycle re-trip → reset → ..."的死亡螺旋。重启后 breaker 状态从磁盘恢复（`circuit_breaker_tripped=true`），但触发它的 stale counter 丢失（未持久化）→ breaker 无原因存活（"幽灵 breaker"），必须等待 cooldown 超时才能恢复。
- **关联 FIX IDs**: FIX-20260608-009 (root cause), FIX-20260608-006 (circular dependency), FIX-20260608-003 (asymmetric reset), FIX-20260605-120 (persistence), FIX-20260522-019 (initial implementation)
- **关联 Docket IDs**: DQAF-20260608-003
- **预防策略**:
  1. **断路器 trip 路径必须使用统一计数器** — 任何新的 trip 条件必须 increment 同一 `_consecutive_degraded_cycles` 计数器
  2. **Auto-reset 必须清除所有计数器** — 添加新计数器时必须同步更新 reset 逻辑；使用 `_ALL_DEGRADATION_COUNTERS` 元组强制编译时检查
  3. **持久化与恢复必须对称** — `save` 存什么，`restore` 就恢复什么；保存时记录 `trip_reason` 使运维可溯源
  4. **单路径打补丁是反模式** — 如果同一个子系统被修复 ≥ 3 次，必须从架构层面审查整体设计
- **检测方法**:
  1. Code review 规则：搜索 `_circuit_breaker_tripped = True` 的所有赋值点，验证是否存在独立计数器未在 auto-reset 中清除
  2. 运行时监控：`circuit_breaker_trip_reason` 字段值的变化频率——同一 reason 短时间内重复出现 = 死亡螺旋
  3. 启动诊断：若 `circuit_breaker_tripped=true` 但所有 counter=0，判定为"幽灵 breaker"，发出 `ghost_breaker_detected` 告警

---

## ReB-20260609-001: Hesitation Permanent Deadlock

- **发现日期**: 2026-06-09
- **来源 Docket**: DQAF-20260609-001
- **分类**: 边界条件死锁 (Boundary Deadlock) / 代码骨架不完整 (Incomplete Code Skeleton)
- **模式签名**: 重入守卫某退出类别同时缺少 `_MAX_THRESHOLD` 天花板和 TTL 硬解锁，正边际加法 (`exit_confidence + margin`) 产生的阈值超过模型输出范围形成数学死锁。签名关键词: `category=hesitation AND exit_confidence + 0.15 > model_P99 AND no_TTL AND no_MAX_THRESHOLD`.
- **典型症状**:
  1. 某策略线连续数小时至数天零开仓
  2. intent log 中出现大量连续同一 `reentry_blocked` 事件，reason 包含同一退出类别
  3. 被拦截信号的置信度明显正常（非极低值），但始终无法达到阈值
  4. 计算 `exit_confidence + margin` 若超过 0.82 (树模型输出天花板)，即为本模式
- **根因机制**: 多个 FIX 向 reentry guard 添加保护（`_MAX_THRESHOLD` / TTL 硬解锁）时，每次仅针对特定类别施加，遗漏了 hesitation 类别。每次遗漏都是因为"此 FIX 针对 X 类别"的范围限定，没有系统性验证"所有类别是否都需要此保护"。结果: hesitation 在 FIX-117 (ceiling)、FIX-127 (TTL)、FIX-011 (TTL) 三次广谱加固中均被遗漏，成为唯一裸奔的类别。
- **修复模板**:
  1. 对该类别的正边际阈值施加 `_MAX_THRESHOLD` 包裹: `min(max(exit_conf + margin, floor), _MAX_THRESHOLD)`
  2. 添加 TTL 硬解锁: 超时后降级为基础置信度检查 (confidence > 0.50)
  3. 增强 rejection reason 包含阈值数值，便于未来诊断
- **预防措施**:
  1. 编写 `reentry_guard_category_compliance` 测试：验证每一个退出类别同时具备 (a) `_MAX_THRESHOLD` 包裹（若存在正边际加法）(b) TTL 硬解锁（若存在正边际加法 + price confirmation）
  2. Code review 规则：新增/修改退出类别处理时，必须显式说明是否施加了上述两项保护
  3. 架构审计：每季度运行全类别保护扫描，确保无遗漏
- **关联 CCT**: CCT-20260609-001
- **关联 FIX**: FIX-20260609-001

---

### ReB-20260609-001-B: `BREAKEVEN_FLOOR_TRAIL_DEADLOCK`

- **发现日期**: 2026-06-09
- **发现环境**: BTCUSDc 实盘 — trade 3809501680，保本后 SL 锁死 23 根 bar
- **模式描述**: 保本止损触发后，trail_stop_engine 的 Chandelier 公式要求 `highest_high - trail_mult × ATR > entry_price` 才能让 SL 突破保本地板。当 trail_mult 是静态常量（如 regime-given 2.5）且 ATR 较高时，需要的利润缓冲可能超过仓位实际能达到的最高点。此时 `max(candidate, entry_price)` 将 candidate 锁定在 entry_price，`candidate ≤ current_sl + min_step` 返回 None — 数学死锁形成。SL 永远不动，只有 TP 单向收紧。
- **关联 FIX IDs**: FIX-20260609-003
- **关联 Docket IDs**: DQAF-20260609-001
- **预防策略**:
  1. **trail_mult 必须随利润动态衰减** — 水下求生用大乘数（2.5x），水上锁利用小乘数（1.2x）。线性插值连接两者
  2. **所有"地板"逻辑必须配套"突破"机制** — breakeven floor 保护本金，但必须有路径让 trail 在利润积累后突破地板
  3. **静态参数 + 阈值 = 死锁风险** — `static_mult > profit / ATR` 是死锁的充要条件，必须有一个动态衰减变量打破不等式
- **检测方法**:
  1. 运行时监控：`management_phase_diag` 中 `trail_sl_candidate: null` 连续 ≥ 10 bars → 告警
  2. 回测检查：搜索 `breakeven_triggered=true + trail_fired=false` 持续超过半衰期的仓位
  3. 单元测试：验证 R=1.0, 1.5, 2.0, 3.0 时 trail 均有非 null candidate

---

### ReB-20260609-001-B
- **Pattern Signature**: `CAP_OUTPUT_MISMATCH_DEADLOCK` (Cap-Output Mismatch Deadlock)
- **描述**: Reentry guard 的置信度阈值公式（如 `max(exit_confidence + margin, floor)` + `_MAX_THRESHOLD` 天花板）产生的阈值超过目标模型的 P99 输出范围。当模型是 tree-based (XGBoost/LightGBM) 时，天然输出上限约 0.75-0.82，而 `_MAX_THRESHOLD=0.82` 在天花板有保护的情况下仍因 floor/margin 组合产生不可达阈值。BTC 观测：150+ 连续周期封锁（12.5h）。历史先例：FIX-127/130 (brain_flip, floor 0.70→0.65), FIX-117 (新增 `_MAX_THRESHOLD`), FIX-001 (hesitation TTL+ceiling), FIX-010 (hesitation margin+floor)。
- **关联 FIX IDs**: FIX-20260609-001, FIX-20260609-010, FIX-20260606-127, FIX-20260606-130, FIX-20260605-117
- **关联 Docket IDs**: DQAF-20260609-001
- **预防策略**:
  1. 任何涉及 `exit_confidence + X` 边际加法的阈值公式，必须在代码注释中标注目标模型的 P99 输出范围
  2. 新增 reentry 类别时必须附带模型输出分布分析（histogram + P50/P90/P99 percentiles）
  3. CI 中增加 `_MAX_THRESHOLD` 合规检查：任何 exit_category 的阈值公式必须包含 `_MAX_THRESHOLD` 天花板 且 floor 不超过目标模型 P90
- **检测方法**: 搜索 alert_audit 中 `reentry_persistent_block` 连续 ≥ 50 cycles → 触发 `_MAX_THRESHOLD` 审查

### ReB-20260609-001-C
- **Pattern Signature**: `BUDGET_RECONSTRUCTION_AMNESIA` (Budget Reconstruction Amnesia)
- **描述**: 策略对象（含 StrategyBudget）在每个 cycle 被重建（`_build_strategy_lines()`），但持久化状态仅在 cycle 1 恢复（`restore_execution_state()`）。Cycle 2+ 的 budget 计数器恒为零 → 所有累计风控闸门（daily_loss_limit, max_consecutive_losses, intraday_dd, consecutive_degraded）永久失效。本质是对象生命周期管理（recreate-on-every-cycle）与状态生命周期管理（restore-once）之间的契约断裂。关联模式：FIX-20260603-072 引入了 `restore_execution_state()` 但未预见 `_build_strategy_lines()` 会移至循环内（FIX-20260530-070 Strangler Fig #5）。
- **关联 FIX IDs**: FIX-20260609-010, FIX-20260603-072, FIX-20260530-070
- **关联 Docket IDs**: DQAF-20260609-001
- **预防策略**:
  1. 任何 `_build_*` / `_create_*` 在循环内被调用时，必须配套 `_restore_*` / `_hydrate_*` 在同一循环迭代中
  2. CI 中添加 "destructive-rebuild-in-loop" 检测：扫描循环内的 `_build_*` 调用 → 检查是否紧跟 `restore`/`hydrate` 调用 → 缺失则告警
  3. 架构原则：状态对象应在循环外创建一次（构造即持久），而非每 cycle 重建
- **检测方法**:
  1. 启动后（loop_iteration ≥ 3）检查 `execution_state.json` 中 `total_trades_today` 是否 > 0 — 若为 0 但 trade_journal 中当日有记录 → 告警
  2. 运行时断言：每个 cycle 开始时 budget counters ≥ 上一 cycle 结束时 counters（单调不减，除日切外）

---

### ReB-20260609-011
- **Pattern Signature**: `GOVERNANCE_VACUUM_CADET_BRAINS` (治理真空——未成年模型驾驶重型机甲)
- **描述**: 所有活跃大脑均处于 `candidate`（从未证明盈利）状态，无 `live` 大脑。治理状态在整个开单链路中完全是"死数据"——governance_state.json 被 daily_ops 写入但没有任何交易门禁消费它。逻辑倒挂：`probation`（已退化）被罚 vote_weight×0.5，而 `candidate`（未证明）获全票权。结果是 profit_factor=0.72、sharpe=-30 的候选大脑以 0.1 lot 实盘裸奔。
- **关联 FIX IDs**: FIX-20260609-011
- **关联 Docket IDs**: DQAF-20260609-011
- **预防策略**:
  1. governance_state.json 必须被至少一个交易门禁作为 BLOCKING 条件消费——不能仅仅是"记录"
  2. 新增大脑状态时必须在 `live_startup.py` 的 `filter_brains_by_governance()` 中显式处理，不允许 fall-through
  3. daily_ops 中增加 "全 candidate 超时告警"：如果连续 N 天无大脑晋升 live → 触发人工审核
- **检测方法**:
  1. 每个 cycle 检查 governance_state 中 `status=="live"` 的大脑数量 → 0 且 strategy 在开单 → 告警
  2. 搜索 `filter_brains_by_governance` 中未被显式处理的状态 → CI lint 检查

---

### ReB-20260609-012
- **Pattern Signature**: `BTC_SURVIVAL_ALPHA` (BTC 生存策略即 Alpha)
- **描述**: BTC 市场结构不支持传统高盈亏比 Alpha（R:R ≥ 1.0）。跨 4 个时间框架 × 15 组参数的网格搜索证明：所有高 R:R 组合 EV 为负。BTC 的 Alpha 形态是"宽止损 + 紧止盈 + 极高胜率"的生存策略——M15 SL=3.0/TP=2.0 以 EV=+0.456R 位居全场最佳。这不是模型的缺陷，而是 BTC 价格行为物理规律（趋势性强、回调浅）的结构性结果。
- **关联 FIX IDs**: FIX-20260609-012
- **关联 Docket IDs**: DQAF-20260609-012
- **预防策略**:
  1. 任何新资产的大脑训练必须首先执行 SL/TP 网格搜索以确定该资产的正 EV 区域
  2. 不要假设高 R:R = 高 Alpha —— 先在数据上验证该资产是否支持
  3. 训练管线必须包含时间衰减权重 + Walk-Forward Purged CV + 真实摩擦，缺一不可
- **检测方法**:
  1. `python scripts/training/train_btc_swing_v9.py --build-only` 可复现全部网格搜索
  2. CI 中检测 brain config 的 SL/TP 参数是否落入该资产的已知正 EV 区域

---

### ReB-20260610-001
- **Pattern Signature**: `TRAIL_TELEMETRY_BLINDSPOT`
- **描述**: 移动止损(Chandelier Trail)通过 modify_sltp 持续调整 SL 水平，但平仓时的 exit label 永远不包含 'trail' 标签——无论 SL 被 trail 推了多少个 ATR，最终平仓一律标记为 `sl_hit_first` 或 `loss`。这导致 trail 的利润锁定贡献完全不可测量：无法区分"原始 SL 被命中"(trail 未生效) vs "已收紧的 SL 被命中"(trail 保护了部分利润)。整个 trail 子系统的运维只能间接通过 modify_sltp 记录和 snapshot 推测，形同盲飞。
- **关联 FIX IDs**: —
- **关联 Docket IDs**: DQAF-20260610-001
- **预防策略**:
  1. 平仓 dispatch 时比较 final_sl 与 initial_sl——如果不同，label 应为 `trail_sl_hit` 而非 `sl_hit_first`
  2. 在 live_trade_journal 的 exit label 字段增加 trail 相关的子标签（如 `sl_hit_trailed`, `sl_hit_original`）
  3. TrailStopEngine 输出 trail 贡献指标（sl_advance_count, final_sl_delta_from_entry）供遥测
- **检测方法**: 搜索 live_trade_journal 中 `label=="trail"` 的计数 → 应为非零。当前 counter=0。

---

### ReB-20260610-002
- **Pattern Signature**: `MICRO_LIFESPAN_COUNTER_TREND`
- **描述**: 当大脑信号方向与宏观趋势相反时（急跌中生成 LONG 信号做反弹），配合激进防守参数（trail_activation_atr 0.3-0.5, breakeven 激活早），仓位呈现"微型生命周期"——平均持仓 21 分钟（4 根 M5 bar）。价格短暂反弹触发 trail 收紧 → 趋势重力重新压回 → 迅速击穿已收紧的 SL/breakeven → 保本微亏快速出场。这不是系统缺陷，而是逆势交易中防御机制正常工作的表现——系统用高换手率保护了本金，而非被单边碾压。
- **关联 FIX IDs**: —
- **关联 Docket IDs**: DQAF-20260610-001
- **预防策略**:
  1. 趋势隔离门禁(trend isolation gate)应作为第一道防线——逆势信号在 gate 层就应降权或拦截，而非依赖 trail 做后发补救
  2. 当检测到仓位平均持仓时间 < N 个 bar 且全为单一方向时，触发"逆势微仓模式"告警
  3. 大脑训练时应在标签中包含趋势方向信息，使模型学会"顺大势、逆小势"的区别
- **检测方法**: `python scripts/analyze_trail_impact.py` 已包含持仓时间分析。定期运行监控 avg_hold_mins 和方向集中度。

---

### ReB-20260610-003
- **Pattern Signature**: `CONFIG_SYMMETRY_DRIFT`
- **描述**: 双品种部署架构中，对共享大脑的配置修改只应用到单一品种配置文件(live_btc.yaml)，未同步到另一品种(live.yaml)。多见于退役/禁用/参数调整操作。典型场景: 大脑在 commit A 被添加到两个品种的配置中(如 Phase 5b 批量注册)，在 commit B 退役时只更新了主品种配置——因为退役决策基于主品种的实盘表现，次品种的引用被遗忘。
- **关联 FIX IDs**: FIX-20260610-008
- **关联 Docket IDs**: DQAF-20260610-002
- **预防策略**:
  1. `_check_config_consistency()` in verify.py — 静态扫描所有 `live*.yaml`，检测 `status: retired/frozen` 但 `enabled: true` 的大脑
  2. 退役流程标准化: 退役大脑时必须(1)更新脑 JSON(status+vote_weight),(2)在所有引用该脑的配置文件中设 enabled=false,(3)运行 verify.py 确认
  3. 未来: governance_service 自动退役时同步更新所有配置文件引用
- **检测方法**: `python scripts/verify.py --quick` 自动检测并报错。也可手动: `grep -r "BTC_Swing_V5" configs/live*.yaml`

### ReB-20260612-001
- **Pattern Signature**: `SILENT_FALLBACK_ZERO_OBSERVABILITY`
- **描述**: 纯函数在降级路径上返回安全默认值 (0.40)，但不发出任何信号表明降级发生。下游消费方无法区分"真实统计值"与"兜底默认值"，导致系统在降级模式下裸奔而运维无感知。根本原因：返回值设计为裸 float，缺少 quality/source 元数据；fallback 路径无日志。本次实例：`resolve_p_win_from_brains()` 三条静默路径全部返回 0.40。
- **关联 FIX IDs**: FIX-20260612-001
- **关联 Docket IDs**: DQAF-20260612-004
- **预防策略**:
  1. 所有返回统计估计值的函数必须记录降级日志（含降级原因和影响范围）
  2. 调用链透传 `source` + `degraded` 标记至 journal 供事后审计
  3. Iron Law #10: BLE001 替换为 `fail_open_guard()` 确保异常至少被记录
- **检测方法**: `grep -n "return 0\.40\|return 0\.5[0]*$" core/execution/pwin_chain.py` 检查是否仍有未日志化 fallback；`grep "FALLBACK_PATH" data_btc/logs/` 监控降级频率

---

### ReB-20260612-002
- **Pattern Signature**: `PHANTOM_CLOSE_FLOOD`
- **描述**: 退出看门狗每次周期重新评估仓位是否需要平仓——若无 close-in-flight 状态追踪，已发送但未确认的平仓请求会在一段时间后重复发送。每次重试创建新 journal entry（不同 message_id），形成幽灵洪水。典型案例：ticket 3807506009 在 80 分钟内产生 76 条平仓记录（75 rejected + 1 closed）。根因：`PENDING_CLOSE_MAX_CYCLES=3` 太短 + 无 attempt counter 上限。
- **关联 FIX IDs**: FIX-20260612-003
- **关联 Docket IDs**: DQAF-20260612-001
- **预防策略**:
  1. `PositionManager` 追踪 `_close_attempt_count`，超过 `PENDING_CLOSE_FLOOD_THRESHOLD=3` 永久锁定
  2. `PENDING_CLOSE_MAX_CYCLES` 延长至 10（50 分钟）给 MT5 充足处理时间
  3. `clear_position()` 一次性清理 counter + lock
- **检测方法**: `python -c "import json; from collections import Counter; ..."` 统计每 ticket 的 close entry 数 — 超过 5 条触发告警

---

### ReB-20260612-003
- **Pattern Signature**: `TRAIL_LABEL_BLINDSPOT`
- **描述**: 移动止损（Chandelier Trail）通过 247 条 `modify_sltp` 记录持续收紧 SL，但所有 246 条平仓记录中 `label='trail'` 计数为 0。Reconciliation 路径遇到 `close_reason=4 (SL)` 无条件分配 `sl_hit_first`，不检查 `trail_advances`。Bridge worker 按 PnL 符号分配 `loss`/`win`，忽略 `trail_contribution`。仅 MIA enrichment 路径正确分配 `sl_hit_trailed`（FIX-20260610-006 已修）。
- **关联 FIX IDs**: FIX-20260612-003
- **关联 Docket IDs**: DQAF-20260612-001
- **预防策略**:
  1. Reconciliation: `close_reason==4` → 检查 `state.position_manager.get_position(ticket).trail_advances > 0` → `sl_hit_trailed`
  2. Bridge worker: 检查 payload 的 `trail_contribution.trail_advances > 0` → 调整 label
  3. 所有平仓 label 路径统一检查 trail 历史
- **检测方法**: `python -c "..."` 统计 journal 中 label='sl_hit_trailed' 计数 → 应随实盘交易增长

---

### ReB-20260612-004
- **Pattern Signature**: `PNL_BACKFILL_GAP`
- **描述**: 平仓 PnL 在两个独立路径中无法捕获：(a) Bridge worker 使用 dispatch 时的 mid-price 估算 PnL，journal 写入后永不更新实际成交价/利润；(b) MIA 检测调用 `history_deals_get()` 单次无重试——MT5 成交数据延迟 1-3 秒时 PnL 为 null（23% 失败率）。这两个缺口合计导致 17.6% PnL null rate (JOURNAL_PNL_NULL_RATE_HIGH)。
- **关联 FIX IDs**: FIX-20260612-004
- **关联 Docket IDs**: DQAF-20260612-001
- **预防策略**:
  1. Bridge worker: 平仓成功后立即查询 `history_deals_get(position=ticket)` 获取 `deal.price` + `deal.profit` → journal 使用实际成交 PnL
  2. MIA enrichment: `history_deals_get()` 包装 3 次重试 + 1 秒延迟（对齐 PositionCloseAdapter 模式）
  3. `detail.close_price` + `detail.profit` + `detail.fill_volume` 填入 journal 供审计
- **检测方法**: `python scripts/analyze_live_journal.py --data-dir data_btc` → PnL null rate < 5%

---

### ReB-20260612-005
- **Pattern Signature**: `CALIBRATOR_COLD_STALLED`
- **描述**: ConformalCalibrator 的 `cold_started` 标志被 `cold_start_from_journal()` 设为 True 后永不改为 False——即使已积累 51+ 条历史记录（超过 warmup_samples=50）。`total_computations` 计数器因实盘无交易（无 brain proposal → gate filter 不触发 → `compute_threshold()` 从不被调用）保持为 0。两个指标叠加导致 `CONFORMAL_COLD_STALLED` 误报。Calibrator 实际运作正常（有足够历史数据计算 Q10 分位数），只是状态标志不反映真实就绪度。
- **关联 FIX IDs**: FIX-20260612-005
- **关联 Docket IDs**: DQAF-20260612-001
- **预防策略**:
  1. `_save_state()`: `history >= _warmup_samples` → `cold_started = False`
  2. `_load_state()`: 对旧状态文件反向补填过渡（历史 ≥ 50 → 非 cold）
  3. 就绪度判断基于历史计数而非计算计数——历史是 Q10 分位数的实际数据源
- **检测方法**: 检查 `conformal_calibrator_state.json` → `cold_started` 应在 history≥50 后变为 false

---

### ReB-20260612-006
- **Pattern Signature**: `POSITIONAL_FRAGILITY`
- **描述**: 特征字典通过 `list(feature_source.values())` 转换为模型输入数组时，特征顺序依赖 Python dict 插入顺序（Python 3.7+ 稳定但不同代码路径构建顺序可能不同）。若上游字典键顺序错乱或多插特征，模型静默使用错误特征位置——MACD 值被当成 RSI 权重，产生垃圾预测。影响范围：3 个 adapter + 1 个 feedback hook 共 5 个 `.values()` 站点。LightGBM adapter 已通过命名查找修复（FIX-20260516-004），其余站点为遗留回退路径。
- **关联 FIX IDs**: FIX-20260612-002
- **关联 Docket IDs**: DQAF-20260612-001
- **预防策略**:
  1. 所有 adapter 使用 `brain_entry["features"]` 命名投影 → `[feature_source[n] for n in feature_names]`
  2. BrainFactory 加载时验证 `features` 列表 ≡ `.meta.json feature_names`（已有）
  3. XGBoost adapter 48h 影子校验（新旧数组比对 + mismatch 告警）
  4. 禁止在特征组装路径使用 `dict.values()` — ruff 自定义规则检测
- **检测方法**: `grep -rn "\.values()" core/brains/ core/feedback/` → 应为 0 结果（5 站点全部替换后）

## ReB-20260612-007: TRIPLE_BOOKKEEPING_RESIDUAL

- **Docket**: DQAF-20260612-002
- **Pattern**: 退役大脑时在多处独立配置位置留下残留（registry status, vote_weight, live.yaml enabled），后续重新激活时任何一处未同步都会静默阻止大脑投票
- **Signature**: 三处独立配置点中任一为 'retired/disabled/zero' 即可形成合力阻断——无任何一处是 SSOT
- **Detection**: governance 有 live brain 但 voted_brain_ids 中缺失 + disabled_brains_filtered 日志 + strategy.brains 不包含该 brain_id
- **Prevention**: 大脑退役/重激活应通过单一原子操作执行，或至少包含一致性检查（governance live ↔ registry status ↔ yaml enabled ↔ vote_weight）。参考 FIX-20260612-006。

## ReB-20260612-008: GOVERNANCE_BRAIN_SOURCE_MISMATCH

- **Docket**: DQAF-20260612-002
- **Pattern**: 两套大脑状态源（brain registry JSON + governance_state.json）各自独立维护，状态变更未双向同步
- **Signature**: governance 标记 brain 为 live，但 registry 仍为 retired/frozen，strategy_builder 使用 registry 状态过滤→governance 的 live 标记无效
- **Detection**: 检查 governance_state live brains ∩ brain registry entries → 交集为空时告警
- **Prevention**: strategy_builder 过滤时应同时检查 governance_state（如在 governance 中为 live，覆盖 registry retired）。参考 FIX-20260610-001 → FIX-20260612-006 根因链。
