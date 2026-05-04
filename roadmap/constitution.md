# 量化构建系统最高宪法

> **不可逾越的系统约束。** 本文档定义系统在任何阶段都绝对不可违反的规则。所有人、代码、自动化流程、AI决策均受此约束。违反即系统崩溃级事故。

---

## 宪法第一条：资本保全优先

**系统在任何情况下保证本金安全优先于盈利。**

1.1 最大总回撤（Maximum Drawdown）不得超过 **硬编码值**（当前设定见 `engine_config.json`）。
1.2 当回撤达到此阈值的80%时，系统强制进入 `SAFE` 模式（仅平仓，不开新仓）。
1.3 当回撤达到此阈值的100%时，系统强制进入 `EMERGENCY` 模式（全部平仓，断开交易网关）。

```
实现: SystemModeState (core/state/stores/system_mode_store.py)
门禁: ModePolicy (core/risk/risk_policies.py)
```

---

## 宪法第二条：模型必须通过沙盒晋升

**任何模型（无论模型类型：ONNX / XGBoost / OU参数 / 树模型 等）不得直接从训练产出进入实盘。**

2.1 新模型必须先在 Shadow Live 环境中观察至少 **3个月**（自然日）。
2.2 观察期内的模型信号必须与实际成交记录对比，记录对账偏差。
2.3 晋升条件（不可绕过）：
  - 信息比率 > 阈值（最小可接受值定义于 `alpha_promotion_gate.json`）
  - 观察期内无单日回撤超过沙盒限制
  - 至少 90% 的交易日有信号产出（非空仓日）
2.4 晋升由 `PromotionGate` 自动评估，**人工无权直接晋升**。

```
实现: core/alpha/promotion_gate.py
沙盒: scripts/live_shadow_intent_producer.py
```

---

## 宪法第三条：信号必须经过风控门禁

**任何信号在到达执行网关前必须经过 RiskEvaluationService。**

3.1 风控管道不可绕过。信号链路为：
```
Brain推理 → DecisionCompiler → RiskEvaluationService → CommunicationDispatcher → 执行网关
```
3.2 任何试图绕过风控门禁直接将信号写入执行网关的行为，视为系统安全事件。
3.3 `live_dispatch_block.flag` 为其时，**所有**信号中断（紧急熔断）。

```
实现: core/runtime/execution_gates.py
熔断: data/live_dispatch_block.flag (由 live_dispatch_policy.py 管理)
```

---

## 宪法第四条：配置分层，下层不得触碰上层

**系统采用四层架构，下层只能在上层划定的范围内操作。**

| 层级 | 文件/目录 | 可修改者 | 修改方式 |
|------|----------|---------|---------|
| **国家宪法** | `core/runtime/` `core/protocol/` | 架构师 | 仅限版本发布，需ADR |
| **州法律** | `core/brains/adapters/` | 模型工程师 | 需Code Review |
| **县条例** | `brain_entries.json` | 数据科学家 | 注册表添加条目 |
| **郡细则** | `engine_config.json` | 交易员/运维 | 热加载，无需重启 |

```
实现: ConfigHotReload (core/deployment/config_hot_reload.py)
注册: BrainRegistryService (core/features/feature_service.py)
```

---

## 宪法第五条：决策可追溯

**每一个信号从生成到执行到最终平仓必须全程可追溯。**

5.1 每个信号必须有唯一的 `message_id` 和 `correlation_id`。
5.2 信号从生成、风控评价、执行、进场、平仓的全链条记录必须写入 ledger。
5.3 任何时刻都可以用 `message_id` 回溯该信号的完整生命周期。

```
实现: core/ledger/ (JSONL ledger存储)
记录: communication_record, decision_record, execution_event
```

---

## 宪法第六条：优雅降级

**任何模块故障不得导致整个系统崩溃。系统必须有降级路径。**

6.1 当 Feature Service 故障时，系统进入 `SAFE` 模式（仅平仓）。
6.2 当执行网关断开时，系统持续重连，不产生新信号。
6.3 当任何 Brain（模型）推理失败时，该Brain被临时禁用，其余Brain继续运行。
6.4 当 Parliament（议会投票）无法达到法定人数时，不产生任何信号。

```
实现: SystemModeState + RuntimeLoop 异常处理
降级逻辑: 在各服务层实现 try/except + 模式切换
```

---

## 宪法第七条：人类最终控制权

**任何自动化决策链条必须保留人类紧急干预的能力。**

7.1 `live_dispatch_block.flag` 可以由运维随时创建以紧急熔断所有信号。
7.2 `SystemMode` 可以由运维从 `NORMAL` 手动切换为 `SAFE` 或 `EMERGENCY`。
7.3 系统不得自动从 `EMERGENCY` 模式恢复到 `NORMAL`——必须人工确认。
7.4 所有自动化参数调整（如 Alpha 资金分配）的边界受宪法第一条和第二条约束。

```
实现: SystemModeState.set_mode() 拒绝 EMERGENCY→NORMAL 自动转换
熔断: live_dispatch_block.flag 优先级最高
```

---

## 宪法修正程序

本宪法的修改必须满足以下全部条件：
1. 提交 ADR（Architecture Decision Record）说明修改理由
2. 在 `decisions/ARCHITECTURE_DECISIONS.md` 中记录
3. 所有修改必须保持向后兼容（不能推翻已有约束）
4. 修改生效后必须更新 `roadmap.json` 中的 `constitution_version`

---

> **版本**: 1.1.0
> **生效日期**: 2026-05-01
> **最后修订**: 去ONNX中心化 — 系统支持多种模型类型（ONNX / XGBoost JSON / OU参数 / LightGBM / PyTorch JIT），Brain与模型类型解耦，所有宪法条款对模型类型中立
