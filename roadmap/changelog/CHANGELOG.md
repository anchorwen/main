# 路线图变更日志

> 所有对路线图的修改均应记录在此。由 `scripts/update_roadmap.py` 自动追加，也可手动添加。

---

## [2026-05-01] 路线图初始化

### 创建
- 创建 `roadmap/` 文件夹结构
- 创建 `README.md` — 路线图导航入口
- 创建 `constitution.md` — 最高宪法 v1.0.0
- 创建 `roadmap.json` — 机器可读路线图
- 创建 `phases/01_phase_a_hub_agent.md` — 方案A详细设计
- 创建 `phases/02_phase_b_alpha_market.md` — 方案B详细设计
- 创建 `phases/03_phase_c_quant_os.md` — 方案C详细设计
- 创建 `decisions/ARCHITECTURE_DECISIONS.md` — 4条ADR
- 创建 `changelog/CHANGELOG.md` — 本文件

### 发现
- `core/` 层已有完整中枢 (`ServiceContainer`, `RuntimeLoop`, `DecisionCycleOrchestrator`)，但生产链路从未使用
- `scripts/` 中的实盘链路通过 shell 脚本串联，与 `core/` 层割裂
- 训练产出的ONNX模型与实盘消费之间完全断链
- `live_intent_loop.py` 仅使用硬编码价格差策略，未加载任何ONNX模型

### 待实施
- 创建 `main.py` 中枢入口
- 创建 `configs/brain_entries.json` 模型注册清单
- 创建 `configs/live.yaml` 统一实盘配置
- 打通训练→ONNX模型→实盘信号链路

---


## [2026-05-02] 架构文档强制执行 + 包结构修复

### 类型
架构修改 (Architecture Change) + 包结构修复

### 变更摘要
1. **包结构修复**: 删除根目录空 `__init__.py`，消除了所有 `core.*` 子包导入的 Pylance 命名空间冲突。170+ 个模块现已全部通过 pyright 零错误零警告验证。
2. **Pre-commit 门禁**: `.pre-commit-config.yaml` 新增 `enforce-architecture-docs` 钩子，任何对 `core/` 的修改必须同步更新 `roadmap/architecture/` 文档，否则提交被拒绝。
3. **门禁脚本**: 新增 `roadmap/scripts/architecture_gate.py`，基于文件 mtime 比较实现 CHECKPOINT 模式门禁。CI 环境自动跳过，`ARCHITECTURE_GATE_BYPASS=1` 手动绕过。
4. **大型批量修改**: `core/` 下几乎所有模块（contracts, brains, features, protocol, ledger, risk, execution, parliament, governance, feedback, observability, state, alpha, deployment, runtime）均引入 `schema_versions.py` 和 `__init__.py` 标准化。

### 新模块
| 模块 | 说明 |
|------|------|
| `main.py` | 中枢入口，接入 RuntimeLoop 和 ServiceContainer |
| `configs/live.yaml` | 统一实盘配置 |
| `configs/brain_entries.json` | 模型注册清单 |
| `configs/live_gate_policy.json` | 活盘闸口策略 |
| `configs/live_shadow_config.json` | Shadow 对比配置 |
| `core/brains/adapters/base_adapter.py` | 抽象基类 (load/infer/get_signal) |
| `core/brains/adapters/xgboost_brain_adapter.py` | XGBoost JSON 适配器 |
| `core/brains/adapters/params_brain_adapter.py` | OU参数/Z-Score 适配器 |
| `core/brains/services/brain_registry_service.py` | 模型注册表服务 |
| `core/protocol/services/mt5_communication_adapter.py` | MT5 实盘通信适配器 |
| `core/protocol/services/fix_communication_adapter.py` | FIX 协议适配器 |
| `core/features/computers/` | V9 实时特征计算包 |
| `roadmap/` | 完整的架构文档系统 |

### 修改的文件
| 文件 | 修改内容 |
|------|---------|
| `core/*/schema_versions.py` (20+ 模块) | 新增 Schema 版本编号 |
| `core/*/\_\_init\_\_.py` (15+ 包) | Python 包声明标准化 |
| `constitution.md` v1.0.0 → v1.1.0 | 去ONNX中心化，支持多模型类型 |
| `.pre-commit-config.yaml` | 新增架构文档门禁钩子 |
| `roadmap/scripts/architecture_gate.py` | 新增门禁脚本 |

### 删除的文件
| 文件 | 原因 |
|------|------|
| `d:\future\__init__.py` | 空文件导致 Pylance 命名空间冲突 |

### 需要关注的后续事项
- [ ] 将架构门禁集成到 CI pipeline（与现有 GitHub Actions 合并）
- [ ] `scripts/update_evolution_plan.ps1` / `scripts/update_evolution_plan_daily.ps1` 需要更新以适配新 schema
- [ ] XGBoost / Params 适配器仍需实盘验证（⚠️ 状态）

---

## [2026-05-01] 去ONNX中心化 — 多模型类型抽象

### 类型
架构修改 (Architecture Change)

### 变更摘要
系统不再仅支持 ONNX 格式。引入 `ModelArtifactAdapter` 抽象层，`brain_type` 成为模型类型的正式维度。训练产出的 XGBoost JSON 和 OU参数模型可直接接入实盘，不再需要转换为 ONNX。

### 修改的文件

| 文件 | 修改内容 |
|------|---------|
| `constitution.md` v1.0.0 → v1.1.0 | 第二条（沙盒晋升）和版本声明去除ONNX中心性 |
| `phases/01_phase_a_hub_agent.md` | A-2 模型管线打通：从单一 ONNX 模型扩展为多模型蓝本；新增 `base_adapter.py` + `XGBoostBrainAdapter` + `OUParamsBrainAdapter` 步骤 |
| `phases/02_phase_b_alpha_market.md` | B-1 Brain并行化：10-20个条目覆盖 ≥3 种 brain_type；B-2 议会投票与模型类型无关 |
| `phases/03_phase_c_quant_os.md` | C-1 基因池：model_type 成为核心基因维度，变异含模型类型切换，交叉需在 Adapter 注册表范围内 |
| `roadmap.json` v1.0.0 → v1.1.0 | Phase A-C 模块描述含 brain_type 多样性；milestones B1/C1 含 ≥3 brain_type/模型类型变异；missing_critical 添加 Adapter 相关模块 |
| `decisions/ARCHITECTURE_DECISIONS.md` | 新增 ADR-005 — 去ONNX中心化：多模型类型抽象 |
| `changelog/CHANGELOG.md` | 本条目 |

### 关联 ADR
- ADR-005: 去ONNX中心化 — 多模型类型抽象

### 新模块依赖
- `core/brains/adapters/base_adapter.py` — 抽象基类：统一 `load()` + `infer()` 接口
- `core/brains/adapters/xgboost_brain_adapter.py` — XGBoost JSON 推理 Adapter
- `core/brains/adapters/params_brain_adapter.py` — OU参数/Z-Score 信号 Adapter
- `core/brains/services/brain_factory.py` — 按 brain_type 路由到正确 Adapter（已存在）
- `brain_entries.json` — 注册多种 brain_type 的模型（待创建）

---

> **格式**: 每条记录包含日期、类型（创建/修改/废弃）、说明、关联文件

## 2026-05-02T15:03:57.105193+00:00 — 初始扫描

- 扫描 219 个模块
- active: 202
- stub: 2


## 2026-05-02T16:44:15.095247+00:00 — 初始扫描

- 扫描 219 个模块
- active: 202
- stub: 2


## 2026-05-04T01:27:54.735000+00:00 — 初始扫描

- 扫描 219 个模块
- active: 202
- stub: 2

