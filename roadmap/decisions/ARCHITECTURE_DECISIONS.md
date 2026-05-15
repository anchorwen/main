# 架构决策记录 (Architecture Decision Records)

> 任何重大架构变更必须记录为 ADR。格式：标题、日期、状态、背景、决策、后果。

## ADR 列表

| # | 标题 | 日期 | 状态 |
|---|------|------|------|
| 1 | 中枢加冕：main.py 替代 shell 脚本 | 2026-05-01 | 提议 |
| 2 | Feature Store 方案选型 | 2026-05-01 | 提议 |
| 3 | 执行网关选型：优先 MT5 | 2026-05-01 | 提议 |
| 4 | 配置分层架构 | 2026-05-01 | 已接受 |
| 5 | 去ONNX中心化 — 多模型类型抽象 | 2026-05-01 | 已接受 |
| 6 | 生产架构选型 — live_intent_loop 为规范路径 | 2026-05-06 | 已接受 |
| 7 | LLM/RL 资产分配委员会延后至 Phase C | 2026-05-06 | 已接受 |

---

## ADR-001: 中枢加冕 — main.py 替代 shell 脚本

- **状态**: 提议
- **日期**: 2026-05-01

### 背景
当前系统通过 `start_live_ops.ps1` 串联多个独立脚本（`live_intent_loop.py`、`live_dispatch_policy.py` 等），各脚本之间通过文件队列松耦合。而 `core/` 层已有 `ServiceContainer`、`RuntimeLoop`、`DecisionCycleOrchestrator` 等完整的运行时框架，从未被生产链路使用。

### 决策
创建 `main.py` 作为唯一入口，组装 `core/` 层所有服务，替代零散的 shell 脚本编排。

### 后果
- **正面**: 统一进程管理、异常处理、优雅退出；30+服务通过DI容器管理；配置热加载
- **负面**: 现有 `scripts/` 中的脚本需标记为 legacy 并逐步废弃
- **风险**: 单点故障 → 缓解: systemd/Docker auto-restart

---

## ADR-002: Feature Store 方案选型

- **状态**: 提议
- **日期**: 2026-05-01

### 背景
当前 `live_intent_loop.py` 仅基于硬编码的价格差判断方向，缺乏 EMA/ADX/波动率等特征。需要选型特征存储方案。

### 决策
- 优先使用 `LocalFeatureStore`（Parquet 格式），搭配每日定时增量更新
- 中期可扩展至 ClickHouse/TimescaleDB
- 特征计算与训练侧使用相同的 `V9InstitutionalSchema`，确保一致性

### 后果
- **正面**: Parquet 读写高效，无需额外数据库依赖，与训练侧对齐
- **负面**: 不支持实时特征（仅每日更新），后续需扩展流式处理
- **风险**: 特征计算逻辑与训练时不符 → 缓解: schema 版本管理

---

## ADR-003: 执行网关选型 — 优先 MT5

- **状态**: 提议
- **日期**: 2026-05-01

### 背景
`core/` 中同时存在 `MT5CommunicationAdapter` 和 `FIXGatewayAdapter`，需决定优先完善哪条通道。

### 决策
优先对接 MT5（代码完整度更高），FIX 作为备用通道。开发阶段默认使用 `StubCommunicationAdapter`（纸质交易）。

### 后果
- **正面**: MT5 上手快，适合当前阶段快速验证
- **负面**: MT5 依赖本地终端，稳定性不如 FIX
- **风险**: MT5 断连 → 缓解: FileQueue 缓冲 + 自动重连

---

## ADR-004: 配置分层架构

- **状态**: 已接受
- **日期**: 2026-05-01

### 背景
系统需要支持不同角色的配置修改权限：架构师可改核心运行时，交易员只能调参数。

### 决策
四层配置架构：
1. **宪法层** (`core/runtime/`, `core/protocol/`) — 架构师，需 ADR + 版本发布
2. **法律层** (`core/brains/adapters/`) — 模型工程师，需 Code Review
3. **条例层** (`brain_entries.json`) — 数据科学家，注册表添加
4. **细则层** (`engine_config.json`) — 交易员/运维，热加载

### 后果
- **正面**: 清晰的权限边界，降低误操作风险
- **负面**: 增加配置管理复杂度
- **实现**: ConfigHotReload (core/deployment/config_hot_reload.py)

---

## ADR-005: 去ONNX中心化 — 多模型类型抽象

- **状态**: 已接受
- **日期**: 2026-05-01

### 背景
- 当前 `core/` 层硬编码绑定 ONNX 格式：`V9OnnxBrainAdapter` 是唯一的模型推理适配器，`brain_type: "onnx_v9"` 是唯一能进入决策链的 Brain 类型
- 但训练侧已产出 3 种模型产物：`.onnx`、`.xgb.json`（XGBoost）、`.params.json`（OU/Z-Score 参数型），后两者从未接入实盘
- 训练侧的 `lane_trainers.json` 已包含 `model_type: "xgboost_json"` 和 `model_type: "ou_params_json"` 的 trainer
- 训练产出物与实盘消费之间存在格式断链：XGBoost 和 OU参数模型虽有信号，但无法被 `BrainRunService` 消费

### 决策
引入 **`ModelArtifactAdapter` 抽象层**，将 `brain_type` 作为模型类型的正式维度，Brain 与模型格式解耦：

1. 创建 `base_adapter.py` — 抽象基类，定义 `load(model_path)` + `infer(feature_vector)` + `get_signal()` 统一接口
2. 创建 **XGBoostBrainAdapter** — 加载 `.xgb.json` 格式，支持多分类输出
3. 创建 **OUParamsBrainAdapter** — 加载 `.params.json`，计算 Z-Score，返回统计信号
4. `BrainFactory` 按 `brain_entries.json` 中的 `brain_type` 字段路由到正确的 Adapter
5. 后续可扩展 `LightGBMBrainAdapter`、`TorchJITBrainAdapter` 等，遵守相同的 `load/infer/get_signal` 契约
6. `DecisionCandidate` 接口保持不变 — 所有 Adapter 输出统一的 `DecisionCandidate{side, confidence, reason}`

### 后果
- **正面**:
  - XGBoost/OU参数模型可立即接入实盘，无需等待 ONNX 转换
  - 新模型类型的添加不影响现有代码，只需实现 Adapter 接口并在 `BrainFactory` 注册
  - 基因池变异（Phase C）时模型类型成为可切换的基因维度，不再被 ONNX 锁定
  - 议会投票（ParliamentService）天然支持多模型类型投票，权重计算与模型格式无关
- **负面**:
  - Adapter 协议需要持续向后兼容，新增 `brain_type` 必须通过 Code Review
  - XGBoost 的推理性能与 ONNX 不同（GPU 无加速），需要独立基准测试
- **风险**: 新增 Adapter 的推理 bug 可能污染 DecisionCandidate → 缓解：新 Adapter 必须先在 Shadow Live 验证 90 天（宪法第二条）

---

---

## ADR-006: 生产架构选型 — live_intent_loop 为规范路径

- **状态**: 已接受
- **日期**: 2026-05-06

### 背景

系统存在两套推理→决策→派发架构：

| 架构 | 路径 | 使用场景 |
|------|------|----------|
| **A**: live_intent_loop | `main.py live` → `live_launcher.py` → `live_intent_loop.py` → `core/runtime/live_cycle.py` | **当前生产**（5-brain 多脑模式运行中） |
| **B**: RuntimeLoop | `ServiceContainer` → `RuntimeLoop` → `DecisionCycleOrchestrator` | 测试/影子场景/`main_v9_shadow.py` |

两套架构维护成本翻倍，功能分歧风险持续增长。`roadmap.json` v1.1.0 曾标记架构 A 为 "legacy, 将被 RuntimeLoop 替代"，但架构 A 是唯一经过实盘验证的路径。

### 决策

1. **架构 A（live_intent_loop → live_cycle）为规范生产路径**。
2. **架构 B（RuntimeLoop → Orchestrator）降为备用架构**，保留用于测试和影子场景分析，不再作为迁移目标。
3. `core/runtime/live_cycle.py` 已通过 2026-05-06 模块化提取，将核心周期逻辑独立于 CLI 脚本，满足可测试性要求。
4. `BrokerAdapter` Protocol（`core/execution/broker_adapter.py`）作为执行层抽象，统一两套架构的执行入口。

### 后果

- **正面**: 单一生产路径降低维护成本；live_cycle 可独立单元测试；BrokerAdapter 为 FIX/云端迁移提供统一接口
- **负面**: RuntimeLoop/Orchestrator 中的部分功能（断路器、指标收集）需在 live_cycle 中重新实现
- **风险**: 无

### 2026-05-15 补充：watchdog 残骸清理

`scripts/hourly_watchdog.py` 是 2026-05-05 的一夜实验（最后一次写入 `data/watchdog.log` 为 2026-05-06）。无任何 scheduler/cron 调用它。其 `restart_live_system()` 使用 `taskkill /F` 粗暴杀进程，与 `live_launcher.py` 内置的逐子进程健康监控（launcher 内部 watchdog 循环，lines 422-648）机制冲突。

已于 2026-05-15 删除：
- `scripts/hourly_watchdog.py`
- `data/watchdog.log`

`live_launcher.py` 的内置子进程监控是唯一的生产健康检查机制。`scripts/training/monitor_training.py` 中的 "watchdog" 是训练批次监控，与此无关。

---

## ADR-007: LLM/RL 资产分配委员会延后至 Phase C

- **状态**: 已接受
- **日期**: 2026-05-06

### 背景

用户原始蓝图方案 B 的核心设计是"引入大语言模型（LLM）或强化学习作为资产分配委员会"。当前系统使用 `ParliamentService` 规则驱动加权平均完成多脑投票——不涉及任何 LLM 调用或 RL 策略梯度更新。

### 决策

1. **当前阶段（Phase A→B 过渡）使用规则驱动 ParliamentService 作为资产分配机制**。
2. **LLM/RL 智能分配延后至 Phase C**，届时系统已有稳定的多 Alpha 并行基础设施（20-50 个 Alpha）和足够的交易历史数据供 RL 训练。
3. `ParliamentService` 的加权投票机制（vote_weight × confidence × fallback_penalty）保留为 baseline，LLM/RL 作为可插拔的高级替代。
4. `DynamicBrainWeighter` + `BrainPerformanceTracker` 闭环提供数据驱动的权重调整，构成 RL 的前置数据管道。

### 后果

- **正面**: 降低 Phase B 复杂度，聚焦可验证的规则驱动分配；ParliamentService 已通过多脑实盘验证
- **负面**: 偏离原始 B 方案愿景中的"LLM 智能分配"特征
- **风险**: 规则驱动分配在极端市场条件下可能不如 LLM 灵活 → 缓解: RegimeDetector 动态调整 multiplier

---

> **最后更新**: 2026-05-06
